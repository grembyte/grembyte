"""Take over what Grembyte kept under its former name, Hermes Hook.

Up to 0.6 the program was called Hermes Hook and kept its files in folders
named ``hermes-hook``: the database ``hermes.db`` and the Wine gadgets in the
data directory, ``hermes.log`` and the activity log in the state directory,
themes of the user's own (``hermes_themes_*.json``) in the config directory,
and window sizes in ``QSettings("hermes-hook", "hermes-hook")``.

``adopt`` runs once at start, before anything opens these files. It moves
the folders to ``grembyte``, renames the files in them and copies the Qt
settings. Whatever already exists under the new name is left alone, so it is
safe to run on every start. It moves rather than copies: the gadgets and the
catalog cache can be large, and two copies of the database would drift apart.
"""

import shutil
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths  # pylint: disable=import-error,no-name-in-module

OLD_FOLDER = "hermes-hook"
NEW_FOLDER = "grembyte"
# Files renamed inside the moved folders: old name -> new name
_FILES = {
    "hermes.db": "grembyte.db",
    "hermes.db-wal": "grembyte.db-wal",
    "hermes.db-shm": "grembyte.db-shm",
    "hermes.db-journal": "grembyte.db-journal",
    "hermes.log": "grembyte.log",
}
_OLD_LOG_BACKUP = "hermes.log."
_OLD_THEME_PREFIX = "hermes_themes_"
_NEW_THEME_PREFIX = "grembyte_themes_"
# QSettings("hermes-hook", "hermes-hook") lives in ~/.config/hermes-hook on Linux
_OLD_SETTINGS_FILE = "hermes-hook.conf"


def base_directories():
    """The directories that hold a ``hermes-hook`` folder (data, state, config)."""
    locations = (
        QStandardPaths.StandardLocation.GenericDataLocation,
        QStandardPaths.StandardLocation.GenericStateLocation,
        QStandardPaths.StandardLocation.GenericConfigLocation,
    )
    bases = []
    for location in locations:
        path = QStandardPaths.writableLocation(location)
        if path and Path(path) not in bases:
            bases.append(Path(path))
    return bases


def _move_contents(old, new, moved):
    """Move what is in ``old`` to ``new``; entries already in ``new`` stay where they are."""
    new.mkdir(parents=True, exist_ok=True)
    for entry in sorted(old.iterdir()):
        target = new / entry.name
        if target.exists():
            continue
        shutil.move(str(entry), str(target))
        moved.append((entry, target))
    try:
        old.rmdir()
    except OSError:
        pass  # Something was in both; the rest stays behind.


def _rename_files(folder, moved):
    """Give the moved files their new names (database, log, themes)."""
    for directory in [folder, *(path for path in folder.rglob("*") if path.is_dir())]:
        for entry in sorted(directory.iterdir()):
            name = entry.name
            if name in _FILES:
                new_name = _FILES[name]
            elif name.startswith(_OLD_LOG_BACKUP):
                new_name = "grembyte.log." + name[len(_OLD_LOG_BACKUP):]
            elif name.startswith(_OLD_THEME_PREFIX) and name.endswith(".json"):
                new_name = _NEW_THEME_PREFIX + name[len(_OLD_THEME_PREFIX):]
            else:
                continue
            target = entry.with_name(new_name)
            if target.exists():
                continue
            entry.rename(target)
            moved.append((entry, target))


def adopt_folders(bases=None):
    """Move each ``hermes-hook`` folder in ``bases`` to ``grembyte``.

    Returns the (old, new) paths of what was moved or renamed.
    """
    moved = []
    for base in base_directories() if bases is None else bases:
        old = Path(base) / OLD_FOLDER
        if not old.is_dir():
            continue
        new = Path(base) / NEW_FOLDER
        if new.exists():
            _move_contents(old, new, moved)
        else:
            shutil.move(str(old), str(new))
            moved.append((old, new))
        _rename_files(new, moved)
        # The old Qt settings file came along; adopt_settings has read it.
        stale = new / _OLD_SETTINGS_FILE
        if stale.is_file() and (new / f"{NEW_FOLDER}.conf").is_file():
            stale.unlink()
    return moved


def adopt_settings(old=None, new=None):
    """Copy the old Qt settings (window sizes, theme) if the new ones are empty.

    Returns whether anything was copied.
    """
    old = old or QSettings(OLD_FOLDER, OLD_FOLDER)
    new = new or QSettings(NEW_FOLDER, NEW_FOLDER)
    keys = old.allKeys()
    if not keys or new.allKeys():
        return False
    for key in keys:
        new.setValue(key, old.value(key))
    new.sync()
    return True


def adopt():
    """Take over files and settings of Hermes Hook; returns a list of what was done.

    The Qt settings go first: on Linux their file is in the config folder
    that ``adopt_folders`` moves.
    """
    done = []
    if adopt_settings():
        done.append("Qt settings")
    done.extend(f"{old} -> {new}" for old, new in adopt_folders())
    return done
