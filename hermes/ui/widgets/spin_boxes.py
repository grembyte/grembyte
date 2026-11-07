"""Spin boxes whose numbers keep the distance of the line edits beside them."""

from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QDoubleSpinBox,
    QSpinBox,
)

# The stylesheet's padding does not reach the text of a spin box: it stuck
# to the left border. This margin puts it where a QLineEdit with its 10px
# padding shows its text.
TEXT_MARGIN = 13


def _pad(spin_box):
    spin_box.lineEdit().setTextMargins(TEXT_MARGIN, 0, 0, 0)


class SpinBox(QSpinBox):
    """QSpinBox with its text indented like a line edit's."""

    def __init__(self, parent=None):
        super().__init__(parent)
        _pad(self)


class DoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox with its text indented like a line edit's."""

    def __init__(self, parent=None):
        super().__init__(parent)
        _pad(self)
