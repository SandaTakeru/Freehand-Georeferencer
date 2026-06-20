# Freehand Georeferencer

An intuitive QGIS georeferencing (coordinate-correction) tool for **vector and
raster** layers. Add ground control points (GCPs) with the mouse only — grab a
point and release it at its correct position — preview the Helmert/Affine
transform live, then apply.

This plugin is the successor to **Freehand Vector Georeferencer** and is
published as its next version (it adds raster support; the vector workflow is
unchanged).

## Features

- **Vector**: press an old node and release at the new position. Sources snap to
  vertices of any visible vector layer (self-snap supported); destinations snap
  too. Scope = single feature / selected / whole layer. Apply = new memory
  layer, add to current layer, or in-place edit (undoable).
- **Raster**: press a recognizable point on the image and release at its correct
  map position (free-point source; a raster has no vertices). Live image-overlay
  preview. Apply writes a new georeferenced layer as a **VRT with an updated
  geotransform — no resampling, lossless**. Web/tile rasters (GSI maps, XYZ,
  WMS/WMTS) are excluded from the target list.
- Transforms: **Helmert** (fixed scale / with scale) and **Affine**.
- Per-GCP X/Y error, overall RMS, standard deviation and scale factor; residuals
  colored green→red; sortable list. Toggle/drag GCPs live.
- Save/load GCPs as CSV.

## Important: same-CRS workflow

This plugin applies a plain 2D affine transform and does **not** reproject. Keep
the **layer CRS equal to the project CRS** before georeferencing; a warning is
shown when they differ.

## Install

From the QGIS Plugin Manager (search "Freehand Georeferencer"), or manually copy
this folder into your QGIS `python/plugins` directory.

## Packaging for plugins.qgis.org

The official plugin id is `freehand_vector_georeferencer` (kept so existing users
get this as an update). When building the upload zip, the **top-level folder
inside the zip must be named `freehand_vector_georeferencer`**.

## Acknowledgment

The interactive "grab and move" UI is inspired by the *Freehand Raster
Georeferencer* plugin by Guilhem Vellut.

## License

See [LICENSE](LICENSE).
