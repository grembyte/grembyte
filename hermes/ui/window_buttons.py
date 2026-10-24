"""Minimize and maximize buttons for Hermes' dialogs on Windows.

On Windows Qt gives a dialog only a close button; on Linux the window
manager decides and lets any window be maximized, by a double click on its
title too. ``add_window_buttons`` gives every dialog of Hermes the two
buttons when it first gets the focus. Qt leaves the maximize button out for
a dialog of a fixed size. Qt's own dialogs (message boxes, file dialogs)
stay as they are.

Not an event filter on the application (see activity_recorder.py): the
focus signal is enough, and changing the flags of the native window neither
hides nor re-creates it, so a modal dialog stays open.
"""

import sys

from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import QApplication, QDialog  # pylint: disable=import-error,no-name-in-module

_BUTTONS = Qt.WindowType.WindowMinMaxButtonsHint


def add_window_buttons(app):
    """Have ``app`` add the buttons to Hermes' dialogs (Windows only)."""
    if sys.platform == "win32":
        app.focusWindowChanged.connect(_on_focus_window)


def _on_focus_window(window):
    if window is None:
        return
    dialog = next((widget for widget in QApplication.topLevelWidgets()
                   if widget.windowHandle() == window), None)
    if not isinstance(dialog, QDialog) or not type(dialog).__module__.startswith("hermes."):
        return
    flags = dialog.windowFlags()
    if flags & _BUTTONS == _BUTTONS:
        return
    flags |= _BUTTONS
    # Tell the widget without re-creating it, then the native window.
    dialog.overrideWindowFlags(flags)
    window.setFlags(flags)
