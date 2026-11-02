"""Label widgets shared by dialogs."""

import math

from PySide6.QtCore import QSize, Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QTextOption  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QFrame,
    QLabel,
    QSizePolicy,
    QTextEdit,
)


class SelectableText(QTextEdit):
    """Selectable text that looks like a label and wraps anywhere.

    For long names without spaces, such as method signatures. A QLabel
    wraps only at spaces, and without wrapping it asks for the width of its
    whole text: its dialog grew beyond the screen. This one breaks lines
    anywhere when a word does not fit, takes the height its lines need, and
    copies the text as it is.
    """

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("background: transparent;")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(0)
        self.setText(text)

    def text(self):
        """The text shown."""
        return self.toPlainText()

    def setText(self, text):  # pylint: disable=invalid-name
        """Show ``text`` and take the height its lines need."""
        self.setPlainText(text)
        self._fit_height()

    def sizeHint(self):  # pylint: disable=invalid-name
        """Any width; the height of the text."""
        return QSize(0, self.height())

    def minimumSizeHint(self):  # pylint: disable=invalid-name
        """Any width; the height of the text."""
        return self.sizeHint()

    def resizeEvent(self, event):  # pylint: disable=invalid-name
        """Lay out the lines for the new width."""
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self):
        # An empty text keeps the height of one line, so nothing jumps.
        document = self.document()
        document.setTextWidth(self.viewport().width())
        height = math.ceil(document.size().height()) + self.frameWidth() * 2
        if height != self.height():
            self.setFixedHeight(height)


class WrappedLabel(QLabel):
    """Word-wrapped label that reserves the height its text needs.

    QLabel reports the minimum height of wrapped text for an arbitrary width,
    so layouts (e.g. inside a QSplitter) can give it too little room and the
    text overlaps the widgets around it. This label sets its minimum height
    from its actual width instead.
    """

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)

    def setText(self, text):  # pylint: disable=invalid-name
        """Set the text and recompute the needed height."""
        super().setText(text)
        self._fit_height()

    def resizeEvent(self, event):  # pylint: disable=invalid-name
        """Recompute the needed height for the new width."""
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self):
        current = self.minimumHeight()
        # heightForWidth() never goes below the minimum height; lift it first
        # so the label can shrink again when it gets wider.
        self.setMinimumHeight(0)
        height = self.heightForWidth(self.width()) if self.text() else 0
        self.setMinimumHeight(max(height, 0) if height != current else current)
