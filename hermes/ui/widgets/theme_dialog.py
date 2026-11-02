"""Dialog for selecting and editing UI color themes."""

from PySide6.QtGui import QColor  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QFormLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from hermes.ui.styles.styles import build_stylesheet
from hermes.ui.styles.themes import COLOR_KEYS
from hermes.ui.widgets.code_editor import apply_code_theme
from hermes.ui.widgets.dialog_buttons import dialog_button_row
from hermes.ui.widgets.icons import apply_icon_theme


class ThemeDialog(QDialog):
    """Preview and save a named theme with editable palette colors."""

    def __init__(self, translations, manager, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.manager = manager
        self.application = QApplication.instance()
        self.original_stylesheet = self.application.styleSheet()
        self.original_theme = manager.load_theme()
        self.colors = dict(self.original_theme["colors"])
        self.setWindowTitle(translations["theme.title"])
        self.resize(560, 680)

        layout = QVBoxLayout(self)
        self.theme_combo = QComboBox()
        self.theme_combo.setEditable(True)
        self.theme_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.theme_combo.addItems(manager.available_themes())
        self.theme_combo.setCurrentText(self.original_theme["name"])
        self.theme_combo.activated.connect(self._select_theme)

        form = QFormLayout()
        form.addRow(QLabel(translations["theme.name"]), self.theme_combo)
        self.color_buttons = {}
        for key in COLOR_KEYS:
            button = QPushButton()
            button.clicked.connect(lambda _checked=False, color_key=key:
                                   self._choose_color(color_key))
            self.color_buttons[key] = button
            form.addRow(QLabel(translations[f"theme.color.{key}"]), button)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        contents = QWidget()
        contents.setLayout(form)
        scroll.setWidget(contents)
        layout.addWidget(scroll)
        layout.addLayout(dialog_button_row(
            translations, "button.save", self._save_theme, self.reject))
        self._refresh_color_buttons()

    def _select_theme(self, _index):
        theme = self.manager.load_theme(self.theme_combo.currentText())
        self.theme_combo.setCurrentText(theme["name"])
        self.colors = dict(theme["colors"])
        self._refresh_color_buttons()
        self._preview()

    def _choose_color(self, key):
        color = QColorDialog.getColor(
            QColor(self.colors[key]), self, self.translations[f"theme.color.{key}"],
            options=QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if not color.isValid():
            return
        self.colors[key] = (color.name() if color.alpha() == 255 else
                            f"rgba({color.red()}, {color.green()}, {color.blue()}, "
                            f"{color.alphaF():.3f})")
        self._refresh_color_buttons()
        self._preview()

    def _refresh_color_buttons(self):
        for key, button in self.color_buttons.items():
            value = self.colors[key]
            button.setText(value)
            button.setToolTip(value)
            button.setStyleSheet(f"background-color: {value}; color: #ffffff;")

    def _preview(self):
        self.application.setStyleSheet(build_stylesheet(self.colors))
        apply_icon_theme(self.colors)
        apply_code_theme(self.colors)

    def _save_theme(self):
        name = self.theme_combo.currentText().strip()
        try:
            self.manager.save_theme(name, self.colors)
            self.manager.set_current(name)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, self.translations["theme.title"],
                                self.translations["theme.save_error"].format(error=error))
            return
        self.application.setStyleSheet(self.manager.stylesheet(name))
        apply_icon_theme(self.colors)
        apply_code_theme(self.colors)
        self.accept()

    def reject(self):
        self.application.setStyleSheet(self.original_stylesheet)
        apply_icon_theme(self.original_theme["colors"])
        apply_code_theme(self.original_theme["colors"])
        super().reject()