"""A text field for JavaScript (scripts, hook code, templates).

Fixed-width font; Tab indents by spaces, as the built-in templates are
written, rather than inserting a tab character eight characters wide.
With several lines selected Tab and Shift+Tab indent and outdent them all.
"""

from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QFontDatabase, QFontMetricsF, QTextCursor  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import QPlainTextEdit  # pylint: disable=import-error,no-name-in-module

INDENT = "  "


class CodeEditor(QPlainTextEdit):
    """QPlainTextEdit for code: fixed-width font, Tab indents by INDENT."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        # Tabs pasted from elsewhere are as wide as one indent
        self.setTabStopDistance(
            QFontMetricsF(self.font()).horizontalAdvance(" ") * len(INDENT))

    def keyPressEvent(self, event):  # pylint: disable=invalid-name
        if event.key() == Qt.Key.Key_Tab and not event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            self.indent()
            return
        if event.key() == Qt.Key.Key_Backtab:
            self.outdent()
            return
        super().keyPressEvent(event)

    def _selected_blocks(self):
        """The first and last block (line) of the selection; a selection that
        ends at the start of a line leaves that line out."""
        cursor = self.textCursor()
        document = self.document()
        first = document.findBlock(cursor.selectionStart())
        last = document.findBlock(cursor.selectionEnd())
        if (last.blockNumber() > first.blockNumber()
                and cursor.selectionEnd() == last.position()):
            last = last.previous()
        return first, last

    def indent(self):
        """Tab: indent the selected lines, or insert an indent at the cursor."""
        cursor = self.textCursor()
        first, last = self._selected_blocks()
        if first == last:
            cursor.insertText(INDENT)
            return
        cursor.beginEditBlock()
        block = first
        while block.isValid() and block.blockNumber() <= last.blockNumber():
            line = QTextCursor(block)
            line.insertText(INDENT)
            block = block.next()
        cursor.endEditBlock()

    def outdent(self):
        """Shift+Tab: remove an indent (or a tab) from the start of the selected lines."""
        cursor = self.textCursor()
        first, last = self._selected_blocks()
        cursor.beginEditBlock()
        block = first
        while block.isValid() and block.blockNumber() <= last.blockNumber():
            text = block.text()
            if text.startswith("\t"):
                remove = 1
            else:
                remove = min(len(text) - len(text.lstrip(" ")), len(INDENT))
            if remove:
                line = QTextCursor(block)
                line.movePosition(QTextCursor.MoveOperation.Right,
                                  QTextCursor.MoveMode.KeepAnchor, remove)
                line.removeSelectedText()
            block = block.next()
        cursor.endEditBlock()
