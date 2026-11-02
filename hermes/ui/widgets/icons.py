"""Material Icons (Outlined) from ui/resources/icons, tinted to match the dark theme."""

from functools import lru_cache
from pathlib import Path
import re

from PySide6.QtCore import QByteArray, QEvent, QObject, QRectF, Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap  # pylint: disable=import-error,no-name-in-module
from PySide6.QtSvg import QSvgRenderer  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

ICONS_DIR = Path(__file__).resolve().parent.parent / "resources" / "icons"

# Text colours in styles.py: (normal, hover/selected, disabled) of menu items
# and of the kinds of buttons.
_MENU = ("#c9d1d9", "#ffffff", "#484f58")
_BUTTON_COLORS = {
    "": ("#c9d1d9", "#58a6ff", "#484f58"),
    "btn_danger": ("#f85149", "#ffffff", "#6e3a37"),
    "btn_primary": ("#ffffff", "#ffffff", "#6e7f99"),
}
_MENU_ICON_NAMES = {}
# The colours of the theme last applied; until then those of Default.
_DEFAULT_COLORS = {"muted": "#8b949e", "warning": "#f0883e"}
_theme_colors = dict(_DEFAULT_COLORS)


def theme_color(key):
    """Return the colour ``key`` (e.g. "muted") of the current theme."""
    return _theme_colors.get(key, _DEFAULT_COLORS.get(key, "#808080"))


def _render(name, color, size, ratio):
    """Paint the icon ``name`` in ``color`` on a square pixmap."""
    svg = (ICONS_DIR / f"{name}.svg").read_text(encoding="utf-8")
    svg = re.sub(r'(<svg\b[^>]*?)\s+fill=(["\']).*?\2', r"\1", svg, count=1)
    renderer = QSvgRenderer(QByteArray(svg.replace("<svg ", f'<svg fill="{color}" ', 1).encode()))
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
