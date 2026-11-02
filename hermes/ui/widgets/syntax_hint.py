"""A line under a code editor that says whether its JavaScript compiles."""

from PySide6.QtCore import QTimer  # pylint: disable=import-error,no-name-in-module

from hermes.hooks.syntax import FUNCTION, checker, looks_like_function
from hermes.ui.widgets.labels import WrappedLabel

# Typing pauses this long before the code is checked.
CHECK_DELAY_MS = 400

_COLORS = {"error": "#f85149", "warning": "#d29922", "ok": "#3fb950"}


def syntax_problem(translations, kind, code):
    """What is wrong with the syntax of ``code`` (see hermes.hooks.syntax), else None."""
    if not code.strip():
        return None
    issue = checker().check(kind, code)
    if issue is None:
        return None
    if issue.line is None:
        return translations["syntax.error_no_line"].format(message=issue.message)
    return translations["syntax.error"].format(line=issue.line, message=issue.message)


class SyntaxHint(WrappedLabel):
    """Check ``editor`` (a QPlainTextEdit) while one types; ``kind`` as in hermes.hooks.syntax.

    Shows nothing when the check cannot run (no Frida system session).
    """

    def __init__(self, translations, editor, kind, parent=None):
        super().__init__("", parent)
        self.translations = translations
        self.editor = editor
        self.kind = kind
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(CHECK_DELAY_MS)
        self._timer.timeout.connect(self.check)
        editor.textChanged.connect(self._timer.start)
        self._timer.start()

    def problem(self):
        """The syntax error of the editor's code now (also shown), else None."""
        self._timer.stop()
        return self.check()

    def check(self):
        code = self.editor.toPlainText()
        problem = syntax_problem(self.translations, self.kind, code)
        if problem is not None:
            self._show(problem, "error")
        elif not code.strip() or not checker().available:
            self._show("", "ok")
        elif self.kind == FUNCTION and not looks_like_function(code):
            self._show(self.translations["syntax.not_function"], "warning")
        else:
            self._show(self.translations["syntax.ok"], "ok")
        return problem

    def _show(self, text, level):
        self.setStyleSheet(f"color: {_COLORS[level]};")
        self.setText(text)
