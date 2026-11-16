# Croquis on model

**Croquis on model** is a single-file IngeTrazo extension for drawing and
annotating directly over a 3D model.

Strokes are stored in the project in world coordinates. Each stroke is drawn
on the plane facing the camera at the point where it began, so the marks stay
with the model as you orbit the view.

<img width="461" height="447" alt="Croqui" src="https://github.com/user-attachments/assets/956efac5-ef54-4c38-8eca-92a17ec69c5c" />

## Why freehand drawing in a modeling environment?

Precise geometry is essential for building a model, but it is not always the
best medium for exploring an idea. A loose line, wash or quick annotation can
communicate movement, scale, emphasis and atmosphere before those impressions
are resolved into exact geometry.

Croquis on model brings that freer graphic language into the 3D workspace.
Because marks belong to the model rather than the screen, they remain in
context as the view changes. Use them to sketch alternatives, point out
relationships, suggest people or landscape, and communicate design intent
without interrupting the modeling process or pretending that an early idea
is already precise.

The plugin is a complement to modeling, not a replacement for it: keep exact
geometry for what must be measured, and use pen and brush when a more
expressive, exploratory mark says it better.

## Features

- **Pen** for freehand strokes, with adjustable colour, width and turbulence.
- **Brush** for soft, translucent marks with adjustable size, softness,
  profile, jitter and opacity.
- **Shapes** including lines, polylines, rectangles, ellipses, circles,
  three-point arcs, regular polygons and stars.
- **Snapping** to stroke ends, shape centres, a grid and 45-degree angles.
- **Layers** with visibility, locking, opacity and reordering.
- **Eraser** for whole strokes or partial stroke segments.
- **Selection and transforms** for moving, rotating, scaling and mirroring
  marks.
- **Export** to PNG or SVG, or copy the drawing to the clipboard.

## Requirements

- IngeTrazo with the extension API version 2.
- No additional Python packages; the extension uses the Qt modules supplied
  by IngeTrazo.

## Installation

1. In IngeTrazo, choose **Extensions → Open plugins folder**.
2. Copy `croquis_on_model.py` into the plugins folder.
3. Restart IngeTrazo. **Croquis on model** will be available from the
   Extensions menu and the side tray.

## Use

1. Choose **Pen**, **Brush**, or **Shapes** in the Croquis on model panel.
2. Draw in the viewport. For shapes, click to set points or drag to size;
   use **Shift** to constrain to 45 degrees.
3. Use the layer list to organize marks. The `.igz` project stores the
   annotations, and the project's undo/redo history includes drawing and
   erasing.
4. Choose **PNG…** or **SVG…** to export, or **Copy** to copy the drawing.

PNG export uses a 4× scale by default; adjust **PNG scale** from 1× to 4× in
the panel. Choose **Export over** to select a transparent, white, dark, or
model-and-annotations background. Viewport capture for the last option
depends on support in the current IngeTrazo build.

## Sheet Composer limitation

Croquis strokes are viewport overlays; they are **not automatically visible
in Sheet Composer layouts**. This plugin does not provide a direct **To
sheet** button, and automatic image insertion is not guaranteed because a
stable, documented image-insertion API is not available in every IngeTrazo
build.

For a reliable workflow, export with **PNG…**, then use the Sheet Composer's
**Image** tool to choose the saved PNG and place it on the sheet. With a
transparent background, the image contains only the Croquis annotations.
Choose **Model + annotations** if you want to include the current viewport
image and the host can capture it.

## License

GPL-3.0-or-later. See the SPDX identifier at the top of
`croquis_on_model.py`.
