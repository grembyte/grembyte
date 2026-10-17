"""Label widgets shared by dialogs."""

from PyQt6.QtWidgets import QLabel  # pylint: disable=import-error,no-name-in-module


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
