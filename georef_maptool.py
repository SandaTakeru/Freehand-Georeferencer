# -*- coding: utf-8 -*-
'''Map tool for georeferencing.

Adding a new GCP:
  1. Pick the source with mouse press (priority: vertex of the target geometry
     -> vertex of another visible layer -> free point)
  2. Release at the matching new position -> add the GCP

Operating an existing GCP:
  - light click -> toggle whether it is included in the fit
  - drag        -> move the destination (dst)

Helpers:
  - the grabbable source node under the cursor is always highlighted
  - while dragging, the cursor position acts as a provisional GCP for a live
    preview
  - destinations snap to the vertices of any visible vector layer (incl. self)
'''

from qgis.core import Qgis, QgsPointXY
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QColor

# Pixel tolerance for snapping / click detection.
SNAP_PIXELS = 14
CLICK_PIXELS = 6


class GeorefMapTool(QgsMapTool):
    def __init__(self, iface, session):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        super().__init__(self.canvas)
        self.session = session

        self._pending_src = None      # snapped source coordinate (x, y)
        self._press_screen = None     # screen position at press
        self._press_gcp = None        # index of the nearby existing GCP at press

        # Displacement guide line shown while dragging.
        self.rb_drag = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.rb_drag.setColor(QColor(255, 140, 0, 220))
        self.rb_drag.setWidth(2)

        # Highlight for the grabbable source node (yellow circle).
        self.hoverMarker = QgsVertexMarker(self.canvas)
        self.hoverMarker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.hoverMarker.setColor(QColor(255, 210, 0))
        self.hoverMarker.setIconSize(16)
        self.hoverMarker.setPenWidth(3)
        self.hoverMarker.hide()

        # Highlight for the destination snap (cyan box).
        self.destMarker = QgsVertexMarker(self.canvas)
        self.destMarker.setIconType(QgsVertexMarker.ICON_BOX)
        self.destMarker.setColor(QColor(0, 200, 255))
        self.destMarker.setIconSize(16)
        self.destMarker.setPenWidth(3)
        self.destMarker.hide()

    def _tol_map(self, pixels):
        return self.canvas.mapUnitsPerPixel() * pixels

    def _snapped_dest(self, screen_pos):
        '''Resolves the destination coordinate from a screen position (snapping
        when possible).

        Returns: (QgsPointXY, snapped: bool)
        '''
        cur = self.toMapCoordinates(screen_pos)
        snap = self.session.snap_dest(cur, self._tol_map(SNAP_PIXELS))
        if snap is not None:
            return QgsPointXY(snap[0], snap[1]), True
        return cur, False

    def canvasPressEvent(self, e):
        if not self.session:
            return
        self._press_screen = e.pos()
        map_pt = self.toMapCoordinates(e.pos())

        # Check proximity to an existing GCP first; if close, prefer editing it
        # (click = toggle on/off, drag = move the destination dst).
        self._press_gcp = self.session.nearest_gcp(
            map_pt, self._tol_map(SNAP_PIXELS))
        if self._press_gcp is not None:
            self._pending_src = None
            return

        # Resolve the source. Priority:
        #   1. vertex of the target geometry (exact source coordinate)
        #   2. vertex of another visible layer (map coord -> inverse to source)
        #   3. free point (no node; map coord -> inverse to source)
        tol = self._tol_map(SNAP_PIXELS)
        src = self.session.snap_source(map_pt, tol)
        if src is not None:
            self._pending_src = src
            self.hoverMarker.setCenter(self.session.preview_pos(src))
            self.hoverMarker.show()
        else:
            snapped = self.session.snap_source_other(map_pt, tol)
            pick = snapped if snapped is not None else (map_pt.x(), map_pt.y())
            # A free point / other layer does not identify a target feature
            # (single-feature mode locks onto the nearest feature instead).
            self.session._last_snap_fid = None
            self._pending_src = self.session.map_to_source(pick)
            self.hoverMarker.setCenter(QgsPointXY(pick[0], pick[1]))
            self.hoverMarker.show()

    def canvasMoveEvent(self, e):
        if not self.session:
            return

        if self._press_gcp is not None:
            # Drag the destination dst of an existing GCP (live preview).
            dest, snapped = self._snapped_dest(e.pos())
            start = self.session.preview_pos(self.session.gcps[self._press_gcp].src)
            self.rb_drag.reset(Qgis.GeometryType.Line)
            self.rb_drag.addPoint(start, False)
            self.rb_drag.addPoint(dest, True)
            if snapped:
                self.destMarker.setCenter(dest)
                self.destMarker.show()
            else:
                self.destMarker.hide()
            self.session.set_drag_gcp_preview(
                self._press_gcp, (dest.x(), dest.y()))
        elif self._pending_src is not None:
            # Dragging: use the cursor position as a provisional GCP for preview.
            dest, snapped = self._snapped_dest(e.pos())
            start = self.session.preview_pos(self._pending_src)
            self.rb_drag.reset(Qgis.GeometryType.Line)
            self.rb_drag.addPoint(start, False)
            self.rb_drag.addPoint(dest, True)
            if snapped:
                self.destMarker.setCenter(dest)
                self.destMarker.show()
            else:
                self.destMarker.hide()
            self.session.set_drag_preview(
                self._pending_src, (dest.x(), dest.y()))
        else:
            # Hovering: highlight the grabbable source node (target / other layer).
            map_pt = self.toMapCoordinates(e.pos())
            tol = self._tol_map(SNAP_PIXELS)
            src = self.session.snap_source(map_pt, tol)
            if src is not None:
                self.hoverMarker.setCenter(self.session.preview_pos(src))
                self.hoverMarker.show()
            else:
                other = self.session.snap_source_other(map_pt, tol)
                if other is not None:
                    self.hoverMarker.setCenter(QgsPointXY(other[0], other[1]))
                    self.hoverMarker.show()
                else:
                    self.hoverMarker.hide()

    def canvasReleaseEvent(self, e):
        self.rb_drag.reset(Qgis.GeometryType.Line)
        self.destMarker.hide()
        if self.session:
            self.session.clear_drag_preview()
        if not self.session or self._press_screen is None:
            self._reset_pending()
            return

        moved = (e.pos() - self._press_screen).manhattanLength()

        # Operating an existing GCP: click = toggle, drag = move destination dst.
        if self._press_gcp is not None:
            if moved <= CLICK_PIXELS:
                self.session.toggle_gcp(self._press_gcp)
            else:
                dest, _snapped = self._snapped_dest(e.pos())
                self.session.move_gcp_dst(
                    self._press_gcp, (dest.x(), dest.y()))
            self._reset_pending()
            return

        # Add a GCP if a source was resolved (always set on press).
        if self._pending_src is not None:
            dest, _snapped = self._snapped_dest(e.pos())
            self.session.add_gcp(self._pending_src, (dest.x(), dest.y()))
        self._reset_pending()

    def _reset_pending(self):
        self._pending_src = None
        self._press_screen = None
        self._press_gcp = None
        self.hoverMarker.hide()

    def deactivate(self):
        self.rb_drag.reset(Qgis.GeometryType.Line)
        self.hoverMarker.hide()
        self.destMarker.hide()
        super().deactivate()

    def cleanup(self):
        self.canvas.scene().removeItem(self.rb_drag)
        self.canvas.scene().removeItem(self.hoverMarker)
        self.canvas.scene().removeItem(self.destMarker)
