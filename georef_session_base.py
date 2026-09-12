# -*- coding: utf-8 -*-
'''Shared core for a georeferencing session (data-type agnostic).

Holds the GCPs (control points), computes the transform matrix, shows the
markers, computes residuals and notifies the UI. Data-type specific behavior
(preview drawing, source snapping, output) is overridden by the vector / raster
subclasses. The UI (dock) drives this class.
'''

import csv
import time

import numpy as np

from qgis.core import (
    Qgis,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QColor

QGIS_VERTEX_MARKER_ICON_TYPE = getattr(QgsVertexMarker, 'IconType', QgsVertexMarker)
QGIS_ICON_CROSS = getattr(QGIS_VERTEX_MARKER_ICON_TYPE, 'ICON_CROSS', QgsVertexMarker.ICON_CROSS)
QGIS_ICON_X = getattr(QGIS_VERTEX_MARKER_ICON_TYPE, 'ICON_X', QgsVertexMarker.ICON_X)

from . import transform


# Max number of anchor vertices for snapping (decimated for heavy data).
MAX_ANCHORS = 5000
# Total-vertex threshold above which the preview is simplified.
SIMPLIFY_THRESHOLD = 20000

# Update interval (ms) and simplification factor per preview quality.
QUALITY = {
    'High (30fps)': {'interval': 33, 'simplify': 0.5},
    'Medium (10fps)': {'interval': 100, 'simplify': 1.0},
    'Light (4fps)': {'interval': 250, 'simplify': 2.0},
    'Minimum (1fps)': {'interval': 1000, 'simplify': 3.0},
}
DEFAULT_QUALITY = 'Medium (10fps)'

# Endpoints of the residual color ramp (green -> yellow -> red).
_COLOR_GREEN = (0, 180, 0)
_COLOR_YELLOW = (230, 200, 0)
_COLOR_RED = (220, 0, 0)


def _lerp(a, b, f):
    return int(round(a + (b - a) * f))


def residual_color(mag, max_mag):
    '''Returns a color interpolated linearly green -> yellow -> red over
    0..max_mag.

    Equal mag values get the same color. If max_mag is 0 (all equal, no
    residual) the color is green.
    '''
    if max_mag <= 1e-12:
        t = 0.0
    else:
        t = min(max(mag / max_mag, 0.0), 1.0)
    if t <= 0.5:
        f = t / 0.5
        c0, c1 = _COLOR_GREEN, _COLOR_YELLOW
    else:
        f = (t - 0.5) / 0.5
        c0, c1 = _COLOR_YELLOW, _COLOR_RED
    return QColor(_lerp(c0[0], c1[0], f),
                  _lerp(c0[1], c1[1], f),
                  _lerp(c0[2], c1[2], f))


class Gcp(object):
    '''A single control point. src is the source coordinate, dst is the target.'''

    def __init__(self, src, dst, active=True):
        self.src = (float(src[0]), float(src[1]))
        self.dst = (float(dst[0]), float(dst[1]))
        self.active = active


class GeorefSessionBase(object):
    '''Data-type agnostic base for a georeferencing session.'''

    def __init__(self, iface, layer, mode, lock_scale, quality, on_update):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.layer = layer
        self.mode = mode              # 'helmert' / 'affine'
        self.lock_scale = lock_scale
        self.quality = quality if quality in QUALITY else DEFAULT_QUALITY
        self.on_update = on_update    # callback notifying the UI of stats/table

        self.gcps = []
        self.matrix = transform._identity()
        self._draw_matrix = transform._identity()  # matrix actually drawn (provisional while dragging)
        self._dragging = False        # render a lighter preview while dragging

        self._layer_was_visible = True
        self._layer_hidden = False
        self._last_snap_fid = None    # feature id of the node last grabbed by snap_source

        # Residual vectors (transformed src -> dst).
        self.rb_residual = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.rb_residual.setColor(QColor(255, 0, 0, 200))
        self.rb_residual.setWidth(1)
        self.rb_residual.setZValue(1000)

        self.markers = []             # one QgsVertexMarker per GCP

        # Throttling of preview updates.
        self._last_update = 0.0
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._do_preview)

    # ------------------------------------------------------------------
    # Hooks overridden by subclasses (no-op defaults)
    # ------------------------------------------------------------------
    def _ensure_single_locked(self, src):
        '''Vector single-feature confirmation hook. No-op by default.'''
        pass

    def _unlock_single(self):
        '''Vector single-feature release hook. No-op by default.'''
        pass

    def _refresh_static(self):
        '''Vector static preview hook. No-op by default.'''
        pass

    def _after_load_gcps(self, loaded):
        '''Post-load hook (e.g. vector single-mode locking). No-op by default.'''
        pass

    def feature_count(self):
        return 0

    def total_vertices(self):
        return 0

    def snap_source(self, map_point, tol_map) -> tuple[float, float] | None:
        '''Source snap onto the target itself. None by default (e.g. raster has
        no vertices); vector overrides this.'''
        return None

    def _do_preview(self):
        raise NotImplementedError

    def apply(self):
        raise NotImplementedError

    def apply_add(self):
        '''Add transformed features to the current layer. Not supported by default.'''
        return None

    def apply_edit(self):
        '''Edit the current layer in place. Not supported by default.'''
        return None

    # ------------------------------------------------------------------
    # Settings changes
    # ------------------------------------------------------------------
    def set_transform(self, mode, lock):
        # Set mode and fixed-scale together, recompute only once.
        self.mode = mode
        self.lock_scale = lock
        self.recompute()

    def transform_label(self):
        '''Short label of the transform type, used e.g. in the output layer name.'''
        if self.mode == 'affine':
            return 'Affine'
        return 'Helmert(x1)' if self.lock_scale else 'Helmert'

    def set_quality(self, quality):
        if quality in QUALITY:
            self.quality = quality

    # ------------------------------------------------------------------
    # GCP operations
    # ------------------------------------------------------------------
    def add_gcp(self, src, dst):
        # In single-feature mode (vector), confirm the target from the first source.
        self._ensure_single_locked(src)
        self.gcps.append(Gcp(src, dst))
        # On the first point, hide the source layer and switch to the preview.
        if len(self.gcps) == 1:
            self._hide_source_layer()
        self.recompute()

    def toggle_gcp(self, idx):
        if 0 <= idx < len(self.gcps):
            self.gcps[idx].active = not self.gcps[idx].active
            self.recompute()

    def set_active(self, idx, active):
        if 0 <= idx < len(self.gcps):
            self.gcps[idx].active = active
            self.recompute()

    def move_gcp_dst(self, idx, new_dst):
        '''Move and commit the destination (dst) coordinate of an existing GCP.'''
        if 0 <= idx < len(self.gcps):
            self.gcps[idx].dst = (float(new_dst[0]), float(new_dst[1]))
            self.recompute()

    def remove_last(self):
        if self.gcps:
            self.gcps.pop()
            if not self.gcps:
                self._restore_source_layer()
                self._unlock_single()
            self.recompute()

    def clear_gcps(self):
        self.gcps = []
        self._restore_source_layer()
        self._unlock_single()
        self.recompute()

    def active_gcps(self):
        return [g for g in self.gcps if g.active]

    # ------------------------------------------------------------------
    # Transform computation and preview
    # ------------------------------------------------------------------
    def recompute(self):
        active = self.active_gcps()
        if active:
            src = np.array([g.src for g in active])
            dst = np.array([g.dst for g in active])
            self.matrix = transform.compute_matrix(
                src, dst, self.mode, self.lock_scale)
        else:
            self.matrix = transform._identity()

        self._draw_matrix = self.matrix
        self._request_preview()
        self._update_markers()
        self._notify_stats()

    def set_drag_preview(self, src, dst):
        '''Update only the preview with a transform that includes the
        provisional point (src -> dst) being dragged.'''
        # In single mode, confirm the target on drag start so only one feature moves.
        self._ensure_single_locked(src)
        active = self.active_gcps()
        srcs = [g.src for g in active] + [tuple(src)]
        dsts = [g.dst for g in active] + [tuple(dst)]
        self._dragging = True
        self._draw_matrix = transform.compute_matrix(
            np.array(srcs), np.array(dsts), self.mode, self.lock_scale)
        self._request_preview()

    def set_drag_gcp_preview(self, idx, new_dst):
        '''Update only the preview with an existing GCP's dst replaced by the
        dragged position.'''
        srcs = []
        dsts = []
        for j, g in enumerate(self.gcps):
            if not g.active:
                continue
            srcs.append(g.src)
            dsts.append(tuple(new_dst) if j == idx else g.dst)
        self._dragging = True
        if srcs:
            self._draw_matrix = transform.compute_matrix(
                np.array(srcs), np.array(dsts), self.mode, self.lock_scale)
        else:
            self._draw_matrix = self.matrix
        self._request_preview()

    def clear_drag_preview(self):
        '''Return to the normal preview when a drag is committed/cancelled.'''
        self._dragging = False
        self._draw_matrix = self.matrix
        self._request_preview()

    def preview_pos(self, src):
        '''Return the current preview position of the source coordinate src.'''
        M = self._draw_matrix if self._dragging else self.matrix
        p = transform.apply_matrix(M, [src])[0]
        return QgsPointXY(p[0], p[1])

    def _request_preview(self):
        '''Request a preview redraw with throttling.'''
        interval = QUALITY[self.quality]['interval'] / 1000.0
        now = time.monotonic()
        if now - self._last_update >= interval:
            self._do_preview()
        elif not self._timer.isActive():
            remaining = int((interval - (now - self._last_update)) * 1000)
            self._timer.start(max(1, remaining))

    # ------------------------------------------------------------------
    # GCP markers (colored by error, active state shown)
    # ------------------------------------------------------------------
    def _clear_markers(self):
        for m in self.markers:
            self.canvas.scene().removeItem(m)
        self.markers = []

    def _update_markers(self):
        self._clear_markers()
        self.rb_residual.reset(Qgis.GeometryType.Line)

        stats = self._stats()
        per_point = stats['per_point']

        # Map active GCP indices (per_point covers active points only).
        active_idx = [i for i, g in enumerate(self.gcps) if g.active]
        mag_by_gcp = {}
        for k, gi in enumerate(active_idx):
            mag_by_gcp[gi] = per_point[k, 2] if k < len(per_point) else 0.0
        max_mag = float(per_point[:, 2].max()) if len(per_point) else 0.0

        for i, g in enumerate(self.gcps):
            m = QgsVertexMarker(self.canvas)
            m.setCenter(QgsPointXY(g.dst[0], g.dst[1]))
            m.setIconSize(14)
            m.setPenWidth(3)
            if g.active:
                m.setIconType(QGIS_ICON_CROSS)
                m.setColor(residual_color(mag_by_gcp.get(i, 0.0), max_mag))
            else:
                m.setIconType(QGIS_ICON_X)
                m.setColor(QColor(150, 150, 150))
            self.markers.append(m)

        # Residual display: for each GCP draw an independent segment from the
        # node that landed on the preview to the target point (dst). addGeometry
        # keeps each segment separate.
        for g in self.gcps:
            if not g.active:
                continue
            pred = transform.apply_matrix(self.matrix, [g.src])[0]
            seg = QgsGeometry.fromPolylineXY([
                QgsPointXY(pred[0], pred[1]),
                QgsPointXY(g.dst[0], g.dst[1]),
            ])
            self.rb_residual.addGeometry(seg, None)

    # ------------------------------------------------------------------
    # Residual statistics
    # ------------------------------------------------------------------
    def _stats(self):
        active = self.active_gcps()
        if not active:
            return {'per_point': np.zeros((0, 3)), 'rms': 0.0, 'std': 0.0}
        src = np.array([g.src for g in active])
        dst = np.array([g.dst for g in active])
        return transform.error_stats(self.matrix, src, dst)

    def scale_factors(self):
        '''Return the scale of the current transform matrix (overall, x, y).

        For Helmert (similarity) the three agree; affine can differ per axis.
        overall is the square root of the area ratio (= linear scale).
        '''
        a, b = self.matrix[0, 0], self.matrix[0, 1]
        d, e = self.matrix[1, 0], self.matrix[1, 1]
        det = a * e - b * d
        overall = float(np.sqrt(abs(det)))
        sx = float(np.hypot(a, d))
        sy = float(np.hypot(b, e))
        return overall, sx, sy

    def _notify_stats(self):
        if not self.on_update:
            return
        stats = self._stats()  # RMS/std computed from active points only
        rows = []
        mags = []
        for i, g in enumerate(self.gcps):
            # Show the residual at the current matrix for every point, inactive included.
            res = transform.residuals(self.matrix, [g.src], [g.dst])[0]
            ex, ey = float(res[0]), float(res[1])
            mag = float((ex * ex + ey * ey) ** 0.5)
            mags.append(mag)
            rows.append({
                'index': i,
                'active': g.active,
                'ex': ex, 'ey': ey, 'mag': mag,
            })
        overall, sx, sy = self.scale_factors()
        max_mag = max(mags) if mags else 0.0
        self.on_update({
            'rms': stats['rms'], 'std': stats['std'],
            'scale': overall, 'scale_x': sx, 'scale_y': sy,
            'max_mag': max_mag, 'rows': rows,
        })

    # ------------------------------------------------------------------
    # Snapping (queries from canvas interaction)
    # ------------------------------------------------------------------
    def _nearest_layer_vertex(self, map_point, tol_map, include_self):
        '''Return the vertex (map coordinate) closest to map_point among the
        visible vector layers.

        include_self=True also includes the target layer itself (self-snap),
        which only applies when the target is a vector layer. Only layers with
        the same CRS are considered; a rect filter scans only nearby features.
        None if nothing is found.
        '''
        rect = QgsRectangle(
            map_point.x() - tol_map, map_point.y() - tol_map,
            map_point.x() + tol_map, map_point.y() + tol_map)
        layers = list(self.canvas.layers())
        if include_self and isinstance(self.layer, QgsVectorLayer) \
                and self.layer not in layers:
            layers.append(self.layer)

        best = None
        best_d = tol_map
        seen = set()
        for lyr in layers:
            if not isinstance(lyr, QgsVectorLayer) or lyr.id() in seen:
                continue
            seen.add(lyr.id())
            if not include_self and lyr.id() == self.layer.id():
                continue
            if lyr.crs() != self.layer.crs():
                continue
            req = QgsFeatureRequest().setFilterRect(rect).setNoAttributes()
            for f in lyr.getFeatures(req):
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                for v in g.vertices():
                    d = ((v.x() - map_point.x()) ** 2 +
                         (v.y() - map_point.y()) ** 2) ** 0.5
                    if d <= best_d:
                        best_d = d
                        best = (v.x(), v.y())
        return best

    def snap_dest(self, map_point, tol_map):
        '''Destination snap: nearest vertex of any visible vector layer (incl. self).'''
        return self._nearest_layer_vertex(map_point, tol_map, include_self=True)

    def snap_source_other(self, map_point, tol_map):
        '''Source snap (other than the target geometry): nearest vertex of other visible layers.'''
        return self._nearest_layer_vertex(map_point, tol_map, include_self=False)

    def map_to_source(self, pt):
        '''Inverse-transform a map coordinate (current preview space) to source space.

        Used to bring a free point or another layer's node back into the source
        coordinate system when used as a transform source.
        '''
        A = self.matrix[:, :2]
        t = self.matrix[:, 2]
        try:
            a_inv = np.linalg.inv(A)
        except np.linalg.LinAlgError:
            return (float(pt[0]), float(pt[1]))
        o = a_inv @ (np.array([pt[0], pt[1]], dtype=float) - t)
        return (float(o[0]), float(o[1]))

    def nearest_gcp(self, map_point, tol_map):
        '''Return the index of the existing GCP (dst marker) near map_point.'''
        best = None
        best_d = tol_map
        for i, g in enumerate(self.gcps):
            d = ((g.dst[0] - map_point.x()) ** 2 +
                 (g.dst[1] - map_point.y()) ** 2) ** 0.5
            if d <= best_d:
                best_d = d
                best = i
        return best

    # ------------------------------------------------------------------
    # Source layer visibility control
    # ------------------------------------------------------------------
    def _hide_source_layer(self):
        node = QgsProject.instance().layerTreeRoot().findLayer(self.layer.id())
        if node:
            self._layer_was_visible = node.itemVisibilityChecked()
            # Do not change the layer tree visibility here. Previously the
            # source layer was hidden to avoid double-drawing (original vs
            # preview). User preference is to keep original features visible
            # while showing the preview, so keep visibility unchanged.
        self._layer_hidden = False
        self._refresh_static()
        # Re-render the map so the preview rubber band updates.
        self.canvas.refresh()

    def _restore_source_layer(self):
        # No-op for layer-tree visibility: we never hide the source layer,
        # so there is nothing to restore. Keep the internal flag consistent
        # and refresh overlays.
        self._layer_hidden = False
        self._refresh_static()
        self.canvas.refresh()

    # ------------------------------------------------------------------
    # GCP file I/O
    # ------------------------------------------------------------------
    def save_gcps(self, path):
        '''Save the GCPs and residual info to CSV.'''
        stats = self._stats()
        per_point = stats['per_point']
        active_idx = [i for i, g in enumerate(self.gcps) if g.active]
        with open(path, 'w', newline='', encoding='utf-8') as fp:
            w = csv.writer(fp)
            w.writerow(['index', 'active', 'srcX', 'srcY', 'dstX', 'dstY',
                        'errX', 'errY', 'residual'])
            for i, g in enumerate(self.gcps):
                if g.active and i in active_idx:
                    k = active_idx.index(i)
                    ex, ey, mag = per_point[k] if k < len(per_point) else (0, 0, 0)
                else:
                    ex = ey = mag = ''
                w.writerow([i, int(g.active), g.src[0], g.src[1],
                            g.dst[0], g.dst[1], ex, ey, mag])
            overall, sx, sy = self.scale_factors()
            w.writerow([])
            w.writerow(['# mode', self.mode, 'lock_scale', int(self.lock_scale)])
            w.writerow(['# RMS', stats['rms'], 'STD', stats['std']])
            w.writerow(['# scale', overall, 'scaleX', sx, 'scaleY', sy])

    def load_gcps(self, path):
        '''Load and reproduce GCPs from CSV (to reuse the same transform on
        another layer).

        Reads the format written by save_gcps (index, active, srcX, srcY, dstX,
        dstY, ...). Assumes the same CRS and uses the src/dst coordinates as is.
        Returns the number of points loaded.
        '''
        loaded = []
        with open(path, newline='', encoding='utf-8') as fp:
            for row in csv.reader(fp):
                if not row or row[0].strip().startswith('#'):
                    continue
                if row[0].strip() == 'index':
                    continue
                try:
                    active = bool(int(row[1]))
                    sx, sy = float(row[2]), float(row[3])
                    dx, dy = float(row[4]), float(row[5])
                except (ValueError, IndexError):
                    continue
                loaded.append(((sx, sy), (dx, dy), active))
        if not loaded:
            return 0

        self.gcps = [Gcp(src, dst, active) for src, dst, active in loaded]
        self._after_load_gcps(loaded)
        if self.gcps:
            self._hide_source_layer()
        self.recompute()
        return len(self.gcps)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------
    def cleanup(self):
        self._timer.stop()
        self._clear_markers()
        self.canvas.scene().removeItem(self.rb_residual)
        self._restore_source_layer()
        self.canvas.refresh()

    def set_preview_opacity(self, opacity):
        pass