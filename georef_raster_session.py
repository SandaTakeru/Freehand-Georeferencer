# -*- coding: utf-8 -*-
'''Raster georeferencing session.

The raster has no vertices, so the source is picked as a free point (snapping
the destination to vector vertices still works). The live preview overlays the
rendered raster image with the affine transform applied. On apply, a new
georeferenced layer is written as a VRT with an updated geotransform — no
resampling, lossless (Helmert/Affine map exactly to a GDAL geotransform).
'''

import os
import tempfile
import time

from qgis.core import (
    QgsMapRendererSequentialJob,
    QgsMapSettings,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
)
from qgis.gui import QgsMapCanvasItem
from qgis.PyQt.QtCore import QPointF, QRectF, QSize
from qgis.PyQt.QtGui import QColor, QImage, QPainter, QPolygonF, QTransform

from . import transform
from .georef_session_base import GeorefSessionBase


# Max pixel dimension of the rendered preview image (caps cost for huge rasters).
PREVIEW_MAX_DIM = 2048
# Opacity of the preview overlay image.
PREVIEW_OPACITY = 0.7


class RasterPreviewItem(QgsMapCanvasItem):
    '''Canvas overlay that draws a cached raster image with an affine transform.

    The image is in source (layer) space, covering `extent`. Each paint maps the
    image corners through the current matrix and the canvas map->device
    transform, then draws the image into that parallelogram.
    '''

    def __init__(self, canvas, image, extent):
        super().__init__(canvas)
        self._canvas = canvas
        self._image = image
        self._extent = extent
        self._matrix = transform._identity()
        self._opacity = PREVIEW_OPACITY

    def set_matrix(self, matrix):
        self._matrix = matrix
        self.update()

    def updatePosition(self):
        # Recompute on canvas pan/zoom.
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        # Cover the whole canvas (the overlay is clipped to it anyway).
        return QRectF(0, 0, self._canvas.width(), self._canvas.height())

    def _dst_quad(self):
        e = self._extent
        corners_src = [
            (e.xMinimum(), e.yMaximum()),  # image px (0, 0)
            (e.xMaximum(), e.yMaximum()),  # (w, 0)
            (e.xMaximum(), e.yMinimum()),  # (w, h)
            (e.xMinimum(), e.yMinimum()),  # (0, h)
        ]
        pts = []
        for mx, my in corners_src:
            d = transform.apply_matrix(self._matrix, [(mx, my)])[0]
            p = self.toCanvasCoordinates(QgsPointXY(d[0], d[1]))
            pts.append(QPointF(p.x(), p.y()))
        return pts

    def paint(self, painter, option=None, widget=None):
        if self._image is None or self._image.isNull():
            return
        w = self._image.width()
        h = self._image.height()
        if w <= 0 or h <= 0:
            return
        dst = self._dst_quad()
        src_poly = QPolygonF([
            QPointF(0, 0), QPointF(w, 0), QPointF(w, h), QPointF(0, h)])
        dst_poly = QPolygonF(dst)
        t = QTransform()
        if not QTransform.quadToQuad(src_poly, dst_poly, t):
            return
        painter.save()
        painter.setOpacity(self._opacity)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setTransform(t, True)
        painter.drawImage(0, 0, self._image)
        painter.restore()

    def set_opacity(self, opacity):
        self._opacity = opacity
        self.update()


def _render_layer_image(layer, max_dim=PREVIEW_MAX_DIM):
    '''Render the raster layer to a transparent-background QImage once.

    The image is cropped to exactly layer.extent() so the preview and the
    output (whose geotransform is built from layer.extent()) share the identical
    frame. The renderer pads the visible extent to the output aspect ratio, so
    without cropping the preview would be offset by that padding.

    Returns (QImage, QgsRectangle) where the rectangle is layer.extent().
    '''
    ext = layer.extent()
    w_m = ext.width()
    h_m = ext.height()
    if w_m <= 0 or h_m <= 0:
        return None, ext
    if w_m >= h_m:
        w = max_dim
        h = max(1, int(round(max_dim * h_m / w_m)))
    else:
        h = max_dim
        w = max(1, int(round(max_dim * w_m / h_m)))

    ms = QgsMapSettings()
    ms.setLayers([layer])
    ms.setExtent(ext)
    ms.setOutputSize(QSize(w, h))
    ms.setDestinationCrs(layer.crs())
    ms.setBackgroundColor(QColor(0, 0, 0, 0))
    job = QgsMapRendererSequentialJob(ms)
    job.start()
    job.waitForFinished()
    img = job.renderedImage()

    # Crop the padded render down to exactly layer.extent().
    vis = ms.visibleExtent()
    if vis.width() > 0 and vis.height() > 0:
        sx = img.width() / vis.width()
        sy = img.height() / vis.height()
        x0 = int(round((ext.xMinimum() - vis.xMinimum()) * sx))
        y0 = int(round((vis.yMaximum() - ext.yMaximum()) * sy))
        cw = int(round(ext.width() * sx))
        ch = int(round(ext.height() * sy))
        if cw > 0 and ch > 0:
            img = img.copy(x0, y0, cw, ch)
    return img, ext


class RasterGeorefSession(GeorefSessionBase):
    def __init__(self, iface, layer, mode, lock_scale, quality, on_update):
        super().__init__(iface, layer, mode, lock_scale, quality, on_update)
        img, ext = _render_layer_image(layer)
        self._image = img
        self._image_extent = ext
        self.preview_item = RasterPreviewItem(self.canvas, img, ext)

    def feature_count(self):
        # Non-zero so the dock does not abort the session (raster has no features).
        return 1 if self._image is not None else 0

    def total_vertices(self):
        return 0

    def _source_frame_points(self):
        '''Return the 3x3 raster-frame points in the raster's source plane.

        This is the same source frame used by the preview overlay: the image is
        treated as a rectangular quad spanning `layer.extent()`, not the raw pixel
        grid. Using raw pixel coordinates here is wrong for previewed/rotated VRTs
        because their transform is defined in source-space map coordinates, not in
        the unprojected image pixel grid.
        '''
        if self.layer is None:
            return []
        ext = self.layer.extent()
        if not ext.isValid() or ext.width() <= 0 or ext.height() <= 0:
            return []
        xs = [ext.xMinimum(), ext.xMinimum() + ext.width() / 2.0, ext.xMaximum()]
        ys = [ext.yMaximum(), ext.yMaximum() - ext.height() / 2.0, ext.yMinimum()]
        return [(x, y) for y in ys for x in xs]

    def _map_frame_points(self, layer):
        '''Return the 3x3 raster frame points in map coordinates.'''
        if layer is None:
            return []
        ext = layer.extent()
        if not ext.isValid() or ext.width() <= 0 or ext.height() <= 0:
            return []
        xs = [ext.xMinimum(), ext.xMinimum() + ext.width() / 2.0, ext.xMaximum()]
        ys = [ext.yMaximum(), ext.yMaximum() - ext.height() / 2.0, ext.yMinimum()]
        return [(x, y) for y in ys for x in xs]

    def snap_source(self, map_point, tol_map) -> tuple[float, float] | None:
        '''Snap to the currently previewed raster frame in the source plane.

        The source points belong to the raster's own frame, not to the map frame.
        We therefore evaluate the 3x3 frame corners/midpoints in the source plane
        defined by layer.extent(), then project them through the current preview
        transform to compare against the cursor. This keeps the snap points aligned
        with the rendered raster and avoids the inversion/jitter seen when using
        raw pixel coordinates on transformed VRTs.
        '''
        best = None
        best_d = tol_map
        for src in self._source_frame_points():
            pt = self.preview_pos(src)
            d = ((pt.x() - map_point.x()) ** 2 + (pt.y() - map_point.y()) ** 2) ** 0.5
            if d <= best_d:
                best_d = d
                best = src
        return best

    def _nearest_raster_frame_point(self, map_point, tol_map, include_self=False):
        '''Nearest point on the frame of any visible raster layer in map coordinates.'''
        layers = list(self.canvas.layers())
        if include_self and isinstance(self.layer, QgsRasterLayer) and self.layer not in layers:
            layers.append(self.layer)

        best = None
        best_d = tol_map
        seen = set()
        for lyr in layers:
            if not isinstance(lyr, QgsRasterLayer) or lyr.id() in seen:
                continue
            seen.add(lyr.id())
            if not include_self and lyr.id() == self.layer.id():
                continue
            if lyr.crs() != self.layer.crs():
                continue
            for x, y in self._map_frame_points(lyr):
                d = ((x - map_point.x()) ** 2 + (y - map_point.y()) ** 2) ** 0.5
                if d <= best_d:
                    best_d = d
                    best = (x, y)
        return best

    def snap_dest(self, map_point, tol_map):
        '''Destination snap: nearest vector vertex or raster frame point.'''
        best = self._nearest_layer_vertex(map_point, tol_map, include_self=True)
        if best is not None:
            return best
        return self._nearest_raster_frame_point(map_point, tol_map, include_self=True)

    def snap_source_other(self, map_point, tol_map):
        '''Source snap from other visible layers, including other rasters.'''
        best = self._nearest_layer_vertex(map_point, tol_map, include_self=False)
        if best is not None:
            return best
        return self._nearest_raster_frame_point(map_point, tol_map, include_self=False)

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    def _do_preview(self):
        self._last_update = time.monotonic()
        if self.preview_item is not None:
            self.preview_item.set_matrix(self._draw_matrix)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def _source_path(self):
        '''Return the GDAL-openable file path of the source raster, or None.'''
        src = self.layer.source()
        if not src:
            return None
        # Strip provider-specific options (e.g. "path|option=...").
        path = src.split('|', 1)[0]
        return path if os.path.exists(path) else None

    def apply(self):
        '''Write a new georeferenced raster (VRT) with the composed geotransform.

        Returns the new QgsRasterLayer, or None on failure.
        '''
        if not self.active_gcps():
            return None
        path = self._source_path()
        if not path:
            return None
        try:
            from osgeo import gdal
        except ImportError:
            return None

        ds = gdal.Open(path)
        if ds is None:
            return None
        # Use the source dataset's genuine GDAL geotransform only when it is an
        # actual georeferenced raster/VRT (including the north-up VRT output from
        # a previous pass). A plain unreferenced image usually has a synthetic
        # default geotransform with a positive Y pixel size, which would cause
        # the output to appear upside-down. In that case, fall back to the layer's
        # currently displayed north-up extent, which matches the preview math.
        gt = ds.GetGeoTransform()
        is_vrt = bool(ds.GetDriver() and ds.GetDriver().ShortName.lower() == 'vrt')
        is_valid_gt = (
            gt is not None and len(gt) == 6 and gt[5] < 0
        )
        if is_vrt and gt is not None:
            g = gt
        elif is_valid_gt:
            g = gt
        else:
            cols = self.layer.width()
            rows = self.layer.height()
            if cols <= 0 or rows <= 0:
                ds = None
                return None
            ext = self.layer.extent()
            g = (
                ext.xMinimum(), ext.width() / cols, 0.0,
                ext.yMaximum(), 0.0, -ext.height() / rows,
            )
        assert g is not None
        # Compose M (world->world') with the base pixel->world transform. This
        # keeps both the current preview space and any already-applied VRT
        # georeferencing consistent.
        m = self.matrix
        m00, m01, m02 = float(m[0, 0]), float(m[0, 1]), float(m[0, 2])
        m10, m11, m12 = float(m[1, 0]), float(m[1, 1]), float(m[1, 2])
        new_gt = (
            m00 * g[0] + m01 * g[3] + m02,
            m00 * g[1] + m01 * g[4],
            m00 * g[2] + m01 * g[5],
            m10 * g[0] + m11 * g[3] + m12,
            m10 * g[1] + m11 * g[4],
            m10 * g[2] + m11 * g[5],
        )

        out_path = self._output_path(path)
        vrt = gdal.GetDriverByName('VRT').CreateCopy(out_path, ds)
        if vrt is None:
            ds = None
            return None
        vrt.SetGeoTransform(new_gt)
        # Write the CRS so the output is self-describing (avoids the "CRS was
        # undefined" warning). Use the layer CRS, or the project CRS if the
        # source raster has none (the georeferencing is done in that CRS).
        crs = self.layer.crs()
        if not crs.isValid():
            crs = QgsProject.instance().crs()
        if crs.isValid():
            vrt.SetProjection(crs.toWkt())
        vrt.FlushCache()
        vrt = None
        ds = None

        out_name = os.path.splitext(os.path.basename(out_path))[0]
        rl = QgsRasterLayer(out_path, out_name)
        if not rl.isValid():
            return None
        stats = self._stats()
        overall, sx, sy = self.scale_factors()
        rl.setCustomProperty('fvg/mode', self.mode)
        rl.setCustomProperty('fvg/lock_scale', str(self.lock_scale))
        rl.setCustomProperty('fvg/gcp_count', str(len(self.active_gcps())))
        rl.setCustomProperty('fvg/rms', '{:.6f}'.format(stats['rms']))
        rl.setCustomProperty('fvg/scale', '{:.6f}'.format(overall))
        QgsProject.instance().addMapLayer(rl)
        return rl

    def _output_path(self, src_path):
        '''Build the output VRT path beside the source, or in temp if not writable.'''
        base = os.path.splitext(os.path.basename(src_path))[0]
        fname = '{}_{}.vrt'.format(base, self.transform_label())
        src_dir = os.path.dirname(src_path)
        if src_dir and os.access(src_dir, os.W_OK):
            return self._unique_output_path(src_dir, fname)
        return self._unique_output_path(tempfile.gettempdir(), fname)

    def _unique_output_path(self, dest_dir, filename):
        '''Return a path that does not overwrite an existing file by adding a numeric suffix.'''
        base, ext = os.path.splitext(filename)
        candidate = os.path.join(dest_dir, filename)
        index = 1
        while os.path.exists(candidate):
            candidate = os.path.join(dest_dir, f'{base}_{index}{ext}')
            index += 1
        return candidate

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------
    def cleanup(self):
        if self.preview_item is not None:
            self.canvas.scene().removeItem(self.preview_item)
            self.preview_item = None
        super().cleanup()

    def set_preview_opacity(self, opacity):
        if self.preview_item is not None:
            self.preview_item.set_opacity(opacity)
            self.canvas.refresh()
