"""Editors that stay open beside the rest of Grembyte.

The script editor and the method hook editor were modal dialogs: while one
was open, the object inspector, the notes and the object browser could not
be used. ``EditorDialog`` is a QDialog that is shown with ``show()`` (not
``exec()``) and docks in the main window with its ``pin_button`` (see
grembyte.ui.widgets.docking). It knows whether it has changes that are not
saved – a snapshot of its input fields, taken when it opens and when it is
saved – and asks before closing then (Cancel, Escape, its tab's close
button, the window's).

A subclass calls ``setup_editor`` once its fields are built and ``accept``
when saving; ``save_check``, if given, may refuse that with a text shown in
the editor (another game attached …), so nothing typed is lost. When the
game ends or another one is attached, ``close_if_saved`` closes an editor
without unsaved changes; one with changes stays open.
"""

from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractButton,
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QKeySequenceEdit,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QTableWidget,
)

from grembyte.ui.widgets.docking import TAB_TITLE, PinButton, close_dialog


def input_snapshot(widget):
    """The values of all input fields in ``widget``, in a stable order: two
    snapshots differ when something was typed or chosen in between."""
    values = []
    for child in widget.findChildren(QLineEdit) + widget.findChildren(QAbstractSpinBox):
        values.append(child.text())
    values += [child.toPlainText() for child in widget.findChildren(QPlainTextEdit)]
    values += [(child.currentIndex(), child.currentText())
               for child in widget.findChildren(QComboBox)]
    values += [child.keySequence().toString() for child in widget.findChildren(QKeySequenceEdit)]
    values += [child.isChecked() for child in widget.findChildren(QAbstractButton)
               if child.isCheckable() and not isinstance(child, PinButton)]
    for table in widget.findChildren(QTableWidget):
        values.append(tuple(
            (item.text(), item.checkState().value) if item is not None else None
            for item in (table.item(row, column) for row in range(table.rowCount())
                         for column in range(table.columnCount()))))
    return tuple(values)


class EditorDialog(QDialog):
    """A dialog that edits a script or a hook and stays open beside the rest."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.pin_button = None
        self.save_check = None
        self._saved = None
        self._discard_confirmed = False
        self._translations = None

    def setup_editor(self, translations, tab_title, save_check=None):
        """Give the editor its pin button (added to a layout by the subclass),
        a short title for its tab, and take the snapshot it is compared to."""
        self._translations = translations
        self.save_check = save_check
        self.pin_button = PinButton(translations)
        self.setProperty(TAB_TITLE, tab_title)
        self.mark_saved()

    def snapshot(self):
        """What counts as the editor's content (see input_snapshot)."""
        return input_snapshot(self)

    def mark_saved(self):
        """The content now is the saved one."""
        self._saved = self.snapshot()

    def is_dirty(self):
        """Whether something changed since the editor opened or was saved."""
        return self._saved is not None and self.snapshot() != self._saved

    def refused(self):
        """The text why saving is refused now (see the module), or None."""
        return None if self.save_check is None else self.save_check(self)

    def confirm_discard(self):
        """Whether closing may go on: nothing unsaved, or the user agreed to
        discard it (asked once per closing)."""
        if self._discard_confirmed or not self.is_dirty() or self._translations is None:
            return True
        answer = QMessageBox.question(
            self, self._translations["editor.discard_title"],
            self._translations["editor.discard_text"],
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        self._discard_confirmed = answer == QMessageBox.StandardButton.Discard
        return self._discard_confirmed

    def close_if_saved(self):
        """Close the editor when it holds nothing unsaved (the game ended or
        another one was attached); returns whether it closed. One with changes
        stays open, so nothing typed is lost."""
        if self.is_dirty():
            return False
        close_dialog(self)
        return True

    def accept(self):  # pylint: disable=missing-function-docstring
        self.mark_saved()
        super().accept()

    def reject(self):  # pylint: disable=missing-function-docstring
        if self.confirm_discard():
            super().reject()

    def closeEvent(self, event):  # pylint: disable=invalid-name
        # A docked editor in a tab behind is hidden, where QDialog would
        # close without asking
        if not self.confirm_discard():
            event.ignore()
            return
        super().closeEvent(event)
