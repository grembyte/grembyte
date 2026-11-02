"""Tests for JSON UI theme loading and persistence."""

from hermes.ui.styles.styles import STYLE, build_stylesheet
from hermes.ui.styles.themes import COLOR_KEYS, ThemeManager
from PyQt6.QtCore import QStandardPaths  # pylint: disable=import-error,no-name-in-module


class MemorySettings:
    def __init__(self):
        self.values = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value


def test_default_theme_renders_existing_stylesheet():
    manager = ThemeManager(settings=MemorySettings(), user_theme_dir="/tmp/no-hermes-themes")

    assert manager.load_theme()["name"] == "Default"
    assert manager.stylesheet() == STYLE


def test_saved_theme_is_discovered_and_can_be_selected(tmp_path):
    settings = MemorySettings()
    manager = ThemeManager(settings=settings, user_theme_dir=tmp_path)
    colors = manager.load_theme()["colors"]
    colors["background"] = "#102030"

    saved_path = manager.save_theme("Night_Test", colors)
    manager.set_current("Night_Test")

    assert saved_path.name == "hermes_themes_Night_Test.json"
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

    assert manager.user_theme_dir == tmp_path / "hermes-hook" / "themes"


def test_stylesheet_replaces_each_palette_color():
    colors = {key: "#123456" for key in COLOR_KEYS}

    assert "#0d1117" not in build_stylesheet(colors)


def test_malformed_theme_is_ignored(tmp_path):
    (tmp_path / "hermes_themes_Broken.json").write_text('{"colors": {}}', encoding="utf-8")
    manager = ThemeManager(settings=MemorySettings(), user_theme_dir=tmp_path)

    assert "Broken" not in manager.available_themes()


def test_solarized_themes_are_bundled():
    manager = ThemeManager(settings=MemorySettings(), user_theme_dir="/tmp/no-hermes-themes")

    assert manager.load_theme("Solarized-Dark")["colors"]["background"] == "#002b36"
    assert manager.load_theme("Solarized-Light")["colors"]["background"] == "#fdf6e3"