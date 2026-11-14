"""A QGraphicsScene as a self-contained HTML page with an SVG drawing.

Used by the reference graph's export. The page shows the scene as it was
drawn: boxes, lines, arrows and texts in the colours of the theme the
scene was drawn in, the tooltips of items as SVG titles (shown when the
pointer rests on them). It needs nothing besides itself (no scripts, no
files), and escapes every text of the game. The browser's zoom scales it.
"""

import html

from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QFontInfo, QFontMetricsF, QPainterPath  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsSimpleTextItem,
)

# Room around the drawing, in scene pixels
MARGIN = 20

_DASHES = {
    Qt.PenStyle.DashLine: "6 4",
    Qt.PenStyle.DotLine: "2 3",
}


def _number(value):
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _color(color):
    """A QColor as SVG text; transparent as "none"."""
    if color.alpha() == 0:
        return "none"
    if color.alpha() == 255:
        return color.name()
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alphaF():.3f})"


def _paint(item):
    """The fill and stroke attributes of a shape item."""
    brush, pen = item.brush(), item.pen()
    fill = "none" if brush.style() == Qt.BrushStyle.NoBrush else _color(brush.color())
    parts = [f'fill="{fill}"']
    if pen.style() == Qt.PenStyle.NoPen:
        parts.append('stroke="none"')
    else:
        parts.append(f'stroke="{_color(pen.color())}" stroke-width="{_number(pen.widthF())}"')
        if pen.style() in _DASHES:
            parts.append(f'stroke-dasharray="{_DASHES[pen.style()]}"')
    return " ".join(parts)


def svg_path(path):
    """The SVG path data of a QPainterPath (moves, lines and cubic curves)."""
    parts = []
    count = path.elementCount()
    i = 0
    while i < count:
        element = path.elementAt(i)
        point = f"{_number(element.x)} {_number(element.y)}"
        if element.type == QPainterPath.ElementType.MoveToElement:
            parts.append(f"M {point}")
        elif element.type == QPainterPath.ElementType.LineToElement:
            parts.append(f"L {point}")
        elif element.type == QPainterPath.ElementType.CurveToElement and i + 2 < count:
            second, end = path.elementAt(i + 1), path.elementAt(i + 2)
            parts.append(f"C {point} {_number(second.x)} {_number(second.y)} "
                         f"{_number(end.x)} {_number(end.y)}")
            i += 2
        i += 1
    return " ".join(parts)


def _title(item):
    tip = item.toolTip()
    return f"<title>{html.escape(tip)}</title>" if tip else ""


def _shape(item, dx, dy):
    """The SVG of one shape item (scene coordinates moved by ``dx``, ``dy``)."""
    if isinstance(item, QGraphicsRectItem):
        rect = item.mapRectToScene(item.rect()).translated(dx, dy)
        return (f'<rect x="{_number(rect.x())}" y="{_number(rect.y())}" '
                f'width="{_number(rect.width())}" height="{_number(rect.height())}" '
                f'{_paint(item)}>{_title(item)}</rect>')
    if isinstance(item, QGraphicsPathItem):
        path = item.mapToScene(item.path()).translated(dx, dy)
        return f'<path d="{svg_path(path)}" {_paint(item)}>{_title(item)}</path>'
    if isinstance(item, QGraphicsPolygonItem):
        polygon = item.mapToScene(item.polygon()).translated(dx, dy)
        points = " ".join(f"{_number(point.x())},{_number(point.y())}" for point in polygon)
        return f'<polygon points="{points}" {_paint(item)}>{_title(item)}</polygon>'
    if isinstance(item, QGraphicsSimpleTextItem):
        font = item.font()
        top_left = item.mapToScene(0, 0)
        baseline = top_left.y() + dy + QFontMetricsF(font).ascent()
        weight = ' font-weight="bold"' if font.bold() else ""
        # Without a tooltip of its own it lets the pointer through to the box below
        tip = ' class="tip"' if item.toolTip() else ""
        return (f'<text{tip} x="{_number(top_left.x() + dx)}" y="{_number(baseline)}" '
                f'font-size="{QFontInfo(font).pixelSize()}"{weight} '
                f'fill="{_color(item.brush().color())}">{html.escape(item.text())}'
                f'{_title(item)}</text>')
    return ""


def scene_svg(scene):
    """The visible items of ``scene`` as an SVG element, bottom to top."""
    bounds = scene.itemsBoundingRect().adjusted(-MARGIN, -MARGIN, MARGIN, MARGIN)
    dx, dy = -bounds.x(), -bounds.y()
    shapes = [_shape(item, dx, dy)
              for item in scene.items(Qt.SortOrder.AscendingOrder) if item.isVisible()]
    width, height = _number(bounds.width()), _number(bounds.height())
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            + "".join(shape for shape in shapes if shape) + "</svg>")


def render_html(scene, title, facts, colors, footer="", language="en"):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """The page: ``title``, ``facts`` [(label, value)] above the drawing of
    ``scene``, ``footer`` below it; ``colors`` the theme's background, text
    and muted colours (``{"background": ..., "text": ..., "muted": ...}``)."""
    def esc(text):
        return html.escape("" if text is None else str(text))

    style = (f"body {{ font-family: system-ui, sans-serif; margin: 1.5rem; "
             f"background: {colors['background']}; color: {colors['text']}; }}\n"
             "h1 { font-size: 1.3rem; margin-bottom: .25rem; word-break: break-all; }\n"
             f"dl {{ display: grid; grid-template-columns: max-content auto; gap: .2rem 1rem; "
             f"color: {colors['muted']}; }}\n"
             "dd { margin: 0; word-break: break-all; }\n"
             "svg { display: block; margin-top: 1rem; }\n"
             f"footer {{ margin-top: 1.5rem; color: {colors['muted']}; font-size: .8rem; }}\n"
             "svg text { white-space: pre; font-family: system-ui, sans-serif; }\n"
             "svg text:not(.tip) { pointer-events: none; }\n")
    return "\n".join([
        "<!DOCTYPE html>",
        f'<html lang="{esc(language)}">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{esc(title)}</title>",
        f"<style>{style}</style>",
        "</head>",
        "<body>",
        f"<h1>{esc(title)}</h1>",
        "<dl>",
        *(f"<dt>{esc(label)}</dt><dd>{esc(value)}</dd>" for label, value in facts),
        "</dl>",
        scene_svg(scene),
        f"<footer>{esc(footer)}</footer>" if footer else "",
        "</body>",
        "</html>",
        "",
    ])
