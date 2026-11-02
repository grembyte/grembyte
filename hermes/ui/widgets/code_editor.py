"""A text field for JavaScript (scripts, hook code, templates).

Fixed-width font; Tab indents by spaces, as the built-in templates are
written, rather than inserting a tab character eight characters wide.
With several lines selected Tab and Shift+Tab indent and outdent them all.
"""

import re

from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import (  # pylint: disable=import-error,no-name-in-module
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PyQt6.QtWidgets import QApplication, QPlainTextEdit  # pylint: disable=import-error,no-name-in-module

INDENT = "  "
_BLOCK_COMMENT = 1
_TEMPLATE_STRING = 2
_NUMBER = re.compile(
    r"(?:0[xX][\da-fA-F](?:_?[\da-fA-F])*|0[bB][01](?:_?[01])*|"
    r"0[oO][0-7](?:_?[0-7])*|(?:\d(?:_?\d)*(?:\.(?:\d(?:_?\d)*)?)?|"
    r"\.\d(?:_?\d)*)(?:[eE][+-]?\d(?:_?\d)*)?)n?(?![\w$])")
_KEYWORDS = frozenset({
    "async", "await", "break", "case", "catch", "class", "const", "continue",
    "debugger", "default", "delete", "do", "else", "export", "extends", "false",
    "finally", "for", "from", "function", "get", "if", "implements", "import", "in",
    "instanceof", "interface", "let", "new", "null", "of", "package", "private",
    "protected", "public", "return", "set", "static", "super", "switch", "this", "throw",
    "true", "try", "typeof", "undefined", "var", "void", "while", "with", "yield",
})
_CODE_COLORS = {
    "code_text": "#c9d1d9",
    "code_keyword": "#ff7b72",
    "code_string": "#a5d6ff",
    "code_number": "#79c0ff",
    "muted": "#8b949e",
}


def _closing_quote(text, start, quote):
    escaped = False
    for position in range(start, len(text)):
        character = text[position]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == quote:
            return position + 1
    return None


class JavaScriptHighlighter(QSyntaxHighlighter):
    """Highlight JavaScript tokens while keeping multiline lexical state."""

    def __init__(self, document, colors):
        super().__init__(document)
        self.set_colors(colors)

    def set_colors(self, colors):
        """Update token formats when the application's theme changes."""
        self.formats = {}
        for token, key in (("text", "code_text"), ("keyword", "code_keyword"),
                           ("string", "code_string"), ("number", "code_number"),
                           ("comment", "muted")):
            text_format = QTextCharFormat()
            text_format.setForeground(QColor(colors[key]))
            if token == "keyword":
                text_format.setFontWeight(QFont.Weight.Bold)
            self.formats[token] = text_format
        self.rehighlight()

    def highlightBlock(self, text):  # pylint: disable=invalid-name
        self.setFormat(0, len(text), self.formats["text"])
        position = 0
        previous_state = self.previousBlockState()
        self.setCurrentBlockState(0)

        if previous_state == _BLOCK_COMMENT:
            end = text.find("*/")
            if end < 0:
                self.setFormat(0, len(text), self.formats["comment"])
                self.setCurrentBlockState(_BLOCK_COMMENT)
                return
            self.setFormat(0, end + 2, self.formats["comment"])
            position = end + 2
        elif previous_state == _TEMPLATE_STRING:
            end = _closing_quote(text, 0, "`")
            if end is None:
                self.setFormat(0, len(text), self.formats["string"])
                self.setCurrentBlockState(_TEMPLATE_STRING)
                return
            self.setFormat(0, end, self.formats["string"])
            position = end

        while position < len(text):
            if text.startswith("//", position):
                self.setFormat(position, len(text) - position, self.formats["comment"])
                break
            if text.startswith("/*", position):
                end = text.find("*/", position + 2)
                if end < 0:
                    self.setFormat(position, len(text) - position, self.formats["comment"])
                    self.setCurrentBlockState(_BLOCK_COMMENT)
                    break
                self.setFormat(position, end + 2 - position, self.formats["comment"])
                position = end + 2
                continue

            character = text[position]
            if character in ("'", '"', "`"):
                end = _closing_quote(text, position + 1, character)
                if end is None:
                    self.setFormat(position, len(text) - position, self.formats["string"])
                    if character == "`":
                        self.setCurrentBlockState(_TEMPLATE_STRING)
                    break
                self.setFormat(position, end - position, self.formats["string"])
                position = end
                continue

            if character.isdigit() or (character == "." and position + 1 < len(text)
                                       and text[position + 1].isdigit()):
                number = _NUMBER.match(text, position)
                if number is not None:
                    self.setFormat(position, number.end() - position, self.formats["number"])
                    position = number.end()
                    continue

            if character == "$" or character == "_" or character.isalpha():
                end = position + 1
                while end < len(text):
                    next_character = text[end]
                    if not (next_character == "$" or next_character == "_"
                            or next_character.isalnum()):
                        break
                    end += 1
                if text[position:end] in _KEYWORDS:
                    self.setFormat(position, end - position, self.formats["keyword"])
                position = end
                continue

            position += 1


class CodeEditor(QPlainTextEdit):
    """QPlainTextEdit for code: fixed-width font, Tab indents by INDENT."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.highlighter = JavaScriptHighlighter(self.document(), _CODE_COLORS)
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


def apply_code_theme(colors):
    """Apply syntax colors to current and future code editors."""
    _CODE_COLORS.update({key: colors[key] for key in (
        "code_text", "code_keyword", "code_string", "code_number", "muted")})
    application = QApplication.instance()
    if application is None:
        return
    for widget in application.allWidgets():
        if isinstance(widget, CodeEditor):
            widget.highlighter.set_colors(_CODE_COLORS)

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
