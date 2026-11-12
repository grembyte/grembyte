"""Application-wide Qt stylesheet for grembyte (dark GitHub theme)."""

import re
import tempfile
from pathlib import Path

from PySide6.QtCore import QStandardPaths  # pylint: disable=import-error,no-name-in-module

# NOTE: Qt style sheets do not resolve the generic family name "monospace",
# so concrete font names must be listed. Add your preferred font first.
MONO = "'JetBrains Mono', 'Fira Code', 'DejaVu Sans Mono', 'Liberation Mono'"

STYLE = """
    /* ------------------------------------------------------------------
       Base
       ------------------------------------------------------------------ */
    QMainWindow, QWidget {
        background-color: #0d1117;
        color: #c9d1d9;
        font-size: 13px;
    }
    /* Labels must never paint their own background (visible as dark
       patches on the lighter toolbar and status bar otherwise). */
    QLabel {
        background: transparent;
    }

    /* ------------------------------------------------------------------
       Panels: one subtle frame, title clearly above the field labels
       ------------------------------------------------------------------ */
    QGroupBox {
        border: 1px solid #21262d;
        border-radius: 8px;
        margin-top: 14px;
        padding: 14px 10px 10px 10px;
        color: #c9d1d9;
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 6px;
    }
    /* Field labels inside panels: quieter and smaller than the title */
    QGroupBox QLabel {
        color: #8b949e;
        font-size: 12px;
        font-weight: normal;
        text-transform: none;
        letter-spacing: 0;
    }

    /* ------------------------------------------------------------------
       Tables: no own frame (the panel already has one), no focus rectangle
       ------------------------------------------------------------------ */
    QTableWidget {
        background-color: #0d1117;
        border: none;
        outline: none;
        gridline-color: #21262d;
        selection-background-color: rgba(31, 111, 235, 0.28);
        selection-color: #c9d1d9;
        alternate-background-color: #11161d;
        font-family: __MONO__;
        font-size: 12px;
    }
    QTableWidget::item {
        padding: 4px 8px;
        border: none;
    }
    /* Widgets in cells (links, check boxes) let the row's selection show through. */
    QWidget#cell_widget, QWidget#cell_widget QCheckBox {
        background: transparent;
    }
    /* The theme's text colour: white would vanish on a light theme's selection. */
    QTableWidget::item:selected {
        background-color: rgba(31, 111, 235, 0.28);
        color: #c9d1d9;
    }
    QHeaderView {
        background-color: #161b22;
    }
    QHeaderView::section {
        background-color: #161b22;
        color: #8b949e;
        border: none;
        border-bottom: 1px solid #30363d;
        /* Shows where a column ends, i.e. where it can be resized */
        border-right: 1px solid #30363d;
        padding: 6px 8px;
        font-size: 11px;
        font-weight: normal;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    /* Selected rows would otherwise turn the header text bold */
    QHeaderView::section:checked {
        font-weight: normal;
        background-color: #161b22;
    }

    /* ------------------------------------------------------------------
       Buttons
       ------------------------------------------------------------------ */
    QPushButton {
        background-color: #21262d;
        color: #c9d1d9;
        border: 1px solid #30363d;
        border-radius: 6px;
        padding: 6px 14px;
        font-size: 12px;
    }
    QPushButton:hover {
        background-color: #30363d;
        border-color: #58a6ff;
        color: #58a6ff;
    }
    QPushButton:pressed {
        background-color: #1f6feb;
        border-color: #1f6feb;
        color: #ffffff;
    }
    QPushButton:disabled {
        background-color: #161b22;
        border-color: #21262d;
        color: #484f58;
    }

    /* Primary action: use the object name or the dynamic property
       primary="true" so the highlighted button can follow the workflow. */
    QPushButton#btn_primary, QPushButton[primary="true"] {
        background-color: #1f6feb;
        border-color: #1f6feb;
        color: #ffffff;
    }
    QPushButton#btn_primary:hover, QPushButton[primary="true"]:hover {
        background-color: #388bfd;
        border-color: #388bfd;
        color: #ffffff;
    }
    QPushButton#btn_primary:disabled, QPushButton[primary="true"]:disabled {
        background-color: #12315f;
        border-color: #12315f;
        color: #6e7f99;
    }

    QPushButton#btn_danger {
        background-color: #21262d;
        border-color: #f85149;
        color: #f85149;
    }
    QPushButton#btn_danger:hover {
        background-color: #f85149;
        color: #ffffff;
    }
    QPushButton#btn_danger:disabled {
        background-color: #161b22;
        border-color: #4a2523;
        color: #6e3a37;
    }

    /* ------------------------------------------------------------------
       Inputs
       ------------------------------------------------------------------ */
    QLineEdit, QComboBox, QPlainTextEdit, QSpinBox, QDoubleSpinBox {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 6px;
        padding: 6px 10px;
        color: #c9d1d9;
        font-family: __MONO__;
        font-size: 13px;
    }
    QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus,
    QSpinBox:focus, QDoubleSpinBox:focus {
        border-color: #58a6ff;
    }
    /* Editors inside table cells: the cell height is fixed, so use less padding. */
    QTableWidget QLineEdit, QTableWidget QComboBox {
        padding: 2px 6px;
        border-radius: 4px;
    }
    QComboBox QAbstractItemView {
        background-color: #161b22;
        border: 1px solid #30363d;
        selection-background-color: #1f6feb;
        outline: none;
    }

    /* ------------------------------------------------------------------
       Layout chrome
       ------------------------------------------------------------------ */
    /* A 2px line in the middle of the handle shows where to drag;
       hovered or dragged, the whole handle turns blue. */
    QSplitter::handle:horizontal {
        width: 8px;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(0, 0, 0, 0), stop:0.37 rgba(0, 0, 0, 0),
            stop:0.38 #6e7681, stop:0.62 #6e7681,
            stop:0.63 rgba(0, 0, 0, 0), stop:1 rgba(0, 0, 0, 0));
    }
    QSplitter::handle:vertical {
        height: 8px;
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 rgba(0, 0, 0, 0), stop:0.37 rgba(0, 0, 0, 0),
            stop:0.38 #6e7681, stop:0.62 #6e7681,
            stop:0.63 rgba(0, 0, 0, 0), stop:1 rgba(0, 0, 0, 0));
    }
    QSplitter::handle:hover, QSplitter::handle:pressed {
        background: #1f6feb;
    }

    QToolBar {
        background-color: #161b22;
        border-bottom: 1px solid #30363d;
        spacing: 6px;
        padding: 4px;
    }
    QWidget#toolbar_spacer {
        background-color: transparent;
    }

    QToolBar::separator {
        background-color: #30363d;
        width: 1px;
        margin: 4px 8px;
    }

    QStatusBar {
        background-color: #161b22;
        border-top: 1px solid #30363d;
        color: #8b949e;
        font-size: 11px;
    }
    QStatusBar::item {
        border: none;
    }
    QStatusBar QLabel {
        color: #8b949e;
        font-size: 11px;
        padding: 0 10px;
    }

    QProgressBar {
        background-color: #161b22;
        border: none;
        border-radius: 3px;
        max-height: 6px;
        color: transparent;
    }
    QProgressBar::chunk {
        background-color: #1f6feb;
        border-radius: 3px;
    }

    /* ------------------------------------------------------------------
       Menus, tooltips, scroll bars
       ------------------------------------------------------------------ */
    QMenu {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 6px;
        padding: 4px;
    }
    QMenu::item {
        padding: 6px 20px;
        border-radius: 4px;
    }
    QMenu::item:selected {
        background-color: #1f6feb;
        color: #ffffff;
    }
    QMenu::icon {
        padding-left: 10px;
    }
    QMenu::item:disabled,
    QMenu::item:disabled:selected {
        background-color: transparent;
        color: #484f58;
    }
    QMenu::separator {
        height: 1px;
        background-color: #30363d;
        margin: 4px 8px;
    }
    QToolTip {
        background-color: #161b22;
        color: #c9d1d9;
        border: 1px solid #30363d;
        padding: 4px 6px;
    }

    QScrollBar:vertical {
        background: transparent;
        width: 10px;
        margin: 0;
    }
    QScrollBar:horizontal {
        background: transparent;
        height: 10px;
        margin: 0;
    }
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
        background: #30363d;
        border-radius: 5px;
        min-height: 24px;
        min-width: 24px;
    }
    QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
        background: #484f58;
    }
    QScrollBar::add-line, QScrollBar::sub-line,
    QScrollBar::add-page, QScrollBar::sub-page {
        background: transparent;
        border: none;
        width: 0;
        height: 0;
    }

    /* ------------------------------------------------------------------
       Lists
       ------------------------------------------------------------------ */
    QListWidget {
        background-color: #0d1117;
        border: 1px solid #30363d;
        border-radius: 4px;
        alternate-background-color: #11161d;
        outline: none;
        color: #c9d1d9;
    }
    /* No colour on the items: what a list colours itself (e.g. method
       templates) has to show, and disabled lists their disabled colour. */
    QListWidget::item {
        padding: 3px 6px;
        font-family: __MONO__;
        font-size: 12px;
    }
    QListWidget::item:selected {
        background-color: rgba(31, 111, 235, 0.28);
        color: #c9d1d9;
    }
    QListWidget::item:hover {
        background-color: #21262d;
    }

    /* ------------------------------------------------------------------
       Named widgets
       ------------------------------------------------------------------ */
    QLabel#lbl_addr {
        color: #3fb950;
        font-family: __MONO__;
    }
    QLabel#lbl_title {
        color: #8cfa5a;
        font-size: 15px;
        font-weight: bold;
    }
    QLabel#lbl_process {
        color: #f0883e;
        font-size: 12px;
    }
    /* The scan options scroll inside their group box, on its background */
    QScrollArea#scan_options, QScrollArea#scan_options > QWidget > QWidget {
        background: transparent;
    }
    /* Group boxes used as tab pages: the tab pane already draws the frame */
    QGroupBox#tab_page {
        border: none;
        margin-top: 0;
        padding: 8px 2px 2px 2px;
    }

    QTabWidget::pane {
        border: 1px solid #21262d;
        border-radius: 8px;
        top: -1px;
        padding: 4px 8px 8px 8px;
    }
    QTabBar::tab {
        background: transparent;
        color: #8b949e;
        border: none;
        border-bottom: 2px solid transparent;
        padding: 6px 14px;
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    QTabBar::tab:selected {
        color: #c9d1d9;
        border-bottom-color: #58a6ff;
    }
    QTabBar::tab:hover:!selected {
        color: #c9d1d9;
    }
    QTabBar::close-button {
        image: url(__TAB_CLOSE_IMAGE__);
        subcontrol-position: right;
        border-radius: 4px;
        width: 14px;
        height: 14px;
        padding: 1px;
    }
    QTabBar::close-button:hover {
        image: url(__TAB_CLOSE_HOVER_IMAGE__);
        background-color: #21262d;
    }
    QFrame#divider {
        background-color: #21262d;
        border: none;
        min-height: 1px;
        max-height: 1px;
    }

    /* ------------------------------------------------------------------
       Check boxes, also those of list and table items. Without these rules
       Fusion draws them from the desktop's palette, not the theme's: near
       invisible on the light themes. The images are files of the theme's
       colours (see _indicator_images).
       ------------------------------------------------------------------ */
    QCheckBox::indicator, QAbstractItemView::indicator {
        width: 12px;
        height: 12px;
        border: 1px solid #8b949e;
        border-radius: 3px;
        background-color: #0d1117;
    }
    QCheckBox::indicator:hover, QAbstractItemView::indicator:hover {
        border-color: #58a6ff;
    }
    QCheckBox::indicator:checked, QAbstractItemView::indicator:checked {
        border-color: #58a6ff;
        background-color: #58a6ff;
        image: url(__CHECK_IMAGE__);
    }
    QCheckBox::indicator:indeterminate, QAbstractItemView::indicator:indeterminate {
        border-color: #58a6ff;
        background-color: #58a6ff;
        image: url(__PARTIAL_IMAGE__);
    }
    QCheckBox::indicator:disabled, QAbstractItemView::indicator:disabled {
        border-color: #484f58;
    }
    QCheckBox::indicator:checked:disabled, QAbstractItemView::indicator:checked:disabled {
        background-color: #484f58;
        image: url(__CHECK_DISABLED_IMAGE__);
    }
    QCheckBox::indicator:indeterminate:disabled,
    QAbstractItemView::indicator:indeterminate:disabled {
        background-color: #484f58;
        image: url(__PARTIAL_DISABLED_IMAGE__);
    }

    /* ------------------------------------------------------------------
       Spin boxes: framed like the line edits beside them. With a border of
       their own Fusion no longer draws the buttons' arrows, so they are
       images of the theme's colours too.
       ------------------------------------------------------------------ */
    QSpinBox, QDoubleSpinBox {
        padding: 4px 6px;
    }
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
        width: 18px;
        border: none;
        background: transparent;
    }
    QSpinBox::up-button:hover, QSpinBox::down-button:hover,
    QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
        background-color: #21262d;
    }
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
        width: 14px;
        height: 14px;
        image: url(__SPIN_UP_IMAGE__);
    }
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
        width: 14px;
        height: 14px;
        image: url(__SPIN_DOWN_IMAGE__);
    }
    QSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off,
    QDoubleSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:off {
        image: url(__SPIN_UP_DISABLED_IMAGE__);
    }
    QSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off,
    QDoubleSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:off {
        image: url(__SPIN_DOWN_DISABLED_IMAGE__);
    }
""".replace("__MONO__", MONO)


_STYLE_COLOR_KEYS = {
    "#0d1117": "background",
    "#c9d1d9": "text",
    "#21262d": "border_subtle",
    "#8b949e": "muted",
    "rgba(31, 111, 235, 0.28)": "selection",
    "#ffffff": "white",
    "#11161d": "alternate",
    "#161b22": "surface",
    "#30363d": "border",
    "#58a6ff": "accent",
    "#1f6feb": "accent_pressed",
    "#484f58": "disabled_text",
    "#388bfd": "accent_hover",
    "#12315f": "accent_disabled_background",
    "#6e7f99": "accent_disabled_text",
    "#f85149": "danger",
    "#4a2523": "danger_disabled_border",
    "#6e3a37": "danger_disabled_text",
    "#6e7681": "handle",
    "#3fb950": "success",
    "#f0883e": "warning",
    "#8cfa5a": "brand",
}


_ICONS_DIR = Path(__file__).resolve().parents[1] / "resources" / "icons"
# Placeholder in STYLE: (Material icon, colour key of the mark)
_INDICATOR_IMAGES = {
    "__CHECK_IMAGE__": ("check", "white"),
    "__PARTIAL_IMAGE__": ("remove", "white"),
    "__CHECK_DISABLED_IMAGE__": ("check", "background"),
    "__PARTIAL_DISABLED_IMAGE__": ("remove", "background"),
    "__SPIN_UP_IMAGE__": ("expand_less", "muted"),
    "__SPIN_DOWN_IMAGE__": ("expand_more", "muted"),
    "__SPIN_UP_DISABLED_IMAGE__": ("expand_less", "disabled_text"),
    "__SPIN_DOWN_DISABLED_IMAGE__": ("expand_more", "disabled_text"),
    "__TAB_CLOSE_IMAGE__": ("close", "muted"),
    "__TAB_CLOSE_HOVER_IMAGE__": ("close", "danger"),
}
_RGBA = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)")


def _indicator_dir():
    cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericCacheLocation)
    return Path(cache or tempfile.gettempdir()) / "grembyte" / "indicators"


def _indicator_image(icon, color, directory):
    """The path of ``icon`` in ``color`` as an SVG file, written once.

    Style sheets take images only as files, and an SVG's colour is in the file.
    """
    rgba = _RGBA.fullmatch(color)
    fill = (f'fill="rgb({rgba[1]},{rgba[2]},{rgba[3]})" fill-opacity="{rgba[4]}"'
            if rgba else f'fill="{color}"')
    path = directory / f"{icon}-{re.sub(r'[^0-9A-Za-z]', '', color).lower()}.svg"
    if not path.exists():
        svg = (_ICONS_DIR / f"{icon}.svg").read_text(encoding="utf-8")
        svg = re.sub(r'(<svg\b[^>]*?)\s+fill=(["\']).*?\2', r"\1", svg, count=1)
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(svg.replace("<svg ", f"<svg {fill} ", 1), encoding="utf-8")
    return path.as_posix()


def build_stylesheet(colors, indicator_dir=None):
    """Build the application stylesheet with the supplied theme colors.

    The check box images go to ``indicator_dir`` (default: the user's cache).
    """
    stylesheet = STYLE
    for original, key in _STYLE_COLOR_KEYS.items():
        stylesheet = stylesheet.replace(original, colors[key])
    directory = Path(indicator_dir) if indicator_dir is not None else _indicator_dir()
    for placeholder, (icon, key) in _INDICATOR_IMAGES.items():
        try:
            image = _indicator_image(icon, colors[key], directory)
        except OSError:
            image = ""  # no mark rather than no stylesheet
        stylesheet = stylesheet.replace(placeholder, image)
    return stylesheet
