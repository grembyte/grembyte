"""Application entry point for the Grembyte desktop application."""

import os
import signal
import sys

from PySide6.QtCore import QStandardPaths, QTimer  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QIcon  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

from grembyte.core import activity, former_name, logs, settings
from grembyte.core.sessions import SESSIONS
from grembyte.db.engine import init_db
from grembyte.db.repository.settings import SettingsRepository
from grembyte.ui.main_window import ICON_PATH, MainWindow
from grembyte.ui.activity_recorder import UiRecorder
from grembyte.ui.garbage import GarbageCollector
from grembyte.ui.window_buttons import add_window_buttons
from grembyte.ui.window_state import keep_window_state

DESKTOP_FILE_NAME = "grembyte"
WINDOWS_APP_ID = "grembyte.win"


def _set_windows_app_id():
    """Give the process an app ID of its own on Windows.

    The taskbar groups windows by program, here python.exe, and shows its
    icon. With an app ID of its own it shows the window icon instead. It has
    to be set before the first window opens.
    """
    if sys.platform != "win32":
        return
    import ctypes  # pylint: disable=import-outside-toplevel

    try:
        ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except (AttributeError, OSError) as error:
        logs.log.warning("cannot set the Windows app ID: %s", error)


def start_activity_log():
    try:
        activity.setup()
    except OSError as error:
        logs.log.error("cannot write the activity log: %s", error)


def _follow_activity_setting(changed):
    # Switched on or off in the settings: at once, no restart needed.
    if "activity_log" not in changed:
        return
    if changed["activity_log"]:
        start_activity_log()
    else:
        activity.record("activity_log_off")
        activity.teardown()


def main() -> int:
    """Initialize the database and run the application event loop."""
    # Before anything opens the database, the logs or the Qt settings.
    try:
        adopted, adopt_error = former_name.adopt(), None
    except OSError as error:
        adopted, adopt_error = [], error
    try:
        logs.setup()
    except OSError as error:
        print(f"Grembyte cannot write its log: {error}", file=sys.stderr)
    for step in adopted:
        logs.log.info("taken over from Hermes Hook: %s", step)
    if adopt_error is not None:
        logs.log.error("cannot take over the files of Hermes Hook: %s", adopt_error)
    logs.install_exception_hooks()
    init_db()
    settings.load(SettingsRepository().load())
    if settings.get("activity_log"):
        start_activity_log()
    settings.subscribe(_follow_activity_setting)

    _set_windows_app_id()
    app = QApplication(sys.argv)
    garbage_collector = GarbageCollector(app)
    garbage_collector.start()
    add_window_buttons(app)
    keep_window_state(app)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    # Wayland shows the icon of the app's .desktop file. Only name it when it
    # is installed; otherwise the desktop portal rejects the app ID with a warning.
    if QStandardPaths.locate(
        QStandardPaths.StandardLocation.ApplicationsLocation,
        f"{DESKTOP_FILE_NAME}.desktop",
    ):
        app.setDesktopFileName(DESKTOP_FILE_NAME)
    window = MainWindow(settings.language())
    recorder = UiRecorder(app)
    window.show()
    recorder.start()

    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    signal.signal(
        signal.SIGINT,
        lambda _signum, _frame: QTimer.singleShot(0, window.close),
    )

    signal_check_timer = QTimer(app)
    signal_check_timer.setInterval(100)
    # Let Python process SIGINT while Qt's event loop is waiting in C++.
    signal_check_timer.timeout.connect(lambda: None)
    signal_check_timer.start()
    try:
        code = app.exec()
    finally:
        recorder.stop()
        signal_check_timer.stop()
        garbage_collector.stop()
        signal.signal(signal.SIGINT, previous_sigint_handler)
    logs.log.info("Grembyte exits (code %s)", code)
    hanging = SESSIONS.sessions()
    activity.flush()
    activity.record("app_exited", code=code, hanging=[
        {"kind": session.kind, "pid": session.pid} for session in hanging],
        in_flight=activity.in_flight())
    activity.teardown()
    if hanging:
        logs.log.warning("exiting hard: %d session(s) did not end",
                         len(SESSIONS.sessions()))
        # A worker hangs in a call to the target (see ShutdownDialog). Python
        # would abort destroying its running QThread: leave without that.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)  # pylint: disable=protected-access
    return code


if __name__ == "__main__":
    raise SystemExit(main())
