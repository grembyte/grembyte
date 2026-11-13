"""Markdown shown formatted, in the colours of the theme (the notes' preview).

QTextBrowser.setMarkdown takes no style sheet, so the document it builds is
formatted afterwards: headings, inline code, code blocks (coloured like the
code editor), quotes, links, rules and tables.
"""

from PySide6.QtGui import (  # pylint: disable=import-error,no-name-in-module
    QBrush,
    QFont,
    QFontDatabase,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
    QTextFrameFormat,
    QTextLength,
    QTextTable,
)
from PySide6.QtWidgets import QTextBrowser  # pylint: disable=import-error,no-name-in-module

from grembyte.ui.widgets.code_editor import highlight_code
from grembyte.ui.widgets.icons import parse_color, theme_color

_COLOR_KEYS = ("muted", "accent", "border", "border_subtle", "surface", "alternate",
               "code_text", "code_keyword", "code_string", "code_number")
_TOKEN_COLORS = {"text": "code_text", "keyword": "code_keyword", "string": "code_string",
                 "number": "code_number", "comment": "muted"}
_HEADING_SPACE = {1: 14, 2: 12, 3: 10}
_Property = QTextFormat.Property


def _char_format(color=None, background=None, bold=False, italic=False, underline=False):
    text_format = QTextCharFormat()
    if color is not None:
        text_format.setForeground(color)
    if background is not None:
        text_format.setBackground(background)
    if bold:
        text_format.setFontWeight(QFont.Weight.Bold)
    if italic:
        text_format.setFontItalic(True)
    if underline:
        text_format.setFontUnderline(True)
    return text_format


def _merge(document, start, length, text_format):
    cursor = QTextCursor(document)
    cursor.setPosition(start)
    cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
    cursor.mergeCharFormat(text_format)


def _merge_block(block, block_format):
    QTextCursor(block).mergeBlockFormat(block_format)


def _is_code(char_format):
    return char_format.fontFixedPitch() or "monospace" in (char_format.fontFamilies() or [])


def _style_code_line(document, block, language, state, colors):
    def paint(start, length, token):
        _merge(document, block.position() + start, length,
               _char_format(colors[_TOKEN_COLORS[token]], bold=token == "keyword"))
    return highlight_code(paint, language, block.text(), state)


def _style_inline(document, block, colors):
    code = _char_format(colors["code_string"], colors["surface"])
    link = _char_format(colors["accent"], underline=True)
    ranges = []
    fragments = block.begin()
    while not fragments.atEnd():
        fragment = fragments.fragment()
        char_format = fragment.charFormat()
        if char_format.isAnchor():
            ranges.append((fragment.position(), fragment.length(), link))
        elif _is_code(char_format):
            ranges.append((fragment.position(), fragment.length(), code))
        fragments += 1
    for start, length, text_format in ranges:
        _merge(document, start, length, text_format)


def _style_table(table, colors):
    table_format = table.format()
    table_format.setBorder(1)
    table_format.setBorderBrush(QBrush(colors["border"]))
    table_format.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
    table_format.setBorderCollapse(True)
    table_format.setCellSpacing(0)
    table_format.setCellPadding(5)
    table_format.setTopMargin(4)
    table_format.setBottomMargin(8)
    table_format.setWidth(QTextLength(QTextLength.Type.VariableLength, 0))
    table.setFormat(table_format)
    for row in range(table.rows()):
        background = colors["border_subtle"] if row == 0 else (
            colors["alternate"] if row % 2 == 0 else None)
        if background is None:
            continue
        for column in range(table.columns()):
            cell = table.cellAt(row, column)
            cell_format = cell.format()
            cell_format.setBackground(background)
            cell.setFormat(cell_format)


def _frame_code(document, start, end, indent, colors):
    """Put the code block from ``start`` to ``end`` in a padded, shaded box
    ``indent`` pixels from the left."""
    frame_format = QTextFrameFormat()
    frame_format.setBackground(colors["surface"])
    frame_format.setBorder(1)
    frame_format.setBorderBrush(QBrush(colors["border_subtle"]))
    frame_format.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
    frame_format.setPadding(6)
    frame_format.setTopMargin(4)
    frame_format.setBottomMargin(8)
    frame_format.setLeftMargin(indent)
    cursor = QTextCursor(document)
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    frame = cursor.insertFrame(frame_format)
    # A frame needs a block before and after it: keep the empty ones flat
    flat = QTextBlockFormat()
    flat.setLineHeight(0, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
    for position in (frame.firstPosition() - 1, frame.lastPosition() + 1):
        block = document.findBlock(position)
        if block.isValid() and not block.text():
            _merge_block(block, flat)


def style_markdown(document, colors):
    """Format the ``document`` QTextDocument.setMarkdown built in the theme ``colors``
    (a theme's colour keys to ``#rrggbb``)."""
    colors = {key: parse_color(colors[key]) for key in _COLOR_KEYS}
    fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    cursor = QTextCursor(document)
    cursor.beginEditBlock()
    code_language = None
    code_state = 0
    code_runs = []
    block = document.begin()
    while block.isValid():
        block_format = block.blockFormat()
        if block_format.hasProperty(_Property.BlockCodeLanguage):
            language = block_format.stringProperty(_Property.BlockCodeLanguage)
            if language != code_language:
                code_state = 0
                # The box takes the indent of code in a list or quote
                indent = (block_format.leftMargin()
                          + block_format.indent() * document.indentWidth())
                code_runs.append([block.position(), block.position() + block.length() - 1,
                                  indent])
            else:
                code_runs[-1][1] = block.position() + block.length() - 1
            unindented = QTextBlockFormat()
            unindented.setLeftMargin(0)
            unindented.setIndent(0)
            _merge_block(block, unindented)
            line = QTextCharFormat()
            line.setFontFamilies([fixed.family()])
            _merge(document, block.position(), block.length() - 1, line)
            code_state = _style_code_line(document, block, language, code_state, colors)
            code_language = language
            block = block.next()
            continue
        code_language = None
        level = block_format.headingLevel()
        if level:
            heading = QTextBlockFormat()
            heading.setTopMargin(_HEADING_SPACE.get(level, 8))
            heading.setBottomMargin(4)
            if level <= 2:
                heading.setProperty(_Property.BlockTrailingHorizontalRulerWidth,
                                    QTextLength(QTextLength.Type.PercentageLength, 100))
                heading.setBackground(colors["border_subtle"])
            _merge_block(block, heading)
            _merge(document, block.position(), block.length() - 1,
                   _char_format(colors["code_keyword"], bold=True))
        elif block_format.hasProperty(_Property.BlockTrailingHorizontalRulerWidth):
            rule = QTextBlockFormat()
            rule.setBackground(colors["border"])
            _merge_block(block, rule)
        elif block_format.intProperty(_Property.BlockQuoteLevel):
            _merge(document, block.position(), block.length() - 1,
                   _char_format(colors["muted"], italic=True))
        _style_inline(document, block, colors)
        block = block.next()
    for frame in document.rootFrame().childFrames():
        if isinstance(frame, QTextTable):
            _style_table(frame, colors)
    # From the end: a frame moves the text after it
    for start, end, indent in reversed(code_runs):
        _frame_code(document, start, end, indent, colors)
    cursor.endEditBlock()


class MarkdownPreview(QTextBrowser):
    """A QTextBrowser for Markdown formatted in the current theme's colours."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self.document().setDocumentMargin(10)
        self._markdown = ""
        self._colors = {key: theme_color(key) for key in _COLOR_KEYS}

    def set_markdown(self, text):
        """Show ``text`` formatted, keeping the scroll position."""
        self._markdown = text
        scroll = self.verticalScrollBar().value()
        self.setMarkdown(text)
        style_markdown(self.document(), self._colors)
        self.verticalScrollBar().setValue(scroll)

    def code_theme_changed(self, colors):
        """Format the text again in the theme ``colors`` (see apply_code_theme)."""
        self._colors = {key: colors[key] for key in _COLOR_KEYS}
        self.set_markdown(self._markdown)

