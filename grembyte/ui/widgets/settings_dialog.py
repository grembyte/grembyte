"""The window of the general settings (grembyte.core.settings), under Settings in the menu."""

from PySide6.QtCore import QSize, Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from grembyte.core import settings
from grembyte.ui.widgets.dialog_buttons import dialog_button_row
from grembyte.ui.widgets.icons import material_icon, set_button_icon, theme_color
from grembyte.ui.widgets.labels import WrappedLabel
from grembyte.ui.widgets.spin_boxes import SpinBox


# (category, icon, keys): a page each, listed on the left in this order
_SETTING_CATEGORIES = (
    ("general", "settings", ("activity_log", "refresh_interval_ms", "language", "ui_scale",
                                    "hotkey_sounds")),
    ("scan", "search", ("scan_aligned",)),
    ("pointer", "link", ("pointer_max_results", "pointer_stacks", "pointer_location_step",
                         "pointer_value_alignment")),
    ("trace", "track_changes", ("wine_trace_limit", "large_trace", "trace_sequence")),
    ("hooks", "webhook", ("hook_short_jump", "hook_repatch", "hook_profiling")),
    ("objects", "account_tree", ("object_browser_limit",)),
    ("graphs", "hub", ("graph_max_nodes", "reference_other_memory")),
    ("wine", "wine_bar", ("wine_gadget_download",)),
)


class SettingsDialog(QDialog):
    """Show the settings, store the changed ones and apply them.

    ``repository`` stores them (grembyte.db.repository.settings); ``changed``
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
        self.setMinimumSize(680, 380)
        layout = QVBoxLayout(self)
        pages = QHBoxLayout()
        # Parented here: added to the still unattached ``pages`` layout, PySide kept them as
        # Python's own and deleted them a second time at exit, which crashed.
        self.categories = QListWidget(self)
        self.categories.setObjectName("settings_categories")
        self.categories.setIconSize(QSize(18, 18))
        self.pages = QStackedWidget(self)
        for category, icon, keys in _SETTING_CATEGORIES:
            item = QListWidgetItem(material_icon(icon, 18),
                                   translations[f"settings.category.{category}"])
            item.setData(Qt.ItemDataRole.UserRole, category)
            self.categories.addItem(item)
            self.pages.addWidget(self._page(category, keys))
        self.categories.setFixedWidth(self.categories.sizeHintForColumn(0) + 24)
        self.categories.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.categories.setCurrentRow(0)
        pages.addWidget(self.categories)
        pages.addWidget(self.pages, 1)
        layout.addLayout(pages, 1)
        self.restart_label = QLabel(translations["settings.restart"])
        self.restart_label.setStyleSheet(f"color: {theme_color('warning')};")
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

    def _page(self, category, keys):
        """The page of ``category``: a form of its settings, scrolled when it gets long."""
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        for key in keys:
            setting = settings.BY_KEY[key]
            if key == "hook_repatch":
                # Reserves the height of its lines: a QLabel was cut off here.
                warning = WrappedLabel(self.translations["settings.hook_repatch_warning"])
                warning.setObjectName("settings_hook_repatch_warning")
                warning.setStyleSheet(f"color: {theme_color('warning')}; font-weight: bold;")
                form.addRow(warning)
            editor = self._editor(setting)
            self.editors[setting.key] = editor
            label = QLabel(self.translations[f"settings.{setting.key}"])
            tooltip = self.translations[f"settings.{setting.key}_tooltip"]
            label.setToolTip(tooltip)
            editor.setToolTip(tooltip)
            form.addRow(label, editor)
        page = QScrollArea()
        page.setObjectName(f"settings_category_{category}")
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.Shape.NoFrame)
        page.setWidget(form_widget)
        return page

    def _editor(self, setting):
        if setting.kind == settings.BOOL:
            return QCheckBox()
        if setting.kind == settings.INT:
            editor = SpinBox()
            editor.setRange(setting.minimum, setting.maximum)
            editor.setSingleStep(setting.step)
            editor.setGroupSeparatorShown(True)
            if setting.minimum == 0:
                editor.setSpecialValueText(self.translations[f"settings.{setting.key}_off"])
            editor.valueChanged.connect(lambda _value: self._update_restart())
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
            elif isinstance(editor, SpinBox):
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
            elif isinstance(editor, SpinBox):
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
