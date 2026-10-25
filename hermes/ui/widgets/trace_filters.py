"""Choosing the methods of a trace with filters (see hermes.features.trace_filters)."""

import itertools
from dataclasses import replace

from PyQt6.QtCore import Qt, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from hermes.features.trace_filters import BUILTIN_FILTERS, TraceFilter, select_methods
from hermes.ui.widgets.dialog_buttons import dialog_button_row

# Methods the editor shows as examples of what a filter matches
_PREVIEW = 8
# Ids of filters kept without a repository (the database's are positive)
_unsaved_ids = itertools.count(-1, -1)


def filter_title(translations, trace_filter):
    """The name a filter is shown with (built-in ones are translated)."""
    if trace_filter.builtin:
        return translations[f"trace.filter_{trace_filter.builtin}"]
    return trace_filter.name


def filter_summary(translations, trace_filter):
    """A tooltip that says what a filter matches."""
    parts = []
    if trace_filter.words:
        parts.append(translations["trace.filter_summary_words"].format(
            words=", ".join(trace_filter.words)))
    if trace_filter.exclude:
        parts.append(translations["trace.filter_summary_exclude"].format(
            words=", ".join(trace_filter.exclude)))
    if trace_filter.numbers:
        parts.append(translations["trace.filter_numbers"])
    if trace_filter.changes:
        parts.append(translations["trace.filter_changes"])
    return "\n".join(parts)


class TraceFilterPanel(QWidget):
    """The filters to choose from, how many methods each one picks, and the total.

    ``repository`` keeps the user's filters (TraceFilterRepository); without
    one they last until the window closes.
    """

    # pylint: disable=too-many-instance-attributes
    selection_changed = pyqtSignal()

    def __init__(self, translations, repository=None, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.repository = repository
        self.candidates = None
        self._user_filters = repository.list_filters() if repository is not None else []
        self._build_ui()
        self._fill()

    # --- UI ------------------------------------------------------------------
    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        left = QVBoxLayout()
        title = QLabel(self.translations["trace.filters_label"])
        title.setToolTip(self.translations["trace.filters_tooltip"])
        left.addWidget(title)
        self.list = QListWidget()
        self.list.setMaximumHeight(130)
        self.list.itemChanged.connect(lambda _item: self._changed())
        self.list.currentRowChanged.connect(lambda _row: self._update_buttons())
        self.list.itemDoubleClicked.connect(lambda _item: self._edit())
        left.addWidget(self.list)
        layout.addLayout(left, 1)

        right = QVBoxLayout()
        self.new_button = QPushButton(self.translations["trace.filter_new"])
        self.new_button.clicked.connect(self._new)
        self.edit_button = QPushButton(self.translations["trace.filter_edit"])
        self.edit_button.clicked.connect(self._edit)
        self.delete_button = QPushButton(self.translations["trace.filter_delete"])
        self.delete_button.clicked.connect(self._delete)
        self.check_all_button = QPushButton(self.translations["trace.filter_check_all"])
        self.check_all_button.clicked.connect(self._toggle_all)
        for button in (self.new_button, self.edit_button, self.delete_button,
                       self.check_all_button):
            right.addWidget(button)
        self.generated_check = QCheckBox(self.translations["trace.filter_generated"])
        self.generated_check.setToolTip(self.translations["trace.filter_generated_tooltip"])
        self.generated_check.toggled.connect(lambda _checked: self._changed(recount=True))
        right.addWidget(self.generated_check)
        right.addStretch()
        layout.addLayout(right)

    def _filters(self):
        return list(BUILTIN_FILTERS) + self._user_filters

    def _fill(self):
        checked = {self._key(item) for item in self.checked_filters()}
        self.list.blockSignals(True)
        self.list.clear()
        for trace_filter in self._filters():
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, trace_filter)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if self._key(trace_filter) in checked
                               else Qt.CheckState.Unchecked)
            item.setToolTip(filter_summary(self.translations, trace_filter))
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._update_counts()
        self._update_buttons()

    @staticmethod
    def _key(trace_filter):
        return trace_filter.builtin or ("user", trace_filter.filter_id, trace_filter.name)

    def _update_counts(self):
        generated = self.generated_check.isChecked()
        self.list.blockSignals(True)
        for row in range(self.list.count()):
            item = self.list.item(row)
            trace_filter = item.data(Qt.ItemDataRole.UserRole)
            title = filter_title(self.translations, trace_filter)
            if self.candidates is not None:
                count = len(select_methods(self.candidates, [trace_filter], generated=generated))
                title = self.translations["trace.filter_with_count"].format(
                    name=title, count=count)
            item.setText(title)
        self.list.blockSignals(False)

    def _update_buttons(self):
        current = self._current()
        self.edit_button.setEnabled(current is not None)
        self.delete_button.setEnabled(current is not None and not current.builtin)
        key = "trace.filter_uncheck_all" if self._all_checked() else "trace.filter_check_all"
        self.check_all_button.setText(self.translations[key])

    def _all_checked(self):
        return self.list.count() > 0 and len(self.checked_filters()) == self.list.count()

    def _toggle_all(self):
        """Tick every filter, or untick them all when every one is ticked already."""
        state = Qt.CheckState.Unchecked if self._all_checked() else Qt.CheckState.Checked
        self.list.blockSignals(True)
        for row in range(self.list.count()):
            self.list.item(row).setCheckState(state)
        self.list.blockSignals(False)
        self._changed()

    def _changed(self, recount=False):
        # What each filter picks only changes with the candidates, the
        # generated-code switch and the filters themselves, not with a tick.
        if recount:
            self._update_counts()
        self._update_buttons()
        self.selection_changed.emit()

    # --- State ---------------------------------------------------------------
    def set_candidates(self, methods):
        """The methods to choose from (CatalogMethods), or None while unknown."""
        self.candidates = methods
        self._update_counts()
        self.selection_changed.emit()

    def checked_filters(self):
        """The filters the user ticked."""
        return [self.list.item(row).data(Qt.ItemDataRole.UserRole)
                for row in range(self.list.count())
                if self.list.item(row).checkState() == Qt.CheckState.Checked]

    def include_generated(self):
        """Whether compiler-generated methods may be chosen."""
        return self.generated_check.isChecked()

    def selection(self, words=()):
        """The candidates the ticked filters and ``words`` pick (None while unknown)."""
        if self.candidates is None:
            return None
        return select_methods(self.candidates, self.checked_filters(), words,
                              generated=self.include_generated())

    def _current(self):
        item = self.list.currentItem()
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    # --- Editing -------------------------------------------------------------
    def _new(self):
        self._open_editor(TraceFilter(""))

    def _edit(self):
        current = self._current()
        if current is None:
            return
        if current.builtin:
            # Built-in filters stay as shipped; edit a copy.
            name = self.translations["trace.filter_copy_name"].format(
                name=filter_title(self.translations, current))
            current = current.copy_as(name)
        self._open_editor(current)

    def _open_editor(self, trace_filter):
        dialog = TraceFilterDialog(self.translations, trace_filter, self.candidates,
                                   self._save, parent=self)
        dialog.exec()

    def _save(self, trace_filter):
        """Store an edited filter; raises ValueError for a duplicate name."""
        if any(other.name == trace_filter.name and other.filter_id != trace_filter.filter_id
               for other in self._user_filters):
            raise ValueError(trace_filter.name)
        if self.repository is not None:
            self.repository.save_filter(trace_filter)
            self._user_filters = self.repository.list_filters()
        else:
            if trace_filter.filter_id is None:
                trace_filter = replace(trace_filter, filter_id=next(_unsaved_ids))
            self._user_filters = sorted(
                [other for other in self._user_filters
                 if other.filter_id != trace_filter.filter_id] + [trace_filter],
                key=lambda other: other.name.lower())
        self._fill()
        self.selection_changed.emit()

    def _delete(self):
        current = self._current()
        if current is None or current.builtin:
            return
        answer = QMessageBox.question(
            self, self.translations["trace.filter_dialog_title"],
            self.translations["trace.filter_delete_confirm"].format(name=current.name))
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.repository is not None:
            self.repository.delete_filter(current.filter_id)
            self._user_filters = self.repository.list_filters()
        else:
            self._user_filters = [f for f in self._user_filters
                                  if f.filter_id != current.filter_id]
        self._fill()
        self.selection_changed.emit()


class TraceFilterDialog(QDialog):
    """Create or change a user's trace filter, with a preview of what it picks.

    ``save`` stores the result and raises ValueError for a duplicate name.
    """

    # pylint: disable=too-many-instance-attributes,too-many-arguments
    def __init__(self, translations, trace_filter, candidates, save, parent=None):
        # pylint: disable=too-many-positional-arguments
        super().__init__(parent)
        self.translations = translations
        self.original = trace_filter
        self.candidates = candidates
        self._save = save
        self.result_filter = None
        self.setWindowTitle(translations["trace.filter_dialog_title"])
        self.resize(560, 420)
        self._build_ui()
        self._preview()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_input = QLineEdit(self.original.name)
        form.addRow(self.translations["trace.filter_name"], self.name_input)
        self.words_input = QLineEdit(", ".join(self.original.words))
        self.words_input.setToolTip(self.translations["trace.filter_words_tooltip"])
        self.words_input.setPlaceholderText(self.translations["trace.filter_words_placeholder"])
        form.addRow(self.translations["trace.filter_words"], self.words_input)
        self.exclude_input = QLineEdit(", ".join(self.original.exclude))
        self.exclude_input.setToolTip(self.translations["trace.filter_words_tooltip"])
        form.addRow(self.translations["trace.filter_exclude"], self.exclude_input)
        self.numbers_check = QCheckBox(self.translations["trace.filter_numbers"])
        self.numbers_check.setToolTip(self.translations["trace.filter_numbers_tooltip"])
        self.numbers_check.setChecked(self.original.numbers)
        form.addRow("", self.numbers_check)
        self.changes_check = QCheckBox(self.translations["trace.filter_changes"])
        self.changes_check.setToolTip(self.translations["trace.filter_changes_tooltip"])
        self.changes_check.setChecked(self.original.changes)
        form.addRow("", self.changes_check)
        layout.addLayout(form)

        self.preview_label = QLabel("")
        layout.addWidget(self.preview_label)
        self.preview_list = QListWidget()
        layout.addWidget(self.preview_list, 1)
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #f85149;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        layout.addLayout(dialog_button_row(self.translations, "button.save",
                                           self._accept, self.reject))

        for line in (self.words_input, self.exclude_input):
            line.textChanged.connect(lambda _text: self._preview())
        for check in (self.numbers_check, self.changes_check):
            check.toggled.connect(lambda _checked: self._preview())

    def edited_filter(self):
        """The filter as the fields describe it."""
        return TraceFilter.from_text(
            self.name_input.text().strip(), self.words_input.text(), self.exclude_input.text(),
            self.numbers_check.isChecked(), self.changes_check.isChecked(),
            filter_id=self.original.filter_id, description=self.original.description)

    def _preview(self):
        self.preview_list.clear()
        if self.candidates is None:
            self.preview_label.setText(self.translations["trace.filters_loading"])
            return
        chosen = select_methods(self.candidates, [self.edited_filter()])
        self.preview_label.setText(
            self.translations["trace.filter_preview"].format(count=len(chosen)))
        for method in chosen[:_PREVIEW]:
            self.preview_list.addItem(method.signature)
        if len(chosen) > _PREVIEW:
            self.preview_list.addItem("…")

    def _accept(self):
        edited = self.edited_filter()
        if not edited.name:
            self.error_label.setText(self.translations["trace.filter_error_name"])
            return
        if not (edited.words or edited.numbers or edited.changes):
            self.error_label.setText(self.translations["trace.filter_error_empty"])
            return
        try:
            self._save(edited)
        except ValueError:
            self.error_label.setText(
                self.translations["trace.filter_error_duplicate"].format(name=edited.name))
            return
        self.result_filter = edited
        self.accept()
