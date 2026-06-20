# -*- coding: utf-8 -*-
'''Freehand Georeferencer main plugin.

An intuitive georeferencing tool that aligns vector or raster layers to known
points. Open the dock panel from the toolbar icon to operate it.

Acknowledgment:
  The interactive UI of this plugin (the map tool, the live preview and the
  "grab a node and move it" feel) is inspired by the
  "Freehand Raster Georeferencer" plugin by Guilhem Vellut
  (https://github.com/gvellut/FreehandRasterGeoreferencer).
  Many thanks for that great earlier work.
'''

import os.path

from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction


class FreehandGeoreferencer(object):
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.menu = '&Freehand Georeferencer'
        self.action = None
        self.toolbar = None
        self.dock = None

    def initGui(self):
        # Create the action and toolbar icon first; the dock is created lazily
        # so the toolbar icon never disappears even if dock creation fails.
        icon = QIcon(os.path.join(self.plugin_dir, 'icon.png'))
        self.action = QAction(icon, 'Freehand Georeferencer',
                              self.iface.mainWindow())
        self.action.setObjectName('FreehandGeoreferencer_Action')
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_dock)

        # Show the icon on a dedicated toolbar.
        self.toolbar = self.iface.addToolBar('Freehand Georeferencer')
        self.toolbar.setObjectName('FreehandGeoreferencerToolbar')
        self.toolbar.addAction(self.action)
        self.iface.addPluginToMenu(self.menu, self.action)

    def unload(self):
        if self.dock:
            self.dock._stop_session()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginMenu(self.menu, self.action)
        # Destroy the toolbar/action immediately so that on reload the
        # objectName is not duplicated (avoids the "duplicated widget not
        # cleaned up" warning).
        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            sip.delete(self.toolbar)
            self.toolbar = None
        if self.action is not None:
            sip.delete(self.action)
            self.action = None

    def _ensure_dock(self):
        # Create the dock on first use.
        if self.dock is None:
            from .georef_dockwidget import GeorefDockWidget
            self.dock = GeorefDockWidget(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(
                Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(self.action.setChecked)

    def toggle_dock(self, checked):
        if checked:
            self._ensure_dock()
            # Each time the icon opens the dock, default to the active layer.
            self.dock._preselect_active_layer()
            self.dock.show()
            self.dock.raise_()
        elif self.dock:
            self.dock.hide()
