# -*- coding: utf-8 -*-
'''Vector georeferencing session.

Transforms vector geometries: rubber-band live preview, vertex snapping, the
single/selected/all scope, and output as a new memory layer / added features /
in-place edit.
'''

import time

import numpy as np

from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtGui import QColor

from . import transform
from .georef_session_base import (
    MAX_ANCHORS, QUALITY, SIMPLIFY_THRESHOLD, GeorefSessionBase,
)


class VectorGeorefSession(GeorefSessionBase):
    def __init__(self, iface, layer, scope, mode, lock_scale, quality,
                 on_update):
        super().__init__(iface, layer, mode, lock_scale, quality, on_update)
        self.scope = scope            # 'single' / 'selected' / 'all'

        self._geom_type = QgsWkbTypes.geometryType(layer.wkbType())
        self._features = []           # list of (fid, QgsGeometry): source geometries to transform
        self._anchors = np.zeros((0, 2))
        self._anchor_fids = np.zeros(0, dtype=np.int64)  # feature id of each anchor vertex
        self._total_vertices = 0

        # Single-feature mode: keep all features as candidates and narrow down
        # to the feature of the first grabbed node.
        self._candidates = []         # all candidate (fid, QgsGeometry)
        self._locked_fid = None       # confirmed target feature id (single mode)

        # Rubber band for the preview.
        self.rb_preview = QgsRubberBand(self.canvas, self._geom_type)
        self.rb_preview.setColor(QColor(30, 120, 255, 180))
        self.rb_preview.setFillColor(QColor(30, 120, 255, 40))
        self.rb_preview.setWidth(2)
        # Static preview that keeps the non-target features at their original
        # position in single-feature mode.
        self.rb_static = QgsRubberBand(self.canvas, self._geom_type)
        self.rb_static.setColor(QColor(120, 120, 120, 160))
        self.rb_static.setFillColor(QColor(120, 120, 120, 30))
        self.rb_static.setWidth(1)

        self._build_scope()

    # ------------------------------------------------------------------
    # Initialization / collecting the target features
    # ------------------------------------------------------------------
    def _build_scope(self):
        '''Collects the target features and snapping anchors per the scope.

        In single-feature mode all features are loaded as candidates at start,
        and the feature of the first grabbed node is confirmed as the target
        (narrowed down by _lock_single).
        '''
        if self.scope == 'all':
            feats = list(self.layer.getFeatures())
        elif self.scope == 'selected':
            feats = list(self.layer.getSelectedFeatures())
        else:  # single: keep all features as candidates
            feats = list(self.layer.getFeatures())

        collected = []
        for f in feats:
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            collected.append((f.id(), QgsGeometry(g)))

        if self.scope == 'single':
            # Keep all features as candidates; preview them all until confirmed.
            self._candidates = collected
            self._features = list(collected)
        else:
            self._candidates = []
            self._features = collected

        self._rebuild_anchors()

    def _rebuild_anchors(self):
        '''Rebuilds the snapping anchors (vertices and feature ids) from
        the current _features.'''
        pts = []
        fids = []
        for fid, g in self._features:
            for v in g.vertices():
                pts.append((v.x(), v.y()))
                fids.append(fid)
        self._total_vertices = len(pts)
        if pts:
            arr = np.array(pts, dtype=float)
            fid_arr = np.array(fids, dtype=np.int64)
            # Decimate by stride if there are too many (lighter snap candidates).
            if len(arr) > MAX_ANCHORS:
                step = int(np.ceil(len(arr) / MAX_ANCHORS))
                arr = arr[::step]
                fid_arr = fid_arr[::step]
            self._anchors = arr
            self._anchor_fids = fid_arr
        else:
            self._anchors = np.zeros((0, 2))
            self._anchor_fids = np.zeros(0, dtype=np.int64)

    def feature_count(self):
        return len(self._features)

    def total_vertices(self):
        return self._total_vertices

    # ------------------------------------------------------------------
    # Single-feature mode hooks
    # ------------------------------------------------------------------
    def _ensure_single_locked(self, src):
        '''In single-feature mode, confirm the target feature if not yet set.

        If a node was grabbed, use its feature; for a free point / other layer,
        use the candidate feature nearest to src.
        '''
        if self.scope != 'single' or self._locked_fid is not None:
            return
        if self._last_snap_fid is not None:
            self._lock_single(self._last_snap_fid)
        elif self._candidates:
            fid = self._nearest_candidate_fid(QgsPointXY(src[0], src[1]))
            if fid is not None:
                self._lock_single(fid)

    def _lock_single(self, fid):
        '''Narrow single-feature mode to the target feature fid (only one
        feature moves in the preview).'''
        geom = dict(self._candidates).get(fid)
        if geom is None:
            return
        self._locked_fid = fid
        self._features = [(fid, geom)]
        self._rebuild_anchors()
        self._refresh_static()

    def _unlock_single(self):
        '''Release the single-feature narrowing and return to all candidates.'''
        if self.scope != 'single':
            return
        self._locked_fid = None
        self._features = list(self._candidates)
        self._rebuild_anchors()
        self._refresh_static()

    def _refresh_static(self):
        '''Show the non-target features statically at their original position
        (only while the source layer is hidden).

        While the source layer is visible the features themselves are shown, so
        the static preview is skipped (avoids double drawing).
        '''
        self.rb_static.reset(self._geom_type)
        if (self.scope == 'single' and self._locked_fid is not None
                and self._layer_hidden):
            for f2, g2 in self._candidates:
                if f2 != self._locked_fid:
                    self.rb_static.addGeometry(g2, self.layer)

    def _nearest_candidate_fid(self, point):
        '''Return the candidate feature id nearest to point (single mode).'''
        pg = QgsGeometry.fromPointXY(point)
        best = None
        best_d = None
        for fid, g in self._candidates:
            d = g.distance(pg)
            if best_d is None or d < best_d:
                best_d = d
                best = fid
        return best

    def _after_load_gcps(self, loaded):
        # In single mode, if not yet confirmed, target the candidate nearest the first GCP.
        if (self.scope == 'single' and self._locked_fid is None
                and self._candidates):
            first = QgsPointXY(loaded[0][0][0], loaded[0][0][1])
            fid = self._nearest_candidate_fid(first)
            if fid is not None:
                self._lock_single(fid)

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    def _do_preview(self):
        self._last_update = time.monotonic()
        self.rb_preview.reset(self._geom_type)
        t = transform.to_qtransform(self._draw_matrix)
        for _fid, g in self._features:
            gg = QgsGeometry(g)
            gg.transform(t)
            self.rb_preview.addGeometry(self._maybe_simplify(gg), self.layer)
        self.canvas.refresh()

    def _maybe_simplify(self, geom):
        '''Simplify the geometry for display only when it has many vertices.

        While dragging, lower the threshold and raise the tolerance to make it
        even lighter.
        '''
        threshold = SIMPLIFY_THRESHOLD
        factor = QUALITY[self.quality]['simplify']
        if self._dragging:
            threshold = SIMPLIFY_THRESHOLD // 4
            factor *= 2.0
        if self._total_vertices <= threshold:
            return geom
        tol = self.canvas.mapUnitsPerPixel() * factor
        simplified = geom.simplify(tol)
        return simplified if simplified and not simplified.isEmpty() else geom

    # ------------------------------------------------------------------
    # Snapping
    # ------------------------------------------------------------------
    def snap_source(self, map_point, tol_map):
        '''Find the anchor vertex closest to map_point on the current preview.

        If found, return its source (original) coordinate, not the preview
        position. This supports taking nodes from the preview from the 2nd
        point onward.
        '''
        if len(self._anchors) == 0:
            return None
        prev = transform.apply_matrix(self.matrix, self._anchors)
        dx = prev[:, 0] - map_point.x()
        dy = prev[:, 1] - map_point.y()
        d2 = dx * dx + dy * dy
        i = int(np.argmin(d2))
        if np.sqrt(d2[i]) <= tol_map:
            # Remember the feature of the grabbed node (used to confirm the single target).
            if i < len(self._anchor_fids):
                self._last_snap_fid = int(self._anchor_fids[i])
            return (self._anchors[i, 0], self._anchors[i, 1])
        return None

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def apply(self):
        '''Apply the final transform to all target features (full resolution)
        and return a new memory layer.'''
        if not self.active_gcps():
            return None
        t = transform.to_qtransform(self.matrix)

        geom_str = QgsWkbTypes.displayString(self.layer.wkbType())
        crs = self.layer.crs().authid()
        uri = '{}?crs={}'.format(geom_str, crs)
        out_name = '{}_{}'.format(self.layer.name(), self.transform_label())
        out = QgsVectorLayer(uri, out_name, 'memory')
        dp = out.dataProvider()
        dp.addAttributes(self.layer.fields().toList())
        out.updateFields()

        in_scope = set(fid for fid, _ in self._features)
        new_feats = []
        for f in self.layer.getFeatures():
            if f.id() not in in_scope:
                continue
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            gg = QgsGeometry(g)
            gg.transform(t)
            nf = QgsFeature(out.fields())
            nf.setAttributes(f.attributes())
            nf.setGeometry(gg)
            new_feats.append(nf)
        dp.addFeatures(new_feats)
        out.updateExtents()

        # Keep GCP/error info in the layer custom properties.
        out.setCustomProperty('fvg/mode', self.mode)
        out.setCustomProperty('fvg/lock_scale', str(self.lock_scale))
        out.setCustomProperty('fvg/gcp_count', str(len(self.active_gcps())))
        stats = self._stats()
        out.setCustomProperty('fvg/rms', '{:.6f}'.format(stats['rms']))
        overall, sx, sy = self.scale_factors()
        out.setCustomProperty('fvg/scale', '{:.6f}'.format(overall))
        out.setCustomProperty('fvg/scale_x', '{:.6f}'.format(sx))
        out.setCustomProperty('fvg/scale_y', '{:.6f}'.format(sy))

        QgsProject.instance().addMapLayer(out)
        return out

    def apply_add(self):
        '''Add the transformed features to the current layer as new features
        (run in edit mode).

        Done with the layer in edit mode so it can be undone with Ctrl+Z.
        Returns the number of features added, or None on failure.
        '''
        if not self.active_gcps():
            return None
        if not self._ensure_editable():
            return None
        t = transform.to_qtransform(self.matrix)
        in_scope = set(fid for fid, _ in self._features)
        feats = []
        for f in self.layer.getFeatures():
            if f.id() not in in_scope:
                continue
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            gg = QgsGeometry(g)
            gg.transform(t)
            nf = QgsFeature(self.layer.fields())
            nf.setAttributes(f.attributes())
            nf.setGeometry(gg)
            feats.append(nf)
        self.layer.beginEditCommand('Freehand Georeferencer: add')
        self.layer.addFeatures(feats)
        self.layer.endEditCommand()
        self.layer.triggerRepaint()
        return len(feats)

    def apply_edit(self):
        '''Replace the geometry of the current layer's target features with the
        transformed geometry (run in edit mode).

        Done with the layer in edit mode so it can be undone with Ctrl+Z.
        Returns the number of features changed, or None on failure.
        '''
        if not self.active_gcps():
            return None
        if not self._ensure_editable():
            return None
        t = transform.to_qtransform(self.matrix)
        self.layer.beginEditCommand('Freehand Georeferencer: edit')
        n = 0
        for fid, g in self._features:
            gg = QgsGeometry(g)
            gg.transform(t)
            self.layer.changeGeometry(fid, gg)
            n += 1
        self.layer.endEditCommand()
        self.layer.triggerRepaint()
        return n

    def _ensure_editable(self):
        '''Put the layer into edit mode. No-op if already editing. Returns ok.'''
        if self.layer.isEditable():
            return True
        return bool(self.layer.startEditing())

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------
    def cleanup(self):
        self.canvas.scene().removeItem(self.rb_preview)
        self.canvas.scene().removeItem(self.rb_static)
        super().cleanup()
