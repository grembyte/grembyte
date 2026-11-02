"""Load, persist and render JSON UI themes."""

import json
import re
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths  # pylint: disable=import-error,no-name-in-module

from hermes.ui.styles.styles import _STYLE_COLOR_KEYS, build_stylesheet


THEME_PREFIX = "hermes_themes_"
DEFAULT_THEME = "Default"
THEME_DIRECTORY = Path(__file__).parents[1] / "themes"
CODE_COLOR_KEYS = ("code_text", "code_keyword", "code_string", "code_number")
COLOR_KEYS = tuple(dict.fromkeys((*_STYLE_COLOR_KEYS.values(), *CODE_COLOR_KEYS)))
_THEME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_COLOR = re.compile(
    r"(?:#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?|"
    r"rgba\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*(?:0(?:\.\d+)?|1(?:\.0+)?)\s*\))\Z")


def _valid_color(value):
    if not _COLOR.fullmatch(value):
        return False
    if value.startswith("rgba"):
        channels = [int(channel) for channel in re.findall(r"\d{1,3}", value)[:3]]
        return all(channel <= 255 for channel in channels)
    return True


class ThemeManager:
    """Manage bundled themes and per-user theme files."""

    def __init__(self, settings=None, user_theme_dir=None):
        self.settings = settings or QSettings("hermes-hook", "hermes-hook")
        if user_theme_dir is not None:
            self.user_theme_dir = Path(user_theme_dir)
        else:
            config_dir = Path(QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.GenericConfigLocation))
            self.user_theme_dir = config_dir / "hermes-hook" / "themes"
        legacy_dir = Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppConfigLocation)) / "themes"
        self.legacy_theme_dir = legacy_dir if legacy_dir != self.user_theme_dir else None

    def available_themes(self):
        """Return valid themes, with user themes overriding bundled names."""
        themes = {}
        for directory in (THEME_DIRECTORY, self.legacy_theme_dir, self.user_theme_dir):
            if directory is None or not directory.is_dir():
                continue
            for path in directory.glob(f"{THEME_PREFIX}*.json"):
                try:
                    theme = self._read_theme(path, themes)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                themes[theme["name"]] = theme
        return dict(sorted(themes.items(), key=lambda item: (
            item[0] != DEFAULT_THEME, item[0].casefold())))

    def load_theme(self, name=None):
        """Return a named theme, falling back to Default if it is unavailable."""
        name = name or self.settings.value("ui_theme", DEFAULT_THEME)
        themes = self.available_themes()
        return themes.get(name, themes[DEFAULT_THEME])

    def stylesheet(self, name=None):
        """Render the active stylesheet for ``name`` or the saved theme."""
        return build_stylesheet(self.load_theme(name)["colors"])

    def set_current(self, name):
        """Persist the active theme name."""
        if name not in self.available_themes():
            raise ValueError(f"Unknown theme: {name}")
        self.settings.setValue("ui_theme", name)

    def save_theme(self, name, colors):
        """Save a theme to the writable user theme directory."""
        if not _THEME_NAME.fullmatch(name):
            raise ValueError("Theme names may contain letters, numbers, '_' and '-'.")
        if set(colors) != set(COLOR_KEYS) or any(
                not isinstance(value, str) or not _valid_color(value)
                for value in colors.values()):
            raise ValueError("Theme colors must be hexadecimal or CSS rgba colors.")
        self.user_theme_dir.mkdir(parents=True, exist_ok=True)
        path = self.user_theme_dir / f"{THEME_PREFIX}{name}.json"
        path.write_text(json.dumps({"name": name, "colors": colors}, indent=2) + "\n",
                        encoding="utf-8")
        return path

    @staticmethod
    def _read_theme(path, fallback_themes=None):
        with path.open(encoding="utf-8") as theme_file:
            theme = json.load(theme_file)
        if not isinstance(theme, dict):
            raise ValueError(f"Invalid theme: {path}")
        name = theme.get("name")
        colors = theme.get("colors")
        if (not isinstance(name, str) or not _THEME_NAME.fullmatch(name)
                or not isinstance(colors, dict) or set(colors) - set(COLOR_KEYS)):
            raise ValueError(f"Invalid theme: {path}")
        missing = set(COLOR_KEYS) - set(colors)
        if missing - set(CODE_COLOR_KEYS):
            raise ValueError(f"Invalid theme: {path}")
        if missing:
            fallbacks = fallback_themes or {}
            fallback = fallbacks.get(name, fallbacks.get(DEFAULT_THEME, {}))
            fallback_colors = fallback.get("colors", {})
            if missing - set(fallback_colors):
                raise ValueError(f"Invalid theme: {path}")
            colors = {**fallback_colors, **colors}
        if any(not isinstance(value, str) or not _valid_color(value)
               for value in colors.values()):
            raise ValueError(f"Invalid theme: {path}")
        return {"name": name, "colors": colors}