"""Application entry point for the Hermes Hook desktop application."""

import sys

from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

from hermes.db.engine import init_db
from hermes.ui.main_window import MainWindow


def main() -> int:
    """Initialize the database and run the application event loop."""
    init_db()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
