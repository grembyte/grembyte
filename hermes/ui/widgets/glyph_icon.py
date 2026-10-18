"""Icons drawn from text glyphs, tinted to match the dark theme."""

from PyQt6.QtCore import Qt, QRectF  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import (  # pylint: disable=import-error,no-name-in-module
    QColor, QFont, QFontMetricsF, QGuiApplication, QIcon, QPainter, QPixmap,
)

# Text colours of menu items in styles.py: normal, selected, disabled.
_MODE_COLORS = (
    (QIcon.Mode.Normal, "#c9d1d9"),
    (QIcon.Mode.Active, "#ffffff"),
    (QIcon.Mode.Selected, "#ffffff"),
    (QIcon.Mode.Disabled, "#484f58"),
)


def _render(glyph, color, size, ratio):
    """Paint ``glyph`` centred and scaled to fit a square pixmap."""
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)

    font = QFont(QGuiApplication.font())
    font.setPixelSize(size)
    width = QFontMetricsF(font).horizontalAdvance(glyph)
    if width > size:  # wide glyphs such as "{ }" shrink to fit
        font.setPixelSize(max(1, int(size * size / width)))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setFont(font)
    painter.setPen(QColor(color))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return pixmap


def glyph_icon(glyph, size=16):
    """Return a QIcon showing ``glyph`` in the theme's colour for each state."""
    screen = QGuiApplication.primaryScreen()
    ratio = screen.devicePixelRatio() if screen else 1.0
    icon = QIcon()
    for mode, color in _MODE_COLORS:
        icon.addPixmap(_render(glyph, color, size, ratio), mode)
    return icon
