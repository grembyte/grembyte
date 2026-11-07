"""Render the screenshots of the documentation (docs/assets/screenshots).

    python scripts/screenshots/run.py                 # every scene, dark and light
    python scripts/screenshots/run.py main-window     # only scenes whose name starts so
    python scripts/screenshots/run.py --theme dark --list

Each theme renders in a process of its own, offscreen, against a hermes whose
settings, database, logs and caches live in a temporary folder. The images
are named ``<scene>-<dark|light>.webp`` (lossless); the pages show the one matching the
reader's color scheme. The scenes are in the scenes_*.py modules beside
this file.
"""

import argparse
import importlib
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs" / "assets" / "screenshots"
THEMES = {"dark": "Github-Dark", "light": "Github-Light"}
# Logical pixels are doubled so the images stay sharp when the page scales them.
SCALE = "2"


def _isolate(home):
    """Point every directory hermes writes to into ``home``; before importing hermes."""
    for variable, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                             ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
        path = Path(home) / folder
        path.mkdir(parents=True, exist_ok=True)
        os.environ[variable] = str(path)
    os.environ["HERMES_DB"] = str(Path(home) / "hermes.db")
    os.environ["HERMES_CACHE_DIR"] = str(Path(home) / "catalog-cache")
    os.environ["HERMES_LOG_DIR"] = str(Path(home) / "logs")
    os.environ["HERMES_LANGUAGE"] = "en"
    # Numbers as English readers write them (1,000 rather than 1.000)
    os.environ["LANG"] = os.environ["LC_ALL"] = "en_US.UTF-8"
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = SCALE


def _load_scenes():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    for module in sorted(Path(__file__).resolve().parent.glob("scenes_*.py")):
        importlib.import_module(module.stem)


def _render(theme, prefixes, list_only):
    """Render the scenes in this process, with ``theme`` (dark or light)."""
    with tempfile.TemporaryDirectory(prefix="hermes-screenshots-") as home:
        _isolate(home)
        _load_scenes()
        # pylint: disable=import-outside-toplevel
        from PySide6.QtGui import QImageWriter  # pylint: disable=import-error,no-name-in-module
        from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

        import studio

        chosen = [scene for name, scene in studio.SCENES.items()
                  if not prefixes or name.startswith(tuple(prefixes))]
        if list_only:
            for scene in chosen:
                print(f"{scene.name:32} {scene.description}")
            return 0
        from hermes.core import settings
        from hermes.db.engine import init_db
        from hermes.db.repository.settings import SettingsRepository
        from hermes.ui.window_buttons import add_window_buttons

        init_db()
        settings.load(SettingsRepository().load())
        application = QApplication.instance() or QApplication(sys.argv[:1])
        add_window_buttons(application)
        application.setStyle("Fusion")
        OUTPUT.mkdir(parents=True, exist_ok=True)
        failed = 0
        for scene in chosen:
            # Each scene gets a main window of its own: what one scene fills
            # in or folds away must not show up in the next.
            stage = studio.Studio(THEMES[theme])
            try:
                widget = scene.build(stage)
                if scene.size:
                    widget.resize(*scene.size)
                widget.show()
                studio.settle(300)
                path = OUTPUT / f"{scene.name}-{theme}.webp"
                writer = QImageWriter(str(path))
                writer.setQuality(100)  # lossless WebP: about half the size of PNG
                if not writer.write(widget.grab().toImage()):
                    raise OSError(f"cannot write {path}: {writer.errorString()}")
                print(f"{theme:5} {scene.name}")
            except Exception:  # pylint: disable=broad-exception-caught
                failed += 1
                print(f"{theme:5} {scene.name} FAILED", file=sys.stderr)
                traceback.print_exc()
            finally:
                stage.close()
        return 1 if failed else 0


def main():
    """Render the chosen scenes for the chosen themes."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("scenes", nargs="*", help="name prefixes of the scenes to render")
    parser.add_argument("--theme", choices=("dark", "light", "both"), default="both")
    parser.add_argument("--list", action="store_true", help="list the scenes and exit")
    args = parser.parse_args()
    if args.theme != "both":
        return _render(args.theme, args.scenes, args.list)
    status = 0
    for theme in ("dark", "light") if not args.list else ("dark",):
        status |= subprocess.call([sys.executable, __file__, "--theme", theme, *args.scenes]
                                  + (["--list"] if args.list else []))
    return status


if __name__ == "__main__":
    sys.exit(main())
