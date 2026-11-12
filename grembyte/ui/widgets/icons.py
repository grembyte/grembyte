"""Material Icons (Outlined) from ui/resources/icons, tinted to match the theme.

Also the colours of the current theme for widgets that colour their content
(``theme_color``, ``theme_qcolor``): status texts, pointers, value types.
"""

from functools import lru_cache
import json
from pathlib import Path
import re

from PySide6.QtCore import QByteArray, QEvent, QObject, QRectF, Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap  # pylint: disable=import-error,no-name-in-module
from PySide6.QtSvg import QSvgRenderer  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

ICONS_DIR = Path(__file__).resolve().parent.parent / "resources" / "icons"
# The logo, and the colour it is drawn in (scripts/make_logo.py); the toolbar
# shows it in the theme's "brand" colour instead.
BRAND_ICON = ICONS_DIR.parent / "favicon.svg"
_BRAND_FILE_COLOR = "#8cfa5a"
# The default theme, whose colours apply until a theme is applied (tests,
# widgets built without the main window).
_DEFAULT_THEME = (Path(__file__).resolve().parent.parent / "themes"
                  / "grembyte_themes_Github-Dark.json")

# Text colours in styles.py: (normal, hover/selected, disabled) of menu items
# and of the kinds of buttons.
_MENU = ("#e6edf3", "#ffffff", "#484f58")
_BUTTON_COLORS = {
    "": ("#e6edf3", "#2f81f7", "#484f58"),
    "btn_danger": ("#f85149", "#ffffff", "#6e3a37"),
    "btn_primary": ("#ffffff", "#ffffff", "#6e7f99"),
}
_MENU_ICON_NAMES = {}
# The colours of the theme last applied; until then those of Github-Dark.
_DEFAULT_COLORS = json.loads(_DEFAULT_THEME.read_text(encoding="utf-8"))["colors"]
_theme_colors = dict(_DEFAULT_COLORS)


def theme_color(key):
    """Return the colour ``key`` (e.g. "muted") of the current theme, as text."""
    return _theme_colors.get(key, _DEFAULT_COLORS.get(key, "#808080"))


def parse_color(value):
    """Return a theme colour (``#rrggbb`` or CSS ``rgba(r, g, b, a)``) as a QColor.

    QColor reads the hex form but not CSS rgba.
    """
    match = re.fullmatch(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)", value)
    if match:
        return QColor(int(match[1]), int(match[2]), int(match[3]), round(float(match[4]) * 255))
    return QColor(value)


def theme_qcolor(key):
    """Return the colour ``key`` of the current theme as a QColor."""
    return parse_color(theme_color(key))


def _render(name, color, size, ratio):
    """Paint the icon ``name`` in ``color`` on a square pixmap."""
    svg = (ICONS_DIR / f"{name}.svg").read_text(encoding="utf-8")
    svg = re.sub(r'(<svg\b[^>]*?)\s+fill=(["\']).*?\2', r"\1", svg, count=1)
    return _paint(svg.replace("<svg ", f'<svg fill="{color}" ', 1), size, ratio)


def _paint(svg, size, ratio):
    """Paint the SVG text ``svg`` on a square pixmap of ``size`` logical pixels."""
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # The target in logical pixels: without it the renderer misjudges the
    # size on scaled screens (device pixel ratio above 1) and crops the icon.
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


@lru_cache(maxsize=None)
def _icon(name, normal, active, disabled, size):
    screen = QGuiApplication.primaryScreen()
    ratio = screen.devicePixelRatio() if screen else 1.0
    icon = QIcon()
    for mode, color in ((QIcon.Mode.Normal, normal), (QIcon.Mode.Active, active),
                        (QIcon.Mode.Selected, active), (QIcon.Mode.Disabled, disabled)):
        icon.addPixmap(_render(name, color, size, ratio), mode)
    return icon


def material_icon(name, size=16):
    """Return the icon ``name`` (e.g. "edit") coloured like menu items."""
    icon = _icon(name, *_MENU, size)
    _MENU_ICON_NAMES[icon.cacheKey()] = name
    return icon


class _ButtonIcon(QObject):
    """Shows a button's icon in its text colour, also while hovered."""

    def __init__(self, button):
        super().__init__(button)
        self.normal = self.hover = None
        self.name = None
        self.size = 16
        button.installEventFilter(self)

    def show(self, button, normal, hover):
        self.normal, self.hover = normal, hover
        button.setIcon(hover if button.underMouse() and button.isEnabled() else normal)

    def eventFilter(self, watched, event):  # pylint: disable=invalid-name
        if event.type() == QEvent.Type.Enter and watched.isEnabled():
            watched.setIcon(self.hover)
        elif event.type() in (QEvent.Type.Leave, QEvent.Type.EnabledChange):
            watched.setIcon(self.normal)
        return False


def set_button_icon(button, name, size=16):
    """Give ``button`` the icon ``name`` in the colours of its kind of button.

    Call it after the button's object name (btn_danger, btn_primary) is set,
    and again when it changes; also to change the icon, e.g. from "pause" to
    "play_arrow". ``name`` None takes the icon away.
    """
    holder = button.findChild(_ButtonIcon, options=Qt.FindChildOption.FindDirectChildrenOnly)
    if holder is None:
        holder = _ButtonIcon(button)
    holder.name = name
    holder.size = size
    if name is None:
        holder.show(button, QIcon(), QIcon())
        return
    normal, hover, disabled = _BUTTON_COLORS.get(button.objectName(), _BUTTON_COLORS[""])
    # Focus shows QIcon.Mode.Active: in the normal colour like the text.
    holder.show(button, _icon(name, normal, normal, disabled, size),
                _icon(name, hover, hover, disabled, size))


class _LabelIcon(QObject):
    """Remembers which icon a label shows, to tint it anew with the theme."""

    def __init__(self, label):
        super().__init__(label)
        self.name = None
        self.color_key = None
        self.size = 16


def set_label_icon(label, name, color_key, size=16):
    """Show the icon ``name`` on ``label`` in the theme colour ``color_key``.

    For a label of its own beside a text label, e.g. in the text's colour.
    """
    holder = label.findChild(_LabelIcon, options=Qt.FindChildOption.FindDirectChildrenOnly)
    if holder is None:
        holder = _LabelIcon(label)
    holder.name, holder.color_key, holder.size = name, color_key, size
    label.setPixmap(_render(name, theme_color(color_key), size, label.devicePixelRatioF()))


class _BrandIcon(QObject):
    """Marks a label that shows the logo, to colour it anew with the theme."""

    def __init__(self, label):
        super().__init__(label)
        self.size = 16


def set_brand_icon(label, size):
    """Show the logo on ``label`` in the theme's "brand" colour.

    The bright green of the logo file is hard to see on light themes; they
    give a darker one.
    """
    holder = label.findChild(_BrandIcon, options=Qt.FindChildOption.FindDirectChildrenOnly)
    if holder is None:
        holder = _BrandIcon(label)
    holder.size = size
    svg = BRAND_ICON.read_text(encoding="utf-8").replace(_BRAND_FILE_COLOR, theme_color("brand"))
    label.setPixmap(_paint(svg, size, label.devicePixelRatioF()))


def apply_icon_theme(colors):
    """Tint menu, button and label icons to match a theme, updating existing
    widgets; theme_color gives its colours from now on."""
    global _MENU, _BUTTON_COLORS  # pylint: disable=global-statement
    _theme_colors.clear()
    _theme_colors.update(colors)
    previous_menu_icons = dict(_MENU_ICON_NAMES)
    _MENU = (colors["text"], colors["white"], colors["disabled_text"])
    _BUTTON_COLORS = {
        "": (colors["text"], colors["accent"], colors["disabled_text"]),
        "btn_danger": (colors["danger"], colors["white"], colors["danger_disabled_text"]),
        "btn_primary": (colors["white"], colors["white"], colors["accent_disabled_text"]),
    }
    _icon.cache_clear()
    _MENU_ICON_NAMES.clear()

    application = QApplication.instance()
    if application is None:
        return
    for widget in application.allWidgets():
        for action in widget.actions():
            name = previous_menu_icons.get(action.icon().cacheKey())
            if name is not None:
                action.setIcon(material_icon(name))
        holder = widget.findChild(
            _ButtonIcon, options=Qt.FindChildOption.FindDirectChildrenOnly)
        if holder is not None:
            set_button_icon(widget, holder.name, holder.size)
        label_icon = widget.findChild(
            _LabelIcon, options=Qt.FindChildOption.FindDirectChildrenOnly)
        if label_icon is not None:
            set_label_icon(widget, label_icon.name, label_icon.color_key, label_icon.size)
        brand_icon = widget.findChild(
            _BrandIcon, options=Qt.FindChildOption.FindDirectChildrenOnly)
        if brand_icon is not None:
            set_brand_icon(widget, brand_icon.size)
