"""The window of the general settings (hermes.core.settings), under Settings in the menu."""

from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from hermes.core import settings
from hermes.ui.widgets.dialog_buttons import dialog_button_row
from hermes.ui.widgets.icons import set_button_icon


_SETTING_CATEGORIES = (
    ("general", ("activity_log", "refresh_interval_ms", "language")),
    ("trace", ("wine_trace_limit", "large_trace", "trace_sequence")),
    ("hooks", ("hook_short_jump", "hook_repatch", "hook_profiling")),
    ("wine", ("wine_gadget_download",)),
)


class SettingsDialog(QDialog):
    """Show the settings, store the changed ones and apply them.

    ``repository`` stores them (hermes.db.repository.settings); ``changed``
    holds {key: value} of what the last save changed.
    """

    # pylint: disable=too-few-public-methods

    def __init__(self, translations, repository, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.repository = repository
        self.changed = {}
        self.editors = {}
        self.setWindowTitle(translations["settings.title"])
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        for category, keys in _SETTING_CATEGORIES:
            group = QGroupBox(translations[f"settings.category.{category}"])
            group.setObjectName(f"settings_category_{category}")
            form = QFormLayout(group)
            for key in keys:
                setting = settings.BY_KEY[key]
                if key == "hook_repatch":
                    warning = QLabel(translations["settings.hook_repatch_warning"])
                    warning.setObjectName("settings_hook_repatch_warning")
                    warning.setWordWrap(True)
                    warning.setStyleSheet("color: #d29922; font-weight: bold;")
                    form.addRow(warning)
                editor = self._editor(setting)
                self.editors[setting.key] = editor
                label = QLabel(translations[f"settings.{setting.key}"])
                tooltip = translations[f"settings.{setting.key}_tooltip"]
                label.setToolTip(tooltip)
                editor.setToolTip(tooltip)
                form.addRow(label, editor)
            layout.addWidget(group)
        self.restart_label = QLabel(translations["settings.restart"])
        self.restart_label.setStyleSheet("color: #d29922;")
        self.restart_label.setVisible(False)
        layout.addWidget(self.restart_label)
        defaults = QHBoxLayout()
        reset = QPushButton(translations["settings.defaults"])
        set_button_icon(reset, "restart_alt")
        reset.setToolTip(translations["settings.defaults_tooltip"])
        reset.clicked.connect(self.show_defaults)
        defaults.addWidget(reset)
        defaults.addStretch()
        layout.addLayout(defaults)
        layout.addLayout(dialog_button_row(translations, "button.save", self._save, self.reject))
        self.show_values(settings.values())

    def _editor(self, setting):
        if setting.kind == settings.BOOL:
            return QCheckBox()
        if setting.kind == settings.INT:
            editor = QSpinBox()
            editor.setRange(setting.minimum, setting.maximum)
            editor.setSingleStep(setting.step)
            editor.setGroupSeparatorShown(True)
            if setting.minimum == 0:
                editor.setSpecialValueText(self.translations[f"settings.{setting.key}_off"])
            return editor
        editor = QComboBox()
        for choice in setting.choices:
            editor.addItem(self.translations[f"settings.{setting.key}_{choice}"], choice)
        editor.currentIndexChanged.connect(lambda _index: self._update_restart())
        return editor

    def show_values(self, values):
        """Put ``values`` ({key: value}) into the editors."""
        for key, value in values.items():
            editor = self.editors[key]
            if isinstance(editor, QCheckBox):
                editor.setChecked(value)
            elif isinstance(editor, QSpinBox):
                editor.setValue(value)
            else:
                editor.setCurrentIndex(max(0, editor.findData(value)))
        self._update_restart()

    def show_defaults(self):
        """Put the defaults into the editors; saving stores them."""
        self.show_values({setting.key: setting.default for setting in settings.SETTINGS})

    def chosen(self):
        """{key: value} as the editors show them."""
        values = {}
        for key, editor in self.editors.items():
            if isinstance(editor, QCheckBox):
                values[key] = editor.isChecked()
            elif isinstance(editor, QSpinBox):
                values[key] = editor.value()
            else:
                values[key] = editor.currentData()
        return values

    def _update_restart(self):
        chosen = self.chosen()
        self.restart_label.setVisible(any(
            setting.restart and chosen[setting.key] != settings.get(setting.key)
            for setting in settings.SETTINGS))

    def _save(self):
        values = self.chosen()
        self.repository.save(values)
        self.changed = settings.apply(values)
        self.accept()
