"""Tests for JSON UI theme loading and persistence."""

from grembyte.ui.styles.styles import build_stylesheet
from grembyte.ui.localization import load_translations
from grembyte.ui.styles.themes import COLOR_KEYS, ThemeManager
from PySide6.QtCore import QStandardPaths  # pylint: disable=import-error,no-name-in-module


class MemorySettings:
    def __init__(self):
        self.values = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value


def test_default_theme_is_github_dark():
    manager = ThemeManager(settings=MemorySettings(), user_theme_dir="/tmp/no-grembyte-themes")

    assert manager.load_theme()["name"] == "Github-Dark"
    assert "Default" not in manager.available_themes()
    assert next(iter(manager.available_themes())) == "Github-Dark"
    assert manager.stylesheet() == build_stylesheet(manager.load_theme("Github-Dark")["colors"])
    assert "#e6edf3" in manager.stylesheet()


def test_unknown_saved_theme_falls_back_to_github_dark():
    settings = MemorySettings()
    settings.setValue("ui_theme", "Default")
    manager = ThemeManager(settings=settings, user_theme_dir="/tmp/no-grembyte-themes")

    assert manager.load_theme()["name"] == "Github-Dark"


def test_saved_theme_is_discovered_and_can_be_selected(tmp_path):
    settings = MemorySettings()
    manager = ThemeManager(settings=settings, user_theme_dir=tmp_path)
    colors = manager.load_theme()["colors"]
    colors["background"] = "#102030"

    saved_path = manager.save_theme("Night_Test", colors)
    manager.set_current("Night_Test")

    assert saved_path.name == "grembyte_themes_Night_Test.json"
    assert manager.load_theme()["colors"]["background"] == "#102030"
    assert "$" not in manager.stylesheet()
    assert "#102030" in manager.stylesheet()


def test_user_theme_directory_is_stable_across_launch_methods(tmp_path, monkeypatch):
    monkeypatch.setattr(
        QStandardPaths, "writableLocation",
        lambda location: str(tmp_path if location ==
                             QStandardPaths.StandardLocation.GenericConfigLocation
                             else tmp_path / "legacy"))
    manager = ThemeManager(settings=MemorySettings())

    assert manager.user_theme_dir == tmp_path / "grembyte" / "themes"


def test_stylesheet_replaces_each_palette_color():
    colors = {key: "#123456" for key in COLOR_KEYS}

    assert "#0d1117" not in build_stylesheet(colors)


def test_all_theme_colors_have_english_and_german_labels():
    for language in ("en", "de"):
        translations = load_translations(language)

        assert all(f"theme.color.{key}" in translations for key in COLOR_KEYS)


def test_malformed_theme_is_ignored(tmp_path):
    (tmp_path / "grembyte_themes_Broken.json").write_text('{"colors": {}}', encoding="utf-8")
    manager = ThemeManager(settings=MemorySettings(), user_theme_dir=tmp_path)

    assert "Broken" not in manager.available_themes()


def test_older_user_theme_inherits_syntax_colors(tmp_path):
    import json

    manager = ThemeManager(settings=MemorySettings(), user_theme_dir=tmp_path)
    colors = manager.load_theme("Github-Dark")["colors"]
    for key in ("code_text", "code_keyword", "code_string", "code_number"):
        colors.pop(key)
    colors["background"] = "#102030"
    (tmp_path / "grembyte_themes_Old_Custom.json").write_text(
        json.dumps({"name": "Old_Custom", "colors": colors}), encoding="utf-8")

    loaded = manager.load_theme("Old_Custom")["colors"]
    defaults = manager.load_theme("Github-Dark")["colors"]

    assert loaded["background"] == "#102030"
    assert all(loaded[key] == defaults[key]
               for key in ("code_text", "code_keyword", "code_string", "code_number"))


def test_user_theme_saved_before_content_colors_still_loads(tmp_path):
    import json

    manager = ThemeManager(settings=MemorySettings(), user_theme_dir=tmp_path)
    colors = manager.load_theme("Github-Light")["colors"]
    for key in ("purple", "orange"):
        colors.pop(key)
    # Same name as the bundled theme: the missing colours come from that one.
    (tmp_path / "grembyte_themes_Github-Light.json").write_text(
        json.dumps({"name": "Github-Light", "colors": colors}), encoding="utf-8")

    loaded = manager.load_theme("Github-Light")["colors"]

    assert loaded["purple"] == "#8250df"
    assert loaded["orange"] == "#bc4c00"


def test_theme_qcolor_reads_css_rgba(monkeypatch):
    from grembyte.ui.widgets import icons

    monkeypatch.setattr(icons, "_theme_colors", {"selection": "rgba(9, 105, 218, 0.5)"})

    color = icons.theme_qcolor("selection")

    assert (color.red(), color.green(), color.blue(), color.alpha()) == (9, 105, 218, 128)
    # A key the theme lacks falls back to Github-Dark
    assert icons.theme_qcolor("purple").name() == "#d2a8ff"


def test_color_swatch_labels_stay_readable():
    from grembyte.ui.widgets.theme_dialog import _label_color

    assert _label_color("#ffffff", "#ffffff") == "#000000"
    assert _label_color("#0d1117", "#ffffff") == "#ffffff"
    # A translucent selection over a white background is light
    assert _label_color("rgba(9, 105, 218, 0.18)", "#ffffff") == "#000000"


def test_solarized_themes_are_bundled():
    manager = ThemeManager(settings=MemorySettings(), user_theme_dir="/tmp/no-grembyte-themes")

    assert manager.load_theme("Solarized-Dark")["colors"]["background"] == "#002b36"
    assert manager.load_theme("Solarized-Light")["colors"]["background"] == "#fdf6e3"


def test_github_themes_are_bundled():
    manager = ThemeManager(settings=MemorySettings(), user_theme_dir="/tmp/no-grembyte-themes")

    assert manager.load_theme("Github-Dark")["colors"]["background"] == "#0d1117"
    assert manager.load_theme("Github-Light")["colors"]["background"] == "#ffffff"


def test_existing_menu_icons_follow_theme(monkeypatch):
    # pylint: disable=import-outside-toplevel
    from PySide6.QtGui import QIcon  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtWidgets import QApplication, QMenu  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets.icons import apply_icon_theme, material_icon

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    manager = ThemeManager(settings=MemorySettings(), user_theme_dir="/tmp/no-grembyte-themes")
    default_colors = manager.load_theme("Github-Dark")["colors"]
    apply_icon_theme(default_colors)
    menu = QMenu()
    action = menu.addAction(material_icon("track_changes"), "Trace")

    try:
        apply_icon_theme(manager.load_theme("Solarized-Light")["colors"])
        image = action.icon().pixmap(16, 16, QIcon.Mode.Normal).toImage()
        colors = {
            image.pixelColor(x, y).name()
            for x in range(image.width())
            for y in range(image.height())
            if image.pixelColor(x, y).alpha()
        }
        assert "#30525c" in colors
    finally:
        apply_icon_theme(default_colors)
        menu.deleteLater()
        application.processEvents()

def test_theme_colors_and_label_icons_follow_theme(monkeypatch):
    # pylint: disable=import-outside-toplevel
    from PySide6.QtWidgets import QApplication, QLabel  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets.icons import apply_icon_theme, set_label_icon, theme_color

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    manager = ThemeManager(settings=MemorySettings(), user_theme_dir="/tmp/no-grembyte-themes")
    default_colors = manager.load_theme("Github-Dark")["colors"]
    apply_icon_theme(default_colors)
    label = QLabel()
    set_label_icon(label, "memory", "warning")

    try:
        light = manager.load_theme("Solarized-Light")["colors"]
        apply_icon_theme(light)
        assert theme_color("muted") == light["muted"]
        image = label.pixmap().toImage()
        colors = {
            image.pixelColor(x, y).name()
            for x in range(image.width())
            for y in range(image.height())
            if image.pixelColor(x, y).alpha() == 255
        }
        assert colors == {light["warning"]}
    finally:
        apply_icon_theme(default_colors)
        label.deleteLater()
        application.processEvents()

def test_check_boxes_use_theme_colors(tmp_path):
    manager = ThemeManager(settings=MemorySettings(), user_theme_dir="/tmp/no-grembyte-themes")
    colors = manager.load_theme("Github-Light")["colors"]

    stylesheet = build_stylesheet(colors, indicator_dir=tmp_path)

    assert "__" not in stylesheet.replace("__MONO__", "")
    check = tmp_path / "check-ffffff.svg"
    assert f"image: url({check.as_posix()})" in stylesheet
    assert 'fill="#ffffff"' in check.read_text(encoding="utf-8")
    assert (tmp_path / "check-ffffff.svg").exists() and (tmp_path / "remove-ffffff.svg").exists()
