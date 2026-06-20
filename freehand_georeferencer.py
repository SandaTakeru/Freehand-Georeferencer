# -*- coding: utf-8 -*-
'''Freehand Georeferencer main plugin.

An intuitive georeferencing tool that aligns vector or raster layers to known
points. Open the dock panel from the toolbar icon to operate it; the ▼ dropdown
next to the icon opens the how-to page (with the demo video).

Acknowledgment:
  The interactive UI of this plugin (the map tool, the live preview and the
  "grab a node and move it" feel) is inspired by the
  "Freehand Raster Georeferencer" plugin by Guilhem Vellut
  (https://github.com/gvellut/FreehandRasterGeoreferencer).
  Many thanks for that great earlier work.
'''

import os.path

from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QAction, QMenu, QToolButton


class FreehandGeoreferencer(object):
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.menu = '&Freehand Georeferencer'
        self.action = None
        self.help_action = None
        self.tool_button = None
        self.tool_menu = None
        self.toolbar = None
        self.dock = None
        # How-to page opened from the ▼ menu (the demo video plays on this README).
        self.help_url = ('https://github.com/SandaTakeru/'
                         'Freehand-Georeferencer/blob/main/README.md')

    def initGui(self):
        # Create the action and toolbar icon first; the dock is created lazily
        # so the toolbar icon never disappears even if dock creation fails.
        icon = QIcon(os.path.join(self.plugin_dir, 'icon.png'))
        self.action = QAction(icon, 'Freehand Georeferencer',
                              self.iface.mainWindow())
        self.action.setObjectName('FreehandGeoreferencer_Action')
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_dock)

        # ▼ menu: open the how-to page (video) in the browser.
        self.help_action = QAction('How to use (watch video)',
                                   self.iface.mainWindow())
        self.help_action.triggered.connect(self.open_help)

        self.tool_menu = QMenu(self.iface.mainWindow())
        self.tool_menu.addAction(self.help_action)

        # Clicking the icon toggles the dock; the ▼ on the right opens the menu
        # (same construction as the feature-selection tool).
        self.tool_button = QToolButton()
        self.tool_button.setDefaultAction(self.action)
        self.tool_button.setMenu(self.tool_menu)
        self.tool_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup)

        # Show the icon on a dedicated toolbar.
        self.toolbar = self.iface.addToolBar('Freehand Georeferencer')
        self.toolbar.setObjectName('FreehandGeoreferencerToolbar')
        self.toolbar.addWidget(self.tool_button)
        self.iface.addPluginToMenu(self.menu, self.action)
        self.iface.addPluginToMenu(self.menu, self.help_action)

    def open_help(self):
        QDesktopServices.openUrl(QUrl(self.help_url))

    def unload(self):
        if self.dock:
            self.dock._stop_session()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginMenu(self.menu, self.action)
        if self.help_action is not None:
            self.iface.removePluginMenu(self.menu, self.help_action)
        # Destroy the toolbar/action immediately so that on reload the
        # objectName is not duplicated (avoids the "duplicated widget not
        # cleaned up" warning).
        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            sip.delete(self.toolbar)
            self.toolbar = None
        # tool_button/tool_menu are destroyed with the toolbar; just drop refs.
        self.tool_button = None
        self.tool_menu = None
        if self.help_action is not None:
            sip.delete(self.help_action)
            self.help_action = None
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
