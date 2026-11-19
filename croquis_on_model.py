# SPDX-License-Identifier: GPL-3.0-or-later
# Croquis on model — free-hand and geometric annotation over the model, as an
# IngeTrazo EXTENSION.
"""Croquis on model — a pen, a shapes tool and layers over the model.

The idea is Blender's Grease Pencil: draw with the mouse over the model,
keep the sketch in the document, and have it stay there. Two deliberate
differences, because IngeTrazo is a modeller and not a drawing program:

* **The strokes belong to the model, not to the screen.** Every point is
  stored in world metres, so a scribble made on a wall stays on the wall
  when you orbit, and is in the same place tomorrow.
* **A stroke lies in one plane**, the plane facing the camera through the
  point where the press landed. That is what ``drag_plane`` is for: the
  viewport keeps reading the cursor on that plane for the whole stroke, so
  the line does not wobble between a wall and the sky behind it.

What it gives you:

* **Croquis on model pen** — press, drag, release: one stroke, one undo step. The
  brush is drawn as one continuous, antialiased path (a Catmull-Rom curve
  through the samples), so it is smooth end to end:   no dashes, no clipping pattern, no seams where two segments meet.
* **Croquis on model brush** — a tool of its own, not a pen setting: a soft halo
  that fades to nothing, like a breath or a cloud of dust. No edge, no
  core line; the widest, faintest shell is the outer limit of the mark.
  A jitter control nudges each shell apart, so the mark reads as airbrush
  instead of a uniform band — and the wobble is fixed per stroke, so it
  never shimmers as you orbit.
* **Croquis on model shapes** — straight lines, polylines, rectangles, ellipses,
  circles, three-point arcs, regular polygons and stars, all on the plane
  you start them on, all with snapping (stroke ends, shape centres, a grid
  and 45° angle constraint with Shift). Drag to size, or click-click.
* **Open or filled** — every stroke can be filled. An open line that is
  filled is closed for the fill only: the outline stays open, as drawn.
* **Croquis on model eraser** — drag over strokes; they go on release, together.
* **Layers** — as many as you like, each with a name, visibility, lock and
  opacity, painted bottom to top. Named drawings appear beneath their layer
  in the panel tree, listed top to bottom and draggable above or below one
  another to change their stacking order. They can be selected there and have
  independent visibility toggles. New strokes land on the active layer and
  the eraser only touches that layer.
* **Export** — PNG (with a scale, transparent, white, dark or over the
  current view), SVG and the clipboard. A best-effort Sheet Composer bridge
  is included, but image insertion is not guaranteed by every IngeTrazo
  build. If the bridge is unavailable, export a PNG and add it with the
  Composer's Image tool.
* **Sheet visibility** — Croquis marks are viewport overlays; they do not
  automatically appear in Sheet Composer layouts.
* Everything lives in the ``.igz`` (the extension's document data), so it
  travels with the project and survives Save/Open, undo and redo.

Install: copy this file into the plugins folder (Extensions ▸ Open plugins
folder) and restart IngeTrazo — «Croquis on model» appears in the Extensions menu and
its tab in the side tray. The plugin API is 0.x: written against API 2.
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid

from PySide6.QtCore import (
    QItemSelectionModel,
    QPointF,
    QRectF,
    Qt,
    Signal,
    QTimer,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QImage,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
    QRadialGradient,
    QVector3D,
)
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tools.base import Tool, ToolContext

TAU = math.pi * 2.0

#: The name the panel and the menus use, in one place. The extension's
#: storage key stays «scribble», so drawings saved before the rename open.
TITLE = "Croquis on model"

#: Bumped when the document layout changes; version 1 kept a flat
#: ``strokes`` list and is migrated on read.
DOC_VERSION = 5

#: The pen's palette: the label the panel shows and the colour a stroke
#: keeps for good.
PALETTE = (
    ("Red", "#d1433a"),
    ("Crimson", "#a32b3a"),
    ("Orange", "#e0872e"),
    ("Amber", "#e6a817"),
    ("Yellow", "#dfc02c"),
    ("Lime", "#a8c93a"),
    ("Green", "#3fa560"),
    ("Forest", "#2f7a52"),
    ("Teal", "#2b9c8f"),
    ("Cyan", "#2aa7cf"),
    ("Sky", "#6bb6e8"),
    ("Blue", "#3f7fd1"),
    ("Navy", "#2b4c8c"),
    ("Violet", "#8a5bd1"),
    ("Magenta", "#c04f9c"),
    ("Pink", "#e07a9a"),
    ("Brown", "#8a5a3c"),
    ("Sand", "#c9a878"),
    ("Grey", "#6b7280"),
    ("Black", "#20232a"),
    ("White", "#f4f4f1"),
)

#: The shapes the shapes tool can draw, and the label the panel shows.
SHAPES = (
    ("line", "Line"),
    ("polyline", "Polyline"),
    ("rect", "Rectangle"),
    ("ellipse", "Ellipse"),
    ("circle", "Circle"),
    ("arc", "Arc (3 points)"),
    ("polygon", "Regular polygon"),
    ("star", "Star"),
)

#: The pen always smooths its samples this much: the value is kept per
#: stroke so old drawings render exactly as they were made.
PEN_SMOOTH = 2

#: How hard the pen rounds the free-hand samples. The number is the
#: Catmull-Rom tangent factor: 1/6 is the textbook curve.
SMOOTH_TENSION = {1: 0.10, 2: 1.0 / 6.0, 3: 0.30}
#: How far apart (in pixels) the brush keeps its samples. Smoothing a
#: stroke sampled every 2.5 px changes nothing visible; dropping samples is
#: what makes the level tell.
SMOOTH_SPACING = {1: 0.0, 2: 4.0, 3: 9.0}
#: Extra Chaikin passes, for the very smooth levels.
SMOOTH_CHAIKIN = {1: 0, 2: 0, 3: 1}

#: Grid the snaps can fall on, in metres (0 = off).
GRID_CHOICES = (
    ("Grid off", 0.0),
    ("1 cm", 0.01),
    ("5 cm", 0.05),
    ("10 cm", 0.1),
    ("50 cm", 0.5),
    ("1 m", 1.0),
)

#: How close (in pixels) the eraser has to come to a stroke to take it.
PICK_PX = 6.0
#: How close (in pixels) a snap candidate has to be to win.
SNAP_PX = 10.0
#: Points closer than this on screen are dropped: a 60 Hz mouse would
#: otherwise store a few hundred points per centimetre of line.
MIN_STEP_PX = 2.5
MAX_TRACE_SPACING_PX = 18.0
#: A single stroke never keeps more than this many points.
MAX_POINTS = 4000
#: Fill opacity when a stroke is filled.
FILL_ALPHA = 0.25

#: The pen, as the panel set it (this session's, not the document's).
_PEN = {
    "color": PALETTE[0][1],
    "width": 3,
    "fill": False,
    "fill_color": "",          # empty: the stroke's own colour
    "fill2": "",               # the far end of the fill gradient
    "fill_grad": "solid",
    "fill_pattern": "solid",
    "fill_alpha": FILL_ALPHA,
    "turbulence": 0.0,
}
#: The shapes tool's own settings.
_SHAPE = {"kind": "line", "sides": 6}
#: The brush: a soft halo, kept apart from the pen on purpose.
_BRUSH = {"color": "#3f7fd1", "width": 34.0, "soft": 0.75, "alpha": 0.55,
          "profile": "even", "jitter": 0.30,
          "color2": "", "grad": "off"}
#: Higher trace speed skips more small movements for straighter freehand marks.
_TRACE = {"speed": 0}

#: How a filled shape is coloured: one colour, or from one to another.
FILL_GRADIENTS = (
    ("Solid", "solid"),
    ("Linear", "linear"),
    ("Radial", "radial"),
)
#: How the halo is coloured: one colour, or from the middle outwards.
BRUSH_GRADIENTS = (
    ("Off", "off"),
    ("Outward", "out"),
)
FILL_GRADIENT_NAMES = frozenset(value for _label, value in FILL_GRADIENTS)
FILL_PATTERNS = (("Solid", "solid"), ("Diagonal hatch", "diagonal"),
                 ("Crosshatch", "crosshatch"))
FILL_PATTERN_NAMES = frozenset(value for _label, value in FILL_PATTERNS)
BRUSH_GRADIENT_NAMES = frozenset(value for _label, value in BRUSH_GRADIENTS)

#: How the halo falls away. The profile decides how much of the width stays
#: dense at full softness, how fast each shell outwards fades, and how many
#: shells it takes to get there.
PROFILES = (
    ("Concentrated", "tight"),
    ("Balanced", "even"),
    ("Diffuse", "soft"),
)
#: «Concentrated» keeps a narrow dense middle and dies quickly; «diffuse»
#: keeps most of the width dense and fades slowly. The core drop is how
#: much of the width softness takes away from the dense middle.
PROFILE_CORE_DROP = {"tight": 0.86, "even": 0.70, "soft": 0.35}
PROFILE_DECAY = {"tight": 0.48, "even": 0.58, "soft": 0.67}
#: Enough shells that the steps between them stop being visible: each one
#: is a ring of constant transparency, and too few read as banding.
PROFILE_STEPS = {"tight": 9, "even": 12, "soft": 16}
PROFILE_NAMES = frozenset(PROFILE_CORE_DROP)
#: Snapping.
_SNAP = {"grid": 0.0}
#: Whether the annotations are drawn at all.
_SHOW = {"visible": True}
#: Export defaults.
_EXPORT = {"scale": 4.0, "background": "transparent"}
#: Whether the last render got the viewport's own frame (for «Model +
#: annotations»); the export says so when it did not.
_LAST = {"frame": False}
#: Which layer new strokes land on (session state, keyed by layer id).
_ACTIVE = {"id": None}
#: Active drawing plane is session state; plane definitions live in the document.
_DRAWING_PLANE = {"scene": None, "id": None}

#: The sheet-composer bridge, once ``_register_sheet`` has run.
_SHEET = {"hooked": False, "method": "", "error": "", "api": ""}

#: The ExtensionApp, from setup(). The tools need it to write the document.
_APP = None

#: Parsed document, memoised per scene version: an overlay runs sixty times
#: a second and re-reading every point of every stroke each frame would be
#: the slowest thing in the plugin.
_CACHE: dict = {"key": None, "doc": None}


# ---------------------------------------------------------------------------
# The document: layers
# ---------------------------------------------------------------------------

def _raw(app, scene):
    """This extension's value in ``scene`` (read in place, never mutated)."""
    data = getattr(scene, "plugin_data", {}) or {}
    return data.get(getattr(app, "key", "scribble"))


def _series(value):
    return value if isinstance(value, (list, tuple)) else []


def _point(raw):
    """``(x, y, z)`` from anything that looks like a point, or ``None``."""
    try:
        return (float(raw[0]), float(raw[1]), float(raw[2]))
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _clean_stroke(raw) -> dict | None:
    """One stroke, or ``None`` when it is not a usable one."""
    if not isinstance(raw, dict):
        return None
    pts = []
    for p in _series(raw.get("p")):
        got = _point(p)
        if got is not None:
            pts.append(got)
    if len(pts) < 2:
        return None
    contours = []
    for raw_contour in _series(raw.get("contours")):
        contour = [point for point in (_point(p) for p in _series(raw_contour))
                   if point is not None]
        if len(contour) >= 3:
            contours.append(contour)
    normal = _point(raw.get("normal"))
    if normal is not None:
        length = math.sqrt(sum(value * value for value in normal))
        normal = (tuple(value / length for value in normal)
                  if length > 1e-9 else None)
    try:
        width = float(raw.get("w", _PEN["width"]))
    except (TypeError, ValueError):
        width = float(_PEN["width"])
    try:
        fill = float(raw.get("fill", 0.0))
    except (TypeError, ValueError):
        fill = 0.0
    try:
        smooth = int(raw.get("smooth", 0))
    except (TypeError, ValueError):
        smooth = 0
    try:
        soft = float(raw.get("soft", 0.0))
    except (TypeError, ValueError):
        soft = 0.0
    try:
        jitter = float(raw.get("jt", 0.0))
    except (TypeError, ValueError):
        jitter = 0.0
    try:
        opacity = float(raw.get("a", 0.95))
    except (TypeError, ValueError):
        opacity = 0.95
    profile = str(raw.get("pf") or "even")
    if profile not in PROFILE_NAMES:
        profile = "even"
    fill_grad = str(raw.get("fg") or "solid")
    if fill_grad not in FILL_GRADIENT_NAMES:
        fill_grad = "solid"
    fill_pattern = str(raw.get("fp") or "solid")
    if fill_pattern not in FILL_PATTERN_NAMES:
        fill_pattern = "solid"
    brush_grad = str(raw.get("bg") or "off")
    if brush_grad not in BRUSH_GRADIENT_NAMES:
        brush_grad = "off"
    return {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or "")[:60],
        "visible": bool(raw.get("visible", True)),
        "c": str(raw.get("c") or _PEN["color"]),
        "w": max(0.5, min(40.0, width)),
        "p": pts,
        "contours": contours,
        "normal": normal,
        "kind": str(raw.get("kind") or "free"),
        "fc": str(raw.get("fc") or ""),
        "fc2": str(raw.get("fc2") or ""),
        "fg": fill_grad,
        "fp": fill_pattern,
        "c2": str(raw.get("c2") or ""),
        "bg": brush_grad,
        "fill": max(0.0, min(1.0, fill)),
        "closed": bool(raw.get("closed", False)),
        "smooth": max(0, min(3, smooth)),
        "soft": max(0.0, min(1.0, soft)),
        "a": max(0.05, min(1.0, opacity)),
        "pf": profile,
        "jt": max(0.0, min(1.0, jitter)),
    }


def new_layer(name: str = "Layer") -> dict:
    """A fresh, empty layer."""
    return {
        "id": uuid.uuid4().hex[:8],
        "name": str(name),
        "visible": True,
        "locked": False,
        "opacity": 1.0,
        "strokes": [],
    }


def _name_stroke(layer: dict, stroke: dict) -> None:
    """Give a drawing a stable, readable name within its layer."""
    if str(stroke.get("name") or "").strip():
        return
    labels = {
        "free": "Pen stroke",
        "brush": "Brush stroke",
        "line": "Line",
        "polyline": "Polyline",
        "rect": "Rectangle",
        "ellipse": "Ellipse",
        "circle": "Circle",
        "arc": "Arc",
        "polygon": "Polygon",
        "star": "Star",
        "boolean": "Combined shape",
        "partial": "Partial stroke",
    }
    kind = str(stroke.get("kind") or "free")
    base = labels.get(kind, kind.replace("_", " ").title() or "Stroke")
    prefix = f"{base} "
    highest = 0
    for existing in layer["strokes"]:
        name = str(existing.get("name") or "")
        if name.startswith(prefix):
            try:
                highest = max(highest, int(name[len(prefix):]))
            except ValueError:
                continue
    stroke["name"] = f"{base} {highest + 1}"


def _clean_layer(raw, index: int) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        opacity = float(raw.get("opacity", 1.0))
    except (TypeError, ValueError):
        opacity = 1.0
    strokes = []
    for st in _series(raw.get("strokes")):
        clean = _clean_stroke(st)
        if clean is not None:
            _name_stroke({"strokes": strokes}, clean)
            strokes.append(clean)
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:8]),
        "name": str(raw.get("name") or f"Layer {index + 1}"),
        "visible": bool(raw.get("visible", True)),
        "locked": bool(raw.get("locked", False)),
        "opacity": max(0.0, min(1.0, opacity)),
        "strokes": strokes,
    }


def _clean_plane(raw, index: int) -> dict | None:
    """A named drawing plane with a normalized 3D normal."""
    if not isinstance(raw, dict):
        return None
    origin = _point(raw.get("origin"))
    normal = _point(raw.get("normal"))
    if origin is None or normal is None:
        return None
    length = math.sqrt(sum(value * value for value in normal))
    if length < 1e-9:
        normal = (0.0, 0.0, 1.0)
    else:
        normal = tuple(value / length for value in normal)
    raw_size = raw.get("size")
    try:
        size = None if raw_size is None else max(0.1, float(raw_size))
    except (TypeError, ValueError):
        size = None
    return {"id": str(raw.get("id") or uuid.uuid4().hex[:8]),
            "name": str(raw.get("name") or f"Plane {index + 1}")[:60],
            "origin": origin, "normal": normal, "size": size,
            "visible": bool(raw.get("visible", True))}


def active_drawing_plane(app, scene=None):
    """The activated saved plane for this scene, or None for auto depth."""
    if app is None:
        return None
    scene = scene or getattr(app, "scene", None)
    if scene is None or _DRAWING_PLANE["scene"] != id(scene):
        return None
    ident = _DRAWING_PLANE.get("id")
    if not ident:
        return None
    return next((plane for plane in document(app, scene)["planes"]
                 if plane["id"] == ident), None)


def _clean_doc(raw) -> dict:
    """The document's layers and planes, migrating flat version-1 strokes."""
    layers = []
    planes = []
    if isinstance(raw, dict):
        for i, plane in enumerate(_series(raw.get("planes"))):
            clean = _clean_plane(plane, i)
            if clean is not None:
                planes.append(clean)
        for i, lay in enumerate(_series(raw.get("layers"))):
            layer = _clean_layer(lay, i)
            if layer is not None:
                layers.append(layer)
        if not layers:
            # version 1: one flat list, every stroke on a single layer.
            old = [st for st in (_clean_stroke(s)
                                 for s in _series(raw.get("strokes"))) if st]
            if old:
                layer = new_layer("Annotations")
                for stroke in old:
                    _name_stroke(layer, stroke)
                    layer["strokes"].append(stroke)
                layers.append(layer)
    if not layers:
        layers = [new_layer("Annotations")]
    return {"layers": layers, "planes": planes}


def document(app, scene=None) -> dict:
    """The parsed document (memoised on the scene's version)."""
    if app is None:
        return _clean_doc(None)
    if scene is None:
        scene = getattr(app, "scene", None)
    if scene is None:
        return _clean_doc(None)
    key = (id(scene), getattr(scene, "version", 0))
    if _CACHE["key"] == key and _CACHE["doc"] is not None:
        return _CACHE["doc"]
    doc = _clean_doc(_raw(app, scene))
    _CACHE["key"] = key
    _CACHE["doc"] = doc
    return doc


def _dump(doc) -> dict:
    """The document as plain, JSON-friendly data for ``set_document_data``."""
    layers = []
    for lay in doc.get("layers", []):
        layers.append({
            "id": lay["id"],
            "name": lay["name"],
            "visible": bool(lay["visible"]),
            "locked": bool(lay["locked"]),
            "opacity": round(float(lay["opacity"]), 3),
            "strokes": [{
                "id": s["id"],
                "name": s["name"],
                "visible": bool(s.get("visible", True)),
                "c": s["c"],
                "fc": str(s.get("fc") or ""),
                "fc2": str(s.get("fc2") or ""),
                "fg": str(s.get("fg") or "solid"),
                "fp": str(s.get("fp") or "solid"),
                "normal": (None if s.get("normal") is None
                           else [round(value, 6)
                                 for value in s["normal"]]),
                "contours": [[[round(x, 4), round(y, 4), round(z, 4)]
                               for x, y, z in contour]
                              for contour in (s.get("contours") or [])],
                "c2": str(s.get("c2") or ""),
                "bg": str(s.get("bg") or "off"),
                "w": round(float(s["w"]), 2),
                "kind": s["kind"],
                "fill": round(float(s["fill"]), 3),
                "closed": bool(s["closed"]),
                "smooth": int(s["smooth"]),
                "soft": round(float(s.get("soft") or 0.0), 3),
                "a": round(float(s.get("a") or 1.0), 3),
                "pf": str(s.get("pf") or "even"),
                "jt": round(float(s.get("jt") or 0.0), 3),
                "p": [[round(x, 4), round(y, 4), round(z, 4)]
                      for x, y, z in s["p"]],
            } for s in lay["strokes"]],
        })
    planes = [{"id": plane["id"], "name": plane["name"],
               "origin": [round(value, 4) for value in plane["origin"]],
               "normal": [round(value, 6) for value in plane["normal"]],
               "size": (None if plane.get("size") is None
                        else round(float(plane["size"]), 3)),
               "visible": bool(plane["visible"])}
              for plane in doc.get("planes", [])]
    return {"version": DOC_VERSION, "layers": layers, "planes": planes}


def _store(app, doc) -> None:
    """Replace the document — ONE undo step (set_document_data)."""
    if app is None:
        return
    app.set_document_data(_dump(doc))
    _CACHE["key"] = None            # the version may not have moved yet


def active_layer(doc) -> dict:
    """The layer new strokes land on, resolved against the document."""
    layers = doc.setdefault("layers", [])
    if not layers:
        layer = new_layer("Annotations")
        layers.append(layer)
        _ACTIVE["id"] = layer["id"]
        return layer
    for lay in layers:
        if lay["id"] == _ACTIVE["id"]:
            return lay
    _ACTIVE["id"] = layers[-1]["id"]
    return layers[-1]


def writable_layer(app, scene) -> dict | None:
    """The active layer, or ``None`` when it is hidden or locked."""
    layer = active_layer(document(app, scene))
    if layer["locked"] or not layer["visible"]:
        return None
    return layer


def all_strokes(app, scene=None) -> list:
    """Every visible stroke, bottom to top (for snapping)."""
    out = []
    for lay in document(app, scene)["layers"]:
        if lay["visible"]:
            out.extend(_visible_strokes(lay))
    return out


def _visible_strokes(layer: dict) -> list:
    """The drawings enabled individually within a layer."""
    return [stroke for stroke in layer["strokes"]
            if stroke.get("visible", True)]


def strokes_of(app, scene=None) -> list:
    """Compatibility helper: every visible stroke of the document."""
    return all_strokes(app, scene)


def total_strokes(app, scene=None) -> int:
    return sum(len(lay["strokes"]) for lay in document(app, scene)["layers"])


def _trace_spacing_px() -> float:
    speed = max(0.0, min(100.0, float(_TRACE["speed"]))) / 100.0
    return MIN_STEP_PX + (MAX_TRACE_SPACING_PX - MIN_STEP_PX) * speed


def _append_stroke(app, scene, stroke) -> bool:
    """Add ``stroke`` to the active layer and commit it, or say why not."""
    doc = document(app, scene)
    layer = active_layer(doc)
    if layer["locked"] or not layer["visible"]:
        return False
    _name_stroke(layer, stroke)
    layer["strokes"].append(stroke)
    _store(app, doc)
    return True


# ---------------------------------------------------------------------------
# Presets: the pen's settings, kept between sessions
# ---------------------------------------------------------------------------

#: Loaded once, then shared by the panel and the pen.
_PRESETS: dict = {"items": [], "loaded": False}


def _preset_path() -> str:
    """Where the presets live: the user's config folder, not the project.

    A preset is a way of working, not part of a drawing, so it follows the
    user from document to document instead of travelling in the ``.igz``.
    """
    try:
        from PySide6.QtCore import QStandardPaths

        folder = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppConfigLocation)
    except Exception:  # noqa: BLE001 — no such enum in this PySide6
        folder = ""
    if not folder:
        folder = os.path.join(os.path.expanduser("~"), ".config", "ingetrazo")
    return os.path.join(folder, "scribble-presets.json")


def _clean_preset(raw) -> dict | None:
    """One preset, or ``None`` when it has nothing usable in it."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    try:
        width = float(raw.get("width", _PEN["width"]))
    except (TypeError, ValueError):
        width = float(_PEN["width"])
    try:
        alpha = float(raw.get("fill_alpha", FILL_ALPHA))
    except (TypeError, ValueError):
        alpha = FILL_ALPHA
    try:
        turbulence = float(raw.get("turbulence", 0.0))
    except (TypeError, ValueError):
        turbulence = 0.0
    return {
        "name": name[:60],
        "color": str(raw.get("color") or _PEN["color"]),
        "width": max(1.0, min(40.0, width)),
        "fill": bool(raw.get("fill", False)),
        "fill_color": str(raw.get("fill_color") or ""),
        "fill2": str(raw.get("fill2") or ""),
        "fill_grad": (str(raw.get("fill_grad") or "solid")
                      if str(raw.get("fill_grad") or "solid")
                      in FILL_GRADIENT_NAMES else "solid"),
        "fill_pattern": (str(raw.get("fill_pattern") or "solid")
                         if str(raw.get("fill_pattern") or "solid")
                         in FILL_PATTERN_NAMES else "solid"),
        "fill_alpha": max(0.05, min(1.0, alpha)),
        "turbulence": max(0.0, min(1.0, turbulence)),
    }


def load_presets() -> list:
    """The presets on disk, or an empty list — a bad file never raises."""
    try:
        with open(_preset_path(), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return []
    entries = raw.get("presets") if isinstance(raw, dict) else raw
    out = []
    for item in _series(entries):
        preset = _clean_preset(item)
        if preset is not None:
            out.append(preset)
    return out


def save_presets(items) -> str:
    """Write the presets; ``""`` on success, or why it could not."""
    path = _preset_path()
    try:
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "presets": list(items)}, handle,
                      indent=2)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    _PRESETS["items"] = list(items)
    _PRESETS["loaded"] = True
    return ""


def presets() -> list:
    """Every preset, read from disk on first use."""
    if not _PRESETS["loaded"]:
        _PRESETS["items"] = load_presets()
        _PRESETS["loaded"] = True
    return _PRESETS["items"]


def current_preset(name: str) -> dict:
    """The pen's settings right now, as a preset called ``name``."""
    return {
        "name": str(name)[:60],
        "color": _PEN["color"],
        "width": float(_PEN["width"]),
        "fill": bool(_PEN["fill"]),
        "fill_color": str(_PEN["fill_color"] or ""),
        "fill2": str(_PEN["fill2"] or ""),
        "fill_grad": str(_PEN["fill_grad"] or "solid"),
        "fill_pattern": str(_PEN["fill_pattern"] or "solid"),
        "fill_alpha": float(_PEN["fill_alpha"]),
        "turbulence": float(_PEN["turbulence"]),
    }


def apply_preset(preset) -> None:
    """Put a preset in the hand."""
    _PEN["color"] = str(preset.get("color") or _PEN["color"])
    try:
        _PEN["width"] = max(1.0, min(40.0, float(preset.get("width", 3))))
    except (TypeError, ValueError):
        pass
    _PEN["fill"] = bool(preset.get("fill", False))
    _PEN["fill_color"] = str(preset.get("fill_color") or "")
    _PEN["fill2"] = str(preset.get("fill2") or "")
    _PEN["fill_grad"] = (str(preset.get("fill_grad") or "solid")
                          if str(preset.get("fill_grad") or "solid")
                          in FILL_GRADIENT_NAMES else "solid")
    _PEN["fill_pattern"] = (str(preset.get("fill_pattern") or "solid")
                             if str(preset.get("fill_pattern") or "solid")
                             in FILL_PATTERN_NAMES else "solid")
    try:
        _PEN["turbulence"] = max(0.0, min(1.0,
                                         float(preset.get("turbulence", 0.0))))
    except (TypeError, ValueError):
        pass
    try:
        _PEN["fill_alpha"] = max(0.05, min(1.0,
                                           float(preset.get("fill_alpha",
                                                           FILL_ALPHA))))
    except (TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Projection and painting
# ---------------------------------------------------------------------------

def _project(viewport, points):
    """``[(px, py, in_front), ...]`` for world points, in widget pixels."""
    try:
        import numpy as np

        arr = np.asarray(points, dtype=float).reshape(-1, 3)
        px, py, front = viewport.world_to_pixels(arr)
        return [(float(x), float(y), bool(f)) for x, y, f in zip(px, py, front)]
    except Exception:  # noqa: BLE001 — the single-point path never dies
        out = []
        for p in points:
            got = viewport._world_to_pixel(QVector3D(*p))
            if got is None:
                out.append((0.0, 0.0, False))
            else:
                out.append((float(got[0]), float(got[1]), True))
        return out


def _runs(pts):
    """Split projected points into runs that are all in front of the camera."""
    runs, run = [], []
    for p in pts:
        if p[2]:
            run.append(p)
        else:
            if run:
                runs.append(run)
            run = []
    if run:
        runs.append(run)
    return runs


def _chaikin(pts, closed=False):
    """One Chaikin step: the cheap way to take the corners out."""
    if len(pts) < 3:
        return list(pts)
    out = []
    count = len(pts) if closed else len(pts) - 1
    if not closed:
        out.append(pts[0])
    for i in range(count):
        p = pts[i]
        q = pts[(i + 1) % len(pts)]
        out.append((p[0] * 0.75 + q[0] * 0.25, p[1] * 0.75 + q[1] * 0.25, p[2]))
        out.append((p[0] * 0.25 + q[0] * 0.75, p[1] * 0.25 + q[1] * 0.75, p[2]))
    if not closed:
        out.append(pts[-1])
    return out


def _decimate(pts, spacing: float):
    """Drop the samples a smoother brush does not need.

    Keeping the first and the last point exactly: the stroke has to start
    and end where the hand did, however much is thinned in between.
    """
    if spacing <= 0 or len(pts) < 3:
        return list(pts)
    out = [pts[0]]
    for p in pts[1:-1]:
        last = out[-1]
        if math.hypot(p[0] - last[0], p[1] - last[1]) >= spacing:
            out.append(p)
    last = out[-1]
    if math.hypot(pts[-1][0] - last[0], pts[-1][1] - last[1]) > 1e-6:
        out.append(pts[-1])
    return out


def _smooth_path(pts, closed: bool, smooth: int) -> QPainterPath:
    """A single continuous path through ``pts``.

    Everything Croquis on model paints goes through here, which is what makes the
    brush seamless: one path per run, round caps and joins, antialiased —
    no per-segment lines and no clip pattern to tile.
    """
    path = QPainterPath()
    if not pts:
        return path
    if smooth > 0 and len(pts) >= 3:
        pts = _decimate(pts, SMOOTH_SPACING.get(smooth, 0.0))
        for _ in range(SMOOTH_CHAIKIN.get(smooth, 0)):
            pts = _chaikin(pts, closed)
    n = len(pts)
    if smooth <= 0 or n < 3:
        path.moveTo(float(pts[0][0]), float(pts[0][1]))
        for p in pts[1:]:
            path.lineTo(float(p[0]), float(p[1]))
        if closed and n > 2:
            path.closeSubpath()
        return path
    k = SMOOTH_TENSION.get(smooth, 1.0 / 6.0)
    path.moveTo(float(pts[0][0]), float(pts[0][1]))
    last = n if closed else n - 1
    for i in range(last):
        p0 = pts[(i - 1) % n] if closed else pts[max(i - 1, 0)]
        p1 = pts[i % n]
        p2 = pts[(i + 1) % n]
        p3 = pts[(i + 2) % n] if closed else pts[min(i + 2, n - 1)]
        c1x = p1[0] + (p2[0] - p0[0]) * k
        c1y = p1[1] + (p2[1] - p0[1]) * k
        c2x = p2[0] - (p3[0] - p1[0]) * k
        c2y = p2[1] - (p3[1] - p1[1]) * k
        path.cubicTo(float(c1x), float(c1y), float(c2x), float(c2y),
                     float(p2[0]), float(p2[1]))
    if closed:
        path.closeSubpath()
    return path


def _has_stroke_color(stroke) -> bool:
    """Whether this annotation should draw its outline."""
    return str(stroke.get("c") or _PEN["color"]).strip().lower() != "none"


def _stroke_color(stroke) -> QColor:
    color = QColor(str(stroke.get("c") or _PEN["color"]))
    if not color.isValid():
        color = QColor(_PEN["color"])
    if not color.isValid():
        color = QColor(PALETTE[0][1])
    return color


def _mix_colors(first: QColor, second: QColor, t: float) -> QColor:
    """A colour ``t`` of the way from ``first`` to ``second``."""
    t = max(0.0, min(1.0, t))
    return QColor(int(round(first.red() * (1 - t) + second.red() * t)),
                  int(round(first.green() * (1 - t) + second.green() * t)),
                  int(round(first.blue() * (1 - t) + second.blue() * t)))


def _shell_color(stroke, base, shell: int, total: int) -> QColor:
    """A shell's colour: the brush colour, or a blend towards the second."""
    first = QColor(base)
    second = str(stroke.get("c2") or "")
    if str(stroke.get("bg") or "off") != "out" or not second or total < 2:
        return first
    other = QColor(second)
    if not other.isValid():
        return first
    return _mix_colors(first, other, shell / (total - 1.0))


def _fill_brush(stroke, pts, alpha: float, color) -> QBrush:
    """The fill: one colour, or the gradient the stroke asks for."""
    first = QColor(color)
    first.setAlphaF(max(0.0, min(1.0, alpha)))
    second = str(stroke.get("fc2") or "")
    mode = str(stroke.get("fg") or "solid")
    if mode not in FILL_GRADIENT_NAMES or mode == "solid" or not second \
            or len(pts) < 3:
        return QBrush(first, Qt.SolidPattern)
    other = QColor(second)
    if not other.isValid():
        return QBrush(first, Qt.SolidPattern)
    other.setAlphaF(max(0.0, min(1.0, alpha)))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if mode == "radial":
        gradient = QRadialGradient(
            QPointF((x0 + x1) / 2.0, (y0 + y1) / 2.0),
            max(1.0, math.hypot(x1 - x0, y1 - y0) / 2.0))
    else:
        gradient = QLinearGradient(QPointF(x0, y0), QPointF(x1, y1))
    gradient.setColorAt(0.0, first)
    gradient.setColorAt(1.0, other)
    return QBrush(gradient)


def _stroke_alpha(stroke, alpha: float = 1.0) -> float:
    """The stroke's own opacity times the layer's."""
    try:
        value = float(stroke.get("a", 0.95))
    except (TypeError, ValueError):
        value = 0.95
    return max(0.0, min(1.0, value * alpha))


def _soft_shells(width: float, soft: float, alpha: float,
                 profile: str = "even"):
    """``[(width_px, alpha), ...]`` from the dense middle out to the haze.

    A halo is the same path drawn a few times: the innermost shell is the
    narrowest and densest, each one outwards is wider and fainter. The
    profile settles the falloff — «tight» keeps a bright middle and dies
    quickly, «soft» spreads the whole width thin.

    Shared by the viewport and the SVG export so the two agree.
    """
    name = profile if profile in PROFILE_NAMES else "even"
    steps = PROFILE_STEPS[name]
    soft = max(0.0, min(1.0, soft))
    core = width * (1.0 - PROFILE_CORE_DROP[name] * soft)
    decay = PROFILE_DECAY[name]
    fractions = [i / (steps - 1.0) for i in range(steps)]
    alphas = _normalise_centre([alpha * (decay ** i) for i in range(steps)],
                               alpha)
    return [(core + (width - core) * t, a)
            for t, a in zip(fractions, alphas)]


def _normalise_centre(alphas, target: float):
    """Scale the shells so the middle of the mark keeps ``target`` density.

    Without this a profile with more shells would also be a denser brush,
    and choosing «diffuse» would darken the mark instead of only spreading
    it. The middle is where every shell overlaps, so it is the one place
    the accumulated density can be solved for.
    """
    if not alphas or target <= 0.004:
        return alphas
    low, high = 0.05, 4.0
    for _ in range(18):
        middle = (low + high) / 2.0
        kept = 1.0
        for value in alphas:
            kept *= 1.0 - min(1.0, value * middle)
        if 1.0 - kept < target:
            low = middle
        else:
            high = middle
    scale = (low + high) / 2.0
    return [min(1.0, value * scale) for value in alphas]


#: Specks, memoised per seed: a stroke's dust must not shimmer every frame.
_GRAIN: dict = {"key": None, "dots": []}
#: The wobble of each shell, memoised: a stroke keeps the same grain.
_JITTER: dict = {}


def _hash_text(text) -> int:
    """A stable 32-bit number for a string."""
    state = 0
    for char in str(text):
        state = (state * 131 + ord(char)) & 0x7FFFFFFF
    return state


def _noise(seed: int, a: int, b: int) -> float:
    """A stable number in -1..1 for the triplet ``(seed, a, b)``."""
    value = (seed * 73856093) ^ (a * 19349663) ^ (b * 83492791)
    value &= 0x7FFFFFFF
    value ^= value >> 13
    value = (value * 1274126177) & 0x7FFFFFFF
    value ^= value >> 11
    return ((value % 20001) / 10000.0) - 1.0


def _jitter_amount(stroke, width: float) -> float:
    """How far, in pixels, a shell may wander from the path."""
    try:
        amount = float(stroke.get("jt") or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    return max(0.0, min(1.0, amount)) * width * 0.22


#: Samples between two anchors of the wobble. A per-sample random offset
#: tears the edge into spikes; a value every few samples, interpolated,
#: gives the slow wander an airbrush has.
JITTER_SPAN = 8


def _jitter_noise(seed: int, shell: int, count: int):
    """``[(nx, ny), ...]`` in -1..1 for one shell, cached.

    The offsets are anchored every ``JITTER_SPAN`` samples and blended in
    between, so the shell wanders instead of crackling.
    """
    key = (seed, shell, count)
    got = _JITTER.get(key)
    if got is not None:
        return got
    if len(_JITTER) > 96:            # a handful of strokes' worth is plenty
        _JITTER.clear()
    span = JITTER_SPAN
    anchors = [(i, _noise(seed, shell, i),
                _noise(seed, shell, i + 104729))
               for i in range(0, count + span, span)]
    got = []
    for index in range(count):
        slot = index // span
        t = (index - slot * span) / float(span)
        _i0, x0, y0 = anchors[slot]
        _i1, x1, y1 = anchors[min(slot + 1, len(anchors) - 1)]
        got.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    _JITTER[key] = got
    return got


def _jitter(pts, amount: float, seed: int, shell: int):
    """One shell's points, nudged: the airbrush wobble of the mark.

    The offsets come from the stroke's own seed, so they are the same on
    every frame — the grain belongs to the stroke, not to the moment.
    """
    if amount <= 0.05 or not pts:
        return pts
    noise = _jitter_noise(seed, shell, len(pts))
    return [(x + nx * amount, y + ny * amount, front)
            for (x, y, front), (nx, ny) in zip(pts, noise)]


def _pen_wobble_amount(stroke, width: float) -> float:
    """Maximum screen-space amplitude for the freehand pen's slack line."""
    try:
        level = max(0.0, min(1.0, float(stroke.get("jt") or 0.0)))
    except (TypeError, ValueError):
        level = 0.0
    return level * min(10.0, max(0.8, width * 1.25))


def _pen_wobble(pts, amount: float, seed: int):
    """Add slow, deterministic, tangent-normal waves without sharp jitter."""
    if amount <= 0.05 or len(pts) < 3:
        return pts
    distances = [0.0]
    for first, second in zip(pts, pts[1:]):
        distances.append(distances[-1] + math.hypot(second[0] - first[0],
                                                   second[1] - first[1]))
    total = distances[-1]
    if total <= 1e-6:
        return pts
    phase_a = (seed % 65521) / 65521.0 * TAU
    phase_b = ((seed * 7919) % 65519) / 65519.0 * TAU
    out = []
    for index, (x, y, front) in enumerate(pts):
        prev = pts[max(0, index - 1)]
        nxt = pts[min(len(pts) - 1, index + 1)]
        dx, dy = nxt[0] - prev[0], nxt[1] - prev[1]
        length = math.hypot(dx, dy)
        if length <= 1e-8:
            out.append((x, y, front))
            continue
        nx, ny = -dy / length, dx / length
        distance = distances[index]
        envelope = min(1.0, distance / 20.0, (total - distance) / 20.0)
        wave = (0.72 * math.sin(distance * TAU / 86.0 + phase_a)
                + 0.28 * math.sin(distance * TAU / 47.0 + phase_b))
        offset = amount * max(0.0, envelope) * wave
        out.append((x + nx * offset, y + ny * offset, front))
    return out


def _grain_dots(seed: str, count: int):
    """Stable ``[(dx, dy, faint), ...]`` specks for a stroke."""
    key = (seed, count)
    if _GRAIN["key"] == key:
        return _GRAIN["dots"]
    state = _hash_text(seed)
    dots = []
    for _ in range(count):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        radius = 0.35 + (state % 1000) / 1000.0 * 0.65
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        angle = (state % 62832) / 10000.0
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        faint = 0.25 + (state % 1000) / 1000.0 * 0.55
        dots.append((math.cos(angle) * radius, math.sin(angle) * radius,
                     faint))
    _GRAIN["key"], _GRAIN["dots"] = key, dots
    return dots


def _paint_grain(painter, pts, color, width: float, alpha: float,
                 seed: str) -> None:
    """A few faint specks around the halo — the «dust» of the brush."""
    if width < 8.0 or alpha <= 0.02 or len(pts) < 2:
        return
    dots = _grain_dots(seed, 24)
    if not dots:
        return
    radius = max(0.7, width * 0.022)
    painter.setPen(Qt.NoPen)
    for index in range(0, len(pts), 4):
        x, y, front = pts[index]
        if not front:
            continue
        dx, dy, faint = dots[(index // 4) % len(dots)]
        shade = QColor(color)
        shade.setAlphaF(max(0.0, min(1.0, alpha * 0.22 * faint)))
        painter.setBrush(QBrush(shade, Qt.SolidPattern))
        painter.drawEllipse(QPointF(x + dx * width * 0.42,
                                    y + dy * width * 0.42), radius, radius)
    painter.setBrush(Qt.NoBrush)


def _paint_soft(painter, pts, stroke, color, width: float,
                alpha: float) -> None:
    """A halo instead of a line: no edge, no core, only a fade."""
    runs = _runs(pts)
    if not runs:
        return
    soft = max(0.0, min(1.0, float(stroke.get("soft") or 0.0)))
    base = _stroke_alpha(stroke, alpha)
    smooth = int(stroke.get("smooth") or 2)
    profile = str(stroke.get("pf") or "even")
    seed = _hash_text(stroke.get("id") or "draft")
    amount = _jitter_amount(stroke, width)
    shells = _soft_shells(width, soft, base, profile)
    for shell, (shell_width, shell_alpha) in enumerate(shells):
        if shell_alpha <= 0.004:
            continue
        wobble = _jitter(pts, amount, seed, shell)
        shade = _shell_color(stroke, color, shell, len(shells))
        shade.setAlphaF(max(0.0, min(1.0, shell_alpha)))
        pen = QPen(shade, max(0.5, shell_width))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setStyle(Qt.SolidLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        radius = max(0.5, shell_width / 2.0)
        for run in _runs(wobble):
            if len(run) == 1:
                painter.drawEllipse(QPointF(run[0][0], run[0][1]), radius,
                                    radius)
                continue
            painter.drawPath(_smooth_path(run, False, smooth))
    if soft >= 0.6:
        _paint_grain(painter, pts, color, width, base,
                     str(stroke.get("id") or "draft"))


def _stroke_world_contours(stroke):
    """Every world-space contour, including the holes of a boolean result."""
    contours = stroke.get("contours") or []
    return contours if contours else [stroke.get("p", [])]


def _compound_path(projected_contours, smooth: int = 0) -> QPainterPath:
    """Closed path for all visible contours, using even-odd holes."""
    path = QPainterPath()
    for contour in projected_contours:
        if len(contour) >= 3 and all(point[2] for point in contour):
            path.addPath(_smooth_path(contour, True, smooth))
    path.setFillRule(Qt.FillRule.OddEvenFill)
    return path


def _paint_hatch(painter, path, color, alpha: float, pattern: str) -> None:
    """Paint transparent diagonal hatch lines clipped to a closed path."""
    if pattern not in ("diagonal", "crosshatch") or path.isEmpty():
        return
    shade = QColor(color)
    shade.setAlphaF(max(0.0, min(1.0, alpha)))
    area = path.boundingRect()
    spacing = 8.0
    painter.save()
    painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
    painter.setPen(QPen(shade, 1.0))
    painter.setBrush(Qt.NoBrush)
    start = int(math.floor((area.left() - area.height()) / spacing) * spacing)
    stop = int(math.ceil((area.right() + area.height()) / spacing) * spacing)
    for x in range(start, stop + 1, int(spacing)):
        painter.drawLine(QPointF(x, area.bottom()),
                         QPointF(x + area.height(), area.top()))
        if pattern == "crosshatch":
            painter.drawLine(QPointF(x, area.top()),
                             QPointF(x + area.height(), area.bottom()))
    painter.restore()


def _paint_stroke(painter, pts, stroke, scale: float = 1.0,
                  alpha: float = 1.0, extra_contours=None) -> None:
    """Draw one stroke, optionally with multiple boolean-result contours."""
    if not pts:
        return
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    projected = [pts] + list(extra_contours or [])
    color = _stroke_color(stroke)
    width = max(0.5, float(stroke.get("w") or 3.0) * scale)
    smooth = int(stroke.get("smooth") or 0)

    if float(stroke.get("soft") or 0.0) > 0.01:
        _paint_soft(painter, pts, stroke, color, width, alpha)
        return

    if str(stroke.get("kind") or "") == "free":
        amount = _pen_wobble_amount(stroke, width)
        if amount > 0.05:
            pts = _pen_wobble(pts, amount,
                              _hash_text(stroke.get("id") or "draft"))
            projected[0] = pts

    fill = float(stroke.get("fill") or 0.0)
    wanted = str(stroke.get("fc") or "")
    explicit_fill = QColor(wanted).isValid() if wanted else False
    flat = [point for contour in projected for point in contour]
    if fill > 0.01 and len(flat) >= 3 and             (_has_stroke_color(stroke) or explicit_fill):
        path = _compound_path(projected, smooth)
        if not path.isEmpty():
            fill_color = QColor(color)
            if explicit_fill:
                fill_color = QColor(wanted)
            painter.setPen(Qt.NoPen)
            if str(stroke.get("fp") or "solid") in ("diagonal", "crosshatch"):
                _paint_hatch(painter, path, fill_color, fill * alpha,
                             str(stroke.get("fp")))
            else:
                painter.setBrush(_fill_brush(stroke, flat, fill * alpha,
                                             fill_color))
                painter.drawPath(path)

    if not _has_stroke_color(stroke):
        return
    pen_color = QColor(color)
    pen_color.setAlphaF(_stroke_alpha(stroke, alpha))
    pen = QPen(pen_color, width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setStyle(Qt.SolidLine)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    closed = bool(stroke.get("closed"))
    for contour in projected:
        for run in _runs(contour):
            if len(run) == 1:
                radius = width / 2.0
                painter.setBrush(QBrush(pen_color, Qt.SolidPattern))
                painter.drawEllipse(QPointF(run[0][0], run[0][1]), radius,
                                    radius)
                painter.setBrush(Qt.NoBrush)
                continue
            painter.drawPath(_smooth_path(run,
                                          closed and len(run) == len(contour),
                                          smooth))


def _paint_layers(painter, viewport, doc, scale: float = 1.0) -> None:
    """Every visible layer, bottom to top, at its own opacity."""
    for lay in doc["layers"]:
        if not lay["visible"] or not lay["strokes"]:
            continue
        painter.setOpacity(max(0.0, min(1.0, float(lay["opacity"]))))
        for st in _visible_strokes(lay):
            contours = _stroke_world_contours(st)
            projected = [_project(viewport, contour) for contour in contours]
            _paint_stroke(painter, projected[0], st, scale,
                          extra_contours=projected[1:])
        painter.setOpacity(1.0)


def _path_data(path) -> str:
    """A QPainterPath as an SVG ``d`` attribute."""
    if path.isEmpty():
        return ""
    parts = []
    i = 0
    count = path.elementCount()
    while i < count:
        el = path.elementAt(i)
        kind = _element_type(el)
        if kind == 0:                       # MoveTo
            parts.append(f"M {el.x:.2f} {el.y:.2f}")
        elif kind == 1:                     # LineTo
            parts.append(f"L {el.x:.2f} {el.y:.2f}")
        elif kind == 2 and i + 2 < count:   # CurveTo (+ 2 control data)
            c2 = path.elementAt(i + 1)
            end = path.elementAt(i + 2)
            parts.append(f"C {el.x:.2f} {el.y:.2f} {c2.x:.2f} {c2.y:.2f} "
                         f"{end.x:.2f} {end.y:.2f}")
            i += 2
        i += 1
    return " ".join(parts)


def _element_type(element) -> int:
    """The QPainterPath element type as an int, however it is exposed."""
    got = getattr(element, "type", None)
    if callable(got):
        try:
            got = got()
        except Exception:  # noqa: BLE001
            return 0
    got = getattr(got, "value", got)
    try:
        return int(got)
    except (TypeError, ValueError):
        return 0


def _segment_distance(px, py, x0, y0, x1, y1) -> float:
    """Distance from a pixel to a screen segment."""
    dx, dy = x1 - x0, y1 - y0
    span = dx * dx + dy * dy
    if span <= 1e-9:
        return math.hypot(px - x0, py - y0)
    t = ((px - x0) * dx + (py - y0) * dy) / span
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def _pick(viewport, strokes, sx, sy, skip=()):
    """The id of the stroke nearest to the pixel, within its reach, or None.

    A solid stroke is picked within ``PICK_PX`` of its line; a soft one can
    be picked anywhere inside its halo, because that is the part you see.
    """
    best, best_score = None, 1.0
    for st in strokes:
        if not st.get("visible", True) or st["id"] in skip:
            continue
        reach = PICK_PX
        if float(st.get("soft") or 0.0) > 0.01:
            reach = max(PICK_PX, float(st.get("w") or 0.0) * 0.5)
        for contour in _stroke_world_contours(st):
            pts = _project(viewport, contour)
            for (x0, y0, f0), (x1, y1, f1) in zip(pts, pts[1:]):
                if not (f0 and f1):
                    continue
                score = _segment_distance(sx, sy, x0, y0, x1, y1) / reach
                if score <= best_score:
                    best, best_score = st["id"], score
    return best


# ---------------------------------------------------------------------------
# Snapping
# ---------------------------------------------------------------------------

def _modifiers():
    try:
        return QApplication.keyboardModifiers()
    except Exception:  # noqa: BLE001 — no app, no modifiers
        return Qt.KeyboardModifier.NoModifier


def _snap_candidates(strokes):
    """World points worth snapping to, with the label of what they are."""
    out = []
    for st in strokes:
        contours = _stroke_world_contours(st)
        pts = [point for contour in contours for point in contour]
        if not pts:
            continue
        for contour in contours:
            if contour:
                out.append((QVector3D(*contour[0]), "end"))
                out.append((QVector3D(*contour[-1]), "end"))
        if st["kind"] in ("rect", "circle", "ellipse", "polygon", "star", "boolean"):
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            cz = sum(p[2] for p in pts) / len(pts)
            out.append((QVector3D(cx, cy, cz), "center"))
        # Midpoints of the shape's own corners: the cheap, useful ones.
        if st["kind"] in ("line", "rect", "polyline") and len(pts) > 1:
            for a, b in zip(pts, pts[1:]):
                out.append((QVector3D((a[0] + b[0]) / 2.0,
                                      (a[1] + b[1]) / 2.0,
                                      (a[2] + b[2]) / 2.0), "mid"))
    return out


def _snap_point(viewport, strokes, world):
    """``(world, label)`` — the snapped point and what it snapped to.

    Alt turns snapping off; the grid is tried when no vertex is close
    enough. Nothing here raises: a snap is a convenience, never a reason
    for the tool to stop working.
    """
    try:
        if _modifiers() & Qt.KeyboardModifier.AltModifier:
            return world, ""
    except Exception:  # noqa: BLE001
        pass
    try:
        here = _project(viewport, [(world.x(), world.y(), world.z())])[0]
    except Exception:  # noqa: BLE001
        here = None
    if here is not None and here[2] and strokes:
        try:
            cands = _snap_candidates(strokes)
            pts = _project(viewport, [(c.x(), c.y(), c.z()) for c, _ in cands])
        except Exception:  # noqa: BLE001
            cands, pts = [], []
        best, best_d, label = None, SNAP_PX, ""
        for (p, (cand, lab)) in zip(pts, cands):
            if not p[2]:
                continue
            d = math.hypot(p[0] - here[0], p[1] - here[1])
            if d <= best_d:
                best, best_d, label = cand, d, lab
        if best is not None:
            return QVector3D(best), label
    grid = float(_SNAP.get("grid") or 0.0)
    if grid > 0:
        return (QVector3D(round(world.x() / grid) * grid,
                          round(world.y() / grid) * grid,
                          round(world.z() / grid) * grid), "grid")
    return world, ""


def _paint_snap(painter, viewport, world, label: str) -> None:
    """A small marker, and the name of what the point landed on."""
    try:
        got = _project(viewport, [(world.x(), world.y(), world.z())])[0]
    except Exception:  # noqa: BLE001
        return
    if not got[2]:
        return
    x, y = got[0], got[1]
    color = QColor("#3f7fd1")
    pen = QPen(color, 1.4)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    if label == "center":
        painter.drawEllipse(QPointF(x, y), 6.0, 6.0)
    elif label == "mid":
        painter.drawPolygon(QPolygonF([QPointF(x, y - 5.0),
                                       QPointF(x + 5.0, y),
                                       QPointF(x, y + 5.0),
                                       QPointF(x - 5.0, y)]))
    elif label == "grid":
        painter.drawLine(QPointF(x - 6, y), QPointF(x + 6, y))
        painter.drawLine(QPointF(x, y - 6), QPointF(x, y + 6))
    else:
        painter.drawRect(QRectF(x - 5.0, y - 5.0, 10.0, 10.0))
    if label:
        painter.setPen(QPen(color, 1.0))
        painter.drawText(QPointF(x + 10.0, y - 8.0), label)


# ---------------------------------------------------------------------------
# Geometry: the shapes, on the plane the stroke began on
# ---------------------------------------------------------------------------

def _plane_basis(normal):
    """Two unit vectors spanning the plane whose normal is ``normal``."""
    n = QVector3D(normal)
    if n.length() < 1e-9:
        n = QVector3D(0.0, 0.0, 1.0)
    n = n.normalized()
    ref = QVector3D(0.0, 0.0, 1.0) if abs(n.z()) < 0.95 else QVector3D(1.0, 0.0, 0.0)
    u = QVector3D.crossProduct(ref, n)
    if u.length() < 1e-9:
        u = QVector3D.crossProduct(QVector3D(0.0, 1.0, 0.0), n)
    u = u.normalized()
    v = QVector3D.crossProduct(n, u).normalized()
    return u, v


def _plane_xy(origin, u, v, point):
    """The 2D coordinates of ``point`` in the plane's own frame."""
    d = QVector3D(point) - origin
    return QVector3D.dotProduct(d, u), QVector3D.dotProduct(d, v)


def _from_xy(origin, u, v, x, y):
    return origin + u * x + v * y


def _circumcenter(a, b, c):
    """The centre of the circle through three 2D points, or ``None``."""
    d = 2.0 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1])
               + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-12:
        return None
    a2 = a[0] * a[0] + a[1] * a[1]
    b2 = b[0] * b[0] + b[1] * b[1]
    c2 = c[0] * c[0] + c[1] * c[1]
    ux = (a2 * (b[1] - c[1]) + b2 * (c[1] - a[1]) + c2 * (a[1] - b[1])) / d
    uy = (a2 * (c[0] - b[0]) + b2 * (a[0] - c[0]) + c2 * (b[0] - a[0])) / d
    return (ux, uy)


def _ring(cx, cy, rx, ry, count):
    return [(cx + rx * math.cos(TAU * i / count),
             cy + ry * math.sin(TAU * i / count)) for i in range(count)]


def shape_points(kind, pts, normal, sides: int = 6):
    """``(world points, closed)`` for a shape from the points given.

    ``pts`` is what the user has laid down: the first point anchors the
    shape, the last one sizes it (and, for the arc, the middle one bulges
    it). Everything is built in the plane's own frame, so the shape is
    flat and stays on the plane it was started on.
    """
    if len(pts) < 2:
        return list(pts), False
    if kind == "line":
        return [QVector3D(pts[0]), QVector3D(pts[-1])], False
    if kind == "polyline":
        return [QVector3D(p) for p in pts], False

    origin = QVector3D(pts[0])
    u, v = _plane_basis(normal)
    ax, ay = 0.0, 0.0
    bx, by = _plane_xy(origin, u, v, pts[-1])

    def w(x, y):
        return _from_xy(origin, u, v, x, y)

    if kind == "rect":
        return [w(ax, ay), w(bx, ay), w(bx, by), w(ax, by)], True

    if kind == "ellipse":
        cx, cy = (ax + bx) / 2.0, (ay + by) / 2.0
        rx, ry = abs(bx - ax) / 2.0, abs(by - ay) / 2.0
        if rx < 1e-6 or ry < 1e-6:
            return [QVector3D(pts[0]), QVector3D(pts[-1])], False
        return [w(x, y) for x, y in _ring(cx, cy, rx, ry, 96)], True

    if kind == "circle":
        radius = math.hypot(bx - ax, by - ay)
        if radius < 1e-6:
            return [QVector3D(pts[0]), QVector3D(pts[-1])], False
        return [w(x, y) for x, y in _ring(ax, ay, radius, radius, 96)], True

    if kind in ("polygon", "star"):
        radius = math.hypot(bx - ax, by - ay)
        if radius < 1e-6:
            return [QVector3D(pts[0]), QVector3D(pts[-1])], False
        start = math.atan2(by - ay, bx - ax)
        count = max(3, int(sides))
        if kind == "polygon":
            return [w(ax + radius * math.cos(start + TAU * i / count),
                      ay + radius * math.sin(start + TAU * i / count))
                    for i in range(count)], True
        inner = radius * 0.45
        out = []
        for i in range(count * 2):
            r = radius if i % 2 == 0 else inner
            ang = start + math.pi * i / count
            out.append(w(ax + r * math.cos(ang), ay + r * math.sin(ang)))
        return out, True

    if kind == "arc":
        if len(pts) < 3:
            return [QVector3D(pts[0]), QVector3D(pts[-1])], False
        q0 = _plane_xy(origin, u, v, pts[0])
        q1 = _plane_xy(origin, u, v, pts[1])
        q2 = _plane_xy(origin, u, v, pts[-1])
        center = _circumcenter(q0, q1, q2)
        if center is None:
            return [QVector3D(pts[0]), QVector3D(pts[-1])], False
        cx, cy = center
        radius = math.hypot(q0[0] - cx, q0[1] - cy)
        if radius < 1e-6:
            return [QVector3D(pts[0]), QVector3D(pts[-1])], False
        a0 = math.atan2(q0[1] - cy, q0[0] - cx)
        a1 = math.atan2(q1[1] - cy, q1[0] - cx)
        a2 = math.atan2(q2[1] - cy, q2[0] - cx)
        sweep = (a2 - a0) % TAU
        if (a1 - a0) % TAU > sweep:      # the middle point is the other way
            sweep -= TAU
        steps = max(8, min(200, int(abs(sweep) / math.pi * 64)))
        return [w(cx + radius * math.cos(a0 + sweep * i / steps),
                  cy + radius * math.sin(a0 + sweep * i / steps))
                for i in range(steps + 1)], False

    return [QVector3D(pts[0]), QVector3D(pts[-1])], False


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _hint(viewport, text: str, msec: int = 1500) -> None:
    """Say something in the status bar, never risking the commit that came
    before it: a viewport without a window or a status bar is not a reason
    to lose a stroke."""
    try:
        viewport.flash_status(text, msec)
    except Exception:  # noqa: BLE001
        pass


def _fail(viewport, exc: BaseException) -> None:
    """An unexpected error, said out loud instead of swallowed."""
    _hint(viewport, f"Croquis on model: {type(exc).__name__}: {exc}", 6000)


def _view_normal(viewport):
    """The camera's view direction, or ``None`` — this never raises."""
    camera = getattr(viewport, "camera", None)
    if camera is None:
        return None
    try:
        return QVector3D(camera.forward())
    except Exception:  # noqa: BLE001 — older or newer camera API
        pass
    try:  # the orbital camera: eye = target + distance·(cp·cy, cp·sy, sp)
        cp = math.cos(camera.pitch)
        return QVector3D(-cp * math.cos(camera.yaw),
                         -cp * math.sin(camera.yaw),
                         -math.sin(camera.pitch))
    except Exception:  # noqa: BLE001
        return None


def _make_sticky(viewport, tool) -> None:
    """Put a plugin tool in the hand, and keep it there.

    A tool picked from the Extensions menu is one-shot: ``on_activate`` runs
    and the viewport's tool is left alone. A pen has to stay active to
    receive press/drag/release, so it hands itself to the viewport — and
    ``set_active_tool`` calls ``on_activate`` again, hence the flag.
    """
    if getattr(viewport, "active_tool", None) is tool:
        return
    if getattr(tool, "_entering", False):
        return
    tool._entering = True
    try:
        viewport.set_active_tool(tool)
    finally:
        tool._entering = False
    if getattr(viewport, "active_tool", None) is not tool:
        _hint(viewport, "Croquis on model: the viewport did not take the tool", 6000)


def _take_tool(app, tool_class, announce: bool = True) -> None:
    """Hand a Croquis on model tool to the viewport (menu actions use this)."""
    viewport = getattr(app, "viewport", None)
    if viewport is None:
        return
    try:
        viewport.set_active_tool(tool_class())
        viewport.setFocus()
    except Exception as exc:  # noqa: BLE001
        _hint(viewport, f"Croquis on model: {type(exc).__name__}: {exc}", 6000)
        return
    if announce:
        _hint(viewport, f"Croquis on model: {tool_class.name}")


class _StickyTool(Tool):
    """A plugin tool that keeps the hand: press, drag, release reach it."""

    shortcut = None
    uses_snap = False

    def draw_overlay(self, viewport, painter: QPainter) -> None:
        """Preview is painted last by the extension overlay."""

    def __init__(self) -> None:
        self._entering = False

    # ---- Lifecycle ---------------------------------------------------------
    def on_activate(self, viewport) -> None:
        self._forget()
        _make_sticky(viewport, self)

    def on_deactivate(self, viewport) -> None:
        self._forget()

    def on_cancel(self, viewport) -> None:
        self._forget()
        viewport.update()

    def _forget(self) -> None:  # pragma: no cover — overridden
        pass


def _stroke_dict(points, kind: str, closed: bool, smooth: int,
                 turbulence: float = 0.0, normal=None) -> dict:
    """A stroke as the document keeps it."""
    stroke = {
        "id": uuid.uuid4().hex[:12],
        "c": _PEN["color"],
        "fc": str(_PEN["fill_color"] or ""),
        "fc2": str(_PEN["fill2"] or ""),
        "fg": str(_PEN["fill_grad"] or "solid"),
        "fp": str(_PEN["fill_pattern"] or "solid"),
        "w": float(_PEN["width"]),
        "p": [(float(p.x()), float(p.y()), float(p.z())) for p in points],
        "kind": kind,
        "fill": float(_PEN["fill_alpha"]) if _PEN["fill"] else 0.0,
        "closed": bool(closed),
        "smooth": int(smooth),
        "soft": 0.0,
        "a": 1.0,
        "jt": max(0.0, min(1.0, float(turbulence))),
    }
    if normal is not None and normal.length() > 1e-9:
        unit = normal.normalized()
        stroke["normal"] = (float(unit.x()), float(unit.y()),
                           float(unit.z()))
    return stroke


class ScribbleTool(_StickyTool):
    """Free-hand brush: press, drag, release — one stroke, one undo step."""

    name = "Croquis on model pen"
    description = (
        "Draw free-hand over the model; every stroke is one undo step.")
    uses_snap = False
    #: A pen with no icon of its own still says so on the pointer.
    qt_cursor = Qt.CursorShape.CrossCursor

    def __init__(self) -> None:
        super().__init__()
        self._points: list = []
        self._plane = None
        self._normal = None
        self._last_px = None
        self._pending_point = None
        self._stroke_id = None

    # ---- Input -------------------------------------------------------------
    def drag_plane(self, viewport):
        """Use the active saved plane, or the plane this stroke began on."""
        if self._points and self._plane is not None and self._normal is not None:
            return (self._plane, self._normal)
        plane = active_drawing_plane(_APP, viewport.scene)
        if plane is not None:
            return (QVector3D(*plane["origin"]),
                    QVector3D(*plane["normal"]))
        return None

    def on_click(self, ctx: ToolContext) -> None:
        if not self._points:
            self._stroke_id = uuid.uuid4().hex[:12]
            plane = active_drawing_plane(_APP, ctx.viewport.scene)
            self._plane = QVector3D(ctx.world)
            self._last_px = None
            self._normal = (QVector3D(*plane["normal"]) if plane is not None
                            else _view_normal(ctx.viewport))
        try:
            self._add(ctx)
        except Exception as exc:  # noqa: BLE001 — said in the status bar
            _fail(ctx.viewport, exc)
        ctx.viewport.update()

    def on_hover(self, ctx: ToolContext) -> None:
        if not self._points:
            return
        try:
            self._add(ctx)
        except Exception as exc:  # noqa: BLE001
            _fail(ctx.viewport, exc)
        ctx.viewport.update()

    def on_release(self, viewport) -> None:
        points = list(self._points)
        if (self._pending_point is not None and len(points) < MAX_POINTS
                and (not points
                     or (points[-1] - self._pending_point).lengthSquared()
                     >= 1e-8)):
            points.append(self._pending_point)
        self._points = []
        stroke_id, self._stroke_id = self._stroke_id, None
        normal, self._normal = self._normal, None
        self._plane = None
        self._last_px = None
        self._pending_point = None
        if len(points) < 2 or _APP is None:
            viewport.update()
            return
        try:
            stroke = self._new_stroke(points, normal=normal)
            if stroke_id:
                stroke["id"] = stroke_id
            if not _append_stroke(_APP, viewport.scene, stroke):
                _hint(viewport, "Croquis on model: this layer is hidden or locked",
                      6000)
                viewport.update()
                return
        except Exception as exc:  # noqa: BLE001 — said, not swallowed
            _fail(viewport, exc)
            viewport.update()
            return
        _hint(viewport, f"Croquis on model: {total_strokes(_APP, viewport.scene)} "
                        f"strokes")
        viewport.update()

    # ---- Drawing -----------------------------------------------------------
    def draw_preview(self, viewport, painter: QPainter) -> None:
        """The stroke being drawn, over the committed ones."""
        points = self._preview_points()
        if len(points) < 2:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        _paint_stroke(painter, _project(
            viewport, [(p.x(), p.y(), p.z()) for p in points]),
            self._overlay_stroke())

    # ---- What this tool lays down ------------------------------------------
    def _new_stroke(self, points, normal=None) -> dict:
        """The stroke a drag becomes; the brush overrides this."""
        return _stroke_dict(points, "free", bool(_PEN["fill"]), PEN_SMOOTH,
                            turbulence=float(_PEN["turbulence"]),
                            normal=normal)

    def _overlay_stroke(self) -> dict:
        """The stroke being drawn, as the overlay paints it."""
        return {"c": _PEN["color"], "fc": _PEN["fill_color"],
                "fc2": _PEN["fill2"], "fg": _PEN["fill_grad"],
                "fp": _PEN["fill_pattern"],
                "w": float(_PEN["width"]),
                "fill": float(_PEN["fill_alpha"]) if _PEN["fill"] else 0.0,
                "closed": bool(_PEN["fill"]),
                "smooth": PEN_SMOOTH,
                "soft": 0.0, "a": 1.0,
                "kind": "free", "jt": float(_PEN["turbulence"]),
                "id": self._stroke_id or "draft"}

    # ---- Internals ---------------------------------------------------------
    def _add(self, ctx: ToolContext) -> None:
        sx, sy = ctx.screen.x(), ctx.screen.y()
        spacing = _trace_spacing_px()
        point = QVector3D(ctx.world)
        if self._points and (self._points[-1] - point).lengthSquared() < 1e-8:
            self._pending_point = None
            return
        if self._last_px is not None and \
                abs(sx - self._last_px[0]) + abs(sy - self._last_px[1]) \
                < spacing:
            self._pending_point = point
            return
        if len(self._points) >= MAX_POINTS:
            return
        self._last_px = (sx, sy)
        self._pending_point = None
        self._points.append(point)

    def _preview_points(self):
        points = list(self._points)
        if (self._pending_point is not None and len(points) < MAX_POINTS
                and (not points
                     or (points[-1] - self._pending_point).lengthSquared()
                     >= 1e-8)):
            points.append(self._pending_point)
        return points

    def _forget(self) -> None:
        self._points = []
        self._plane = None
        self._normal = None
        self._last_px = None
        self._pending_point = None
        self._stroke_id = None


class ScribbleBrushTool(ScribbleTool):
    """A soft brush: a halo that fades out, like a breath or dust.

    Deliberately not a pen with a different width. The stroke is laid down
    as shells — narrow and dense in the middle, wider and fainter outwards
    — so the mark has no edge and no core line, and the sample capture,
    smoothing and undo behaviour of the pen are reused as they are.
    """

    name = "Croquis on model brush"
    description = ("A soft brush: a halo that fades to nothing, like a breath "
                   "or a cloud of dust.")
    uses_snap = False
    qt_cursor = Qt.CursorShape.CrossCursor

    def draw_preview(self, viewport, painter: QPainter) -> None:
        points = self._preview_points()
        if len(points) < 2:
            return
        stroke = self._overlay_stroke()
        stroke["p"] = [(point.x(), point.y(), point.z())
                       for point in points]
        stroke = _clean_stroke(stroke)
        if stroke is None:
            return
        layer = active_layer(document(_APP, viewport.scene)) if _APP else None
        painter.save()
        painter.setOpacity(float(layer["opacity"]) if layer else 1.0)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        _paint_stroke(painter, _project(viewport, stroke["p"]), stroke)
        painter.restore()

    def _new_stroke(self, points, normal=None) -> dict:
        stroke = {
            "id": uuid.uuid4().hex[:12],
            "c": str(_BRUSH["color"]),
            "fc": "",
            "w": float(_BRUSH["width"]),
            "p": [(float(p.x()), float(p.y()), float(p.z()))
                  for p in points],
            "kind": "brush",
            "fill": 0.0,
            "closed": False,
            "smooth": 2,
            "soft": max(0.0, min(1.0, float(_BRUSH["soft"]))),
            "a": max(0.05, min(1.0, float(_BRUSH["alpha"]))),
            "pf": str(_BRUSH["profile"] or "even"),
            "jt": max(0.0, min(1.0, float(_BRUSH["jitter"]))),
            "c2": str(_BRUSH["color2"] or ""),
            "bg": str(_BRUSH["grad"] or "off"),
        }
        if normal is not None and normal.length() > 1e-9:
            unit = normal.normalized()
            stroke["normal"] = (float(unit.x()), float(unit.y()),
                                float(unit.z()))
        return stroke

    def _overlay_stroke(self) -> dict:
        return {"c": _BRUSH["color"], "fc": "",
                "w": float(_BRUSH["width"]),
                "fill": 0.0, "closed": False, "smooth": 2,
                "soft": float(_BRUSH["soft"]),
                "a": float(_BRUSH["alpha"]),
                "kind": "brush",
                "pf": str(_BRUSH["profile"] or "even"),
                "jt": float(_BRUSH["jitter"]),
                "id": self._stroke_id or "draft",
                "c2": str(_BRUSH["color2"] or ""),
                "bg": str(_BRUSH["grad"] or "off")}


class ScribbleShapeTool(_StickyTool):
    """Straight lines, arcs, rectangles, circles, polygons — with snaps."""

    name = "Croquis on model shapes"
    description = (
        "Draw planar shapes with snapping: line, polyline, rectangle, "
        "ellipse, circle, arc, polygon, star.")
    uses_snap = True
    qt_cursor = Qt.CursorShape.CrossCursor

    #: How many points the current shape needs before it commits.
    def __init__(self) -> None:
        super().__init__()
        self._pts: list = []
        self._hover = None
        self._hover_px = None
        self._plane = None
        self._normal = None
        self._press_px = None
        self._click_px = None
        self._click_t = 0.0
        self._snap = None

    # ---- Input -------------------------------------------------------------
    def drag_plane(self, viewport):
        if self._plane is not None and self._normal is not None:
            return (self._plane, self._normal)
        plane = active_drawing_plane(_APP, viewport.scene)
        if plane is not None:
            return (QVector3D(*plane["origin"]),
                    QVector3D(*plane["normal"]))
        return None

    def on_click(self, ctx: ToolContext) -> None:
        try:
            world, _label = self._resolve(ctx)
        except Exception as exc:  # noqa: BLE001
            _fail(ctx.viewport, exc)
            return
        kind = str(_SHAPE["kind"])
        now = time.monotonic()
        px = (ctx.screen.x(), ctx.screen.y())
        double = (self._click_px is not None
                  and now - self._click_t < 0.45
                  and math.hypot(px[0] - self._click_px[0],
                                 px[1] - self._click_px[1]) < 6.0)
        self._click_t, self._click_px = now, px
        self._press_px = px
        if self._plane is None:
            plane = active_drawing_plane(_APP, ctx.viewport.scene)
            self._plane = QVector3D(world)
            self._normal = (QVector3D(*plane["normal"]) if plane is not None
                            else (_view_normal(ctx.viewport)
                                  or QVector3D(0.0, 0.0, 1.0)))
        if kind == "polyline":
            if double and len(self._pts) >= 2:
                self._finish(ctx.viewport)
                return
            if not self._pts or \
                    (QVector3D(world) - self._pts[-1]).lengthSquared() > 1e-10:
                self._pts.append(QVector3D(world))
            ctx.viewport.update()
            return
        need = 3 if kind == "arc" else 2
        self._pts.append(QVector3D(world))
        if len(self._pts) >= need:
            self._finish(ctx.viewport)
        else:
            ctx.viewport.update()

    def on_hover(self, ctx: ToolContext) -> None:
        try:
            world, _label = self._resolve(ctx)
        except Exception as exc:  # noqa: BLE001
            _fail(ctx.viewport, exc)
            return
        self._hover = QVector3D(world)
        self._hover_px = (ctx.screen.x(), ctx.screen.y())
        ctx.viewport.update()

    def on_release(self, viewport) -> None:
        press, self._press_px = self._press_px, None
        if press is None or not self._pts or self._hover is None:
            return
        if _SHAPE["kind"] == "arc":
            return                      # the arc needs its third point
        moved = math.hypot(self._hover_px[0] - press[0],
                           self._hover_px[1] - press[1]) if self._hover_px else 0
        if moved < 6.0:
            return                      # a click, not a drag: click-click mode
        if (QVector3D(self._hover) - self._pts[-1]).lengthSquared() < 1e-12:
            return
        self._pts.append(QVector3D(self._hover))
        self._finish(viewport)

    # ---- Drawing -----------------------------------------------------------
    def draw_preview(self, viewport, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pts = list(self._pts)
        if self._hover is not None and pts:
            pts.append(QVector3D(self._hover))
        if len(pts) >= 2:
            try:
                poly, closed = shape_points(
                    str(_SHAPE["kind"]), pts,
                    self._normal or QVector3D(0.0, 0.0, 1.0),
                    int(_SHAPE["sides"]))
                stroke = {
                    "c": _PEN["color"], "fc": _PEN["fill_color"],
                    "fc2": _PEN["fill2"], "fg": _PEN["fill_grad"],
                    "w": float(_PEN["width"]),
                    "p": [(p.x(), p.y(), p.z()) for p in poly],
                    "fill": float(_PEN["fill_alpha"]) if _PEN["fill"] else 0.0,
                    "fp": _PEN["fill_pattern"],
                    "closed": closed, "smooth": 0,
                }
                _paint_stroke(painter, _project(viewport, stroke["p"]), stroke)
            except Exception:  # noqa: BLE001 — a preview is not worth a raise
                pass
        if self._snap is not None:
            _paint_snap(painter, viewport, self._snap[0], self._snap[1])

    # ---- Internals ---------------------------------------------------------
    def _resolve(self, ctx: ToolContext):
        """The point under the cursor, snapped and (with Shift) constrained."""
        world = QVector3D(ctx.world)
        strokes = all_strokes(_APP, ctx.viewport.scene) if _APP else []
        world, label = _snap_point(ctx.viewport, strokes, world)
        if not label and self._pts:
            try:
                if _modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    world = _constrain(self._pts[0], world,
                                       self._normal or QVector3D(0.0, 0.0, 1.0))
            except Exception:  # noqa: BLE001
                pass
        self._snap = (world, label) if label else None
        return world, label

    def _finish(self, viewport) -> None:
        pts = list(self._pts)
        normal = self._normal or QVector3D(0.0, 0.0, 1.0)
        self._forget()
        if len(pts) < 2 or _APP is None:
            viewport.update()
            return
        kind = str(_SHAPE["kind"])
        try:
            poly, closed = shape_points(kind, pts, normal, int(_SHAPE["sides"]))
            stroke = _stroke_dict(poly, kind, closed, 0, normal=normal)
            if not _append_stroke(_APP, viewport.scene, stroke):
                _hint(viewport, "Croquis on model: this layer is hidden or locked",
                      6000)
                viewport.update()
                return
        except Exception as exc:  # noqa: BLE001
            _fail(viewport, exc)
            viewport.update()
            return
        _hint(viewport, f"Croquis on model: {kind} · "
                        f"{total_strokes(_APP, viewport.scene)} strokes")
        viewport.update()

    def _forget(self) -> None:
        self._pts = []
        self._hover = None
        self._hover_px = None
        self._press_px = None
        self._plane = None
        self._normal = None
        self._snap = None


def _constrain(anchor, world, normal):
    """Take ``world`` to the nearest 45° from ``anchor``, in the stroke plane."""
    u, v = _plane_basis(normal)
    ax, ay = _plane_xy(anchor, u, v, anchor)
    x, y = _plane_xy(anchor, u, v, world)
    dx, dy = x - ax, y - ay
    span = math.hypot(dx, dy)
    if span < 1e-9:
        return world
    step = math.pi / 4.0
    angle = round(math.atan2(dy, dx) / step) * step
    return _from_xy(anchor, u, v, ax + span * math.cos(angle),
                    ay + span * math.sin(angle))


def _distance_to_erase_path(x, y, path):
    if not path:
        return float("inf")
    if len(path) == 1:
        return math.hypot(x - path[0][0], y - path[0][1])
    return min(_segment_distance(x, y, a[0], a[1], b[0], b[1])
               for a, b in zip(path, path[1:]))


def _partial_cut_contour(viewport, contour, erase_path, radius: float,
                         closed: bool):
    """Split a world contour wherever the screen-space eraser passes."""
    if len(contour) < 2 or not erase_path:
        return [list(contour)], False
    projected = _project(viewport, contour)
    count = len(contour) if closed else len(contour) - 1
    fragments, current = [], []
    changed = False

    def flush():
        nonlocal current
        if len(current) >= 2:
            fragments.append(current)
        current = []

    for index in range(count):
        next_index = (index + 1) % len(contour)
        a, b = contour[index], contour[next_index]
        pa, pb = projected[index], projected[next_index]
        if not (pa[2] and pb[2]):
            samples = ((a, True), (b, True))
        else:
            span = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
            steps = max(1, min(512, int(math.ceil(span / 2.0))))
            samples = []
            for step in range(steps + 1):
                t = step / float(steps)
                world = (a[0] + (b[0] - a[0]) * t,
                         a[1] + (b[1] - a[1]) * t,
                         a[2] + (b[2] - a[2]) * t)
                sx = pa[0] + (pb[0] - pa[0]) * t
                sy = pa[1] + (pb[1] - pa[1]) * t
                samples.append((world, _distance_to_erase_path(
                    sx, sy, erase_path) > radius))
        for world, keep in samples:
            if keep:
                if not current or sum((current[-1][axis] - world[axis]) ** 2
                                      for axis in range(3)) > 1e-12:
                    current.append(world)
            else:
                changed = True
                flush()
    flush()
    if not changed:
        return [list(contour)], False
    if closed and len(fragments) > 1:
        first, last = fragments[0][0], fragments[-1][-1]
        if sum((first[i] - last[i]) ** 2 for i in range(3)) < 1e-8:
            fragments[0] = fragments[-1] + fragments[0][1:]
            fragments.pop()
    return fragments, True


def _partial_erase_stroke(stroke, viewport, erase_path):
    """Return replacement line fragments and whether anything was cut."""
    pieces = []
    changed = False
    contour_results = []
    for contour in _stroke_world_contours(stroke):
        fragments, did_cut = _partial_cut_contour(
            viewport, contour, erase_path, PICK_PX, bool(stroke.get("closed")))
        contour_results.append((fragments, did_cut))
        changed = changed or did_cut
    if not changed:
        return [], False
    for fragments, did_cut in contour_results:
        for fragment in fragments:
            if len(fragment) < 2:
                continue
            replacement = dict(stroke)
            replacement.update(
                id=uuid.uuid4().hex[:12], p=list(fragment), contours=[],
                kind=("free" if stroke.get("kind") == "free" else "partial"),
                fill=0.0, closed=bool(stroke.get("closed")) and not did_cut)
            pieces.append(replacement)
    return pieces, True


class ScribbleEraserTool(_StickyTool):
    """Erases the strokes a drag sweeps over — one undo step per gesture."""

    name = "Croquis on model eraser"
    description = (
        "Drag over annotations to erase them; they go on release.")
    uses_snap = False
    qt_cursor = Qt.CursorShape.PointingHandCursor

    def __init__(self) -> None:
        super().__init__()
        self._doomed: set = set()
        self._stroke = False
        self._path = []
        self._partial = False

    # ---- Input -------------------------------------------------------------
    def drag_plane(self, viewport):
        """Any plane the ray always hits: the eraser reads pixels, not metres."""
        try:
            normal = _view_normal(viewport)
            if normal is None:
                return None
            return (QVector3D(viewport.camera.target), normal)
        except Exception:  # noqa: BLE001 — no camera, no plane
            return None

    def on_click(self, ctx: ToolContext) -> None:
        self._stroke = True
        self._partial = bool(_ERASER_MODE["partial"])
        self._path = [(ctx.screen.x(), ctx.screen.y())]
        try:
            self._mark(ctx)
        except Exception as exc:  # noqa: BLE001
            _fail(ctx.viewport, exc)
        ctx.viewport.update()

    def on_hover(self, ctx: ToolContext) -> None:
        if not self._stroke:
            return
        point = (ctx.screen.x(), ctx.screen.y())
        if not self._path or math.hypot(point[0] - self._path[-1][0],
                                        point[1] - self._path[-1][1]) >= 1.5:
            self._path.append(point)
        try:
            self._mark(ctx)
        except Exception as exc:  # noqa: BLE001
            _fail(ctx.viewport, exc)
        ctx.viewport.update()

    def on_release(self, viewport) -> None:
        doomed, self._doomed = self._doomed, set()
        erase_path, self._path = self._path, []
        partial, self._partial = self._partial, False
        self._stroke = False
        if not doomed or _APP is None:
            viewport.update()
            return
        try:
            doc = document(_APP, viewport.scene)
            layer = active_layer(doc)
            if layer["locked"]:
                _hint(viewport, "Croquis on model: this layer is locked", 6000)
                viewport.update()
                return
            if partial:
                revised = []
                changed = 0
                for stroke in layer["strokes"]:
                    if (stroke["id"] not in doomed
                            or not stroke.get("visible", True)):
                        revised.append(stroke)
                        continue
                    pieces, cut = _partial_erase_stroke(stroke, viewport,
                                                        erase_path)
                    if cut:
                        for piece in pieces:
                            piece.pop("name", None)
                            _name_stroke({"strokes": revised}, piece)
                            revised.append(piece)
                        changed += 1
                    else:
                        revised.append(stroke)
                if not changed:
                    viewport.update()
                    return
                layer["strokes"] = revised
            else:
                before = len(layer["strokes"])
                layer["strokes"] = [st for st in layer["strokes"]
                                    if st["id"] not in doomed
                                    or not st.get("visible", True)]
                if len(layer["strokes"]) == before:
                    viewport.update()
                    return
            _store(_APP, doc)
        except Exception as exc:  # noqa: BLE001
            _fail(viewport, exc)
            viewport.update()
            return
        left = total_strokes(_APP, viewport.scene)
        _hint(viewport, f"Croquis on model: {left} strokes" if left
              else "Croquis on model: no strokes left")
        viewport.update()

    # ---- Drawing -----------------------------------------------------------
    def draw_preview(self, viewport, painter: QPainter) -> None:
        """Preview either removed marks or the swept partial-erase capsule."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._stroke and self._partial and self._path:
            painter.save()
            red = QColor("#d1433a")
            red.setAlphaF(0.25)
            pen = QPen(red, PICK_PX * 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if len(self._path) == 1:
                painter.drawEllipse(QPointF(*self._path[0]), PICK_PX, PICK_PX)
            else:
                painter.drawPolyline(QPolygonF([QPointF(x, y)
                                                for x, y in self._path]))
            painter.restore()
            return
        if not self._doomed or _APP is None:
            return
        for st in document(_APP, viewport.scene)["layers"]:
            for stroke in st["strokes"]:
                if stroke.get("visible", True) and stroke["id"] in self._doomed:
                    contours = [_project(viewport, contour)
                                for contour in _stroke_world_contours(stroke)]
                    _paint_stroke(painter, contours[0],
                                  {"c": "#d1433a", "w": stroke["w"] + 1.0,
                                   "p": stroke["p"], "fill": 0.0,
                                   "closed": stroke["closed"],
                                   "smooth": stroke["smooth"]},
                                  extra_contours=contours[1:])

    # ---- Internals ---------------------------------------------------------
    def _mark(self, ctx: ToolContext) -> None:
        if _APP is None:
            return
        layer = active_layer(document(_APP, ctx.viewport.scene))
        found = _pick(ctx.viewport, layer["strokes"],
                      ctx.screen.x(), ctx.screen.y(), skip=self._doomed)
        if found is not None:
            self._doomed.add(found)

    def _forget(self) -> None:
        self._doomed = set()
        self._stroke = False
        self._path = []
        self._partial = False


#: The colour of the selection fence and of the marks it keeps.
_MOVE_ACCENT = "#3b82f6"
#: Selection survives switching between Select, Move, and drawing tools.
_SELECTION = {"scene": None, "ids": set()}
#: Selected vertices survive tool re-activation, but belong to one stroke.
_POINT_SELECTION = {"scene": None, "stroke": None, "indices": set()}
#: Whole-stroke erasing remains the default; the checkbox enables partial cuts.
_ERASER_MODE = {"partial": False}


def _inside_box(x, y, left, top, right, bottom):
    """True when the pixel sits inside the selection rectangle."""
    return left <= x <= right and top <= y <= bottom


def _segment_meets_box(x0, y0, x1, y1, left, top, right, bottom):
    """True when the segment touches the rectangle, even crossing it.

    Liang–Barsky: the rectangle is the selection fence, and a mark counts
    when any part of it falls inside — the way a rubber band should feel.
    """
    dx, dy = x1 - x0, y1 - y0
    low, high = 0.0, 1.0
    for span, slack in ((-dx, x0 - left), (dx, right - x0),
                        (-dy, y0 - top), (dy, bottom - y0)):
        if abs(span) < 1e-12:
            if slack < 0.0:
                return False
            continue
        edge = slack / span
        if span < 0.0:
            if edge > high:
                return False
            if edge > low:
                low = edge
        else:
            if edge < low:
                return False
            if edge < high:
                high = edge
    return low <= high


def _normal_from_points(points):
    """Find a usable plane normal from a closed 3D contour."""
    if len(points) < 3:
        return None
    origin = QVector3D(*points[0])
    for i in range(1, len(points) - 1):
        a = QVector3D(*points[i]) - origin
        b = QVector3D(*points[i + 1]) - origin
        normal = QVector3D.crossProduct(a, b)
        if normal.length() > 1e-8:
            return normal.normalized()
    return None


def _boolean_combine(strokes, operation):
    """Return one filled compound stroke, or an error message.

    The contours are converted with Qt's robust painter-path boolean ops;
    every selected shape must be closed and coplanar with the first.
    """
    if len(strokes) < 2:
        return None, "Select at least two closed shapes with Move's fence."
    if any(not st.get("closed") or float(st.get("soft") or 0.0) > 0.01
           for st in strokes):
        return None, "Boolean operations need closed, non-brush shapes."
    first_contours = _stroke_world_contours(strokes[0])
    if not first_contours or len(first_contours[0]) < 3:
        return None, "The selected shape has no closed contour."
    origin = QVector3D(*first_contours[0][0])
    normal = _normal_from_points(first_contours[0])
    if normal is None:
        return None, "Could not find the selected shapes' plane."
    u, v = _plane_basis(normal)
    paths = []
    for stroke in strokes:
        contours = _stroke_world_contours(stroke)
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        for contour in contours:
            if len(contour) < 3:
                continue
            for xyz in contour:
                point = QVector3D(*xyz)
                if abs(QVector3D.dotProduct(point - origin, normal)) > 1e-4:
                    return None, "Selected shapes must lie on the same plane."
            x0, y0 = _plane_xy(origin, u, v, QVector3D(*contour[0]))
            path.moveTo(x0, y0)
            for xyz in contour[1:]:
                x, y = _plane_xy(origin, u, v, QVector3D(*xyz))
                path.lineTo(x, y)
            path.closeSubpath()
        if path.isEmpty():
            return None, "A selected shape has no usable contour."
        paths.append(path)
    result = paths[0]
    if operation == "union":
        for path in paths[1:]:
            result = result.united(path)
    elif operation == "difference":
        for path in paths[1:]:
            result = result.subtracted(path)
    elif operation == "intersection":
        for path in paths[1:]:
            result = result.intersected(path)
    else:
        return None, "Unknown boolean operation."
    if result.isEmpty():
        return None, "The boolean result is empty."
    contours = []
    for polygon in result.toSubpathPolygons():
        pts = [(float(polygon[i].x()), float(polygon[i].y()))
               for i in range(polygon.size())]
        if len(pts) > 1 and math.hypot(pts[0][0] - pts[-1][0],
                                       pts[0][1] - pts[-1][1]) < 1e-8:
            pts.pop()
        if len(pts) >= 3:
            world_contour = []
            for x, y in pts:
                world = _from_xy(origin, u, v, x, y)
                world_contour.append((float(world.x()), float(world.y()),
                                      float(world.z())))
            contours.append(world_contour)
    if not contours:
        return None, "The boolean result has no drawable contour."
    combined = dict(strokes[0])
    combined.update(id=uuid.uuid4().hex[:12], kind="boolean",
                    p=contours[0], contours=contours, closed=True,
                    smooth=0, soft=0.0)
    return combined, ""


class ScribbleMoveTool(_StickyTool):
    """Drag a mark — or, with Shift, its whole layer — where it belongs.

    Nothing is written until the release: the drag only previews the new
    position, so moving a sketch is one undo step and Esc puts it back.
    """

    name = "Croquis on model move"
    description = ("Drag a mark to move it, or drag on empty space to fence "
                   "several and move them together.")
    uses_snap = False
    qt_cursor = Qt.CursorShape.SizeAllCursor

    def __init__(self) -> None:
        super().__init__()
        self._ids: set = set()
        self._start = None
        self._last = None
        self._plane = None
        self._normal = None
        self._band_from = None      # screen corner of the fence being drawn
        self._band_to = None

    @property
    def _sel(self):
        scene = getattr(getattr(_APP, "viewport", None), "scene", None)
        if scene is None or _SELECTION["scene"] != id(scene):
            return set()
        return set(_SELECTION["ids"])

    @_sel.setter
    def _sel(self, ids):
        scene = getattr(getattr(_APP, "viewport", None), "scene", None)
        _SELECTION.update(scene=id(scene) if scene is not None else None,
                          ids=set(ids))

    # ---- Input -------------------------------------------------------------
    def drag_plane(self, viewport):
        """The plane through the grab point: the mark follows the cursor."""
        if self._plane is None or self._normal is None:
            return None
        return (self._plane, self._normal)

    def on_click(self, ctx: ToolContext) -> None:
        if _APP is None:
            return
        doc = document(_APP, ctx.viewport.scene)
        layer = active_layer(doc)
        found = _pick(ctx.viewport, layer["strokes"],
                      ctx.screen.x(), ctx.screen.y())
        if found is None:
            # empty space: this drag draws a selection fence instead
            self._forget()
            self._band_from = (ctx.screen.x(), ctx.screen.y())
            self._band_to = self._band_from
            return
        if layer["locked"]:
            _hint(ctx.viewport, "Croquis on model: this layer is locked", 6000)
            return
        whole = bool(_modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if whole:
            self._ids = {st["id"] for st in _visible_strokes(layer)}
            self._sel = set(self._ids)
        elif found in self._sel:
            self._ids = set(self._sel)      # the whole fence travels
        else:
            self._ids = {found}
            self._sel = {found}
        self._start = QVector3D(ctx.world)
        self._plane = QVector3D(ctx.world)
        self._normal = _view_normal(ctx.viewport) or QVector3D(0.0, 0.0, 1.0)
        self._last = QVector3D(ctx.world)

    def on_hover(self, ctx: ToolContext) -> None:
        if self._band_from is not None:
            self._band_to = (ctx.screen.x(), ctx.screen.y())
            ctx.viewport.update()
            return
        if not self._ids or self._start is None:
            return
        self._last = QVector3D(ctx.world)
        ctx.viewport.update()

    def on_release(self, viewport) -> None:
        if self._band_from is not None:
            self._close_band(viewport)
            return
        ids, delta = self._ids, self._delta()
        self._forget()
        if not ids or delta is None or delta.lengthSquared() < 1e-12 \
                or _APP is None:
            viewport.update()
            return
        try:
            doc = document(_APP, viewport.scene)
            layer = active_layer(doc)
            if layer["locked"] or not layer["visible"]:
                _hint(viewport, "Croquis on model: this layer is locked",
                      6000)
                viewport.update()
                return
            moved = 0
            for stroke in layer["strokes"]:
                if stroke.get("visible", True) and stroke["id"] in ids:
                    shift = lambda contour: [(x + delta.x(), y + delta.y(),
                                              z + delta.z())
                                             for x, y, z in contour]
                    stroke["p"] = shift(stroke["p"])
                    if stroke.get("contours"):
                        stroke["contours"] = [shift(contour)
                                              for contour in stroke["contours"]]
                    moved += 1
            if not moved:
                viewport.update()
                return
            _store(_APP, doc)
        except Exception as exc:  # noqa: BLE001
            _fail(viewport, exc)
            viewport.update()
            return
        _hint(viewport, f"Croquis on model: moved {moved} stroke"
                        f"{'s' if moved != 1 else ''}")
        viewport.update()

    def on_cancel(self, viewport) -> None:
        self._forget()
        if self._band_from is not None:
            self._band_from = None
            self._band_to = None
        self._sel = set()
        viewport.update()

    def transform_selection(self, mode: str, value: float = 1.0):
        """Transform the fenced selection around its centre in its local plane."""
        if _APP is None:
            return 0, "No document is open."
        viewport = getattr(_APP, "viewport", None)
        if viewport is None:
            return 0, "The viewport is unavailable."
        doc = document(_APP, viewport.scene)
        layer = active_layer(doc)
        if layer["locked"] or not layer["visible"]:
            return 0, "The active layer is hidden or locked."
        selected = [st for st in _visible_strokes(layer)
                    if st["id"] in self._sel]
        if not selected:
            return 0, "Select marks first with Move's selection fence."
        points = [point for st in selected
                  for contour in _stroke_world_contours(st)
                  for point in contour]
        if not points:
            return 0, "The selection has no points."
        centre = QVector3D(sum(p[0] for p in points) / len(points),
                           sum(p[1] for p in points) / len(points),
                           sum(p[2] for p in points) / len(points))
        normal = _normal_from_points(_stroke_world_contours(selected[0])[0])
        if normal is None:
            normal = QVector3D(0.0, 0.0, 1.0)
        u, v = _plane_basis(normal)
        angle = math.radians(float(value)) if mode == "rotate" else 0.0
        cosine, sine = math.cos(angle), math.sin(angle)
        factor = float(value) if mode == "scale" else 1.0
        if mode == "scale" and factor <= 0:
            return 0, "Scale must be greater than zero."

        def moved(raw):
            point = QVector3D(*raw)
            delta = point - centre
            x = QVector3D.dotProduct(delta, u)
            y = QVector3D.dotProduct(delta, v)
            z = QVector3D.dotProduct(delta, normal)
            if mode == "rotate":
                x, y = x * cosine - y * sine, x * sine + y * cosine
            elif mode == "scale":
                x, y = x * factor, y * factor
            elif mode == "mirror_h":
                x = -x
            elif mode == "mirror_v":
                y = -y
            else:
                raise ValueError(f"Unknown transform: {mode}")
            result = centre + u * x + v * y + normal * z
            return (float(result.x()), float(result.y()), float(result.z()))

        for stroke in selected:
            stroke["p"] = [moved(point) for point in stroke["p"]]
            if stroke.get("contours"):
                stroke["contours"] = [[moved(point) for point in contour]
                                      for contour in stroke["contours"]]
        _store(_APP, doc)
        viewport.update()
        return len(selected), ""

    # ---- Drawing -----------------------------------------------------------
    def draw_preview(self, viewport, painter: QPainter) -> None:
        """Draw a clear selection outline, handles, and the drag preview."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._band_from is not None:
            left, top, right, bottom = self._band_box()
            painter.setPen(QPen(QColor(_MOVE_ACCENT), 1.0,
                                Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(left, top, right - left, bottom - top))
            return
        if _APP is None:
            return
        delta = self._delta()
        moving = delta is not None and delta.lengthSquared() > 1e-12 \
            and bool(self._ids)
        if not moving and not self._sel:
            return
        layer = active_layer(document(_APP, viewport.scene))
        accent = QColor(_MOVE_ACCENT)
        selection_points = []
        painter.save()
        for stroke in layer["strokes"]:
            if not stroke.get("visible", True):
                continue
            selected = stroke["id"] in self._sel
            dragged = moving and stroke["id"] in self._ids
            if not selected and not dragged:
                continue
            world_contours = _stroke_world_contours(stroke)
            if dragged:
                world_contours = [[(x + delta.x(), y + delta.y(), z + delta.z())
                                   for x, y, z in contour]
                                  for contour in world_contours]
            projected = [_project(viewport, contour)
                         for contour in world_contours]
            if dragged:
                _paint_stroke(painter, projected[0], stroke,
                              extra_contours=projected[1:])
            if not selected:
                continue
            highlight = QColor(accent)
            highlight.setAlphaF(0.72)
            width = max(2.0, min(6.0, float(stroke.get("w") or 3.0) * 0.35 + 1.5))
            painter.setPen(QPen(highlight, width, Qt.PenStyle.SolidLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for contour in projected:
                for run in _runs(contour):
                    if len(run) >= 2:
                        path = _smooth_path(
                            run, bool(stroke.get("closed"))
                            and len(run) == len(contour),
                            int(stroke.get("smooth") or 0))
                        painter.drawPath(path)
                    elif len(run) == 1:
                        painter.drawEllipse(QPointF(run[0][0], run[0][1]),
                                            3.0, 3.0)
                    selection_points.extend((x, y) for x, y, front in run if front)
        painter.restore()
        if selection_points:
            xs = [point[0] for point in selection_points]
            ys = [point[1] for point in selection_points]
            box = QRectF(min(xs) - 6.0, min(ys) - 6.0,
                         max(1.0, max(xs) - min(xs) + 12.0),
                         max(1.0, max(ys) - min(ys) + 12.0))
            painter.setPen(QPen(accent, 1.2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(box)
            painter.setPen(QPen(QColor("#ffffff"), 1.0))
            painter.setBrush(QBrush(accent))
            for x, y in ((box.left(), box.top()), (box.right(), box.top()),
                         (box.right(), box.bottom()), (box.left(), box.bottom())):
                painter.drawRect(QRectF(x - 3.0, y - 3.0, 6.0, 6.0))

    # ---- Internals ---------------------------------------------------------
    def _delta(self):
        """How far the grab point has travelled, on the grid when it is on."""
        if self._start is None or self._last is None:
            return None
        delta = QVector3D(self._last - self._start)
        grid = float(_SNAP.get("grid") or 0.0)
        if grid > 0:
            delta = QVector3D(round(delta.x() / grid) * grid,
                              round(delta.y() / grid) * grid,
                              round(delta.z() / grid) * grid)
        return delta

    def _band_box(self):
        """The fence as (left, top, right, bottom) pixels, whatever the drag."""
        x0, y0 = self._band_from or (0.0, 0.0)
        x1, y1 = self._band_to or (x0, y0)
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    def _close_band(self, viewport) -> None:
        """Take every mark the fence touched; Shift adds to what was picked."""
        left, top, right, bottom = self._band_box()
        additive = bool(_modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._band_from = None
        self._band_to = None
        if right - left < 2.0 and bottom - top < 2.0:
            if not additive:
                self._sel = set()          # a plain click empties the fence
            viewport.update()
            return
        if _APP is None:
            return
        layer = active_layer(document(_APP, viewport.scene))
        picked = set()
        for stroke in layer["strokes"]:
            if not stroke.get("visible", True):
                continue
            hit = False
            for contour in _stroke_world_contours(stroke):
                pts = _project(viewport, contour)
                if len(pts) == 1 and pts[0][2] and _inside_box(
                        pts[0][0], pts[0][1], left, top, right, bottom):
                    hit = True
                    break
                for (x0, y0, f0), (x1, y1, f1) in zip(pts, pts[1:]):
                    if f0 and f1 and _segment_meets_box(
                            x0, y0, x1, y1, left, top, right, bottom):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                picked.add(stroke["id"])
        self._sel = (self._sel | picked) if additive else picked
        viewport.update()
        if self._sel:
            count = len(self._sel)
            _hint(viewport, f"Croquis on model: {count} stroke"
                            f"{'s' if count != 1 else ''} selected")
        else:
            _hint(viewport, "Croquis on model: nothing inside the rectangle")

    def _forget(self) -> None:
        self._ids = set()
        self._start = None
        self._last = None
        self._plane = None
        self._normal = None


class ScribbleSelectTool(ScribbleMoveTool):
    """Select marks by click or a fence, without starting a drag move."""

    name = "Croquis on model selection"
    description = "Click a mark or drag a fence to select it before editing."

    def on_click(self, ctx: ToolContext) -> None:
        if _APP is None:
            return
        layer = active_layer(document(_APP, ctx.viewport.scene))
        found = _pick(ctx.viewport, layer["strokes"],
                      ctx.screen.x(), ctx.screen.y())
        additive = bool(_modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if found is not None:
            self._forget()
            self._sel = (self._sel | {found}) if additive else {found}
            _hint(ctx.viewport, f"Croquis on model: {len(self._sel)} selected")
            ctx.viewport.update()
            return
        self._forget()
        self._band_from = (ctx.screen.x(), ctx.screen.y())
        self._band_to = self._band_from

    def on_hover(self, ctx: ToolContext) -> None:
        if self._band_from is not None:
            self._band_to = (ctx.screen.x(), ctx.screen.y())
            ctx.viewport.update()

    def on_release(self, viewport) -> None:
        if self._band_from is not None:
            self._close_band(viewport)


def _selected_stroke(doc, stroke_id):
    for layer in doc["layers"]:
        for stroke in layer["strokes"]:
            if stroke["id"] == stroke_id:
                return layer, stroke
    return None, None


def _point_plane(stroke, viewport):
    points = _stroke_world_contours(stroke)
    if not points or not points[0]:
        return None
    origin = QVector3D(*points[0][0])
    normal = stroke.get("normal")
    normal = QVector3D(*normal) if normal else _normal_from_points(points[0])
    if normal is None or normal.length() < 1e-9:
        normal = _view_normal(viewport)
    if normal is None or normal.length() < 1e-9:
        return None
    return origin, normal.normalized()


def _move_stroke_points(stroke, indices, delta):
    """Return a copy with selected contour vertices translated."""
    moved = dict(stroke)
    contours = stroke.get("contours") or []
    if contours:
        moved["contours"] = [list(contour) for contour in contours]
        for contour_index, point_index in indices:
            if (0 <= contour_index < len(moved["contours"])
                    and 0 <= point_index < len(moved["contours"][contour_index])):
                x, y, z = moved["contours"][contour_index][point_index]
                moved["contours"][contour_index][point_index] = (
                    x + delta.x(), y + delta.y(), z + delta.z())
        moved["p"] = list(moved["contours"][0])
    else:
        moved["p"] = list(stroke["p"])
        for contour_index, point_index in indices:
            if contour_index == 0 and 0 <= point_index < len(moved["p"]):
                x, y, z = moved["p"][point_index]
                moved["p"][point_index] = (
                    x + delta.x(), y + delta.y(), z + delta.z())
    return moved


class ScribblePointEditTool(_StickyTool):
    """Select stroke vertices with a window, then drag to move those vertices."""

    name = "Croquis on model point editing"
    description = ("Drag a window to select points; drag again to move them "
                   "in the drawing plane. Shift adds points.")
    uses_snap = False
    qt_cursor = Qt.CursorShape.CrossCursor

    def __init__(self) -> None:
        super().__init__()
        self._stroke_id = None
        self._band_from = None
        self._band_to = None
        self._additive = False
        self._move_start = None
        self._move_last = None
        self._move_indices = set()

    def _selected_id(self, scene):
        if (_SELECTION["scene"] != id(scene)
                or len(_SELECTION["ids"]) != 1):
            return None
        return next(iter(_SELECTION["ids"]))

    def _get_stroke(self, scene):
        if _APP is None:
            return None, None
        return _selected_stroke(document(_APP, scene),
                                self._selected_id(scene))

    def drag_plane(self, viewport):
        stroke = self._get_stroke(viewport.scene)[1]
        if stroke is None:
            return None
        plane = _point_plane(stroke, viewport)
        return plane

    def on_click(self, ctx: ToolContext) -> None:
        if _APP is None:
            return
        selected_id = self._selected_id(ctx.viewport.scene)
        if selected_id is None:
            _hint(ctx.viewport, "Select one drawing in Layers before editing points.")
            return
        layer, stroke = _selected_stroke(document(_APP, ctx.viewport.scene),
                                         selected_id)
        if stroke is None or not stroke.get("visible", True) or not layer["visible"]:
            _hint(ctx.viewport, "The selected drawing is hidden.")
            return
        if layer["locked"]:
            _hint(ctx.viewport, "The selected layer is locked.", 4000)
            return
        if not any(_stroke_world_contours(stroke)):
            _hint(ctx.viewport, "The selected drawing has no editable points.")
            return

        same_selection = (
            _POINT_SELECTION["scene"] == id(ctx.viewport.scene)
            and _POINT_SELECTION["stroke"] == selected_id
            and bool(_POINT_SELECTION["indices"]))
        additive = bool(_modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._stroke_id = selected_id
        if same_selection and not additive:
            self._move_indices = set(_POINT_SELECTION["indices"])
            self._move_start = QVector3D(ctx.world)
            self._move_last = QVector3D(ctx.world)
        else:
            self._band_from = (ctx.screen.x(), ctx.screen.y())
            self._band_to = self._band_from
            self._additive = additive
        ctx.viewport.update()

    def on_hover(self, ctx: ToolContext) -> None:
        if self._band_from is not None:
            self._band_to = (ctx.screen.x(), ctx.screen.y())
            ctx.viewport.update()
        elif self._move_start is not None:
            self._move_last = QVector3D(ctx.world)
            ctx.viewport.update()

    def on_release(self, viewport) -> None:
        if self._band_from is not None:
            self._select_points(viewport)
        elif self._move_start is not None:
            self._commit_move(viewport)

    def on_cancel(self, viewport) -> None:
        self._forget()
        _POINT_SELECTION.update(scene=None, stroke=None, indices=set())
        viewport.update()

    def draw_preview(self, viewport, painter: QPainter) -> None:
        if _APP is None:
            return
        scene_id = id(viewport.scene)
        stroke_id = self._stroke_id
        if (stroke_id is None and _POINT_SELECTION["scene"] == scene_id
                and _SELECTION["scene"] == scene_id
                and _POINT_SELECTION["stroke"] in _SELECTION["ids"]):
            stroke_id = _POINT_SELECTION["stroke"]
        if stroke_id is None:
            return
        stroke = _selected_stroke(document(_APP, viewport.scene), stroke_id)[1]
        if stroke is None:
            return
        indices = (_POINT_SELECTION["indices"]
                   if _POINT_SELECTION["scene"] == scene_id
                   and _POINT_SELECTION["stroke"] == stroke_id else set())
        delta = (self._move_last - self._move_start
                 if self._move_start is not None and self._move_last is not None
                 else QVector3D())
        preview = (_move_stroke_points(stroke, indices, delta)
                   if indices and delta.lengthSquared() > 1e-12 else stroke)
        contours = [_project(viewport, contour)
                    for contour in _stroke_world_contours(preview)]
        moving = bool(indices) and delta.lengthSquared() > 1e-12
        if contours and moving:
            _paint_stroke(painter, contours[0], preview,
                          extra_contours=contours[1:])
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#ffffff"), 1.0))
        painter.setBrush(QBrush(QColor(_MOVE_ACCENT)))
        for contour_index, contour in enumerate(_stroke_world_contours(preview)):
            projected = _project(viewport, contour)
            for point_index, (x, y, front) in enumerate(projected):
                if front and (contour_index, point_index) in indices:
                    painter.drawEllipse(QPointF(x, y), 4.0, 4.0)
        painter.restore()
        if self._band_from is not None:
            x0, y0 = self._band_from
            x1, y1 = self._band_to or self._band_from
            left, right = min(x0, x1), max(x0, x1)
            top, bottom = min(y0, y1), max(y0, y1)
            painter.setPen(QPen(QColor(_MOVE_ACCENT), 1.0,
                                Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(left, top, right - left, bottom - top))

    def _select_points(self, viewport) -> None:
        start, end = self._band_from, self._band_to or self._band_from
        stroke_id = self._stroke_id
        additive = self._additive
        self._forget()
        if start is None or end is None or _APP is None:
            return
        left, right = min(start[0], end[0]), max(start[0], end[0])
        top, bottom = min(start[1], end[1]), max(start[1], end[1])
        if right - left < 2.0 and bottom - top < 2.0:
            if not additive:
                _POINT_SELECTION.update(scene=None, stroke=None, indices=set())
            viewport.update()
            return
        stroke = _selected_stroke(document(_APP, viewport.scene), stroke_id)[1]
        if stroke is None:
            viewport.update()
            return
        selected = set()
        for contour_index, contour in enumerate(_stroke_world_contours(stroke)):
            for point_index, (x, y, front) in enumerate(_project(viewport, contour)):
                if front and _inside_box(x, y, left, top, right, bottom):
                    selected.add((contour_index, point_index))
        if additive and _POINT_SELECTION["scene"] == id(viewport.scene) \
                and _POINT_SELECTION["stroke"] == stroke_id:
            selected |= set(_POINT_SELECTION["indices"])
        _POINT_SELECTION.update(scene=id(viewport.scene), stroke=stroke_id,
                                indices=selected)
        _hint(viewport, f"Croquis on model: {len(selected)} point(s) selected"
              if selected else "Croquis on model: no points inside the window")
        viewport.update()

    def _commit_move(self, viewport) -> None:
        stroke_id = self._stroke_id
        indices = set(self._move_indices)
        delta = (self._move_last - self._move_start
                 if self._move_last is not None and self._move_start is not None
                 else None)
        self._forget()
        if not indices or delta is None or delta.lengthSquared() < 1e-12 \
                or _APP is None:
            viewport.update()
            return
        try:
            doc = document(_APP, viewport.scene)
            layer, stroke = _selected_stroke(doc, stroke_id)
            if stroke is None:
                _hint(viewport, "The selected drawing no longer exists.", 4000)
                return
            if layer["locked"] or not layer["visible"] \
                    or not stroke.get("visible", True):
                _hint(viewport, "The selected drawing is hidden or locked.", 4000)
                return
            moved = _move_stroke_points(stroke, indices, delta)
            layer["strokes"] = [
                moved if item["id"] == stroke_id else item
                for item in layer["strokes"]]
            _store(_APP, doc)
        except Exception as exc:  # noqa: BLE001
            _fail(viewport, exc)
            viewport.update()
            return
        _hint(viewport, f"Croquis on model: moved {len(indices)} point(s)")
        viewport.update()

    def _forget(self) -> None:
        self._stroke_id = None
        self._band_from = None
        self._band_to = None
        self._additive = False
        self._move_start = None
        self._move_last = None
        self._move_indices = set()


def _zoom_window(viewport, bounds) -> None:
    """Frame a screen-space rectangle, respecting the camera's current view."""
    camera = getattr(viewport, "camera", None)
    if camera is None:
        raise RuntimeError("The viewport camera is unavailable.")
    x0, y0, x1, y1 = bounds
    width, height = float(viewport.width()), float(viewport.height())
    rect_width, rect_height = x1 - x0, y1 - y0
    if rect_width < 4.0 or rect_height < 4.0:
        _hint(viewport, "Drag a larger window to zoom.")
        return
    if width <= 0.0 or height <= 0.0:
        raise RuntimeError("The viewport has no usable size.")

    forward = _view_normal(viewport)
    if forward is None:
        raise RuntimeError("The camera direction is unavailable.")
    up_hint = getattr(camera, "up", QVector3D(0.0, 0.0, 1.0))
    if callable(up_hint):
        up_hint = up_hint()
    up_hint = QVector3D(up_hint)
    right = QVector3D.crossProduct(forward, up_hint)
    if right.length() < 1e-8:
        right = QVector3D.crossProduct(
            forward, QVector3D(0.0, 1.0, 0.0))
    if right.length() < 1e-8:
        raise RuntimeError("Could not determine the camera screen axes.")
    right.normalize()
    up = QVector3D.crossProduct(right, forward).normalized()

    target = QVector3D(camera.target)
    target_xyz = (target.x(), target.y(), target.z())
    projected = _project(viewport, [
        target_xyz,
        (target.x() + right.x(), target.y() + right.y(), target.z() + right.z()),
        (target.x() + up.x(), target.y() + up.y(), target.z() + up.z()),
    ])
    if not all(point[2] for point in projected):
        raise RuntimeError("Could not project the zoom window onto the camera.")
    p0, pr, pu = projected
    a, b = pr[0] - p0[0], pu[0] - p0[0]
    c, d = pr[1] - p0[1], pu[1] - p0[1]
    determinant = a * d - b * c
    if abs(determinant) < 1e-10:
        raise RuntimeError("The camera projection cannot frame this window.")
    dx_screen = (x0 + x1) * 0.5 - p0[0]
    dy_screen = (y0 + y1) * 0.5 - p0[1]
    right_amount = (dx_screen * d - b * dy_screen) / determinant
    up_amount = (a * dy_screen - dx_screen * c) / determinant
    shift = right * right_amount + up * up_amount

    distance = float(camera.distance)
    fov = float(getattr(camera, "fov_deg", 45.0))
    if distance <= 0.0 or not 0.0 < fov < 180.0:
        raise RuntimeError("The camera zoom settings are invalid.")
    factor = max(rect_width / width, rect_height / height) * 1.08
    new_target = target + shift
    try:
        floor = float(camera.min_distance(new_target))
    except Exception:  # noqa: BLE001 — older camera API
        floor = 0.01
    camera.target = new_target
    if hasattr(camera, "oblique_pivot"):
        camera.oblique_pivot = QVector3D(camera.oblique_pivot) + shift
    camera.distance = max(0.01, min(10000.0, distance * factor))
    camera.distance = max(camera.distance, floor)
    viewport.update()


def _fit_camera_points(viewport, points) -> bool:
    points = [point for point in points
              if all(math.isfinite(value) for value in point)]
    if not points:
        return False
    camera = getattr(viewport, "camera", None)
    fit_to = getattr(camera, "fit_to", None)
    if not callable(fit_to):
        raise RuntimeError("The camera does not support framing drawings.")
    minimum = [min(point[axis] for point in points) for axis in range(3)]
    maximum = [max(point[axis] for point in points) for axis in range(3)]
    for axis in range(3):
        if maximum[axis] - minimum[axis] < 0.01:
            centre = (minimum[axis] + maximum[axis]) * 0.5
            minimum[axis], maximum[axis] = centre - 0.005, centre + 0.005
    fit_to(QVector3D(*minimum), QVector3D(*maximum), 1.3)
    viewport.update()
    return True


def _zoom_extents(app, viewport) -> None:
    """Fit the camera to all individually and layer-visible annotations."""
    if not _SHOW["visible"]:
        _hint(viewport, "Show annotations before using Zoom Extents.")
        return
    points = [
        point
        for stroke in all_strokes(app, viewport.scene)
        for contour in _stroke_world_contours(stroke)
        for point in contour
    ]
    if not _fit_camera_points(viewport, points):
        _hint(viewport, "No visible Croquis annotations to frame.")


def _zoom_object(app, viewport) -> None:
    scene = getattr(viewport, "scene", None)
    if scene is None or _SELECTION["scene"] != id(scene) \
            or not _SELECTION["ids"]:
        _hint(viewport, "Select a drawing in Layers before using Zoom Object.")
        return
    selected = [
        stroke
        for layer in document(app, scene)["layers"]
        for stroke in layer["strokes"]
        if stroke["id"] in _SELECTION["ids"]
    ]
    points = [
        point
        for stroke in selected
        for contour in _stroke_world_contours(stroke)
        for point in contour
    ]
    if not _fit_camera_points(viewport, points):
        _hint(viewport, "The selected drawing has no points to frame.")


def _zoom_extents_action(app) -> None:
    viewport = getattr(app, "viewport", None)
    if viewport is None:
        return
    try:
        _zoom_extents(app, viewport)
    except Exception as exc:  # noqa: BLE001 — surfaced in the viewport
        _fail(viewport, exc)


def _zoom_object_action(app) -> None:
    viewport = getattr(app, "viewport", None)
    if viewport is None:
        return
    try:
        _zoom_object(app, viewport)
    except Exception as exc:  # noqa: BLE001 — surfaced in the viewport
        _fail(viewport, exc)


class ScribbleZoomWindowTool(_StickyTool):
    """Zoom to a viewport rectangle."""

    name = "Croquis on model zoom window"
    description = "Drag a rectangle to zoom the viewport into that area."
    uses_snap = False
    qt_cursor = Qt.CursorShape.CrossCursor

    def __init__(self) -> None:
        super().__init__()
        self._start = None
        self._end = None

    def on_click(self, ctx: ToolContext) -> None:
        self._start = (ctx.screen.x(), ctx.screen.y())
        self._end = self._start
        ctx.viewport.update()

    def on_hover(self, ctx: ToolContext) -> None:
        if self._start is not None:
            self._end = (ctx.screen.x(), ctx.screen.y())
            ctx.viewport.update()

    def on_release(self, viewport) -> None:
        if self._start is None or self._end is None:
            return
        x0, x1 = sorted((self._start[0], self._end[0]))
        y0, y1 = sorted((self._start[1], self._end[1]))
        self._forget()
        try:
            _zoom_window(viewport, (x0, y0, x1, y1))
        except Exception as exc:  # noqa: BLE001 — surfaced in the viewport
            _fail(viewport, exc)
        viewport.update()

    def on_cancel(self, viewport) -> None:
        self._forget()
        viewport.update()

    def draw_preview(self, viewport, painter: QPainter) -> None:
        if self._start is None or self._end is None:
            return
        x0, x1 = sorted((self._start[0], self._end[0]))
        y0, y1 = sorted((self._start[1], self._end[1]))
        painter.setPen(QPen(QColor(_MOVE_ACCENT), 1.2,
                            Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(x0, y0, x1 - x0, y1 - y0))

    def _forget(self) -> None:
        self._start = None
        self._end = None


# ---------------------------------------------------------------------------
# Export: PNG, SVG, the clipboard, the sheet composer
# ---------------------------------------------------------------------------

def _viewport_size(viewport):
    try:
        w, h = int(viewport.width()), int(viewport.height())
        if w > 8 and h > 8:
            return w, h
    except Exception:  # noqa: BLE001
        pass
    return 1280, 800


#: The names a viewport (and the app) might hand a rendered frame over
#: under; the first one that answers with a picture that has pixels wins.
FRAME_GETTERS = (
    "render_to_image", "render_frame", "frame_image", "current_image",
    "grab_framebuffer", "snapshot", "capture", "screenshot",
    "to_image", "to_pixmap", "grab",
)
APP_FRAME_GETTERS = (
    "render_viewport_image", "viewport_image", "grab_viewport",
    "render_current_view",
)


def _as_image(got):
    """Anything Qt-shaped, as a QImage worth using, or ``None``."""
    if isinstance(got, QImage) and not got.isNull():
        return got
    if isinstance(got, QPixmap) and not got.isNull():
        return got.toImage()
    return None


def _frame_ok(image) -> bool:
    """Whether a grabbed frame actually holds a picture.

    A widget grab on an OpenGL viewport often comes back fully transparent:
    it looks like a frame and is an empty one. Sampling a grid of pixels
    tells the two apart, so the export can say what happened instead of
    writing a blank strip of sky.
    """
    if image is None or image.isNull():
        return False
    try:
        width, height = image.width(), image.height()
        if width < 4 or height < 4:
            return False
        for i in range(5):
            for j in range(5):
                x = int(width * (i + 0.5) / 5.0)
                y = int(height * (j + 0.5) / 5.0)
                if image.pixelColor(x, y).alpha() > 0:
                    return True
        return False
    except Exception:  # noqa: BLE001 — cannot tell: trust the picture
        return True


def _grab_view(viewport, app=None):
    """The viewport's own frame, if it can hand one over."""
    owners = [viewport]
    if app is not None:
        owners.append(app)
    for owner in owners:
        names = FRAME_GETTERS if owner is viewport else APP_FRAME_GETTERS
        for name in names:
            func = getattr(owner, name, None)
            if not callable(func):
                continue
            try:
                got = _as_image(func())
            except Exception:  # noqa: BLE001 — try the next name
                continue
            if got is not None and _frame_ok(got):
                return got
    return None


def render_image(app=None, viewport=None, scene=None, size=None,
                 scale=None, background=None) -> QImage:
    """The annotations as an image, ready for a file or the sheet composer."""
    app = _APP if app is None else app
    viewport = viewport if viewport is not None else getattr(app, "viewport",
                                                             None)
    if scene is None:
        scene = getattr(viewport, "scene", None) or getattr(app, "scene", None)
    width, height = size if size else _viewport_size(viewport)
    factor = max(0.25, float(scale if scale is not None
                              else _EXPORT["scale"]))
    mode = background if background is not None else _EXPORT["background"]

    image = QImage(max(1, int(width * factor)), max(1, int(height * factor)),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    _LAST["frame"] = False
    if not scene or viewport is None:
        return image

    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if mode == "white":
            painter.fillRect(image.rect(), QColor("#ffffff"))
        elif mode == "black":
            painter.fillRect(image.rect(), QColor("#20232a"))
        elif mode == "view":
            shot = _grab_view(viewport, app)
            _LAST["frame"] = shot is not None
            if shot is not None:
                painter.drawImage(image.rect(), shot)
        painter.scale(factor, factor)
        _paint_layers(painter, viewport, document(app, scene))
    finally:
        painter.end()
    return image


def _sanitize(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in name)
    return keep.strip("-") or "layer"


def _svg_gradient(stroke, pts, ident: str, color_name: str):
    """``(fill value, defs)`` — a gradient when the stroke asks for one."""
    alpha = float(stroke.get("fill") or 0.0)
    second = str(stroke.get("fc2") or "")
    mode = str(stroke.get("fg") or "solid")
    if mode == "solid" or mode not in FILL_GRADIENT_NAMES or not second \
            or len(pts) < 3:
        return "", ""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if mode == "radial":
        centre = QPointF((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        radius = max(1.0, math.hypot(x1 - x0, y1 - y0) / 2.0)
        head = (f'<radialGradient id="{ident}" gradientUnits="userSpaceOnUse"'
                f' cx="{centre.x():.2f}" cy="{centre.y():.2f}"'
                f' r="{radius:.2f}">')
        tail = "</radialGradient>"
    else:
        head = (f'<linearGradient id="{ident}" gradientUnits="userSpaceOnUse"'
                f' x1="{x0:.2f}" y1="{y0:.2f}"'
                f' x2="{x1:.2f}" y2="{y1:.2f}">')
        tail = "</linearGradient>"
    stops = (f'<stop offset="0" stop-color="{color_name}"'
             f' stop-opacity="{alpha:.3f}"/>'
             f'<stop offset="1" stop-color="{second}"'
             f' stop-opacity="{alpha:.3f}"/>')
    return f"url(#{ident})", head + stops + tail


def _svg_hatch_def(ident: str, color: str, pattern: str) -> str:
    """A small user-space SVG pattern for transparent hatch fills."""
    lines = "M-2 2 L2 -2 M0 8 L8 0 M6 10 L10 6"
    if pattern == "crosshatch":
        lines += " M-2 6 L2 10 M0 0 L8 8 M6 -2 L10 2"
    return (f'<pattern id="{ident}" patternUnits="userSpaceOnUse" '
            f'width="8" height="8">'
            f'<path d="{lines}" fill="none" stroke="{color}" '
            f'stroke-width="1"/></pattern>')


def svg_text(app=None, viewport=None, scene=None, size=None,
             scale=None, background=None) -> str:
    """The annotations as SVG — the shapes stay sharp at any zoom."""
    app = _APP if app is None else app
    viewport = viewport if viewport is not None else getattr(app, "viewport",
                                                             None)
    if scene is None:
        scene = getattr(viewport, "scene", None) or getattr(app, "scene", None)
    width, height = size if size else _viewport_size(viewport)
    factor = max(0.25, float(scale if scale is not None else 1.0))
    mode = background if background is not None else _EXPORT["background"]

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<svg xmlns="http://www.w3.org/2000/svg" '
           f'width="{int(width * factor)}" height="{int(height * factor)}" '
           f'viewBox="0 0 {int(width * factor)} {int(height * factor)}">']
    if mode == "white":
        out.append('<rect width="100%" height="100%" fill="#ffffff"/>')
    elif mode == "black":
        out.append('<rect width="100%" height="100%" fill="#20232a"/>')
    if scene and viewport is not None:
        out.append(f'<g transform="scale({factor})">')
        for layer in document(app, scene)["layers"]:
            if not layer["visible"] or not layer["strokes"]:
                continue
            out.append(f'<g id="{_sanitize(layer["name"])}" '
                       f'opacity="{float(layer["opacity"]):.3f}">')
            for stroke in layer["strokes"]:
                if not stroke.get("visible", True):
                    continue
                projected = [_project(viewport, contour)
                             for contour in _stroke_world_contours(stroke)]
                if not projected or not projected[0]:
                    continue
                pts = projected[0]
                color = _stroke_color(stroke).name()
                width_px = max(0.5, float(stroke["w"]))
                if str(stroke.get("kind") or "") == "free":
                    amount = _pen_wobble_amount(stroke, width_px)
                    if amount > 0.05:
                        pts = _pen_wobble(
                            pts, amount,
                            _hash_text(stroke.get("id") or "draft"))
                        projected[0] = pts
                flat = [point for contour in projected for point in contour]
                fill = float(stroke["fill"] or 0.0)
                softness = float(stroke.get("soft") or 0.0)
                if softness > 0.01:
                    base = _stroke_alpha(stroke)
                    smoothness = int(stroke.get("smooth") or 2)
                    seed = _hash_text(stroke.get("id") or "draft")
                    amount = _jitter_amount(stroke, width_px)
                    shells = _soft_shells(width_px, softness, base,
                                          str(stroke.get("pf") or "even"))
                    for shell, (shell_w, shell_a) in enumerate(shells):
                        if shell_a <= 0.004:
                            continue
                        shell_color = _shell_color(stroke, color, shell,
                                                   len(shells)).name()
                        for run in _runs(_jitter(pts, amount, seed, shell)):
                            if len(run) < 2:
                                continue
                            data = _path_data(_smooth_path(run, False,
                                                           smoothness))
                            if data:
                                out.append(
                                    f'<path d="{data}" fill="none" '
                                    f'stroke="{shell_color}" '
                                    f'stroke-opacity="{shell_a:.3f}" '
                                    f'stroke-width="{shell_w:.2f}" '
                                    f'stroke-linecap="round" '
                                    f'stroke-linejoin="round"/>')
                    continue
                wanted = str(stroke.get("fc") or "")
                explicit_fill = QColor(wanted).isValid() if wanted else False
                all_front = all(point[2] for point in flat)
                if fill > 0.01 and len(flat) >= 3 and all_front and \
                        (_has_stroke_color(stroke) or explicit_fill):
                    compound = _compound_path(projected,
                                              int(stroke.get("smooth") or 0))
                    data = _path_data(compound)
                    if data:
                        fill_color = QColor(wanted).name() if explicit_fill else color
                        pattern = str(stroke.get("fp") or "solid")
                        ident = "fill-" + _sanitize(str(stroke.get("id") or "x"))
                        if pattern in ("diagonal", "crosshatch"):
                            out.append(_svg_hatch_def(ident, fill_color, pattern))
                            out.append(
                                f'<path d="{data}" fill="url(#{ident})" '
                                f'fill-opacity="{fill:.3f}" '
                                f'fill-rule="evenodd"/>')
                        else:
                            value, defs = _svg_gradient(stroke, flat, ident,
                                                        fill_color)
                            if defs:
                                out.append(defs)
                                out.append(
                                    f'<path d="{data}" fill="{value}" '
                                    f'fill-rule="evenodd"/>')
                            else:
                                out.append(
                                    f'<path d="{data}" fill="{fill_color}" '
                                    f'fill-opacity="{fill:.3f}" '
                                    f'fill-rule="evenodd"/>')
                if not _has_stroke_color(stroke):
                    continue
                for contour in projected:
                    for run in _runs(contour):
                        if len(run) < 2:
                            continue
                        data = _path_data(_smooth_path(
                            run, bool(stroke["closed"]) and len(run) == len(contour),
                            int(stroke["smooth"])))
                        if data:
                            out.append(f'<path d="{data}" fill="none" '
                                       f'stroke="{color}" stroke-opacity="0.95" '
                                       f'stroke-width="{width_px:.2f}" '
                                       f'stroke-linecap="round" '
                                       f'stroke-linejoin="round"/>')
            out.append("</g>")
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


def _suggest_path(app, extension: str) -> str:
    base = ""
    for name in ("document_path", "file_path", "path"):
        got = getattr(app, name, None)
        if isinstance(got, str) and got:
            base = got
            break
    if base:
        return os.path.splitext(base)[0] + "-scribble." + extension
    folder = os.path.expanduser("~")
    return os.path.join(folder, "scribble." + extension)


def export_png(app=None, parent=None) -> str:
    """Ask for a file and write the annotations into it as PNG."""
    app = _APP if app is None else app
    path, _f = QFileDialog.getSaveFileName(
        parent, "Export Croquis on model as PNG", _suggest_path(app, "png"),
        "PNG image (*.png);;All files (*)")
    if not path:
        return ""
    if not path.lower().endswith(".png"):
        path += ".png"
    image = render_image(app)
    if image.isNull() or not image.save(path, "PNG"):
        QMessageBox.warning(parent, "Croquis on model",
                            f"Could not write {path}.")
        return ""
    if _EXPORT["background"] == "view" and not _LAST["frame"]:
        QMessageBox.information(
            parent, "Croquis on model",
            "The viewport did not hand over its frame, so the image holds "
            "only the annotations. If the model is missing, export on a "
            "transparent background and put the two together outside "
            "IngeTrazo.")
    return path


def export_svg(app=None, parent=None) -> str:
    app = _APP if app is None else app
    path, _f = QFileDialog.getSaveFileName(
        parent, "Export Croquis on model as SVG", _suggest_path(app, "svg"),
        "SVG drawing (*.svg);;All files (*)")
    if not path:
        return ""
    if not path.lower().endswith(".svg"):
        path += ".svg"
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(svg_text(app))
    except OSError as exc:
        QMessageBox.warning(parent, "Croquis on model",
                            f"Could not write {path}: {exc}")
        return ""
    return path


def copy_to_clipboard(app=None) -> bool:
    """Put the annotations on the clipboard, ready to paste elsewhere."""
    app = _APP if app is None else app
    image = render_image(app)
    if image.isNull():
        return False
    QApplication.clipboard().setImage(image)
    return True


def describe_sheet_api(app) -> str:
    """The sheet- and image-shaped names the host actually offers.

    The panel shows this when no hook is found, so the bridge can be
    pointed at the right method without reading the API's source.
    """
    if app is None:
        return ""
    owners = (
        ("app", app),
        ("sheet_composer", getattr(app, "sheet_composer", None)),
        ("sheet", getattr(app, "sheet", None)),
        ("composer", getattr(app, "composer", None)),
        ("sheets", getattr(app, "sheets", None)),
    )
    seen = set()
    found = []
    for prefix, owner in owners:
        if owner is None or id(owner) in seen:
            continue
        seen.add(id(owner))
        try:
            names = dir(owner)
        except Exception:  # noqa: BLE001
            continue
        picking = []
        for name in names:
            if name.startswith("_"):
                continue
            low = name.lower()
            if not ("sheet" in low or "image" in low or "picture" in low
                    or "pixmap" in low):
                continue
            try:
                value = getattr(owner, name)
            except Exception:  # noqa: BLE001
                continue
            picking.append(name + ("()" if callable(value) else ""))
        if picking:
            found.append(f"{prefix}: " + ", ".join(sorted(picking)))
    return " | ".join(found)


def _register_sheet(app) -> str:
    """Best-effort registration of the drawing as a sheet image provider.

    IngeTrazo builds may not expose an image-provider hook to extensions.
    When no compatible hook is available, users can export a PNG and add it
    through the Sheet Composer's Image tool.
    """
    if _SHEET["hooked"]:
        return _SHEET["method"]
    if app is None:
        return ""
    provider = lambda *_a, **_k: render_image(app)  # noqa: E731
    names = ("register_sheet_provider", "add_sheet_provider",
             "register_sheet_renderer", "add_sheet_renderer",
             "add_sheet_source")
    for name in names:
        func = getattr(app, name, None)
        if not callable(func):
            continue
        for args in (("Croquis on model", provider), (provider,)):
            try:
                func(*args)
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                _SHEET["error"] = f"{type(exc).__name__}: {exc}"
                continue
            _SHEET.update(hooked=True, method=name)
            return name
    _SHEET["api"] = describe_sheet_api(app)
    return ""


# ---------------------------------------------------------------------------
# The side-tray tab
# ---------------------------------------------------------------------------

def _swatch(color: str) -> QIcon:
    """A flat colour square, or a slash for «None»."""
    pix = QPixmap(18, 18)
    if str(color or "").strip().lower() == "none":
        pix.fill(QColor("#ffffff"))
        painter = QPainter(pix)
        painter.setPen(QPen(QColor("#6b7280"), 2))
        painter.drawLine(3, 15, 15, 3)
        painter.end()
        return QIcon(pix)
    got = QColor(color) if color else QColor(_PEN["color"])
    if not got.isValid():
        got = QColor(PALETTE[0][1])
    pix.fill(got)
    return QIcon(pix)


class DrawingPlaneDialog(QDialog):
    """Choose a plane origin and its normal using yaw/pitch angles."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New drawing plane")
        form = QFormLayout(self)
        self.coordinates = []
        for label in ("Origin X (m):", "Origin Y (m):", "Origin Z (m):"):
            field = QDoubleSpinBox()
            field.setRange(-1000000.0, 1000000.0)
            field.setDecimals(4)
            field.setValue(0.0)
            self.coordinates.append(field)
            form.addRow(label, field)
        self.yaw = QDoubleSpinBox()
        self.yaw.setRange(-180.0, 180.0)
        self.yaw.setDecimals(1)
        self.yaw.setSuffix("°")
        form.addRow("Normal yaw:", self.yaw)
        self.pitch = QDoubleSpinBox()
        self.pitch.setRange(-90.0, 90.0)
        self.pitch.setDecimals(1)
        self.pitch.setSuffix("°")
        form.addRow("Normal pitch:", self.pitch)
        self.infinite = QCheckBox("Infinite plane (guide fills the view)")
        self.infinite.setChecked(True)
        self.infinite.toggled.connect(lambda checked: self.size.setEnabled(not checked))
        form.addRow(self.infinite)
        self.size = QDoubleSpinBox()
        self.size.setRange(0.1, 1000000.0)
        self.size.setDecimals(2)
        self.size.setSuffix(" m")
        self.size.setValue(10.0)
        self.size.setEnabled(False)
        form.addRow("Square side length:", self.size)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def plane_values(self):
        origin = tuple(field.value() for field in self.coordinates)
        yaw, pitch = math.radians(self.yaw.value()), math.radians(self.pitch.value())
        normal = (math.cos(pitch) * math.cos(yaw),
                  math.cos(pitch) * math.sin(yaw), math.sin(pitch))
        size = None if self.infinite.isChecked() else self.size.value()
        return origin, normal, size


class _StrokeOrderTree(QTreeWidget):
    orderChanged = Signal()

    def _internal_drag(self, event) -> bool:
        return event.source() is self and len(self.selectedItems()) == 1

    def dragEnterEvent(self, event) -> None:
        if self._internal_drag(event):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if self._internal_drag(event):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            super().dragMoveEvent(event)

    def _move_item(self, item, target, lower_half: bool,
                   on_item: bool) -> bool:
        if item is None or target is None or item is target:
            return False
        kind = item.data(0, Qt.ItemDataRole.UserRole)
        target_kind = target.data(0, Qt.ItemDataRole.UserRole)

        if kind == "layer":
            if target_kind != "layer":
                return False
            self.takeTopLevelItem(self.indexOfTopLevelItem(item))
            index = self.indexOfTopLevelItem(target)
            if index < 0:
                self.addTopLevelItem(item)
            else:
                if lower_half and not on_item:
                    index += 1
                self.insertTopLevelItem(index, item)
        elif kind == "stroke":
            parent = item.parent()
            if parent is None:
                return False
            if target_kind == "stroke":
                dest = target.parent()
                if dest is None:
                    return False
                parent.takeChild(parent.indexOfChild(item))
                index = dest.indexOfChild(target)
                if lower_half:
                    index += 1
                dest.insertChild(index, item)
            elif target_kind == "layer":
                dest = target
                parent.takeChild(parent.indexOfChild(item))
                if on_item:
                    dest.addChild(item)
                elif lower_half:
                    dest.insertChild(dest.childCount(), item)
                else:
                    dest.insertChild(0, item)
                dest.setExpanded(True)
            else:
                return False
            holder = item.parent()
            item.setData(0, Qt.ItemDataRole.UserRole + 2,
                         None if holder is None
                         else holder.data(0, Qt.ItemDataRole.UserRole + 1))
        else:
            return False
        return True

    def dropEvent(self, event) -> None:
        if not self._internal_drag(event):
            event.ignore()
            return

        item = self.selectedItems()[0]
        position = event.position().toPoint()
        target = self.itemAt(position)
        if target is None:
            event.ignore()
            return

        rect = self.visualItemRect(target)
        lower_half = position.y() > rect.center().y()
        on_item = (position.y() >= rect.top() + rect.height() // 4
                   and position.y() < rect.top()
                   + (rect.height() * 3) // 4)

        if not self._move_item(item, target, lower_half, on_item):
            event.ignore()
            return

        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.setCurrentItem(item)
        QTimer.singleShot(0, self.orderChanged.emit)

class ScribblePanel(QWidget):
    """The pen's settings, the layers, and what the document holds."""

    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self._loading = False
        self._refresh_pending = False
        layout = QVBoxLayout(self)

        # ---- Presets -------------------------------------------------------
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        self.presets = QComboBox()
        preset_row.addWidget(self.presets, 1)
        self.preset_save = QPushButton("Save…")
        self.preset_save.setToolTip(
            "Store the current pen settings as a preset"
            f" (saved in {_preset_path()})")
        self.preset_save.clicked.connect(
            lambda _c=False: self._save_preset())
        preset_row.addWidget(self.preset_save)
        self.preset_delete = QPushButton("−")
        self.preset_delete.setToolTip("Delete the selected preset")
        self.preset_delete.setEnabled(False)
        self.preset_delete.clicked.connect(
            lambda _c=False: self._delete_preset())
        preset_row.addWidget(self.preset_delete)
        self.presets.currentIndexChanged.connect(self._on_preset)
        layout.addLayout(preset_row)

        tool_row = QHBoxLayout()
        self.draw_btn = QPushButton("Pen")
        self.brush_btn = QPushButton("Brush")
        self.shape_btn = QPushButton("Shapes")
        self.erase_btn = QPushButton("Erase")
        for button, tool in ((self.draw_btn, ScribbleTool),
                             (self.brush_btn, ScribbleBrushTool),
                             (self.shape_btn, ScribbleShapeTool),
                             (self.erase_btn, ScribbleEraserTool)):
            button.clicked.connect(lambda _c=False, t=tool: self._take(t))
            tool_row.addWidget(button)
        self.partial_erase = QCheckBox("Partial erase")
        self.partial_erase.setToolTip(
            "When enabled, the eraser cuts only the swept section of a stroke")
        self.partial_erase.setChecked(bool(_ERASER_MODE["partial"]))
        self.partial_erase.toggled.connect(
            lambda on: _ERASER_MODE.update(partial=bool(on)))
        tool_row.addWidget(self.partial_erase)
        layout.addLayout(tool_row)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Trace speed:"))
        self.trace_speed = QSpinBox()
        self.trace_speed.setRange(0, 100)
        self.trace_speed.setSuffix(" %")
        self.trace_speed.setValue(int(_TRACE["speed"]))
        self.trace_speed.setToolTip(
            "Higher values filter small hand movements for straighter Pen "
            "and Brush strokes")
        self.trace_speed.valueChanged.connect(self._on_trace_speed)
        speed_row.addWidget(self.trace_speed, 1)
        layout.addLayout(speed_row)

        # ---- Pen -----------------------------------------------------------
        pen_row = QHBoxLayout()
        pen_row.addWidget(QLabel("Pen/shape colour:"))
        self.colors = QComboBox()
        self._build_colors(self.colors, _PEN["color"], same=False,
                           allow_none=True)
        self.colors.currentIndexChanged.connect(self._on_color)
        pen_row.addWidget(self.colors, 1)
        self.color_btn = QPushButton()
        self.color_btn.setFixedWidth(30)
        self.color_btn.setToolTip("Pick any colour…")
        self.color_btn.clicked.connect(
            lambda _c=False: self._pick_color("pen"))
        pen_row.addWidget(self.color_btn)
        layout.addLayout(pen_row)

        width_row = QHBoxLayout()
        width_row.addWidget(QLabel("Width:"))
        self.width = QSpinBox()
        self.width.setRange(1, 40)
        self.width.setSuffix(" px")
        self.width.setValue(int(_PEN["width"]))
        self.width.valueChanged.connect(self._on_width)
        width_row.addWidget(self.width, 1)
        layout.addLayout(width_row)

        turbulence_row = QHBoxLayout()
        turbulence_row.addWidget(QLabel("Pen turbulence:"))
        self.turbulence = QSpinBox()
        self.turbulence.setRange(0, 100)
        self.turbulence.setSuffix(" %")
        self.turbulence.setValue(int(round(_PEN["turbulence"] * 100)))
        self.turbulence.valueChanged.connect(self._on_turbulence)
        self.turbulence.setToolTip(
            "Slow, smooth slack-cord waves; 0 keeps the line taut")
        turbulence_row.addWidget(self.turbulence, 1)
        layout.addLayout(turbulence_row)

        pen_brush_separator = QFrame()
        pen_brush_separator.setFrameShape(QFrame.Shape.HLine)
        pen_brush_separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(pen_brush_separator)

        # ---- Brush ---------------------------------------------------------
        # The brush is its own tool: a halo, not a pen with a big width.
        brush_row = QHBoxLayout()
        brush_row.addWidget(QLabel("Brush colour:"))
        self.brush_colors = QComboBox()
        self._build_colors(self.brush_colors, _BRUSH["color"], same=False)
        self.brush_colors.currentIndexChanged.connect(self._on_brush_color)
        brush_row.addWidget(self.brush_colors, 1)
        self.brush_pick = QPushButton()
        self.brush_pick.setFixedWidth(30)
        self.brush_pick.setToolTip("Pick any brush colour…")
        self.brush_pick.clicked.connect(
            lambda _c=False: self._pick_color("brush"))
        brush_row.addWidget(self.brush_pick)
        layout.addLayout(brush_row)

        brush2_row = QHBoxLayout()
        brush2_row.addWidget(QLabel("Brush 2:"))
        self.brush2_colors = QComboBox()
        self._build_colors(self.brush2_colors, _BRUSH["color2"], same=True,
                           label="Same as brush")
        self.brush2_colors.currentIndexChanged.connect(self._on_brush2)
        brush2_row.addWidget(self.brush2_colors, 1)
        self.brush2_btn = QPushButton()
        self.brush2_btn.setFixedWidth(30)
        self.brush2_btn.setToolTip("The colour the halo fades into")
        self.brush2_btn.clicked.connect(
            lambda _c=False: self._pick_color("brush2"))
        brush2_row.addWidget(self.brush2_btn)
        layout.addLayout(brush2_row)

        brush_grad_row = QHBoxLayout()
        brush_grad_row.addWidget(QLabel("Brush grad:"))
        self.brush_grad = QComboBox()
        for label, value in BRUSH_GRADIENTS:
            self.brush_grad.addItem(label, value)
        self._select_data(self.brush_grad, _BRUSH["grad"])
        self.brush_grad.currentIndexChanged.connect(self._on_brush_grad)
        self.brush_grad.setToolTip(
            "Outward takes the halo from the brush colour to «Brush 2»")
        brush_grad_row.addWidget(self.brush_grad, 1)
        layout.addLayout(brush_grad_row)

        brush_size = QHBoxLayout()
        brush_size.addWidget(QLabel("Brush size:"))
        self.brush_width = QSpinBox()
        self.brush_width.setRange(2, 200)
        self.brush_width.setSuffix(" px")
        self.brush_width.setValue(int(_BRUSH["width"]))
        self.brush_width.valueChanged.connect(self._on_brush_width)
        brush_size.addWidget(self.brush_width, 1)
        layout.addLayout(brush_size)

        brush_soft = QHBoxLayout()
        brush_soft.addWidget(QLabel("Softness:"))
        self.brush_soft = QSpinBox()
        self.brush_soft.setRange(0, 100)
        self.brush_soft.setSuffix(" %")
        self.brush_soft.setValue(int(round(float(_BRUSH["soft"]) * 100)))
        self.brush_soft.valueChanged.connect(self._on_brush_soft)
        brush_soft.addWidget(self.brush_soft, 1)
        layout.addLayout(brush_soft)

        brush_profile = QHBoxLayout()
        brush_profile.addWidget(QLabel("Profile:"))
        self.brush_profile = QComboBox()
        for label, value in PROFILES:
            self.brush_profile.addItem(label, value)
        self._select_data(self.brush_profile, _BRUSH["profile"])
        self.brush_profile.currentIndexChanged.connect(
            self._on_brush_profile)
        self.brush_profile.setToolTip(
            "How the halo falls away: a bright middle that dies quickly, or "
            "the whole width spread thin")
        brush_profile.addWidget(self.brush_profile, 1)
        layout.addLayout(brush_profile)

        brush_jitter = QHBoxLayout()
        brush_jitter.addWidget(QLabel("Jitter:"))
        self.brush_jitter = QSpinBox()
        self.brush_jitter.setRange(0, 100)
        self.brush_jitter.setSuffix(" %")
        self.brush_jitter.setValue(int(round(float(_BRUSH["jitter"]) * 100)))
        self.brush_jitter.valueChanged.connect(self._on_brush_jitter)
        self.brush_jitter.setToolTip(
            "Wander of each layer: airbrush instead of a uniform band")
        brush_jitter.addWidget(self.brush_jitter, 1)
        layout.addLayout(brush_jitter)

        brush_alpha = QHBoxLayout()
        brush_alpha.addWidget(QLabel("Brush opacity:"))
        self.brush_alpha = QSpinBox()
        self.brush_alpha.setRange(5, 100)
        self.brush_alpha.setSuffix(" %")
        self.brush_alpha.setValue(int(round(float(_BRUSH["alpha"]) * 100)))
        self.brush_alpha.valueChanged.connect(self._on_brush_alpha)
        brush_alpha.addWidget(self.brush_alpha, 1)
        layout.addLayout(brush_alpha)

        brush_shape_separator = QFrame()
        brush_shape_separator.setFrameShape(QFrame.Shape.HLine)
        brush_shape_separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(brush_shape_separator)

        # ---- Shapes --------------------------------------------------------
        shape_row = QHBoxLayout()
        shape_row.addWidget(QLabel("Shape:"))
        self.shape = QComboBox()
        for value, label in SHAPES:
            self.shape.addItem(label, value)
        self._select_data(self.shape, _SHAPE["kind"])
        self.shape.currentIndexChanged.connect(self._on_shape)
        shape_row.addWidget(self.shape, 1)
        layout.addLayout(shape_row)

        sides_row = QHBoxLayout()
        sides_row.addWidget(QLabel("Sides:"))
        self.sides = QSpinBox()
        self.sides.setRange(3, 20)
        self.sides.setValue(int(_SHAPE["sides"]))
        self.sides.valueChanged.connect(self._on_sides)
        sides_row.addWidget(self.sides, 1)
        layout.addLayout(sides_row)

        self.filled = QCheckBox("Filled (lines may stay open)")
        self.filled.setChecked(bool(_PEN["fill"]))
        self.filled.toggled.connect(self._on_fill)
        layout.addWidget(self.filled)

        fill_row = QHBoxLayout()
        fill_row.addWidget(QLabel("Fill colour:"))
        self.fill_colors = QComboBox()
        self._build_colors(self.fill_colors, _PEN["fill_color"], same=True)
        self.fill_colors.currentIndexChanged.connect(self._on_fill_color)
        fill_row.addWidget(self.fill_colors, 1)
        self.fill_btn = QPushButton()
        self.fill_btn.setFixedWidth(30)
        self.fill_btn.setToolTip("Pick any fill colour…")
        self.fill_btn.clicked.connect(
            lambda _c=False: self._pick_color("fill"))
        fill_row.addWidget(self.fill_btn)
        layout.addLayout(fill_row)

        fill2_row = QHBoxLayout()
        fill2_row.addWidget(QLabel("Fill 2:"))
        self.fill2_colors = QComboBox()
        self._build_colors(self.fill2_colors, _PEN["fill2"], same=True,
                           label="Same as fill")
        self.fill2_colors.currentIndexChanged.connect(self._on_fill2)
        fill2_row.addWidget(self.fill2_colors, 1)
        self.fill2_btn = QPushButton()
        self.fill2_btn.setFixedWidth(30)
        self.fill2_btn.setToolTip("The far end of the fill gradient")
        self.fill2_btn.clicked.connect(
            lambda _c=False: self._pick_color("fill2"))
        fill2_row.addWidget(self.fill2_btn)
        layout.addLayout(fill2_row)

        alpha_row = QHBoxLayout()
        alpha_row.addWidget(QLabel("Fill opacity:"))
        self.fill_alpha = QSpinBox()
        self.fill_alpha.setRange(5, 100)
        self.fill_alpha.setSuffix(" %")
        self.fill_alpha.setValue(int(round(float(_PEN["fill_alpha"]) * 100)))
        self.fill_alpha.valueChanged.connect(self._on_fill_alpha)
        alpha_row.addWidget(self.fill_alpha, 1)
        layout.addLayout(alpha_row)

        fill_grad_row = QHBoxLayout()
        fill_grad_row.addWidget(QLabel("Fill grad:"))
        self.fill_grad = QComboBox()
        for label, value in FILL_GRADIENTS:
            self.fill_grad.addItem(label, value)
        self._select_data(self.fill_grad, _PEN["fill_grad"])
        self.fill_grad.currentIndexChanged.connect(self._on_fill_grad)
        self.fill_grad.setToolTip(
            "Solid uses one colour; Linear and Radial blend into «Fill 2»")
        fill_grad_row.addWidget(self.fill_grad, 1)
        layout.addLayout(fill_grad_row)

        fill_pattern_row = QHBoxLayout()
        fill_pattern_row.addWidget(QLabel("Fill pattern:"))
        self.fill_pattern = QComboBox()
        for label, value in FILL_PATTERNS:
            self.fill_pattern.addItem(label, value)
        self._select_data(self.fill_pattern, _PEN["fill_pattern"])
        self.fill_pattern.currentIndexChanged.connect(self._on_fill_pattern)
        fill_pattern_row.addWidget(self.fill_pattern, 1)
        layout.addLayout(fill_pattern_row)

        # ---- Snapping ------------------------------------------------------
        snap_row = QHBoxLayout()
        snap_row.addWidget(QLabel("Snap:"))
        self.grid = QComboBox()
        for label, value in GRID_CHOICES:
            self.grid.addItem(label, value)
        self._select_data(self.grid, float(_SNAP["grid"]))
        self.grid.currentIndexChanged.connect(self._on_grid)
        snap_row.addWidget(self.grid, 1)
        layout.addLayout(snap_row)

        self.show_marks = QCheckBox("Show annotations")
        self.show_marks.setChecked(_SHOW["visible"])
        self.show_marks.toggled.connect(self._on_show)
        layout.addWidget(self.show_marks)

        plane_row = QHBoxLayout()
        plane_row.addWidget(QLabel("Drawing plane:"))
        self.plane_combo = QComboBox()
        self.plane_combo.currentIndexChanged.connect(self._on_plane_changed)
        plane_row.addWidget(self.plane_combo, 1)
        self.add_plane_btn = QPushButton("New…")
        self.add_plane_btn.clicked.connect(lambda _c=False: self._add_plane())
        plane_row.addWidget(self.add_plane_btn)
        self.remove_plane_btn = QPushButton("Delete")
        self.remove_plane_btn.clicked.connect(lambda _c=False: self._remove_plane())
        plane_row.addWidget(self.remove_plane_btn)
        layout.addLayout(plane_row)
        self.plane_visible = QCheckBox("Show active plane")
        self.plane_visible.toggled.connect(self._on_plane_visible)
        layout.addWidget(self.plane_visible)

        # ---- Selection operations ----------------------------------------
        boolean_row = QHBoxLayout()
        boolean_row.addWidget(QLabel("Combine selection:"))
        for text, operation in (("Union", "union"),
                                ("Subtract", "difference"),
                                ("Intersect", "intersection")):
            button = QPushButton(text)
            tip = ("Subtract higher selected shapes from the lowest one"
                   if operation == "difference"
                   else "Apply to closed shapes selected with Move")
            button.setToolTip(tip)
            button.clicked.connect(
                lambda _c=False, op=operation: self._boolean(op))
            boolean_row.addWidget(button)
        layout.addLayout(boolean_row)

        transform_row = QHBoxLayout()
        transform_row.addWidget(QLabel("Transform:"))
        self.select_btn = QPushButton("Selection")
        self.select_btn.setToolTip(
            "Click a drawing in the tree or fence drawings to select")
        self.select_btn.clicked.connect(
            lambda _c=False: self._take(ScribbleSelectTool))
        transform_row.addWidget(self.select_btn)
        self.move_btn = QPushButton("Move")
        self.move_btn.setToolTip("Drag a mark or fence several marks to select them")
        self.move_btn.clicked.connect(lambda _c=False: self._take(ScribbleMoveTool))
        transform_row.addWidget(self.move_btn)
        self.point_edit_btn = QPushButton("Edit Points")
        self.point_edit_btn.setToolTip(
            "Select one drawing in Layers, window its points, then drag to move them")
        self.point_edit_btn.clicked.connect(
            lambda _c=False: self._take(ScribblePointEditTool))
        transform_row.addWidget(self.point_edit_btn)
        for text, mode in (("Rotate…", "rotate"), ("Scale…", "scale"),
                           ("Mirror H", "mirror_h"), ("Mirror V", "mirror_v")):
            button = QPushButton(text)
            button.setToolTip("Transform the selection in its drawing plane")
            button.clicked.connect(
                lambda _c=False, action=mode: self._transform_selection(action))
            transform_row.addWidget(button)
        layout.addLayout(transform_row)

        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("View:"))
        self.zoom_extents_btn = QPushButton("Zoom Extents")
        self.zoom_extents_btn.setToolTip(
            "Frame all visible Croquis annotations")
        self.zoom_extents_btn.clicked.connect(
            lambda _c=False: self._zoom_extents())
        view_row.addWidget(self.zoom_extents_btn)
        self.zoom_object_btn = QPushButton("Zoom Object")
        self.zoom_object_btn.setToolTip(
            "Frame the selected drawing(s) from Layers")
        self.zoom_object_btn.clicked.connect(
            lambda _c=False: self._zoom_object())
        view_row.addWidget(self.zoom_object_btn)
        self.zoom_window_btn = QPushButton("Zoom Window")
        self.zoom_window_btn.setToolTip(
            "Drag a rectangle in the viewport to zoom into it")
        self.zoom_window_btn.clicked.connect(
            lambda _c=False: self._take(ScribbleZoomWindowTool))
        view_row.addWidget(self.zoom_window_btn)
        layout.addLayout(view_row)

        self.state = QLabel()
        self.state.setWordWrap(True)
        layout.addWidget(self.state)

        # ---- Layers --------------------------------------------------------
        layout.addWidget(QLabel("Layers (bottom to top):"))
        self.layers = _StrokeOrderTree()
        self.layers.setToolTip(
            "Drawings are listed top to bottom. Drag a drawing above or below "
            "another in the same or another layer to change stacking order or "
            "move it between layers. Use Shift "
            "for a selection range, Ctrl to toggle drawings, or press Delete "
            "to remove selected drawings.")
        self.layers.setHeaderHidden(True)
        self.layers.setMinimumHeight(140)
        self.layers.setDragEnabled(True)
        self.layers.setAcceptDrops(True)
        self.layers.setDropIndicatorShown(True)
        self.layers.setDragDropMode(
            QAbstractItemView.DragDropMode.DragDrop)
        self.layers.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.layers.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.layers.currentItemChanged.connect(self._on_tree_item)
        self.layers.itemSelectionChanged.connect(
            self._on_tree_selection_changed)
        self.layers.orderChanged.connect(self._on_tree_order_changed)
        self.layers.itemChanged.connect(self._on_layer_item)
        self.delete_strokes_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Delete), self.layers)
        self.delete_strokes_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.delete_strokes_shortcut.activated.connect(
            self._delete_selected_strokes)
        layout.addWidget(self.layers)

        layer_buttons = QHBoxLayout()
        for text, slot, tip in (
            ("+", self._add_layer, "New layer above the active one"),
            ("−", self._remove_layer, "Delete the active layer"),
            ("Rename", self._rename_layer, "Rename the active layer"),
            ("Lock", self._lock_layer, "Lock or unlock the active layer"),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda _c=False, s=slot: s())
            layer_buttons.addWidget(button)
        self.recolor_btn = QPushButton("Recolor…")
        self.recolor_btn.setToolTip(
            "Change the outline and fill colours of selected drawings")
        self.recolor_btn.clicked.connect(
            lambda _c=False: self._recolor_selected())
        layer_buttons.addWidget(self.recolor_btn)
        layout.addLayout(layer_buttons)

        opacity_row = QHBoxLayout()
        opacity_row.addWidget(QLabel("Layer opacity:"))
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(5, 100)
        self.opacity.setValue(100)
        self.opacity.valueChanged.connect(self._on_opacity)
        opacity_row.addWidget(self.opacity, 1)
        layout.addLayout(opacity_row)

        # ---- Export --------------------------------------------------------
        export_row = QHBoxLayout()
        for text, slot in (("PNG…", self._export_png),
                           ("SVG…", self._export_svg),
                           ("Copy", self._copy)):
            button = QPushButton(text)
            button.clicked.connect(lambda _c=False, s=slot: s())
            export_row.addWidget(button)
        layout.addLayout(export_row)

        back_row = QHBoxLayout()
        back_row.addWidget(QLabel("Export over:"))
        self.backdrop = QComboBox()
        for label, value in (("Transparent", "transparent"),
                             ("White", "white"),
                             ("Dark", "black"),
                             ("Model + annotations", "view")):
            self.backdrop.addItem(label, value)
        self._select_data(self.backdrop, _EXPORT["background"])
        self.backdrop.currentIndexChanged.connect(self._on_backdrop)
        back_row.addWidget(self.backdrop, 1)
        layout.addLayout(back_row)

        scale_row = QHBoxLayout()
        scale_row.addWidget(QLabel("PNG scale:"))
        self.export_scale = QSpinBox()
        self.export_scale.setRange(1, 4)
        self.export_scale.setSuffix("×")
        self.export_scale.setValue(int(round(float(_EXPORT["scale"]))))
        self.export_scale.valueChanged.connect(self._on_export_scale)
        scale_row.addWidget(self.export_scale, 1)
        layout.addLayout(scale_row)

        self.erase_all = QPushButton("Erase this layer")
        self.erase_all.clicked.connect(self._erase_all)
        layout.addWidget(self.erase_all)

        self.count = QLabel()
        self.count.setWordWrap(True)
        layout.addWidget(self.count)

        self.sheet_note = QLabel()
        self.sheet_note.setWordWrap(True)
        layout.addWidget(self.sheet_note)

        hint = QLabel(
            "Selection: click a mark, choose it under Layers, or drag a fence "
            "to select it first. Pen and "
            "Brush: press, drag and release. Shapes: click to anchor, click "
            "again to finish (drag to size; the arc takes three points, the "
            "polyline ends on a double click). Shift constrains to 45°, Alt "
            "turns snapping off. The eraser and Move only touch the active "
            "layer. Move drags a mark, or drags a fence on empty space to "
            "carry several (Shift adds to the fence; Shift on a mark takes "
            "its whole layer). Use the Partial erase checkbox to cut only the "
            "swept segment. Toggle a layer or a drawing in the tree to control "
            "its visibility. Select a drawing in Layers and press Delete to "
            "remove it (Ctrl+Z to undo). Use the Combine and Transform "
            "controls on the Move selection. Edit Points selects vertices "
            "inside a window; drag again to move them, or Shift-drag to add "
            "vertices. Recolor changes the outline and fill of selected "
            "drawings. Zoom Extents frames visible annotations; Zoom Window "
            "frames a viewport rectangle; Zoom Object frames the selected "
            "drawing(s). Select a drawing plane for fixed "
            "depth, toggle its "
            "visibility, and set Pen turbulence from 0% for a steady line to "
            "100% for a stronger wobble. Trace speed filters small movements "
            "for straighter Pen and Brush strokes. Brush, colour and fill apply to new "
            "strokes.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)

        try:
            self.app.viewport.measurementChanged.connect(
                lambda *_: self._sync_state())
        except Exception:  # noqa: BLE001 — an older viewport: the timer does
            pass
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sync_state)
        self._timer.start(600)

        self._rebuild_presets()
        self._sync_swatches()
        self.refresh()

    # ---- Actions -----------------------------------------------------------
    def _take(self, tool_class) -> None:
        """Hand the pen (or the eraser) to the viewport."""
        viewport = self.app.viewport
        try:
            viewport.set_active_tool(tool_class())
            viewport.setFocus()
        except Exception as exc:  # noqa: BLE001 — said in the panel
            self.state.setText(f"Croquis on model could not take the viewport: "
                               f"{type(exc).__name__}: {exc}")
            return
        self._sync_state()

    def _sync_state(self) -> None:
        """Say which Croquis on model tool is active, as the document actually is."""
        try:
            tool = getattr(self.app.viewport, "active_tool", None)
        except Exception:  # noqa: BLE001 — a tab closing, a window gone
            return
        kind = type(tool).__name__ if tool is not None else ""
        if kind == "ScribbleSelectTool":
            text = "Selection ready — click a mark or drag a fence first."
        elif kind == "ScribbleTool":
            text = "Pen ready — press, drag and release over the model."
        elif kind == "ScribbleBrushTool":
            text = (f"Brush ready — a soft halo, {int(_BRUSH['width'])} px, "
                    f"{int(round(float(_BRUSH['soft']) * 100))}% soft.")
        elif kind == "ScribbleShapeTool":
            text = f"Shapes ready — {_SHAPE['kind']}."
        elif kind == "ScribbleEraserTool":
            text = ("Partial eraser ready — drag to cut a segment."
                    if _ERASER_MODE["partial"]
                    else "Eraser ready — drag over annotations to remove strokes.")
        elif kind == "ScribbleMoveTool":
            text = ("Move ready — drag a mark or fence a selection; use the "
                    "Transform and Combine controls for selected marks.")
        elif kind == "ScribblePointEditTool":
            text = ("Edit Points ready — select one drawing in Layers, drag a "
                    "window over its points, then drag again to move them. "
                    "Shift-drag adds points.")
        elif kind == "ScribbleZoomWindowTool":
            text = "Zoom Window ready — drag a rectangle in the viewport."
        else:
            text = ("No Croquis on model tool in the hand: press Selection, "
                    "Pen, Brush, Shapes, Erase, Move or Zoom Window.")
        if self.state.text() != text:
            self.state.setText(text)
            self.select_btn.setDefault(kind == "ScribbleSelectTool")
            self.draw_btn.setDefault(kind == "ScribbleTool")
            self.brush_btn.setDefault(kind == "ScribbleBrushTool")
            self.shape_btn.setDefault(kind == "ScribbleShapeTool")
            self.erase_btn.setDefault(kind == "ScribbleEraserTool")
            self.move_btn.setDefault(kind == "ScribbleMoveTool")
            self.point_edit_btn.setDefault(kind == "ScribblePointEditTool")
            self.zoom_window_btn.setDefault(kind == "ScribbleZoomWindowTool")

    def _zoom_extents(self) -> None:
        try:
            _zoom_extents(self.app, self.app.viewport)
        except Exception as exc:  # noqa: BLE001 — surfaced in the viewport
            _fail(self.app.viewport, exc)

    def _zoom_object(self) -> None:
        _zoom_object_action(self.app)

    def _recolor_selected(self) -> None:
        viewport = self.app.viewport
        scene = getattr(viewport, "scene", None)
        if (scene is None or _SELECTION["scene"] != id(scene)
                or not _SELECTION["ids"]):
            self.sheet_note.setText("Select one or more drawings to recolor.")
            return
        doc = document(self.app, scene)
        selected = []
        for layer in doc["layers"]:
            for stroke in layer["strokes"]:
                if stroke["id"] in _SELECTION["ids"]:
                    selected.append((layer, stroke))
        if not selected:
            self.sheet_note.setText("The selected drawings no longer exist.")
            return
        if any(layer["locked"] for layer, _stroke in selected):
            self.sheet_note.setText("Unlock the selected layer(s) before recoloring.")
            return
        color = QColorDialog.getColor(
            QColor(_PEN["color"]), self, "Croquis on model — recolor")
        if not color.isValid():
            return
        replacement = color.name()
        for _layer, stroke in selected:
            stroke["c"] = replacement
            stroke["fc"] = replacement
            stroke["fc2"] = replacement
            if stroke.get("c2"):
                stroke["c2"] = replacement
        try:
            _store(self.app, doc)
        except Exception as exc:  # noqa: BLE001 — reported in the panel
            self.sheet_note.setText(
                f"Could not recolor the selected drawings: "
                f"{type(exc).__name__}: {exc}")
            return
        viewport.update()
        self.sheet_note.setText(
            f"Recolored {len(selected)} drawing"
            f"{'s' if len(selected) != 1 else ''}.")

    def _erase_all(self) -> None:
        doc = document(self.app)
        layer = active_layer(doc)
        if not layer["strokes"]:
            return
        answer = QMessageBox.question(
            self, "Croquis on model",
            f"Erase every annotation of «{layer['name']}»?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        layer["strokes"] = []
        _store(self.app, doc)
        self.refresh()

    def _active_move_tool(self):
        tool = getattr(self.app.viewport, "active_tool", None)
        return tool if isinstance(tool, ScribbleMoveTool) else None

    def _boolean(self, operation: str) -> None:
        tool = self._active_move_tool()
        if tool is None:
            self.sheet_note.setText("Select shapes with Move before combining them.")
            return
        doc = document(self.app)
        layer = active_layer(doc)
        if layer["locked"] or not layer["visible"]:
            self.sheet_note.setText("The active layer is hidden or locked.")
            return
        selected = [st for st in _visible_strokes(layer)
                    if st["id"] in tool._sel]
        result, error = _boolean_combine(selected, operation)
        if error:
            self.sheet_note.setText(error)
            return
        ids = {st["id"] for st in selected}
        layer["strokes"] = [st for st in layer["strokes"]
                            if st["id"] not in ids]
        result.pop("name", None)
        _name_stroke(layer, result)
        layer["strokes"].append(result)
        tool._sel = {result["id"]}
        _store(self.app, doc)
        self.app.viewport.update()
        self.refresh()
        self.sheet_note.setText(
            f"{operation.title()} created one compound shape.")

    def _transform_selection(self, mode: str) -> None:
        tool = self._active_move_tool()
        if tool is None:
            self.sheet_note.setText("Choose Move and select marks first.")
            return
        value = 1.0
        if mode == "rotate":
            value, ok = QInputDialog.getDouble(
                self, "Croquis on model", "Rotation angle (degrees):",
                45.0, -3600.0, 3600.0, 1)
            if not ok:
                return
        elif mode == "scale":
            value, ok = QInputDialog.getDouble(
                self, "Croquis on model", "Scale factor:",
                1.5, 0.01, 100.0, 2)
            if not ok:
                return
        moved, error = tool.transform_selection(mode, value)
        if error:
            self.sheet_note.setText(error)
            return
        self.refresh()
        self.sheet_note.setText(f"Transformed {moved} selected mark(s).")

    def _rebuild_planes(self) -> None:
        doc = self._doc()
        active_id = (_DRAWING_PLANE.get("id")
                     if _DRAWING_PLANE.get("scene") == id(self.app.scene)
                     else None)
        self._loading = True
        try:
            self.plane_combo.blockSignals(True)
            self.plane_combo.clear()
            self.plane_combo.addItem("Automatic (camera-facing)", "")
            for plane in doc["planes"]:
                self.plane_combo.addItem(plane["name"], plane["id"])
            index = self.plane_combo.findData(active_id or "")
            self.plane_combo.setCurrentIndex(max(0, index))
            active = next((plane for plane in doc["planes"]
                           if plane["id"] == active_id), None)
            self.plane_visible.setChecked(bool(active and active["visible"]))
            self.plane_visible.setEnabled(active is not None)
            self.remove_plane_btn.setEnabled(active is not None)
        finally:
            self.plane_combo.blockSignals(False)
            self._loading = False

    def _on_plane_changed(self, _index: int) -> None:
        if self._loading:
            return
        plane_id = str(self.plane_combo.currentData() or "")
        _DRAWING_PLANE.update(scene=id(self.app.scene), id=plane_id or None)
        plane = next((item for item in document(self.app)["planes"]
                      if item["id"] == plane_id), None)
        self._loading = True
        try:
            self.plane_visible.setEnabled(plane is not None)
            self.plane_visible.setChecked(bool(plane and plane["visible"]))
            self.remove_plane_btn.setEnabled(plane is not None)
        finally:
            self._loading = False
        self.app.viewport.update()

    def _on_plane_visible(self, visible: bool) -> None:
        if self._loading:
            return
        plane = active_drawing_plane(self.app, self.app.scene)
        if plane is None:
            return
        plane["visible"] = bool(visible)
        _store(self.app, document(self.app))
        self.app.viewport.update()

    def _add_plane(self) -> None:
        dialog = DrawingPlaneDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        origin, normal, size = dialog.plane_values()
        doc = self._doc()
        plane = {"id": uuid.uuid4().hex[:8],
                 "name": f"Plane {len(doc['planes']) + 1}",
                 "origin": origin, "normal": normal, "size": size,
                 "visible": True}
        doc["planes"].append(plane)
        _DRAWING_PLANE.update(scene=id(self.app.scene), id=plane["id"])
        _store(self.app, doc)
        self.refresh()

    def _remove_plane(self) -> None:
        plane = active_drawing_plane(self.app, self.app.scene)
        if plane is None:
            return
        doc = self._doc()
        doc["planes"] = [item for item in doc["planes"]
                         if item["id"] != plane["id"]]
        _DRAWING_PLANE.update(scene=id(self.app.scene), id=None)
        _store(self.app, doc)
        self.refresh()

    # ---- Layer actions -----------------------------------------------------
    def _doc(self):
        return document(self.app)

    def _add_layer(self) -> None:
        doc = self._doc()
        layer = new_layer(f"Layer {len(doc['layers']) + 1}")
        try:
            index = [l["id"] for l in doc["layers"]].index(_ACTIVE["id"])
        except ValueError:
            index = len(doc["layers"]) - 1
        doc["layers"].insert(index + 1, layer)
        _ACTIVE["id"] = layer["id"]
        _store(self.app, doc)
        self.refresh()

    def _remove_layer(self) -> None:
        doc = self._doc()
        layer = active_layer(doc)
        if len(doc["layers"]) <= 1:
            QMessageBox.information(self, "Croquis on model",
                                    "A drawing always keeps one layer.")
            return
        answer = QMessageBox.question(
            self, "Croquis on model",
            f"Delete «{layer['name']}» and its "
            f"{len(layer['strokes'])} strokes?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        doc["layers"] = [l for l in doc["layers"] if l["id"] != layer["id"]]
        _ACTIVE["id"] = doc["layers"][0]["id"]
        _store(self.app, doc)
        self.refresh()

    def _move_layer(self, step: int) -> None:
        doc = self._doc()
        ids = [l["id"] for l in doc["layers"]]
        try:
            index = ids.index(_ACTIVE["id"])
        except ValueError:
            return
        target = index + step
        if not (0 <= target < len(doc["layers"])):
            return
        doc["layers"][index], doc["layers"][target] = (
            doc["layers"][target], doc["layers"][index])
        _store(self.app, doc)
        self.refresh()

    def _rename_layer(self) -> None:
        doc = self._doc()
        layer = active_layer(doc)
        name, ok = QInputDialog.getText(self, "Croquis on model", "Layer name:",
                                        text=layer["name"])
        if not ok or not name.strip():
            return
        layer["name"] = name.strip()[:60]
        _store(self.app, doc)
        self.refresh()

    def _lock_layer(self) -> None:
        doc = self._doc()
        layer = active_layer(doc)
        layer["locked"] = not layer["locked"]
        _store(self.app, doc)
        self.refresh()

    def _on_tree_item(self, item, _previous) -> None:
        if self._loading or item is None:
            return
        item_type = item.data(0, Qt.ItemDataRole.UserRole)
        if item_type == "layer":
            _POINT_SELECTION.update(scene=None, stroke=None, indices=set())
            _ACTIVE["id"] = item.data(0, Qt.ItemDataRole.UserRole + 1)
            self._sync_opacity()
        elif item_type == "stroke":
            layer_id = item.data(0, Qt.ItemDataRole.UserRole + 2)
            _ACTIVE["id"] = layer_id
            self._sync_opacity()
            if not isinstance(getattr(self.app.viewport, "active_tool", None),
                              ScribbleMoveTool):
                self._take(ScribbleSelectTool)
        else:
            return
        self.app.viewport.update()

    def _on_tree_selection_changed(self) -> None:
        if self._loading:
            return
        selected_ids = {
            item.data(0, Qt.ItemDataRole.UserRole + 1)
            for item in self.layers.selectedItems()
            if item.data(0, Qt.ItemDataRole.UserRole) == "stroke"
        }
        current = self.layers.currentItem()
        current_is_stroke = (
            current is not None
            and current.data(0, Qt.ItemDataRole.UserRole) == "stroke")
        if not selected_ids and not current_is_stroke:
            return
        scene = getattr(self.app, "scene", None)
        _SELECTION.update(scene=id(scene) if scene is not None else None,
                          ids=selected_ids)
        _POINT_SELECTION.update(scene=None, stroke=None, indices=set())
        if selected_ids and not isinstance(
                getattr(self.app.viewport, "active_tool", None),
                ScribbleMoveTool):
            self._take(ScribbleSelectTool)
        self.app.viewport.update()

    def _on_tree_order_changed(self) -> None:
        doc = self._doc()
        layer_by_id = {layer["id"]: layer for layer in doc["layers"]}
        strokes_by_id = {stroke["id"]: stroke
                         for layer in doc["layers"]
                         for stroke in layer["strokes"]}

        tree_layers = {}
        for index in range(self.layers.topLevelItemCount()):
            item = self.layers.topLevelItem(index)
            if (item is None
                    or item.data(0, Qt.ItemDataRole.UserRole) != "layer"):
                return
            layer_id = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if layer_id not in layer_by_id:
                return
            tree_layers[layer_id] = [
                item.child(i).data(0, Qt.ItemDataRole.UserRole + 1)
                for i in range(item.childCount())
                if (item.child(i) is not None
                    and item.child(i).data(0, Qt.ItemDataRole.UserRole)
                    == "stroke")
            ]

        if set(tree_layers.keys()) != set(layer_by_id.keys()):
            return
        all_tree_ids = set()
        for ids in tree_layers.values():
            all_tree_ids.update(ids)
        if all_tree_ids != set(strokes_by_id):
            return

        changed = False
        new_layer_order = [layer_by_id[lid] for lid in tree_layers]
        if ([layer["id"] for layer in new_layer_order]
                != [layer["id"] for layer in doc["layers"]]):
            doc["layers"] = new_layer_order
            changed = True

        for layer in doc["layers"]:
            lid = layer["id"]
            new_strokes = [strokes_by_id[sid] for sid in
                           reversed(tree_layers[lid])
                           if sid in strokes_by_id]
            if [s["id"] for s in new_strokes] != [s["id"]
                                                  for s in layer["strokes"]]:
                layer["strokes"] = new_strokes
                changed = True
        if changed:
            _store(self.app, doc)
            self.app.viewport.update()

    def _on_layer_item(self, item) -> None:
        if self._loading:
            return
        item_type = item.data(0, Qt.ItemDataRole.UserRole)
        if item_type == "layer":
            layer_id = item.data(0, Qt.ItemDataRole.UserRole + 1)
        elif item_type == "stroke":
            layer_id = item.data(0, Qt.ItemDataRole.UserRole + 2)
        else:
            return
        doc = self._doc()
        for lay in doc["layers"]:
            if lay["id"] == layer_id:
                visible = (item.checkState(0)
                           == Qt.CheckState.Checked)
                if item_type == "layer":
                    if lay["visible"] == visible:
                        break
                    lay["visible"] = visible
                else:
                    stroke_id = item.data(0, Qt.ItemDataRole.UserRole + 1)
                    stroke = next((st for st in lay["strokes"]
                                   if st["id"] == stroke_id), None)
                    if stroke is None or stroke.get("visible", True) == visible:
                        break
                    stroke["visible"] = visible
                _store(self.app, doc)
                self.app.viewport.update()
                break

    def _delete_selected_strokes(self) -> None:
        targets = {}
        for item in self.layers.selectedItems():
            if item.data(0, Qt.ItemDataRole.UserRole) != "stroke":
                continue
            layer_id = item.data(0, Qt.ItemDataRole.UserRole + 2)
            stroke_id = item.data(0, Qt.ItemDataRole.UserRole + 1)
            targets.setdefault(layer_id, set()).add(stroke_id)
        if not targets:
            return

        doc = self._doc()
        deleted = set()
        removed = 0
        for layer in doc["layers"]:
            stroke_ids = targets.get(layer["id"])
            if not stroke_ids:
                continue
            original = len(layer["strokes"])
            layer["strokes"] = [stroke for stroke in layer["strokes"]
                                if stroke["id"] not in stroke_ids]
            removed += original - len(layer["strokes"])
            deleted.update(stroke_ids)
        if not removed:
            return

        scene = getattr(self.app, "scene", None)
        if scene is not None and _SELECTION["scene"] == id(scene):
            _SELECTION["ids"].difference_update(deleted)
        if _POINT_SELECTION["stroke"] in deleted:
            _POINT_SELECTION.update(scene=None, stroke=None, indices=set())
        _store(self.app, doc)
        self.app.viewport.update()
        self.sheet_note.setText(
            f"Deleted {removed} drawing{'s' if removed != 1 else ''}. "
            "Press Ctrl+Z to undo.")

    def _on_opacity(self, value: int) -> None:
        if self._loading:
            return
        doc = self._doc()
        active_layer(doc)["opacity"] = max(0.05, value / 100.0)
        _store(self.app, doc)
        self.app.viewport.update()

    def _sync_opacity(self) -> None:
        layer = active_layer(self._doc())
        self._loading = True
        try:
            self.opacity.setValue(int(round(float(layer["opacity"]) * 100)))
        finally:
            self._loading = False

    # ---- Export ------------------------------------------------------------
    def _export_png(self) -> None:
        path = export_png(self.app, self)
        if path:
            self.sheet_note.setText(f"Saved {path}")

    def _export_svg(self) -> None:
        path = export_svg(self.app, self)
        if path:
            self.sheet_note.setText(f"Saved {path}")

    def _copy(self) -> None:
        self.sheet_note.setText("Copied to the clipboard."
                                if copy_to_clipboard(self.app)
                                else "Nothing to copy.")

    # ---- Settings ----------------------------------------------------------
    @staticmethod
    def _select_data(combo, value) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _build_colors(self, combo, value: str, same: bool,
                      label: str = "Same as stroke",
                      allow_none: bool = False) -> None:
        """Fill a colour list from the palette, plus optional None/Custom."""
        combo.blockSignals(True)
        try:
            combo.clear()
            if same:
                combo.addItem(label, "")
            if allow_none:
                combo.addItem("None", "none")
            for label, hex_value in PALETTE:
                combo.addItem(label, hex_value)
                combo.setItemData(combo.count() - 1, QColor(hex_value),
                                  Qt.ItemDataRole.DecorationRole)
            index = combo.findData(value)
            if index < 0:
                combo.addItem("Custom", value)
                combo.setItemData(combo.count() - 1, QColor(value),
                                  Qt.ItemDataRole.DecorationRole)
                index = combo.count() - 1
            combo.setCurrentIndex(index)
        finally:
            combo.blockSignals(False)

    def _set_combo_color(self, combo, value: str) -> None:
        """Show ``value`` in ``combo``, adding it as «Custom» when new."""
        combo.blockSignals(True)
        try:
            index = combo.findData(value)
            if index < 0:
                combo.addItem("Custom", value)
                combo.setItemData(combo.count() - 1, QColor(value),
                                  Qt.ItemDataRole.DecorationRole)
                index = combo.count() - 1
            combo.setCurrentIndex(index)
        finally:
            combo.blockSignals(False)

    def _sync_swatches(self) -> None:
        self.color_btn.setIcon(_swatch(_PEN["color"]))
        self.fill_btn.setIcon(_swatch(_PEN["fill_color"] or _PEN["color"]))
        self.fill2_btn.setIcon(_swatch(_PEN["fill2"] or _PEN["fill_color"]
                                       or _PEN["color"]))
        self.brush_pick.setIcon(_swatch(_BRUSH["color"]))
        self.brush2_btn.setIcon(_swatch(_BRUSH["color2"] or _BRUSH["color"]))

    def _pick_color(self, which: str) -> None:
        """The full colour dialog, for the colours the palette lacks."""
        if which == "pen":
            current, combo = QColor(_PEN["color"]), self.colors
        elif which == "fill":
            current = QColor(_PEN["fill_color"] or _PEN["color"])
            combo = self.fill_colors
        elif which == "fill2":
            current = QColor(_PEN["fill2"] or _PEN["fill_color"]
                             or _PEN["color"])
            combo = self.fill2_colors
        elif which == "brush":
            current, combo = QColor(_BRUSH["color"]), self.brush_colors
        else:
            current = QColor(_BRUSH["color2"] or _BRUSH["color"])
            combo = self.brush2_colors
        if not current.isValid():
            current = QColor(PALETTE[0][1])
        got = QColorDialog.getColor(current, self, "Croquis on model — colour")
        if not got.isValid():
            return
        if which == "pen":
            _PEN["color"] = got.name()
        elif which == "fill":
            _PEN["fill_color"] = got.name()
        elif which == "fill2":
            _PEN["fill2"] = got.name()
        elif which == "brush":
            _BRUSH["color"] = got.name()
        else:
            _BRUSH["color2"] = got.name()
        self._set_combo_color(combo, got.name())
        self._sync_swatches()

    def _on_fill2(self, _index: int) -> None:
        _PEN["fill2"] = str(self.fill2_colors.currentData() or "")
        self._sync_swatches()

    def _on_fill_grad(self, _index: int) -> None:
        value = str(self.fill_grad.currentData() or "solid")
        _PEN["fill_grad"] = (value if value in FILL_GRADIENT_NAMES
                             else "solid")

    def _on_fill_pattern(self, _index: int) -> None:
        value = str(self.fill_pattern.currentData() or "solid")
        _PEN["fill_pattern"] = (value if value in FILL_PATTERN_NAMES
                                else "solid")

    def _on_brush2(self, _index: int) -> None:
        _BRUSH["color2"] = str(self.brush2_colors.currentData() or "")
        self._sync_swatches()

    def _on_brush_grad(self, _index: int) -> None:
        value = str(self.brush_grad.currentData() or "off")
        _BRUSH["grad"] = value if value in BRUSH_GRADIENT_NAMES else "off"

    def _on_brush_color(self, _index: int) -> None:
        _BRUSH["color"] = str(self.brush_colors.currentData()
                               or _BRUSH["color"])
        self._sync_swatches()

    def _on_brush_width(self, value: int) -> None:
        _BRUSH["width"] = float(value)

    def _on_brush_soft(self, value: int) -> None:
        _BRUSH["soft"] = max(0.0, min(1.0, value / 100.0))

    def _on_brush_alpha(self, value: int) -> None:
        _BRUSH["alpha"] = max(0.05, min(1.0, value / 100.0))

    def _on_brush_profile(self, _index: int) -> None:
        value = str(self.brush_profile.currentData() or "even")
        _BRUSH["profile"] = value if value in PROFILE_NAMES else "even"

    def _on_brush_jitter(self, value: int) -> None:
        _BRUSH["jitter"] = max(0.0, min(1.0, value / 100.0))

    def _on_color(self, _index: int) -> None:
        _PEN["color"] = str(self.colors.currentData() or _PEN["color"])
        self._sync_swatches()

    def _on_fill_color(self, _index: int) -> None:
        _PEN["fill_color"] = str(self.fill_colors.currentData() or "")
        self._sync_swatches()

    def _on_fill_alpha(self, value: int) -> None:
        _PEN["fill_alpha"] = max(0.05, min(1.0, value / 100.0))

    # ---- Presets -----------------------------------------------------------
    def _rebuild_presets(self) -> None:
        self.presets.blockSignals(True)
        try:
            self.presets.clear()
            self.presets.addItem("— no preset —", "")
            for preset in presets():
                self.presets.addItem(preset["name"], preset["name"])
                self.presets.setItemData(self.presets.count() - 1,
                                         QColor(preset["color"]),
                                         Qt.ItemDataRole.DecorationRole)
            self.presets.setCurrentIndex(0)
        finally:
            self.presets.blockSignals(False)
        self.preset_delete.setEnabled(False)

    def _on_preset(self, _index: int) -> None:
        name = str(self.presets.currentData() or "")
        found = next((p for p in presets() if p["name"] == name), None)
        self.preset_delete.setEnabled(found is not None)
        if found is None:
            return
        apply_preset(found)
        self._sync_controls()

    def _save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Croquis on model", "Preset name:")
        if not ok or not str(name).strip():
            return
        name = str(name).strip()[:60]
        items = list(presets())
        new = current_preset(name)
        for index, preset in enumerate(items):
            if preset["name"] == name:
                items[index] = new       # saving over a name replaces it
                break
        else:
            items.append(new)
        error = save_presets(items)
        if error:
            self.sheet_note.setText(f"Presets: could not save — {error}")
            return
        self._rebuild_presets()
        self._select_data(self.presets, name)
        self.preset_delete.setEnabled(True)

    def _delete_preset(self) -> None:
        name = str(self.presets.currentData() or "")
        if not name:
            return
        items = [p for p in presets() if p["name"] != name]
        error = save_presets(items)
        if error:
            self.sheet_note.setText(f"Presets: could not save — {error}")
        self._rebuild_presets()

    def _sync_controls(self) -> None:
        """Show the pen's settings in every control (applying a preset)."""
        self._set_combo_color(self.colors, _PEN["color"])
        self._set_combo_color(self.fill_colors, _PEN["fill_color"])
        self._set_combo_color(self.fill2_colors, _PEN["fill2"])
        self._select_data(self.fill_grad, _PEN["fill_grad"])
        self._select_data(self.fill_pattern, _PEN["fill_pattern"])
        self.width.setValue(int(round(float(_PEN["width"]))))
        self.turbulence.setValue(int(round(float(_PEN["turbulence"]) * 100)))
        self.filled.setChecked(bool(_PEN["fill"]))
        self.fill_alpha.setValue(int(round(float(_PEN["fill_alpha"]) * 100)))
        self._sync_swatches()

    def _on_width(self, value: int) -> None:
        _PEN["width"] = float(value)

    def _on_turbulence(self, value: int) -> None:
        _PEN["turbulence"] = max(0.0, min(1.0, value / 100.0))

    def _on_trace_speed(self, value: int) -> None:
        _TRACE["speed"] = max(0, min(100, int(value)))

    def _on_shape(self, _index: int) -> None:
        _SHAPE["kind"] = str(self.shape.currentData() or "line")
        self._sync_state()

    def _on_sides(self, value: int) -> None:
        _SHAPE["sides"] = int(value)

    def _on_fill(self, on: bool) -> None:
        _PEN["fill"] = bool(on)

    def _on_grid(self, _index: int) -> None:
        _SNAP["grid"] = float(self.grid.currentData() or 0.0)

    def _on_backdrop(self, _index: int) -> None:
        _EXPORT["background"] = str(self.backdrop.currentData()
                                    or "transparent")

    def _on_export_scale(self, value: int) -> None:
        _EXPORT["scale"] = float(value)

    def _on_show(self, on: bool) -> None:
        _SHOW["visible"] = bool(on)
        self.app.viewport.update()

    def schedule_refresh(self) -> None:
        if self._refresh_pending:
            return
        self._refresh_pending = True
        QTimer.singleShot(0, self._finish_scheduled_refresh)

    def _finish_scheduled_refresh(self) -> None:
        self._refresh_pending = False
        self.refresh()

    # ---- Refresh -----------------------------------------------------------
    def _rebuild_layers(self) -> None:
        doc = self._doc()
        layer = active_layer(doc)
        scene = getattr(self.app, "scene", None)
        selected_ids = (_SELECTION["ids"]
                        if scene is not None and _SELECTION["scene"] == id(scene)
                        else set())
        selected_item = None
        self._loading = True
        try:
            self.layers.clear()
            for lay in doc["layers"]:
                name = lay["name"]
                if lay["locked"]:
                    name += "  (locked)"
                item = QTreeWidgetItem([name])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                              | Qt.ItemFlag.ItemIsDropEnabled
                              | Qt.ItemFlag.ItemIsDragEnabled)
                item.setData(0, Qt.ItemDataRole.UserRole, "layer")
                item.setData(0, Qt.ItemDataRole.UserRole + 1, lay["id"])
                item.setCheckState(0, Qt.CheckState.Checked if lay["visible"]
                                   else Qt.CheckState.Unchecked)
                self.layers.addTopLevelItem(item)
                for stroke in reversed(lay["strokes"]):
                    child = QTreeWidgetItem([stroke["name"]])
                    child.setFlags(
                        child.flags() | Qt.ItemFlag.ItemIsUserCheckable
                        | Qt.ItemFlag.ItemIsDropEnabled
                        | Qt.ItemFlag.ItemIsDragEnabled)
                    child.setData(0, Qt.ItemDataRole.UserRole, "stroke")
                    child.setData(0, Qt.ItemDataRole.UserRole + 1,
                                  stroke["id"])
                    child.setData(0, Qt.ItemDataRole.UserRole + 2, lay["id"])
                    child.setCheckState(
                        0, Qt.CheckState.Checked
                        if stroke.get("visible", True)
                        else Qt.CheckState.Unchecked)
                    item.addChild(child)
                    if stroke["id"] in selected_ids:
                        child.setSelected(True)
                        if lay["id"] == layer["id"]:
                            selected_item = child
                if lay["id"] == layer["id"] and selected_item is None:
                    selected_item = item
                item.setExpanded(True)
            if selected_item is not None:
                self.layers.setCurrentItem(
                    selected_item, 0,
                    QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self._loading = False
        self._sync_opacity()

    def refresh(self) -> None:
        total = total_strokes(self.app)
        layers = len(document(self.app)["layers"])
        self.count.setText(
            f"{total} strokes on {layers} layer{'s' if layers != 1 else ''}"
            if total else "No strokes yet")
        self.erase_all.setEnabled(bool(total))
        self._rebuild_layers()
        self._rebuild_planes()
        self._sync_state()

    def note_sheet(self, method: str) -> None:
        """Say whether the sheet composer took the image provider."""
        if method:
            self.sheet_note.setText(
                f"Sheet composer: connected through {method}.")
        else:
            self.sheet_note.setText(
                "Sheet Composer image insertion may be unavailable. Export "
                "a PNG and add it with the Composer's Image tool.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _overlay(app, viewport, painter: QPainter) -> None:
    """Paint annotations, then the active tool preview above them."""
    if _SHOW["visible"]:
        doc = document(app, viewport.scene)
        if any(lay["visible"] and _visible_strokes(lay)
               for lay in doc["layers"]):
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            _paint_layers(painter, viewport, doc)
    tool = getattr(viewport, "active_tool", None)
    if isinstance(tool, _StickyTool):
        tool.draw_preview(viewport, painter)


def _drawing_plane_overlay(app, viewport, painter: QPainter) -> None:
    """Draw the active plane's light grid when its visibility is enabled."""
    plane = active_drawing_plane(app, viewport.scene)
    if plane is None or not plane["visible"]:
        return
    origin = QVector3D(*plane["origin"])
    normal = QVector3D(*plane["normal"])
    u, v = _plane_basis(normal)
    camera = getattr(viewport, "camera", None)
    distance = float(getattr(camera, "distance", 4.0) or 4.0)
    finite_size = plane.get("size")
    half = (max(10.0, min(10000.0, distance * 2.0))
            if finite_size is None else max(0.05, float(finite_size) / 2.0))
    spacing = half / 4.0
    color = QColor("#2aa7cf")
    color.setAlphaF(0.58)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(color, 1.0))

    def line(a, b):
        projected = _project(viewport,
                             [(a.x(), a.y(), a.z()), (b.x(), b.y(), b.z())])
        if projected[0][2] and projected[1][2]:
            painter.drawLine(QPointF(projected[0][0], projected[0][1]),
                             QPointF(projected[1][0], projected[1][1]))

    for index in range(-4, 5):
        offset = spacing * index
        line(origin + u * -half + v * offset,
             origin + u * half + v * offset)
        line(origin + v * -half + u * offset,
             origin + v * half + u * offset)
    if finite_size is not None:
        corners = [origin + u * -half + v * -half,
                   origin + u * half + v * -half,
                   origin + u * half + v * half,
                   origin + u * -half + v * half]
        for first, second in zip(corners, corners[1:] + corners[:1]):
            line(first, second)
    painter.restore()


def setup(app) -> None:
    """The tab, the overlay, the sheet bridge and the menu entries."""
    global _APP
    _APP = app
    panel = ScribblePanel(app)
    dock = app.add_panel(TITLE, panel)
    app.on_document_changed(panel.schedule_refresh)
    app.add_overlay(lambda vp, painter: _drawing_plane_overlay(app, vp, painter))
    app.add_overlay(lambda vp, painter: _overlay(app, vp, painter))

    method = _register_sheet(app)
    try:
        panel.note_sheet(method)
    except Exception:  # noqa: BLE001
        pass

    if hasattr(app, "add_menu_action"):
        app.add_menu_action(
            "Croquis on model", lambda: app.show_panel(dock),
            tip="Free-hand and geometric annotation over the model, with "
                "layers and image export.")
        app.add_menu_action(
            "Croquis on model — Pen", lambda: _take_tool(app, ScribbleTool),
            tip="Free-hand line over the model.")
        app.add_menu_action(
            "Croquis on model — Brush", lambda: _take_tool(app, ScribbleBrushTool),
            tip="A soft halo that fades out, like a breath or dust.")
        app.add_menu_action(
            "Croquis on model — Shapes", lambda: _take_tool(app, ScribbleShapeTool),
            tip="Lines, arcs, rectangles, circles and polygons, with snaps.")
        app.add_menu_action(
            "Croquis on model — Eraser", lambda: _take_tool(app, ScribbleEraserTool),
            tip="Erase the annotations you sweep over.")
        app.add_menu_action(
            "Croquis on model — Move",
            lambda: _take_tool(app, ScribbleMoveTool),
            tip="Drag a mark, or fence several, and move them together.")
        app.add_menu_action(
            "Croquis on model — Edit Points",
            lambda: _take_tool(app, ScribblePointEditTool),
            tip="Window-select points on one drawing and drag to move them.")
        app.add_menu_action(
            "Croquis on model — Zoom Extents",
            lambda: _zoom_extents_action(app),
            tip="Frame all visible Croquis annotations.")
        app.add_menu_action(
            "Croquis on model — Zoom Object",
            lambda: _zoom_object_action(app),
            tip="Frame the selected Croquis drawing(s).")
        app.add_menu_action(
            "Croquis on model — Zoom Window",
            lambda: _take_tool(app, ScribbleZoomWindowTool),
            tip="Drag a viewport rectangle to zoom into it.")
        app.add_menu_action(
            "Croquis on model — Export PNG…", lambda: export_png(app),
            tip="Write the annotations to a PNG image.")
        app.add_menu_action(
            "Croquis on model — Export SVG…", lambda: export_svg(app),
            tip="Write the annotations to an SVG drawing.")
