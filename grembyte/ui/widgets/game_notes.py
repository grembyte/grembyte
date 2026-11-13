"""The notes the user keeps on a game, as Markdown (View > Notes).

A narrow window that docks to the right of the main window (``pin_button``).
"Write" edits the text, "Preview" shows it rendered (GitHub's Markdown).
The text is saved shortly after typing stops, when the window closes or
changes game, and with the save button (Ctrl+S), which always saves.

It shows one note per saved process, the oldest (see NoteRepository).
"""

from datetime import datetime

from PySide6.QtCore import Qt, QTimer  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import (  # pylint: disable=import-error,no-name-in-module
    QFontDatabase,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
)
from sqlalchemy.exc import SQLAlchemyError

from grembyte.db.repository.notes import NoteRepository
from grembyte.ui.widgets.docking import TAB_TITLE, PinButton
from grembyte.ui.widgets.icons import set_button_icon

# How long typing has to pause before the text is saved
AUTOSAVE_MS = 2000
_WRITE, _PREVIEW = range(2)


class GameNotesDialog(QDialog):
    """Write and preview the note of the saved process ``process_id``."""

    def __init__(self, translations, process_id, game, repository=None, parent=None,
                 autosave_ms=AUTOSAVE_MS):
        super().__init__(parent)
        self.translations = translations
        self.repository = repository or NoteRepository()
        self.process_id = None
        self.game = None
        self.dirty = False
        self.resize(460, 600)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.editor.setPlaceholderText(translations["notes.placeholder"])
        self.editor.textChanged.connect(self._on_text_changed)
        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(True)
        self.preview.setPlaceholderText(translations["notes.preview_empty"])
        self.tabs.addTab(self.editor, translations["notes.write"])
        self.tabs.addTab(self.preview, translations["notes.preview"])
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)

        bar = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        bar.addWidget(self.status, 1)
        self.pin_button = PinButton(translations)
        bar.addWidget(self.pin_button)
        self.save_button = QPushButton(translations["notes.save"])
        self.save_button.setObjectName("btn_primary")
        set_button_icon(self.save_button, "save")
        self.save_button.setToolTip(translations["notes.save_tooltip"])
        self.save_button.setAutoDefault(False)
        self.save_button.clicked.connect(self.save)
        bar.addWidget(self.save_button)
        layout.addLayout(bar)
        save = QShortcut(QKeySequence(QKeySequence.StandardKey.Save), self)
        # Docked, the main window is the dialog's window: only while it has the focus
        save.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        save.activated.connect(self.save)

        self._autosave = QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(autosave_ms)
        self._autosave.timeout.connect(self.save)

        self.set_process(process_id, game)

    def text(self):
        return self.editor.toPlainText()

    def set_process(self, process_id, game):
        """Show the note of another saved process (the text shown is saved first)."""
        if process_id == self.process_id:
            return
        self.save_pending()
        self.process_id = process_id
        self.game = game
        self.setWindowTitle(self.translations["notes.title"].format(game=game))
        self.setProperty(TAB_TITLE, self.translations["notes.tab_title"].format(game=game))
        self.reload()

    def reload(self):
        """Show the note as saved, dropping changes not saved yet."""
        self._autosave.stop()
        try:
            note = self.repository.first_note(self.process_id)
        except SQLAlchemyError as error:
            note = None
            self.status.setText(self.translations["notes.load_failed"].format(error=error))
        else:
            self.status.clear()
        self.editor.blockSignals(True)
        self.editor.setPlainText("" if note is None else note.body)
        self.editor.blockSignals(False)
        self.dirty = False
        self._render_preview()

    def save(self):
        """Save the text now, changed or not; returns whether it was saved."""
        self._autosave.stop()
        if self.process_id is None:
            return False
        try:
            self.repository.save_first_note(self.process_id, self.text())
        except SQLAlchemyError as error:
            self.status.setText(self.translations["notes.save_failed"].format(error=error))
            return False
        self.dirty = False
        self.status.setText(self.translations["notes.saved"].format(
            time=datetime.now().strftime("%H:%M:%S")))
        return True

    def save_pending(self):
        """Save the text if it changed since it was last saved."""
        if self.dirty:
            self.save()

    def _on_text_changed(self):
        self.dirty = True
        self.status.setText(self.translations["notes.unsaved"])
        self._autosave.start()

    def _on_tab_changed(self, index):
        if index == _PREVIEW:
            self._render_preview()

    def _render_preview(self):
        self.preview.setMarkdown(self.text())

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self.save_pending()
        super().closeEvent(event)

    def done(self, result):  # pylint: disable=invalid-name
        # Escape rejects the dialog without a closeEvent
        self.save_pending()
        super().done(result)
