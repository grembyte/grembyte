"""Shared dialog button-row construction."""

from PyQt6.QtWidgets import QHBoxLayout, QPushButton  # pylint: disable=import-error,no-name-in-module


def dialog_button_row(translations, accept_key, on_accept, on_cancel):
    """Build a standard cancel/accept row for a dialog."""
    row = QHBoxLayout()
    row.addStretch()

    cancel_button = QPushButton(translations["button.cancel"])
    cancel_button.clicked.connect(on_cancel)
    accept_button = QPushButton(translations[accept_key])
    accept_button.setObjectName("btn_primary")
    accept_button.clicked.connect(on_accept)

    row.addWidget(cancel_button)
    row.addWidget(accept_button)
    return row
