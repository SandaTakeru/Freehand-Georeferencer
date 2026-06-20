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
        self._image = image          # QImage in source space
        self._extent = extent        # QgsRectangle, source extent of the image
        self._matrix = transform._identity()

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
        painter.setOpacity(PREVIEW_OPACITY)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setTransform(t, True)
        painter.drawImage(0, 0, self._image)
        painter.restore()


def _render_layer_image(layer, max_dim=PREVIEW_MAX_DIM):
    '''Render the raster layer to a transparent-background QImage once.

    Returns (QImage, QgsRectangle) where the rectangle is the exact extent the
    image covers (the map settings' visible extent).
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
    return img, ms.visibleExtent()


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
        # Base geotransform from the layer's displayed extent (north-up), NOT the
        # raw file geotransform. An ungeoreferenced raster stores a positive n-s
        # pixel size (gt[5] > 0), while QGIS displays it north-up — the same
        # basis the preview uses. Composing with the raw gt would flip the
        # output north-south. For a properly north-up raster this equals the
        # file gt, so there is no change.
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
        m = self.matrix
        m00, m01, m02 = float(m[0, 0]), float(m[0, 1]), float(m[0, 2])
        m10, m11, m12 = float(m[1, 0]), float(m[1, 1]), float(m[1, 2])
        # Compose M (world->world') with the geotransform (pixel->world).
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
        vrt.FlushCache()
        vrt = None
        ds = None

        out_name = '{}_{}'.format(self.layer.name(), self.transform_label())
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
            return os.path.join(src_dir, fname)
        return os.path.join(tempfile.gettempdir(), fname)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------
    def cleanup(self):
        if self.preview_item is not None:
            self.canvas.scene().removeItem(self.preview_item)
            self.preview_item = None
        super().cleanup()
