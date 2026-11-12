"""Generate the Grembyte logo SVGs: icon, 16 px icon, wordmarks (dark and light).

Run from anywhere: ``python scripts/make_logo.py [SourceCodePro-Bold.otf]``.
The wordmark text is converted to paths, so the SVGs need no installed font.
Writes grembyte/ui/resources/favicon.svg (app icon), docs/assets/logo.svg
(docs header), docs/assets/favicon.svg (browser tab) and the wordmarks in
docs/assets/.
"""

import sys
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QPainterPath

ROOT = Path(__file__).resolve().parent.parent
FONT_FILE = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/fonts/adobe-source-code-pro-fonts/SourceCodePro-Bold.otf"

LIME = "#8cfa5a"        # brand green on dark backgrounds; icons.py replaces it in
                        # the toolbar logo with the theme's "brand" colour
GREEN_LIGHT = "#2b8a00"  # brand green on light backgrounds
INK_DARK = "#e6edf3"     # "grem" on dark
INK_LIGHT = "#1f2328"    # "grem" on light

# 0110 0111 = 0x67 = ASCII "g"
BITS = ("0110", "0111")
# Squares of 10 with gaps of 3: the grid is 49 x 23.
SIZE, GAP, STROKE = 10, 3, 2.5
GRID_W, GRID_H = 4 * SIZE + 3 * GAP, 2 * SIZE + GAP

COMMENT = ("<!-- Grembyte icon: the bits 0110 0111 (0x67, ASCII \"g\") as two rows of "
           "four squares; filled = 1, outlined = 0 -->")


def grid(green, transform=""):
    parts = []
    for row, bits in enumerate(BITS):
        for col, bit in enumerate(bits):
            x = col * (SIZE + GAP)
            y = row * (SIZE + GAP)
            if bit == "1":
                parts.append(f'<rect x="{x:g}" y="{y:g}" width="{SIZE:g}" height="{SIZE:g}"/>')
            else:
                inset = STROKE / 2
                parts.append(
                    f'<rect x="{x + inset:g}" y="{y + inset:g}" width="{SIZE - STROKE:g}" '
                    f'height="{SIZE - STROKE:g}" fill="none" stroke="{green}" '
                    f'stroke-width="{STROKE:g}"/>')
    inner = "\n      ".join(parts)
    attributes = f' transform="{transform}"' if transform else ""
    return f'<g fill="{green}"{attributes}>\n      {inner}\n    </g>'


def icon():
    """Square, for the app and window icon: the grid fills the width."""
    pad = 1
    top = (GRID_W - GRID_H) / 2
    box = f"{-pad} {-top - pad:g} {GRID_W + 2 * pad} {GRID_W + 2 * pad}"
    return (f'{COMMENT}\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}">\n'
            f'    {grid(LIME)}\n</svg>\n')


def icon_wide():
    """Just the grid, for the docs header (its height is fixed, so it is as large as can be)."""
    box = f"-1 -1 {GRID_W + 2} {GRID_H + 2}"
    return (f'{COMMENT}\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}">\n'
            f'    {grid(LIME)}\n</svg>\n')


def icon16():
    """Pixel grid for 16 px (and 32 px): squares of 3 with gaps of 1, outlines of 1."""
    parts = []
    for row, bits in enumerate(BITS):
        for col, bit in enumerate(bits):
            x, y = col * 4, 4 + row * 4
            if bit == "1":
                parts.append(f'<rect x="{x}" y="{y}" width="3" height="3"/>')
            else:
                parts.append(f'<path d="M{x} {y}h3v3h-3zM{x + 1} {y + 1}v1h1v-1z" '
                             'fill-rule="evenodd"/>')
    body = "\n      ".join(parts)
    return (f'{COMMENT.replace("icon:", "icon for 16 px (and 32 px):")}\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" shape-rendering="crispEdges">\n'
            f'    <g fill="{LIME}">\n      {body}\n    </g>\n</svg>\n')


def text_path(text, font, x, baseline):
    path = QPainterPath()
    path.addText(x, baseline, font, text)
    d = []
    i = 0
    n = path.elementCount()
    while i < n:
        e = path.elementAt(i)
        if e.isMoveTo():
            d.append(f"M{e.x:.2f} {e.y:.2f}")
            i += 1
        elif e.isLineTo():
            d.append(f"L{e.x:.2f} {e.y:.2f}")
            i += 1
        else:  # curveTo + two CurveToData elements
            c1, c2 = path.elementAt(i + 1), path.elementAt(i + 2)
            d.append(f"C{e.x:.2f} {e.y:.2f} {c1.x:.2f} {c1.y:.2f} {c2.x:.2f} {c2.y:.2f}")
            i += 3
    return "".join(d) + "Z", path.boundingRect()


def wordmark(font, ink, green, variant):
    scale = 1.25
    baseline = 47
    grid_w = GRID_W * scale
    grem, rect_grem = text_path("grem", font, grid_w + 14, baseline)
    byte, rect_byte = text_path("byte", font, rect_grem.right() + 2, baseline)
    width = rect_byte.right() + 2
    # The grid stands on the baseline.
    transform = f"translate(1 {baseline - GRID_H * scale:g}) scale({scale:g})"
    return (f'<!-- Grembyte wordmark ({variant} backgrounds): icon and "grembyte" in '
            f'Source Code Pro Bold (SIL OFL 1.1), as paths -->\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} 64">\n'
            f'    {grid(green, transform)}\n'
            f'    <path fill="{ink}" d="{grem}"/>\n'
            f'    <path fill="{green}" d="{byte}"/>\n</svg>\n')


def main():
    app = QGuiApplication([])  # noqa: F841 - fonts need an application
    family = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(FONT_FILE))[0]
    font = QFont(family)
    font.setPixelSize(52)
    font.setWeight(QFont.Weight.Bold)
    assets = ROOT / "docs" / "assets"
    (ROOT / "grembyte" / "ui" / "resources" / "favicon.svg").write_text(icon())
    (assets / "logo.svg").write_text(icon_wide())
    (assets / "favicon.svg").write_text(icon16())
    (assets / "wordmark-dark.svg").write_text(wordmark(font, INK_DARK, LIME, "dark"))
    (assets / "wordmark-light.svg").write_text(wordmark(font, INK_LIGHT, GREEN_LIGHT, "light"))


main()
