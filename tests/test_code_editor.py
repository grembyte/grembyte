"""The code fields of scripts, hooks and templates (see hermes.ui.widgets.code_editor)."""

from tests.test_hooks import fixture_qt_app, fixture_translations  # pylint: disable=unused-import


def _press(editor, key):
    # pylint: disable=import-outside-toplevel
    from PySide6.QtTest import QTest  # pylint: disable=import-error,no-name-in-module

    QTest.keyClick(editor, key)


def test_tab_indents_by_two_spaces_and_shift_tab_outdents(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtGui import QTextCursor  # pylint: disable=import-error,no-name-in-module
    from hermes.ui.widgets.code_editor import CodeEditor

    editor = CodeEditor("a\nb\nc")
    cursor = editor.textCursor()
    cursor.setPosition(0)
    editor.setTextCursor(cursor)
    _press(editor, Qt.Key.Key_Tab)
    assert editor.toPlainText() == "  a\nb\nc"

    # Several lines: each one; a selection ending at a line's start leaves it out
    cursor.setPosition(0)
    cursor.setPosition(editor.document().findBlockByNumber(2).position(),
                       QTextCursor.MoveMode.KeepAnchor)
    editor.setTextCursor(cursor)
    _press(editor, Qt.Key.Key_Tab)
    assert editor.toPlainText() == "    a\n  b\nc"
    _press(editor, Qt.Key.Key_Backtab)
    assert editor.toPlainText() == "  a\nb\nc"
    _press(editor, Qt.Key.Key_Backtab)  # nothing left to remove in b
    assert editor.toPlainText() == "a\nb\nc"
    editor.undo()  # one step per key press
    assert editor.toPlainText() == "  a\nb\nc"

    # A tab pasted from elsewhere is outdented too, and only one indent wide
    editor.setPlainText("\tx")
    _press(editor, Qt.Key.Key_Backtab)
    assert editor.toPlainText() == "x"
    width = editor.fontMetrics().horizontalAdvance(" ")
    assert abs(editor.tabStopDistance() - 2 * width) < 1


def test_the_code_takes_the_height_the_window_grows_by(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.scripts import ScriptDialog

    dialog = ScriptDialog(translations)
    dialog.show()
    dialog.resize(640, 520)
    qt_app.processEvents()
    small = dialog.code_edit.height()
    dialog.resize(640, 820)
    qt_app.processEvents()
    assert dialog.code_edit.height() >= small + 250
    dialog.close()


def test_javascript_tokens_use_theme_colors(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.styles.themes import ThemeManager
    from hermes.ui.widgets.code_editor import CodeEditor

    manager = ThemeManager(user_theme_dir="/tmp/no-hermes-themes")
    colors = manager.load_theme("Solarized-Light")["colors"]
    editor = CodeEditor("const player = 'Hero';\nconst health = 42;")
    editor.highlighter.set_colors(colors)
    try:
        block = editor.document().firstBlock()
        formats = block.layout().formats()

        def color_at(position):
            return next(format_range.format.foreground().color().name()
                        for format_range in formats
                        if format_range.start <= position
                        < format_range.start + format_range.length)

        assert color_at(0) == colors["code_keyword"]
        assert color_at(block.text().index("'Hero'")) == colors["code_string"]
        assert color_at(len(block.text()) - 1) == colors["code_text"]
        editor.highlighter.rehighlight()
        number_block = editor.document().findBlockByNumber(1)
        number_range = next(format_range for format_range in number_block.layout().formats()
                            if format_range.start <= number_block.text().index("42")
                            < format_range.start + format_range.length)
        assert number_range.format.foreground().color().name() == colors["code_number"]
    finally:
        editor.close()


def test_existing_code_editor_rehighlights_after_theme_change(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.styles.themes import ThemeManager
    from hermes.ui.widgets.code_editor import CodeEditor, apply_code_theme

    manager = ThemeManager(user_theme_dir="/tmp/no-hermes-themes")
    editor = CodeEditor("return 12;")
    try:
        apply_code_theme(manager.load_theme("Solarized-Dark")["colors"])
        block = editor.document().firstBlock()
        keyword = next(format_range for format_range in block.layout().formats()
                       if format_range.start == 0)
        assert keyword.format.foreground().color().name() == "#cb4b16"
    finally:
        apply_code_theme(manager.load_theme("Github-Dark")["colors"])
        editor.close()
