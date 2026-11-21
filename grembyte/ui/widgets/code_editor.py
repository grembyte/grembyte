"""A text field for JavaScript (scripts, hook code, templates), and the
syntax highlighters for JavaScript, pseudocode and Markdown.

Fixed-width font; Tab indents by spaces, as the built-in templates are
written, rather than inserting a tab character eight characters wide.
With several lines selected Tab and Shift+Tab indent and outdent them all.
Ctrl+mouse wheel, Ctrl+Plus and Ctrl+Minus make the code larger or smaller
in every code field at once, Ctrl+0 resets it; the size is remembered.
Line numbers run down the left edge. With ``set_completion`` a popup offers
the members of ``ctx``, Frida's API and the keywords while typing (see
grembyte.ui.widgets.code_completion); Ctrl+Space opens it by hand.
"""

import html
import re

from PySide6.QtCore import QEvent, QRect, QSize, Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import (  # pylint: disable=import-error,no-name-in-module
    QColor,
    QFont,
    QFontDatabase,
    QFontInfo,
    QFontMetricsF,
    QPainter,
    QStandardItem,
    QStandardItemModel,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QToolTip,
    QTreeView,
    QWidget,
)

from grembyte.ui import window_state
from grembyte.ui.widgets import code_completion
from grembyte.ui.widgets.icons import theme_color

INDENT = "  "
# Font size of the code in pixels: the application stylesheet's for inputs, and the bounds of zooming
DEFAULT_FONT_SIZE = 13
MIN_FONT_SIZE = 8
MAX_FONT_SIZE = 32
_FONT_SIZE_KEY = "codeEditor/fontSize"
_WHEEL_STEP = 120  # angleDelta of one notch of a mouse wheel
_POPUP_ROWS = 12
# The completion list's descriptions: their size against the code's, and opacity
_DESCRIPTION_SCALE = 0.9
_DESCRIPTION_ALPHA = 0.8
_POPUP_MIN_WIDTH = 320
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


def highlight_javascript_tokens(text, state=0):
    """The token ("text", "keyword", "string", "number", "comment") of each
    character of one line of JavaScript that starts in ``state``."""
    tokens = ["text"] * len(text)

    def paint(start, length, token):
        tokens[start:start + length] = [token] * length

    _highlight_javascript(paint, text, state)
    return tokens


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


def code_font_size():
    """The font size of the code fields in pixels, as last zoomed."""
    try:
        size = int(window_state.settings().value(_FONT_SIZE_KEY, DEFAULT_FONT_SIZE))
    except (TypeError, ValueError):
        size = DEFAULT_FONT_SIZE
    return max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size))


def set_code_font_size(size):
    """Show the code of every code field at ``size`` pixels and remember it."""
    size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, int(size)))
    window_state.settings().setValue(_FONT_SIZE_KEY, size)
    application = QApplication.instance()
    if application is not None:
        for widget in application.allWidgets():
            if isinstance(widget, CodeEditor):
                widget.show_font_size(size)
    return size


class CodeEditor(QPlainTextEdit):
    """QPlainTextEdit for code: fixed-width font, Tab indents by INDENT, zooms."""

    # pylint: disable=too-many-instance-attributes

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.highlighter = JavaScriptHighlighter(self.document(), _CODE_COLORS)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._wheel_rest = 0
        self.line_numbers = _LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_width)
        self.updateRequest.connect(self._update_line_numbers)
        self.cursorPositionChanged.connect(self.line_numbers.update)
        # What set_completion offers: (translations, context, engine), or None
        self._completion = None
        self._scope = None
        self._waiting_at = None  # the cursor position completions wait for the game at
        self._tooltip_at = None  # (position, global position) a tooltip waits for the game at
        self.completion_popup = None
        self.signature_hint = None
        self._signature_waits = False  # the hint waits for the game's members
        self.show_font_size(code_font_size())

    # ------------------------------------------------------------------
    # Line numbers
    # ------------------------------------------------------------------
    def line_number_width(self):
        """The width of the line numbers: at least two digits, a space on
        each side, the dividing line and a space before the code."""
        digits = max(2, len(str(max(1, self.blockCount()))))
        metrics = QFontMetricsF(self.font())
        return round(metrics.horizontalAdvance("9") * digits
                     + metrics.horizontalAdvance(" ") * 3) + 1

    def _update_line_number_width(self, _count=0):
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_line_numbers(self, rect, dy):
        if dy:
            self.line_numbers.scroll(0, dy)
        else:
            self.line_numbers.update(0, rect.y(), self.line_numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_number_width()

    def resizeEvent(self, event):  # pylint: disable=invalid-name
        if event is not None:
            super().resizeEvent(event)
        contents = self.contentsRect()
        self.line_numbers.setGeometry(QRect(contents.left(), contents.top(),
                                            self.line_number_width(), contents.height()))

    def paint_line_numbers(self, event):
        """Paint the numbers of the visible lines; the cursor's stands out."""
        painter = QPainter(self.line_numbers)
        painter.setFont(self.font())
        muted = QColor(_CODE_COLORS["muted"])
        current = QColor(_CODE_COLORS["code_text"])
        space = round(QFontMetricsF(self.font()).horizontalAdvance(" "))
        # A faint line, a space after the numbers and a space before the code
        divider = QColor(muted)
        divider.setAlphaF(0.25)
        painter.setPen(divider)
        line_x = self.line_numbers.width() - space - 1
        painter.drawLine(line_x, event.rect().top(), line_x, event.rect().bottom())
        right = line_x - space
        cursor_block = self.textCursor().blockNumber()
        block = self.firstVisibleBlock()
        offset = self.contentOffset()
        # The line numbers sit beside the viewport, which starts below the
        # frame and the padding of the stylesheet
        shift = self.viewport().geometry().top() - self.line_numbers.geometry().top()
        top = round(self.blockBoundingGeometry(block).translated(offset).top()) + shift
        height = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            bottom = top + round(self.blockBoundingRect(block).height())
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(current if block.blockNumber() == cursor_block else muted)
                painter.drawText(0, top, right, height,
                                 Qt.AlignmentFlag.AlignRight, str(block.blockNumber() + 1))
            block = block.next()
            top = bottom
        painter.end()

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------
    def set_completion(self, translations, context=code_completion.SCRIPT, engine=None,
                       members=None, method=None):
        """Offer completions for code run with ``context``'s ctx (SCRIPT,
        VALUE_HOOK, METHOD_HOOK or HOOK) in a game with ``engine`` (see
        grembyte.ui.widgets.code_completion). ``members`` (a MemberLookup)
        reads the game's classes; ``method`` is a method hook's signature with
        parameter names, for ctx.args and ctx.this."""
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        this, args = code_completion.method_parameters(method) if method else (None, {})
        changed = getattr(self._scope and self._scope.lookup, "changed", None)
        if changed is not None:
            try:
                changed.disconnect(self._members_changed)
            except (RuntimeError, TypeError):
                pass
        self._scope = code_completion.Scope(members, args, this)
        changed = getattr(members, "changed", None)
        if changed is not None:
            changed.connect(self._members_changed)
        self._completion = (translations, context, engine)
        if self.completion_popup is None:
            self.completion_popup = CompletionPopup(self)
            self.signature_hint = SignatureHint(self)
            self.cursorPositionChanged.connect(self._cursor_moved)

    def _members_changed(self):
        """The game answered: show what was waiting for it at the cursor, or
        under the mouse."""
        if self._waiting_at is not None and self._waiting_at == self.textCursor().position():
            self.show_completions(typed=False)
        if self._signature_waits:
            self.update_signature(force=True)
        if self._tooltip_at is not None and QToolTip.isVisible():
            self.show_string_tooltip(*self._tooltip_at)

    def viewportEvent(self, event):  # pylint: disable=invalid-name
        if event.type() == QEvent.Type.ToolTip and self._completion is not None:
            self.show_string_tooltip(event.pos(), event.globalPos())
            return True
        return super().viewportEvent(event)

    def show_string_tooltip(self, position, global_position):
        """Show the tooltip of a ctx.find, ctx.forEach or ctx.static string at
        ``position`` in the viewport (see code_completion.tooltip); hide any
        other. Returns its text, or None."""
        self._tooltip_at = None
        text = self._string_tooltip(position)
        if text is None:
            QToolTip.hideText()
            return None
        if self._scope.pending:
            self._tooltip_at = (position, global_position)
        QToolTip.showText(global_position, text, self.viewport())
        return text

    def _string_tooltip(self, position):
        cursor = self.cursorForPosition(position)
        block = cursor.block()
        column = cursor.positionInBlock()
        line = block.text()
        # Only over the string itself, not past the end of the line
        if column >= len(line) or not self.cursorRect(cursor).adjusted(-8, -2, 8, 2).contains(
                position):
            return None
        previous = block.previous()
        state = max(previous.userState(), 0) if previous.isValid() else 0
        token = highlight_javascript_tokens(line, state)[column]
        translations, _context, engine = self._completion
        self._scope.pending = False
        if token == "string":
            found = code_completion.string_at(line, column)
            return None if found is None else code_completion.tooltip(
                found[0], found[1], self._scope, translations, engine)
        named = code_completion.identifier_at(line, column) if token == "text" else None
        if named is None or line[:named[1]].rstrip().endswith("."):
            return None  # a comment, a keyword, or a member (player.Name)
        # The code up to the end of this line: a declaration on it counts
        source = self.toPlainText()[:block.position() + len(line)]
        return code_completion.variable_tooltip(named[0], source, self._scope, translations)

    def completion_entries(self):
        """``(prefix, entries)`` at the cursor (see code_completion.completions)."""
        if self._completion is None:
            return "", ()
        _translations, context, engine = self._completion
        cursor = self.textCursor()
        block = cursor.block()
        line = block.text()[:cursor.positionInBlock()]
        previous = block.previous()
        state = previous.userState() if previous.isValid() else 0
        source = self.toPlainText()[:cursor.position()]
        return code_completion.completions(source, line, max(state, 0), context, engine,
                                           self._scope)

    def show_completions(self, typed=True):
        """Open, update or close the popup for the cursor's position. ``typed``:
        a key was typed; then a name needs two letters before keywords show."""
        popup = self.completion_popup
        if popup is None:
            return
        prefix, entries = self.completion_entries()
        cursor = self.textCursor()
        # Members the game is asked for: shown when it answers (_members_changed)
        self._waiting_at = (cursor.position()
                            if self._scope is not None and self._scope.pending else None)
        line = cursor.block().text()[:cursor.positionInBlock()]
        # After a dot and inside ctx.find("…") the list opens at once
        after_dot = (line[:len(line) - len(prefix)].rstrip().endswith(".")
                     or code_completion.string_argument(line) is not None)
        entries = [entry for entry in entries if code_completion.matches(entry, prefix)]
        if (not entries or (typed and not after_dot and len(prefix) < 2 and not popup.isVisible())
                or (len(entries) == 1 and entries[0].name == prefix)):
            popup.hide()
            return
        rect = self.cursorRect()
        below = self.viewport().mapToGlobal(rect.bottomLeft())
        popup.show_entries(entries, self._completion[0], below)

    def insert_completion(self, entry):
        """Put ``entry`` in place of the typed part of its name; a method gets
        its parentheses, with the cursor inside if it takes any."""
        if self.completion_popup is not None:
            self.completion_popup.hide()
        prefix, _entries = self.completion_entries()
        cursor = self.textCursor()
        argument = code_completion.string_argument(
            cursor.block().text()[:cursor.positionInBlock()])
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor,
                            len(prefix))
        following = self.document().characterAt(cursor.selectionEnd())
        if argument is not None:
            # A class name: the string is closed after it unless it already is
            quote = argument[1]
            cursor.insertText(entry.name + ("" if following == quote else quote))
        else:
            method = entry.params is not None and following != "("
            cursor.insertText(entry.name + ("()" if method else ""))
            if method and entry.params:
                cursor.movePosition(QTextCursor.MoveOperation.Left)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        if entry.params:
            self.update_signature(force=True)  # inside its parentheses now

    # ------------------------------------------------------------------
    # Signature while typing
    # ------------------------------------------------------------------
    def signature_at_cursor(self):
        """``(signatures, index)`` of the call typed at the cursor (see
        code_completion.signature_help), or None."""
        if self._completion is None:
            return None
        _translations, context, engine = self._completion
        source = self.toPlainText()[:self.textCursor().position()]
        return code_completion.signature_help(source, context, engine, self._scope)

    def update_signature(self, force=False):
        """Show, update or hide the signature of the call at the cursor. The
        hint opens with ``force`` (after "(" or ","), else it is only kept up
        to date while open."""
        hint = self.signature_hint
        if hint is None or (not force and not hint.isVisible()):
            return
        found = self.signature_at_cursor()
        self._signature_waits = self._scope is not None and self._scope.pending
        if found is None:
            hint.hide()
            return
        signatures, index = found
        hint.show_signatures(signatures, index, self._completion[0])

    def _cursor_moved(self):
        if self.signature_hint is not None and self.signature_hint.isVisible():
            self.update_signature()

    def _popup_key(self, event):
        """Handle a key for the open popup; whether it took it."""
        popup = self.completion_popup
        if popup is None or not popup.isVisible():
            return False
        key = event.key()
        steps = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1,
                 Qt.Key.Key_PageUp: -popup.page_rows(), Qt.Key.Key_PageDown: popup.page_rows()}
        if key in steps:
            popup.move_selection(steps[key])
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
            entry = popup.current_entry()
            if entry is None:
                popup.hide()
                return False
            self.insert_completion(entry)
        elif key == Qt.Key.Key_Escape:
            popup.hide()
        else:
            return False
        return True

    def _hint_key(self, event):
        """Escape closes the signature hint; Ctrl+Shift+Space opens it."""
        hint = self.signature_hint
        if hint is None:
            return False
        if event.key() == Qt.Key.Key_Escape and hint.isVisible():
            hint.hide()
            return True
        control_shift = (Qt.KeyboardModifier.ControlModifier
                         | Qt.KeyboardModifier.ShiftModifier)
        if event.key() == Qt.Key.Key_Space and event.modifiers() & control_shift == control_shift:
            self.update_signature(force=True)
            return True
        return False

    def focusOutEvent(self, event):  # pylint: disable=invalid-name
        if self.completion_popup is not None:
            self.completion_popup.hide()
            self.signature_hint.hide()
        super().focusOutEvent(event)

    def mousePressEvent(self, event):  # pylint: disable=invalid-name
        if self.completion_popup is not None:
            self.completion_popup.hide()
        super().mousePressEvent(event)

    def show_font_size(self, size):
        """Show the code at ``size`` pixels.

        The application stylesheet sets the font size of inputs, which beats
        setFont and zoomIn; a stylesheet of the widget's own beats it.
        """
        self.setStyleSheet("" if size == DEFAULT_FONT_SIZE else f"font-size: {size}px;")
        self.ensurePolished()
        # Tabs pasted from elsewhere are as wide as one indent
        self.setTabStopDistance(
            QFontMetricsF(self.font()).horizontalAdvance(" ") * len(INDENT))
        self._update_line_number_width()
        self.resizeEvent(None)

    def wheelEvent(self, event):  # pylint: disable=invalid-name
        if not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            super().wheelEvent(event)
            return
        # Touchpads send small steps: a size step per notch's worth
        self._wheel_rest += event.angleDelta().y()
        steps = int(self._wheel_rest / _WHEEL_STEP)
        if steps:
            self._wheel_rest -= steps * _WHEEL_STEP
            set_code_font_size(code_font_size() + steps)
        event.accept()

    def keyPressEvent(self, event):  # pylint: disable=invalid-name
        if self._popup_key(event) or self._hint_key(event):
            return
        popup = self.completion_popup
        if (popup is not None and event.key() == Qt.Key.Key_Space
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.show_completions(typed=False)
            return
        self._edit_key(event)
        if popup is not None:
            text = event.text()
            if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
                if popup.isVisible():
                    self.show_completions()
            elif text and text.isprintable():
                if text[-1].isalnum() or text[-1] in "_$.\"'`":
                    self.show_completions()
                else:
                    popup.hide()
            elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home,
                                 Qt.Key.Key_End):
                popup.hide()
            # A call's arguments begin or go on: show its signature
            self.update_signature(force=bool(text) and text[-1] in "(,")

    def _edit_key(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            step = {Qt.Key.Key_Plus: 1, Qt.Key.Key_Equal: 1, Qt.Key.Key_Minus: -1}.get(event.key())
            if step:
                set_code_font_size(code_font_size() + step)
                return
            if event.key() == Qt.Key.Key_0:
                set_code_font_size(DEFAULT_FONT_SIZE)
                return
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


class CompletionPopup(QTreeView):
    """The list of completions under a CodeEditor's cursor: name and
    parameters, and a description.

    A window that takes no focus, so typing goes on in the editor, which
    moves the selection and inserts entries (see CodeEditor._popup_key).
    A child of the editor, so Qt deletes it with the editor; QCompleter is not
    used, as it deletes its popup on its own and crashed when the popup was
    deleted first (closing every window at exit).
    """

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAllColumnsShowFocus(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setModel(QStandardItemModel(self))
        header = self.header()
        header.setStretchLastSection(False)
        # Widths are set in show_entries: what fits right of the cursor
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.clicked.connect(self._chosen)

    def show_entries(self, entries, translations, position):
        """Show ``entries`` (code_completion.Completion) at the global
        ``position``, the first one selected."""
        model = self.model()
        model.clear()
        # Names bold in the code's font; descriptions quieter: in the text font,
        # a little smaller and paler. Sized from the code, not the interface
        # font, which scaling may make larger; both centred in the row.
        name_font = QFont(self.editor.font())
        name_font.setBold(True)
        code_size = QFontInfo(self.editor.font()).pixelSize()
        text_font = QFont(self.font())
        text_font.setPixelSize(max(MIN_FONT_SIZE, round(code_size * _DESCRIPTION_SCALE)))
        quiet = QColor(_CODE_COLORS["muted"])
        quiet.setAlphaF(_DESCRIPTION_ALPHA)
        centred = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for entry in entries:
            text = code_completion.description(entry, translations)
            name = QStandardItem(entry.label)
            name.setData(entry, Qt.ItemDataRole.UserRole)
            # A long signature may be cut short in the list
            name.setToolTip(f"{entry.label}\n{text}")
            name.setFont(name_font)
            name.setTextAlignment(centred)
            detail = QStandardItem(text)
            detail.setFont(text_font)
            detail.setForeground(quiet)
            detail.setTextAlignment(centred)
            detail.setToolTip(text)
            model.appendRow([name, detail])
        self.setCurrentIndex(model.index(0, 0))
        self.doItemsLayout()
        rows = min(len(entries), _POPUP_ROWS)
        row_height = max(self.sizeHintForRow(0), 1)
        screen = self.editor.screen().availableGeometry()
        # It starts at the cursor and takes at most the room to its right;
        # only a cursor right at the edge moves it left, to keep it usable
        x = max(screen.left(), min(position.x(), screen.right() + 1 - _POPUP_MIN_WIDTH))
        room = screen.right() + 1 - x
        frame = (self.verticalScrollBar().sizeHint().width() if len(entries) > _POPUP_ROWS
                 else 0) + 2 * self.frameWidth() + 8
        names, details = self.sizeHintForColumn(0), self.sizeHintForColumn(1)
        width = min(names + details + frame, room)
        if names + details + frame > width:
            # Too wide: names get up to two thirds, cut short with "…"
            names = min(names, max((width - frame) * 2 // 3, width - frame - details))
        self.setColumnWidth(0, names)
        self.setColumnWidth(1, max(width - frame - names, 0))
        size = QSize(width, row_height * rows + 2 * self.frameWidth())
        y = position.y()
        if y + size.height() > screen.bottom():  # above the line instead
            y -= size.height() + self.editor.cursorRect().height()
        self.setGeometry(QRect(x, y, size.width(), size.height()))
        self.show()
        self.raise_()

    def page_rows(self):
        """How many rows Page Up and Page Down move."""
        return max(1, min(self.model().rowCount(), _POPUP_ROWS) - 1)

    def move_selection(self, step):
        """Select the entry ``step`` rows further, stopping at the ends."""
        count = self.model().rowCount()
        if count:
            row = max(0, min(count - 1, self.currentIndex().row() + step))
            self.setCurrentIndex(self.model().index(row, 0))

    def current_entry(self):
        """The selected code_completion.Completion, or None."""
        index = self.currentIndex()
        return None if not index.isValid() else index.siblingAtColumn(0).data(
            Qt.ItemDataRole.UserRole)

    def _chosen(self, index):
        entry = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        if entry is not None:
            self.editor.insert_completion(entry)


class SignatureHint(QLabel):
    """The signature of the call whose arguments are typed, above the
    CodeEditor's cursor: each overload on a line of its own with the current
    parameter in bold, the return type, and the description of a fixed one.
    A window that takes no focus, like CompletionPopup; a child of the editor."""

    MAX_LINES = 8

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setMargin(4)

    def show_signatures(self, signatures, index, translations):
        """Show ``signatures`` (code_completion.Signature) with argument
        ``index`` marked, above the editor's cursor."""
        self.setStyleSheet(
            f"QLabel {{ background: {theme_color('surface')}; color: {theme_color('text')};"
            f" border: 1px solid {theme_color('border')}; }}")
        self.setFont(self.editor.font())
        # Overloads that take as many arguments first
        ordered = sorted(signatures, key=lambda signature: len(signature.params) <= index
                         and not _variadic(signature.params))
        lines = [self._line(signature, index) for signature in ordered[:self.MAX_LINES]]
        entry = ordered[0].entry
        if entry is not None:
            muted = theme_color("muted")
            lines.append(f'<span style="color:{muted}">'
                         f"{html.escape(code_completion.description(entry, translations))}</span>")
        self.setText("<br>".join(lines))
        # The stylesheet's font applies when polished: size it after that
        self.ensurePolished()
        self.resize(self.sizeHint())
        rect = self.editor.cursorRect()
        top = self.editor.viewport().mapToGlobal(rect.topLeft())
        screen = self.editor.screen().availableGeometry()
        x = max(screen.left(), min(top.x(), screen.right() + 1 - self.width()))
        y = top.y() - self.height() - 2
        if y < screen.top():  # below the line instead
            y = self.editor.viewport().mapToGlobal(rect.bottomLeft()).y() + 2
        self.move(x, y)
        self.show()
        self.raise_()

    @staticmethod
    def _line(signature, index):
        params = list(signature.params)
        current = index if index < len(params) else (
            len(params) - 1 if _variadic(params) else None)
        shown = [f"<b>{html.escape(param)}</b>" if number == current else html.escape(param)
                 for number, param in enumerate(params)]
        returns = f" → {html.escape(signature.returns)}" if signature.returns else ""
        return f"{html.escape(signature.name)}({', '.join(shown)}){returns}"


def _variadic(params):
    """Whether the last parameter takes any number of arguments (``...args``)."""
    return bool(params) and params[-1].lstrip("[").startswith("...")


class _LineNumberArea(QWidget):
    """The strip left of a CodeEditor's text with the line numbers."""

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):  # pylint: disable=invalid-name
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event):  # pylint: disable=invalid-name
        self.editor.paint_line_numbers(event)


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
            widget.line_numbers.update()
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
