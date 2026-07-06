"""Hermes Hook - application entry point."""

import sys

from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

from hermes.db.engine import init_db
from hermes.ui.main_window import MainWindow

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())