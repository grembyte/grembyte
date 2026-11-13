"""A text field for JavaScript (scripts, hook code, templates), and the
syntax highlighters for JavaScript, pseudocode and Markdown.

Fixed-width font; Tab indents by spaces, as the built-in templates are
written, rather than inserting a tab character eight characters wide.
With several lines selected Tab and Shift+Tab indent and outdent them all.
"""

import re

from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import (  # pylint: disable=import-error,no-name-in-module
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import QApplication, QPlainTextEdit  # pylint: disable=import-error,no-name-in-module

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
# C numbers: 0x1f, 20, 7.5f, 1e-05
_C_NUMBER = re.compile(r"(?:0[xX][\da-fA-F]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)[fFuUlL]*(?!\w)")
# The C the code explorer's pseudocode uses (grembyte/features/pseudocode.py)
_PSEUDOCODE_KEYWORDS = frozenset({
    "__m128", "asm", "char", "double", "else", "float", "goto", "if", "int", "int8_t",
    "int16_t", "int32_t", "int64_t", "return", "uint8_t", "uint16_t", "uint32_t",
    "uint64_t", "void",
})
# Markdown block states: inside a fence, how long the fence is, which
# language, and the JavaScript state (block comment, template string)
# carried across lines
_MD_FENCE_LENGTH_SHIFT = 9
_MD_FENCE_LENGTH_MAX = 0xff
_MD_FENCE = 0x100
_MD_TILDE = 0x80
_MD_LANGUAGE_MASK = 0x30
_MD_PLAIN, _MD_JAVASCRIPT, _MD_C = 0x00, 0x10, 0x20
_MD_INNER_MASK = 0x03
_MD_LANGUAGES = {
    **dict.fromkeys(("js", "javascript", "mjs", "ts", "typescript", "json"), _MD_JAVASCRIPT),
    **dict.fromkeys(("c", "cpp", "c++", "h", "pseudocode"), _MD_C),
}
# A fence and its info string; after backticks the info string has none (CommonMark)
_MD_FENCE_OPEN = re.compile(r" {0,3}(?:(`{3,})([^`]*)|(~{3,})(.*))$")
_MD_INFO_LANGUAGE = re.compile(r"[\w+#.-]*")
_MD_HEADING = re.compile(r" {0,3}(#{1,6})(?:\s|$)")
_MD_RULE = re.compile(r" {0,3}([-*_])(?: *\1){2,} *$")
_MD_QUOTE = re.compile(r"(?: {0,3}> ?)+")
_MD_LIST = re.compile(r"\s*([-*+]|\d+[.)])(?:\s+\[[ xX]\])?(?=\s|$)")
_MD_CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)\s]*(?:\s+\"[^\"]*\")?\)")
_MD_URL = re.compile(r"<?https?://[^\s<>]+>?")
_MD_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_MD_ITALIC = re.compile(r"(?<![*\w])(\*|_)(?![*_\s])(.+?)(?<![*_\s])\1(?![*\w])")
_MD_STRIKE = re.compile(r"(~~)(?=\S)(.+?)(?<=\S)~~")
_CODE_COLORS = {
    "code_text": "#e6edf3",
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


def token_formats(colors):
    """The character formats of code tokens in the theme ``colors``."""
    formats = {}
    for token, key in (("text", "code_text"), ("keyword", "code_keyword"),
                       ("string", "code_string"), ("number", "code_number"),
                       ("comment", "muted")):
        text_format = QTextCharFormat()
        text_format.setForeground(QColor(colors[key]))
        if token == "keyword":
            text_format.setFontWeight(QFont.Weight.Bold)
        formats[token] = text_format
    return formats


def _highlight_javascript(paint, text, previous_state):
    """Call ``paint(start, length, token)`` for the JavaScript tokens of one
    line; returns the state the next line starts in."""
    paint(0, len(text), "text")
    position = 0

    if previous_state == _BLOCK_COMMENT:
        end = text.find("*/")
        if end < 0:
            paint(0, len(text), "comment")
            return _BLOCK_COMMENT
        paint(0, end + 2, "comment")
        position = end + 2
    elif previous_state == _TEMPLATE_STRING:
        end = _closing_quote(text, 0, "`")
        if end is None:
            paint(0, len(text), "string")
            return _TEMPLATE_STRING
        paint(0, end, "string")
        position = end

    while position < len(text):
        if text.startswith("//", position):
            paint(position, len(text) - position, "comment")
            break
        if text.startswith("/*", position):
            end = text.find("*/", position + 2)
            if end < 0:
                paint(position, len(text) - position, "comment")
                return _BLOCK_COMMENT
            paint(position, end + 2 - position, "comment")
            position = end + 2
            continue

        character = text[position]
        if character in ("'", '"', "`"):
            end = _closing_quote(text, position + 1, character)
            if end is None:
                paint(position, len(text) - position, "string")
                return _TEMPLATE_STRING if character == "`" else 0
            paint(position, end - position, "string")
            position = end
            continue

        if character.isdigit() or (character == "." and position + 1 < len(text)
                                   and text[position + 1].isdigit()):
            number = _NUMBER.match(text, position)
            if number is not None:
                paint(position, number.end() - position, "number")
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
                paint(position, end - position, "keyword")
            position = end
            continue

        position += 1
    return 0


def _highlight_pseudocode(paint, text):
    """Call ``paint(start, length, token)`` for the C tokens of one line."""
    paint(0, len(text), "text")
    position = 0
    while position < len(text):
        if text.startswith("//", position):
            paint(position, len(text) - position, "comment")
            break
        if text.startswith("/*", position):
            end = text.find("*/", position + 2)
            end = len(text) if end < 0 else end + 2
            paint(position, end - position, "comment")
            position = end
            continue

        character = text[position]
        if character in ("'", '"'):
            end = _closing_quote(text, position + 1, character)
            end = len(text) if end is None else end
            paint(position, end - position, "string")
            position = end
            continue

        if character.isdigit() or (character == "." and position + 1 < len(text)
                                   and text[position + 1].isdigit()):
            number = _C_NUMBER.match(text, position)
            if number is not None:
                paint(position, number.end() - position, "number")
                position = number.end()
                continue

        if character == "_" or character.isalpha():
            end = position + 1
            while end < len(text) and (text[end] == "_" or text[end].isalnum()):
                end += 1
            if text[position:end] in _PSEUDOCODE_KEYWORDS:
                paint(position, end - position, "keyword")
            position = end
            continue

        position += 1


def fence_opening(text):
    """``(fence, language)`` if the line ``text`` opens a fenced code block
    (fence like ```` ``` ```` or ``~~~``, language the first word of the info
    string), else None."""
    match = _MD_FENCE_OPEN.match(text)
    if match is None:
        return None
    fence, info = (match.group(1), match.group(2)) if match.group(1) else (
        match.group(3), match.group(4))
    return fence, _MD_INFO_LANGUAGE.match(info.strip()).group()


def closes_fence(text, fence):
    """Whether the line ``text`` closes a code block opened by ``fence``: the
    same character, at least as many times, nothing else."""
    stripped = text.rstrip(" \t")
    indent = len(stripped) - len(stripped.lstrip(" "))
    marks = stripped[indent:]
    return (indent <= 3 and len(marks) >= len(fence)
            and marks == fence[0] * len(marks))


def _highlight_kind(paint, kind, text, state):
    if kind == _MD_JAVASCRIPT:
        return _highlight_javascript(paint, text, state)
    if kind == _MD_C:
        _highlight_pseudocode(paint, text)
        return 0
    paint(0, len(text), "string")
    return 0


def highlight_code(paint, language, text, state=0):
    """Call ``paint(start, length, token)`` for one line of code in ``language``
    (the info string of a Markdown fence: js, c …); returns the state the next
    line of the same code starts in. Unknown languages are one "string" token."""
    return _highlight_kind(paint, _MD_LANGUAGES.get(language.lower(), _MD_PLAIN), text, state)


class JavaScriptHighlighter(QSyntaxHighlighter):
    """Highlight JavaScript tokens while keeping multiline lexical state."""

    def __init__(self, document, colors):
        super().__init__(document)
        self.set_colors(colors)

    def set_colors(self, colors):
        """Update token formats when the application's theme changes."""
        self.formats = token_formats(colors)
        self.rehighlight()

    def highlightBlock(self, text):  # pylint: disable=invalid-name
        self.setCurrentBlockState(_highlight_javascript(
            lambda start, length, token: self.setFormat(start, length, self.formats[token]),
            text, self.previousBlockState()))


class PseudocodeHighlighter(QSyntaxHighlighter):
    """Highlight the C-like code shown by the native code explorer."""

    def __init__(self, document, colors):
        super().__init__(document)
        self.set_colors(colors)

    def set_colors(self, colors):
        """Update token formats when the application's theme changes."""
        self.formats = token_formats(colors)
        self.rehighlight()

    def highlightBlock(self, text):  # pylint: disable=invalid-name
        _highlight_pseudocode(
            lambda start, length, token: self.setFormat(start, length, self.formats[token]),
            text)


class MarkdownHighlighter(QSyntaxHighlighter):
    """Colour the Markdown syntax of the notes while they are written.

    Markers (``#``, ``*``, backticks, fences) are muted, headings bold, and
    fenced code blocks are coloured as JavaScript (``js``, ``ts``, ``json``)
    or C (``c``, ``cpp``); other code is shown in the string colour.
    """

    def __init__(self, document, colors):
        super().__init__(document)
        self.set_colors(colors)

    def set_colors(self, colors):
        """Update the formats when the application's theme changes."""
        self.formats = token_formats(colors)
        muted = QColor(colors["muted"])
        heading = QTextCharFormat()
        heading.setForeground(QColor(colors["code_keyword"]))
        heading.setFontWeight(QFont.Weight.Bold)
        marker = QTextCharFormat()
        marker.setForeground(muted)
        list_marker = QTextCharFormat()
        list_marker.setForeground(QColor(colors["code_keyword"]))
        list_marker.setFontWeight(QFont.Weight.Bold)
        link = QTextCharFormat()
        link.setForeground(QColor(colors["code_number"]))
        link.setFontUnderline(True)
        quote = QTextCharFormat()
        quote.setForeground(muted)
        quote.setFontItalic(True)
        bold = QTextCharFormat()
        bold.setFontWeight(QFont.Weight.Bold)
        italic = QTextCharFormat()
        italic.setFontItalic(True)
        strike = QTextCharFormat()
        strike.setFontStrikeOut(True)
        self.formats.update({
            "heading": heading, "marker": marker, "list": list_marker, "link": link,
            "quote": quote, "bold": bold, "italic": italic, "strike": strike,
        })
        self.rehighlight()

    def _paint(self, start, length, name):
        self.setFormat(start, length, self.formats[name])

    def _merge(self, start, length, name, free):
        """Add the format ``name`` to the characters ``free`` still allows,
        one setFormat per run of characters formatted alike."""
        run_start, run_format = None, None
        for position in range(start, start + length + 1):
            text_format = (self.format(position)
                           if position < start + length and free[position] else None)
            if run_format is not None and text_format != run_format:
                merged = QTextCharFormat(run_format)
                merged.merge(self.formats[name])
                self.setFormat(run_start, position - run_start, merged)
                run_format = None
            if text_format is not None and run_format is None:
                run_start, run_format = position, text_format

    def highlightBlock(self, text):  # pylint: disable=invalid-name
        state = self.previousBlockState()
        if state >= 0 and state & _MD_FENCE:
            self._highlight_fenced(text, state)
            return
        self.setCurrentBlockState(0)

        opening = fence_opening(text)
        if opening is not None:
            fence, language = opening
            self._paint(0, len(text), "marker")
            tilde = _MD_TILDE if fence[0] == "~" else 0
            length = min(len(fence), _MD_FENCE_LENGTH_MAX) << _MD_FENCE_LENGTH_SHIFT
            self.setCurrentBlockState(_MD_FENCE | length | tilde
                                      | _MD_LANGUAGES.get(language.lower(), _MD_PLAIN))
            return
        heading = _MD_HEADING.match(text)
        if heading is not None:
            self._paint(0, len(text), "heading")
            self._paint(heading.start(1), len(heading.group(1)), "marker")
            return
        if _MD_RULE.match(text):
            self._paint(0, len(text), "marker")
            return

        start = 0
        quote = _MD_QUOTE.match(text)
        if quote is not None:
            self._paint(0, len(text), "quote")
            self._paint(0, quote.end(), "list")
            start = quote.end()
        item = _MD_LIST.match(text, start)
        if item is not None:
            self._paint(item.start(1), item.end(1) - item.start(1), "list")
            start = item.end()
        self._highlight_inline(text, start)

    def _highlight_fenced(self, text, state):
        fence = ("~" if state & _MD_TILDE else "`") * (
            (state >> _MD_FENCE_LENGTH_SHIFT) & _MD_FENCE_LENGTH_MAX)
        if closes_fence(text, fence):
            self._paint(0, len(text), "marker")
            self.setCurrentBlockState(0)
            return
        inner = _highlight_kind(self._paint, state & _MD_LANGUAGE_MASK, text,
                                state & _MD_INNER_MASK)
        self.setCurrentBlockState((state & ~_MD_INNER_MASK) | inner)

    def _highlight_inline(self, text, start):
        free = [position >= start for position in range(len(text))]
        for match in _MD_CODE_SPAN.finditer(text, start):
            ticks = len(match.group(1))
            self._paint(match.start(), match.end() - match.start(), "marker")
            self._paint(match.start() + ticks, match.end() - match.start() - 2 * ticks,
                        "string")
            free[match.start():match.end()] = [False] * (match.end() - match.start())
        for match in _MD_LINK.finditer(text, start):
            if not all(free[match.start():match.end()]):
                continue
            self._paint(match.start(), match.end() - match.start(), "marker")
            self._paint(match.start(1), match.end(1) - match.start(1), "link")
            free[match.start():match.end()] = [False] * (match.end() - match.start())
        for match in _MD_URL.finditer(text, start):
            if all(free[match.start():match.end()]):
                self._paint(match.start(), match.end() - match.start(), "link")
                free[match.start():match.end()] = [False] * (match.end() - match.start())
        for pattern, name in ((_MD_BOLD, "bold"), (_MD_ITALIC, "italic"),
                              (_MD_STRIKE, "strike")):
            for match in pattern.finditer(text, start):
                if not free[match.start()] or not free[match.end() - 1]:
                    continue
                self._merge(match.start(), match.end() - match.start(), name, free)
                marker = len(match.group(1))
                for position in (match.start(), match.end() - marker):
                    if all(free[position:position + marker]):
                        self._paint(position, marker, "marker")


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
        highlighter = getattr(widget, "pseudocode_highlighter", None)
        if isinstance(highlighter, PseudocodeHighlighter):
            highlighter.set_colors(_CODE_COLORS)
        highlighter = getattr(widget, "markdown_highlighter", None)
        if isinstance(highlighter, MarkdownHighlighter):
            highlighter.set_colors(_CODE_COLORS)
        # Widgets outside this module that colour code (markdown_preview)
        theme_changed = getattr(widget, "code_theme_changed", None)
        if callable(theme_changed):
            theme_changed(colors)


def code_theme_colors():
    """The syntax colors of the currently applied application theme."""
    return dict(_CODE_COLORS)
