"""The stage the screenshot scenes play on: an isolated hermes without a game.

``run.py`` points every directory hermes writes to (QSettings, database,
logs, caches) into a temporary folder before hermes is imported, so the
scenes never touch the user's data. No scene attaches to a process: they
fill hermes' widgets with demo data the way its workers would.
"""

from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication, QEventLoop, QSettings, QTimer  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import QApplication, QWidget  # pylint: disable=import-error,no-name-in-module

# Every scene: name -> Scene, in the order the modules register them.
SCENES = {}

# Demo program, as the test game of the repository would show it.
DEMO_PID = 48213
DEMO_NAME = "dummy_game.exe"


@dataclass
class Scene:
    """A screenshot: ``build(studio)`` returns the widget to capture."""

    name: str
    build: object
    size: tuple = None
    description: str = ""


def scene(name, size=None, description=""):
    """Register the decorated function as the scene ``name``.

    ``size`` (width, height) resizes the returned widget before it is
    captured; without it the widget keeps the size it chose.
    """
    def register(build):
        if name in SCENES:
            raise ValueError(f"scene {name!r} is registered twice")
        SCENES[name] = Scene(name, build, size, description)
        return build
    return register


def settle(milliseconds=150):
    """Let Qt lay out, paint and run pending timers for a moment."""
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()
    QCoreApplication.processEvents()


class Studio:
    """What the scenes share: the language, the theme and the main window."""

    def __init__(self, theme, language="en"):
        self.theme = theme
        self.language = language
        self._window = None
        self._extra = []
        QSettings("hermes-hook", "hermes-hook").setValue("ui_theme", theme)

    @property
    def window(self):
        """The main window, built on first use and shown with no process attached."""
        if self._window is None:
            # pylint: disable=import-outside-toplevel
            from hermes.ui.main_window import MainWindow

            self._window = MainWindow(self.language)
            self._window.show()
            settle()
        return self._window

    @property
    def translations(self):
        """The UI texts of the studio's language."""
        return self.window.translations

    def keep(self, widget):
        """Keep ``widget`` alive until the studio closes; returns it."""
        self._extra.append(widget)
        return widget

    def show_attached(self, name=DEMO_NAME, pid=DEMO_PID, details="64-bit"):
        """Make the status bar say a demo program is attached, without attaching."""
        window = self.window
        text = window.translations["app.process_display_details"].format(
            name=name, pid=pid, details=details)
        window.lbl_status.setText(window.translations["app.process_attached"].format(
            process=text))
        # pylint: disable=protected-access
        window._current_proc = window.translations["app.process_display"].format(
            name=name, pid=pid)
        window.lbl_proc_info.setText(f" {window._current_proc}")
        window._proc_icon_action.setVisible(True)
        return window

    def close(self):
        """Close every window the scenes opened."""
        for widget in reversed(self._extra):
            if isinstance(widget, QWidget):
                widget.close()
                widget.deleteLater()
        self._extra.clear()
        if self._window is not None:
            self._window.close()
            self._window.deleteLater()
            self._window = None
        QApplication.processEvents()
