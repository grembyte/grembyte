"""Where Grembyte suggests saving exported files.

Every export starts in the folder the user saved the last one to. The first
time it starts in the home folder (Linux) or the documents folder (Windows).
The folder is kept in QSettings under ``export/folder``.
"""

import os
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths  # pylint: disable=import-error,no-name-in-module

from grembyte.ui import window_state

_KEY = "export/folder"


def default_folder():
    """The home folder, on Windows the documents folder."""
    if sys.platform == "win32":
        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation)
        if documents and os.path.isdir(documents):
            return Path(documents)
    return Path.home()


def export_folder():
    """The folder to suggest for an export: the last one used, if it still exists."""
    last = window_state.settings().value(_KEY, "", type=str)
    if last and os.path.isdir(last):
        return Path(last)
    return default_folder()


def suggested_path(file_name):
    """``file_name`` in the folder to suggest, as a string for a file dialog."""
    return str(export_folder() / file_name)


def remember(path):
    """Note the folder of ``path`` (a saved file, or a folder) for the next export."""
    path = Path(path).expanduser()
    folder = path if path.is_dir() else path.parent
    window_state.settings().setValue(_KEY, str(folder))
