# -*- coding: utf-8 -*-

# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    from .freehand_georeferencer import FreehandGeoreferencer
    return FreehandGeoreferencer(iface)
