"""Application entry point for the Hermes Hook desktop application."""

import os
import signal
import sys
from pathlib import Path

from PyQt6.QtCore import QStandardPaths, QTimer  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QIcon  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

from hermes.core import activity, logs
from hermes.core.sessions import SESSIONS
from hermes.db.engine import init_db
from hermes.ui.main_window import MainWindow
from hermes.ui.activity_recorder import UiRecorder

ICON_PATH = Path(__file__).parent / "ui" / "resources" / "icon.svg"
DESKTOP_FILE_NAME = "hermes-hook"


def main() -> int:
    """Initialize the database and run the application event loop."""
    try:
        logs.setup()
        activity.setup()
    except OSError as error:
        print(f"Hermes cannot write its log: {error}", file=sys.stderr)
    logs.install_exception_hooks()
    init_db()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    # Wayland shows the icon of the app's .desktop file. Only name it when it
    # is installed; otherwise the desktop portal rejects the app ID with a warning.
    if QStandardPaths.locate(
        QStandardPaths.StandardLocation.ApplicationsLocation,
        f"{DESKTOP_FILE_NAME}.desktop",
    ):
        app.setDesktopFileName(DESKTOP_FILE_NAME)
    window = MainWindow()
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
        signal.signal(signal.SIGINT, previous_sigint_handler)
    logs.log.info("Hermes exits (code %s)", code)
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
