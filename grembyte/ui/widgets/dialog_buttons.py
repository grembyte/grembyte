"""Shared dialog button-row construction."""

from PySide6.QtWidgets import QHBoxLayout, QPushButton  # pylint: disable=import-error,no-name-in-module

from grembyte.ui.widgets.icons import set_button_icon

# Icons of the accept buttons by their text key; others get "check".
_ACCEPT_ICONS = {
    "button.save": "save",
    "button.write": "edit",
    "button.add": "add",
    "delete_entries.button": "delete",
    "export.button": "upload",
    "import.button": "download",
    "games.open": "data_object",
    "hooks.create": "webhook",
    "process.attach": "link",
    "scripts.create": "add",
    "scripts.run_button": "play_circle",
}


def dialog_button_row(translations, accept_key, on_accept, on_cancel):
    """Build a standard cancel/accept row for a dialog."""
    row = QHBoxLayout()
    row.addStretch()

    cancel_button = QPushButton(translations["button.cancel"])
    set_button_icon(cancel_button, "close")
    cancel_button.clicked.connect(on_cancel)
    accept_button = QPushButton(translations[accept_key])
    accept_button.setObjectName("btn_primary")
    set_button_icon(accept_button, _ACCEPT_ICONS.get(accept_key, "check"))
    accept_button.clicked.connect(on_accept)
    # Enter confirms the dialog instead of pressing the first button (cancel).
    cancel_button.setAutoDefault(False)
    accept_button.setDefault(True)

    row.addWidget(cancel_button)
    row.addWidget(accept_button)
    return row
