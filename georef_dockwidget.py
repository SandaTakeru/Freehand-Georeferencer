# -*- coding: utf-8 -*-
'''Operation panel (dock widget).

Provides the layer / scope / transform-mode selection, the GCP table with the
error display, and apply / save. There is no numeric-input UI (the user never
enters parameters directly). Works for both vector and raster target layers;
scope and apply-method are vector-only and are disabled for raster.
'''

import os
import tempfile
import time

from qgis.core import (
    Qgis, QgsMapLayerProxyModel, QgsMessageLog, QgsProject, QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.gui import QgsDockWidget, QgsMapLayerComboBox, QgsMessageViewer
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QSlider,
)

QtHorizontal = getattr(Qt, 'Horizontal', Qt.Orientation.Horizontal)

from .georef_session_base import DEFAULT_QUALITY, QUALITY, residual_color
from .georef_vector_session import VectorGeorefSession
from .georef_raster_session import RasterGeorefSession
from .georef_maptool import GeorefMapTool


class _NumItem(QTableWidgetItem):
    '''Table item sorted numerically (the display is a formatted string).
    Empty values sort last.'''

    def __init__(self, text, value):
        super().__init__(text)
        self._value = value
        # Not editable; selectable and display only.
        self.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

    def __lt__(self, other):
        a = self._value if self._value is not None else float('inf')
        b = getattr(other, '_value', None)
        b = b if b is not None else float('inf')
        return a < b


class _CheckItem(QTableWidgetItem):
    '''Table item sorted by check state (active first) then GCP number.'''

    def __lt__(self, other):
        sa = 0 if self.checkState() == Qt.CheckState.Checked else 1
        sb = 0 if other.checkState() == Qt.CheckState.Checked else 1
        if sa != sb:
            return sa < sb
        ia = self.data(Qt.ItemDataRole.UserRole) or 0
        ib = other.data(Qt.ItemDataRole.UserRole) or 0
        return ia < ib


class GeorefDockWidget(QgsDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__('Freehand Georeferencer', parent)
        self.iface = iface
        self.session = None
        # Keep the most recent (stopped) session so Save still works after stop.
        self._last_session = None
        self.maptool = None
        self._syncing = False
        # Target layer whose selection count is being watched (for start gating).
        self._sel_layer = None

        self._build_ui()
        self._preselect_active_layer()

    def _preselect_active_layer(self):
        # Default the target layer to the active layer if it is a vector or
        # raster layer. Do not override during a running session.
        if self.session is not None:
            return
        layer = self.iface.activeLayer()
        if isinstance(layer, (QgsVectorLayer, QgsRasterLayer)):
            self.layerCombo.setLayer(layer)
        self._on_layer_changed()

    def _is_raster(self):
        return isinstance(self.layerCombo.currentLayer(), QgsRasterLayer)

    def _primary_button_state(self):
        '''Set the primary button label and enable state for the current session.'''
        if self.session is None:
            self.primaryBtn.setText('Start')
            self.primaryBtn.setEnabled(self._can_start())
            return

        if isinstance(self.session, RasterGeorefSession) and not self.session.gcps:
            self.primaryBtn.setText('Add GCP at map center')
            self.primaryBtn.setEnabled(True)
            return

        self.primaryBtn.setText('Apply')
        active = sum(1 for g in self.session.gcps if g.active)
        self.primaryBtn.setEnabled(active > 0)

    def _add_center_gcp_for_raster(self):
        '''Add a provisional first GCP from the raster centre to the map centre.'''
        if self.session is None or not isinstance(self.session, RasterGeorefSession):
            return
        if self.session.gcps:
            return
        ext = self.session.layer.extent()
        if not ext.isValid() or ext.width() <= 0 or ext.height() <= 0:
            return
        center_map = self.iface.mapCanvas().extent().center()
        src = (
            ext.xMinimum() + ext.width() / 2.0,
            ext.yMaximum() - ext.height() / 2.0,
        )
        dest = (center_map.x(), center_map.y())
        self.session.add_gcp(src, dest)
        self.hint.setText(
            'A provisional GCP was added from the raster centre to the map centre. '
            'Add more control points or apply the transform.')
        self._primary_button_state()

    def _refresh_excepted_layers(self, *args):
        # Exclude raster layers that are not local GDAL files (e.g. 地理院地図 /
        # XYZ / WMS / WMTS). They are already georeferenced and have no writable
        # source, so they cannot be a target — keep them out of the combo.
        excepted = []
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsRasterLayer) and lyr.providerType() != 'gdal':
                excepted.append(lyr)
        self.layerCombo.setExceptedLayerList(excepted)

    def _on_layer_changed(self, layer=None):
        # When the target layer changes, refresh the CRS warning, selection
        # watching, the type-specific widgets and the start gating.
        self._update_crs_warning()
        self._reconnect_selection()
        self._update_type_widgets()
        self._update_idle_primary()

    def _update_type_widgets(self):
        # Scope and apply-method are vector-only; disable for raster.
        if self.session is not None:
            return
        raster = self._is_raster()
        self.scopeCombo.setEnabled(not raster)
        self.applyModeCombo.setEnabled(not raster)
        self.transparencySlider.setEnabled(True)
        self.transparencyLabel.setEnabled(True)
        if raster:
            self.applyModeCombo.setCurrentIndex(0)  # generate new layer

    def _reconnect_selection(self):
        # Selection count affects whether Start is possible, so watch the
        # target layer's selectionChanged (vector only).
        if self._sel_layer is not None:
            try:
                self._sel_layer.selectionChanged.disconnect(
                    self._on_selection_changed)
            except (TypeError, RuntimeError):
                pass
            self._sel_layer = None
        layer = self.layerCombo.currentLayer()
        if isinstance(layer, QgsVectorLayer):
            layer.selectionChanged.connect(self._on_selection_changed)
            self._sel_layer = layer

    def _on_selection_changed(self, *args):
        self._update_idle_primary()

    def _can_start(self):
        # Whether starting is meaningful.
        layer = self.layerCombo.currentLayer()
        if isinstance(layer, QgsRasterLayer):
            return layer.isValid()
        if not isinstance(layer, QgsVectorLayer):
            return False
        if layer.featureCount() == 0:
            return False
        if (self._current_scope() == 'selected'
                and layer.selectedFeatureCount() == 0):
            return False
        return True

    def _update_idle_primary(self, *args):
        # When idle (no session), Start is enabled only when starting is possible.
        if self.session is None:
            self.primaryBtn.setEnabled(self._can_start())

    def _update_crs_warning(self):
        # Always show a red warning when the layer CRS differs from the project CRS.
        layer = self.layerCombo.currentLayer()
        proj_crs = QgsProject.instance().crs()
        if layer is not None and layer.crs() != proj_crs:
            self.crsWarn.setText(
                '<a href="#" style="color:#cc0000; text-decoration:none;">'
                '⚠ Layer CRS ({}) differs from project CRS ({}). '
                'Match them. Click for details.</a>'.format(
                    layer.crs().authid() or 'unknown',
                    proj_crs.authid() or 'unknown'))
            self.crsWarn.show()
        else:
            self.crsWarn.hide()

    def _show_crs_detail(self, _link):
        # On click, show the detailed note in the standard (read-only) message viewer.
        viewer = QgsMessageViewer(self)
        viewer.setWindowTitle('CRS / projection — please read')
        viewer.setMessageAsHtml(self._crs_detail_html())
        viewer.exec()

    def _crs_detail_html(self):
        return (
            '<h3>Keep the layer CRS and the project CRS the same.</h3>'
            '<p>This is the one rule to follow. Set the project CRS equal to '
            'the target layer’s CRS before georeferencing.</p>'
            '<hr>'
            '<p><b>Why</b></p>'
            '<ul>'
            '<li>This plugin applies a plain 2D affine transform '
            '(Helmert / Affine). It does <b>not</b> reproject and does '
            '<b>not</b> apply any geodetic or scale-factor correction.</li>'
            '<li>The transform itself never bends shapes: affine maps '
            'straight lines to straight lines, and Helmert keeps the shape.</li>'
            '<li>GCP <i>source</i> coordinates are read in the layer CRS, while '
            '<i>destination</i> points are read in the project/map CRS. If the '
            'two CRS differ, these are mixed and the result becomes wrong, and '
            'the live preview is implicitly reprojected (layer → map), '
            'which can look distorted.</li>'
            '<li>Plane rectangular CS scale factor: coordinates are treated as '
            'a flat plane; the scale factor that varies across a zone is not '
            'corrected. Congruent planar figures stay congruent; true ground '
            'distances are not adjusted.</li>'
            '</ul>'
            '<p><b>Bottom line:</b> make the project CRS match the layer CRS, '
            'then georeference.</p>')

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)

        # Settings form (top to bottom: Transform -> Target layer -> Scope ->
        # Apply as). Scope and Apply as are vector-only and grey out for raster.
        form = QFormLayout()
        # Transform mode (combo). currentData is a (mode, lock_scale) tuple.
        self.transformCombo = QComboBox()
        self.transformCombo.addItem(
            'Helmert (move / rotate, fixed scale)', ('helmert', True))
        self.transformCombo.addItem(
            'Helmert (move / rotate / scale)', ('helmert', False))
        self.transformCombo.addItem(
            'Affine (move / rotate / scale / shear)', ('affine', False))
        self.transformCombo.setCurrentIndex(0)  # default: Helmert (fixed scale)
        form.addRow('Transform', self.transformCombo)

        self.layerCombo = QgsMapLayerComboBox()
        self.layerCombo.setFilters(
            QgsMapLayerProxyModel.Filter.VectorLayer
            | QgsMapLayerProxyModel.Filter.RasterLayer)
        form.addRow('Target layer', self.layerCombo)

        self.transparencySlider = QSlider(QtHorizontal)
        self.transparencySlider.setMinimum(0)
        self.transparencySlider.setMaximum(100)
        self.transparencySlider.setValue(50)
        self.transparencyLabel = QLabel('50%')

        # Scope (combo). The internal value is read via currentData. Vector only.
        self.scopeCombo = QComboBox()
        self.scopeCombo.addItem('Single feature', 'single')
        self.scopeCombo.addItem('Selected features only', 'selected')
        self.scopeCombo.addItem('Whole layer', 'all')
        self.scopeCombo.setCurrentIndex(0)  # default: single feature
        form.addRow('Scope', self.scopeCombo)

        # Apply-method combo (vector only). Raster always generates a new layer.
        self.applyModeCombo = QComboBox()
        self.applyModeCombo.addItem('Generate new layer', 'new')
        self.applyModeCombo.addItem('Add to current layer', 'add')
        self.applyModeCombo.addItem('Edit current layer', 'edit')
        form.addRow('Apply as', self.applyModeCombo)
        layout.addLayout(form)

        # Operation guide.
        self.hint = QLabel(
            'Press Start, then grab an old point and release at the new '
            'position to add control points.')
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet('color: #555;')
        layout.addWidget(self.hint)

        # Persistent CRS-mismatch warning (red, click for details). Placed just
        # above the Start button.
        self.crsWarn = QLabel()
        self.crsWarn.setWordWrap(True)
        self.crsWarn.setOpenExternalLinks(False)
        self.crsWarn.linkActivated.connect(self._show_crs_detail)
        self.crsWarn.hide()
        layout.addWidget(self.crsWarn)

        # Primary button: Start when idle, Apply during a session (same button,
        # larger and bold to stand out).
        self.primaryBtn = QPushButton('Start')
        self.primaryBtn.setMinimumHeight(34)
        pf = self.primaryBtn.font()
        pf.setBold(True)
        self.primaryBtn.setFont(pf)
        layout.addWidget(self.primaryBtn)

        # Secondary buttons: undo the last point / reset the session (cancel, GCPs kept).
        self.undoBtn = QPushButton('Undo point')
        self.resetBtn = QPushButton('Reset')
        rowSec = QHBoxLayout()
        rowSec.addWidget(self.undoBtn)
        rowSec.addWidget(self.resetBtn)
        layout.addLayout(rowSec)

        # GCP file I/O.
        self.saveBtn = QPushButton('Save GCPs')
        self.loadBtn = QPushButton('Load GCPs')
        rowIO = QHBoxLayout()
        rowIO.addWidget(self.saveBtn)
        rowIO.addWidget(self.loadBtn)
        layout.addLayout(rowIO)

        # Preview quality (just below Save/Load).
        formQuality = QFormLayout()
        self.qualityCombo = QComboBox()
        self.qualityCombo.addItems(list(QUALITY.keys()))
        self.qualityCombo.setCurrentText(DEFAULT_QUALITY)
        formQuality.addRow('Preview quality', self.qualityCombo)

        rowTrans = QHBoxLayout()
        rowTrans.addWidget(QLabel('Preview Transparency:'))
        rowTrans.addWidget(self.transparencySlider)
        rowTrans.addWidget(self.transparencyLabel)
        formQuality.addRow(rowTrans)

        layout.addLayout(formQuality)

        # Error summary.
        self.statLabel = QLabel('RMS: -   Std dev: -   Scale: -')
        layout.addWidget(self.statLabel)

        # GCP table (placed at the bottom and stretched to fill the height).
        # Columns: On (check) / # (creation order) / X error / Y error / Residual.
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ['On', '#', 'X error', 'Y error', 'Residual'])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for c in (2, 3, 4):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(22)
        # No hand editing; click a column header to sort by value.
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        # Default is creation order (# column ascending); other columns sortable too.
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        layout.addWidget(self.table, 1)

        self.setWidget(root)

        # Signal connections.
        self.primaryBtn.clicked.connect(self._on_primary)
        self.undoBtn.clicked.connect(self._on_undo)
        self.resetBtn.clicked.connect(self._on_reset)
        self.saveBtn.clicked.connect(self._on_save)
        self.loadBtn.clicked.connect(self._on_load)
        self.transformCombo.currentIndexChanged.connect(self._on_transform_changed)
        self.qualityCombo.currentTextChanged.connect(self._on_quality_changed)
        self.table.itemChanged.connect(self._on_item_changed)
        self.layerCombo.layerChanged.connect(self._on_layer_changed)
        self.scopeCombo.currentIndexChanged.connect(self._update_idle_primary)
        QgsProject.instance().crsChanged.connect(self._update_crs_warning)
        # Keep web/tile raster layers out of the target combo.
        QgsProject.instance().layersAdded.connect(self._refresh_excepted_layers)
        QgsProject.instance().layersRemoved.connect(self._refresh_excepted_layers)

        self._refresh_excepted_layers()
        self._update_enabled(False)
        self._on_layer_changed()
        self.transparencySlider.valueChanged.connect(self._on_transparency_changed)

    # ------------------------------------------------------------------
    # Session control
    # ------------------------------------------------------------------
    def _current_scope(self):
        return self.scopeCombo.currentData()

    def _current_transform(self):
        # Returns (mode, lock_scale).
        return self.transformCombo.currentData()

    def _on_primary(self):
        # Idle: Start (begin a session). Running: Apply.
        if self.session is None:
            self._start_session()
        elif isinstance(self.session, RasterGeorefSession) and not self.session.gcps:
            self._add_center_gcp_for_raster()
        else:
            self._on_apply()

    def _on_reset(self):
        # Cancel the current session (keep the GCP list). Does not apply.
        self._stop_session()

    def _start_session(self):
        layer = self.layerCombo.currentLayer()
        if layer is None:
            self.iface.messageBar().pushMessage(
                'Freehand Georeferencer', 'Please select a target layer.',
                level=Qgis.Warning, duration=3)
            return

        mode, lock = self._current_transform()
        quality = self.qualityCombo.currentText()
        # Starting a new session, so discard the last session kept for saving.
        self._last_session = None

        if isinstance(layer, QgsRasterLayer):
            self.session = RasterGeorefSession(
                self.iface, layer, mode, lock, quality,
                on_update=self._refresh_stats)
        else:
            scope = self._current_scope()
            if scope == 'selected' and layer.selectedFeatureCount() == 0:
                self.iface.messageBar().pushMessage(
                    'Freehand Georeferencer',
                    'No selected features. Select features or change the scope.',
                    level=Qgis.Warning, duration=4)
                return
            self.session = VectorGeorefSession(
                self.iface, layer, scope, mode, lock, quality,
                on_update=self._refresh_stats)

        if self.session.feature_count() == 0:
            self.iface.messageBar().pushMessage(
                'Freehand Georeferencer', 'Nothing to georeference in the layer.',
                level=Qgis.Warning, duration=4)
            self.session.cleanup()
            self.session = None
            return

        self.maptool = GeorefMapTool(self.iface, self.session)
        self.iface.mapCanvas().setMapTool(self.maptool)
        self.session.recompute()
        current_transparency = self.transparencySlider.value()
        opacity = 1.0 - (current_transparency / 100.0)
        self.session.set_preview_opacity(opacity)
        self._primary_button_state()
        self._update_enabled(True)
        self._lock_setup_widgets(True)
        if isinstance(layer, QgsRasterLayer):
            if not self.session.gcps:
                self.hint.setText(
                    'The raster is still unreferenced. Click the primary button to '
                    'add a first GCP from the raster centre to the map centre.')
            else:
                self.hint.setText(
                    'Press a recognizable point on the image and release at its '
                    'correct map position to add control points.')
        elif self._current_scope() == 'single':
            self.hint.setText(
                'The feature of the first grabbed node becomes the target. '
                'Grab an old node and release at the new position.')
        else:
            self.hint.setText(
                '{} feature(s) / ~{} vertices. Grab an old node and release '
                'at the new position.'.format(
                    self.session.feature_count(), self.session.total_vertices()))

    def _stop_session(self):
        if self.maptool:
            self.iface.mapCanvas().unsetMapTool(self.maptool)
            self.maptool.cleanup()
            self.maptool = None
        if self.session:
            # Keep a reference to the session with GCPs so Save still works after stop.
            self._last_session = self.session if self.session.gcps else None
            self.session.cleanup()
            self.session = None
        self._primary_button_state()
        # When idle, Start is enabled only when starting is possible.
        self._update_idle_primary()
        # Keep the GCP list/stats until the next Start (so they can be reviewed
        # after Apply). The next Start overwrites them with the new session's recompute.
        self._update_enabled(False)
        # If GCPs remain, only Save stays usable.
        self.saveBtn.setEnabled(self._last_session is not None)
        self._lock_setup_widgets(False)
        self._update_type_widgets()

    def _lock_setup_widgets(self, locked):
        # During a session, forbid changing layer/scope (the GCP coordinate basis changes).
        self.layerCombo.setEnabled(not locked)
        self.scopeCombo.setEnabled(not locked)

    def _update_enabled(self, active):
        for b in (self.undoBtn, self.resetBtn, self.saveBtn, self.loadBtn):
            b.setEnabled(active)

    # ------------------------------------------------------------------
    # Operation handlers
    # ------------------------------------------------------------------
    def _on_undo(self):
        if self.session:
            self.session.remove_last()

    def _on_transform_changed(self):
        if self.session:
            mode, lock = self._current_transform()
            self.session.set_transform(mode, lock)

    def _on_quality_changed(self, text):
        if self.session:
            self.session.set_quality(text)

    def _on_apply(self):
        if not self.session:
            return
        raster = isinstance(self.session, RasterGeorefSession)
        mode = 'new' if raster else self.applyModeCombo.currentData()
        name = self.session.layer.name()
        if mode == 'new':
            out = self.session.apply()
            if out is None:
                if raster:
                    self._warn_raster_failed()
                else:
                    self._warn_no_gcp()
                return
            text = 'Created layer "{}".'.format(out.name())
        elif mode == 'add':
            n = self.session.apply_add()
            if n is None:
                self._warn_apply_failed()
                return
            text = ('Added {} feature(s) to "{}". Layer is in edit mode — '
                    'undo with Ctrl+Z, then save to keep.'.format(n, name))
        elif mode == 'edit':
            n = self.session.apply_edit()
            if n is None:
                self._warn_apply_failed()
                return
            text = ('Edited {} feature(s) in "{}". Layer is in edit mode — '
                    'undo with Ctrl+Z, then save to keep.'.format(n, name))
        else:
            return
        # On every apply, auto-save the GCPs to a temp CSV and notify with an Open button.
        path = self._autosave_gcps()
        self._notify_apply(text, path)
        # After applying, end the session so another layer/scope can be selected.
        self._stop_session()

    def _autosave_gcps(self):
        '''Save the apply-time GCPs to a temp CSV and return its path. None on failure.'''
        sess = self.session
        if not sess or not sess.gcps:
            return None
        safe = ''.join(c if c.isalnum() else '_' for c in sess.layer.name())
        fname = 'fhg_gcp_{}_{}.csv'.format(
            safe, time.strftime('%Y%m%d_%H%M%S'))
        path = os.path.join(tempfile.gettempdir(), fname)
        try:
            sess.save_gcps(path)
        except OSError:
            return None
        return path

    def _notify_apply(self, text, csv_path):
        '''Show the result in the message bar. If a CSV exists, log its path and
        add a button to open the containing folder.'''
        mb = self.iface.messageBar()
        if csv_path:
            text = text + '  (GCP CSV: {})'.format(csv_path)
            # Also record the path in the Log Messages panel.
            QgsMessageLog.logMessage(
                'GCP CSV saved: {}'.format(csv_path),
                'Freehand Georeferencer', Qgis.Info)
        item = mb.createMessage('Freehand Georeferencer', text)
        if csv_path:
            folder = os.path.dirname(csv_path)
            btn = QPushButton('Open folder')
            btn.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
            item.layout().addWidget(btn)
        mb.pushWidget(item, Qgis.Success, 8)

    def _warn_no_gcp(self):
        self.iface.messageBar().pushMessage(
            'Freehand Georeferencer',
            'No active control points.', level=Qgis.Warning, duration=3)

    def _warn_apply_failed(self):
        self.iface.messageBar().pushMessage(
            'Freehand Georeferencer',
            'Cannot apply: no active control points, or the layer is not '
            'editable.', level=Qgis.Warning, duration=4)

    def _warn_raster_failed(self):
        self.iface.messageBar().pushMessage(
            'Freehand Georeferencer',
            'Cannot apply: no active control points, or the raster source is '
            'not a writable GDAL file.', level=Qgis.Warning, duration=4)

    def _on_save(self):
        # Use the running session, or the most recent stopped session if none.
        sess = self.session or self._last_session
        if not sess or not sess.gcps:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save GCPs', '', 'CSV (*.csv)')
        if not path:
            return
        if not path.lower().endswith('.csv'):
            path += '.csv'
        sess.save_gcps(path)
        self.iface.messageBar().pushMessage(
            'Freehand Georeferencer', 'GCPs saved.',
            level=Qgis.Success, duration=3)

    def _on_load(self):
        if not self.session:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, 'Load GCPs', '', 'CSV (*.csv)')
        if not path:
            return
        n = self.session.load_gcps(path)
        if n == 0:
            self.iface.messageBar().pushMessage(
                'Freehand Georeferencer',
                'No GCPs found in the file.', level=Qgis.Warning, duration=3)
        else:
            self.iface.messageBar().pushMessage(
                'Freehand Georeferencer',
                'Loaded {} GCPs.'.format(n), level=Qgis.Success, duration=3)

    def _on_item_changed(self, item):
        if self._syncing or not self.session or item.column() != 0:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None:
            return
        active = item.checkState() == Qt.CheckState.Checked
        self.session.set_active(idx, active)

    # ------------------------------------------------------------------
    # Stats / table update (callback from the session)
    # ------------------------------------------------------------------
    def _refresh_stats(self, data):
        self._syncing = True
        # Disable sorting during repopulation (avoid re-sorting on every insert),
        # then restore it at the end.
        self.table.setSortingEnabled(False)
        rows = data['rows']
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            # Column 0: on/off check (no text). UserRole holds the GCP's original index.
            chk = _CheckItem('')
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable |
                         Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(
                Qt.CheckState.Checked if row['active'] else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, row['index'])
            self.table.setItem(r, 0, chk)

            # Column 1: creation order (# number). Carries the numeric sort value too.
            num = _NumItem('#{}'.format(row['index'] + 1), row['index'] + 1)
            self.table.setItem(r, 1, num)

            ex = '' if row['ex'] is None else '{:.3f}'.format(row['ex'])
            ey = '' if row['ey'] is None else '{:.3f}'.format(row['ey'])
            mag = '' if row['mag'] is None else '{:.3f}'.format(row['mag'])
            mag_item = _NumItem(mag, row['mag'])
            if row['mag'] is not None:
                mag_item.setForeground(
                    residual_color(row['mag'], data.get('max_mag', 0.0)))
            self.table.setItem(r, 2, _NumItem(ex, row['ex']))
            self.table.setItem(r, 3, _NumItem(ey, row['ey']))
            self.table.setItem(r, 4, mag_item)

        self.table.setSortingEnabled(True)
        self.statLabel.setText(
            'RMS: {:.3f}   Std dev: {:.3f}   Scale: ×{:.4f}'.format(
                data['rms'], data['std'], data.get('scale', 1.0)))
        self._primary_button_state()
        self._syncing = False

    def closeEvent(self, event):
        self._stop_session()
        super().closeEvent(event)

    def _on_transparency_changed(self, value):
        self.transparencyLabel.setText(f'{value}%')
        if self.session is not None:
            opacity = 1.0 - (value / 100.0)
            self.session.set_preview_opacity(opacity)
