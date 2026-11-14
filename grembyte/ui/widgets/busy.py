"""Three bouncing dots in the brand colour: Grembyte waits for an answer.

For waits that report no progress (an agent's answer), where a progress bar
would have nothing to show. ``set_busy(True)`` shows and animates the dots,
``set_busy(False)`` hides them; the timer runs only while they show. The
dots keep their place in the layout when hidden, so the text beside them
does not jump.
"""

import math

from PySide6.QtCore import QRectF, QSize, Qt, QTimer  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QPainter  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import QSizePolicy, QWidget  # pylint: disable=import-error,no-name-in-module

from grembyte.ui.widgets.icons import theme_qcolor

DOTS = 3
FRAME_MS = 40
# One bounce of a dot, and how far each dot lags behind the one before it
PERIOD_MS = 1200
LAG = 0.15


class BusyDots(QWidget):
    """Bouncing dots, sized by the font so that they grow with the interface."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._step)
        policy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        policy.setRetainSizeWhenHidden(True)
        self.setSizePolicy(policy)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.hide()

    def sizeHint(self):  # pylint: disable=invalid-name
        dot = self._dot()
        return QSize(round(dot * (2 * DOTS + 1)), round(dot * 3))

    def _dot(self):
        """The diameter of a dot."""
        return max(4.0, self.fontMetrics().height() * 0.45)

    def is_busy(self):
        return self._timer.isActive()

    def set_busy(self, busy):
        """Show and animate the dots, or hide them."""
        if busy == self.is_busy():
            return
        if busy:
            self._phase = 0.0
            self._timer.start()
            self.show()
        else:
            self._timer.stop()
            self.hide()

    def _step(self):
        self._phase = (self._phase + FRAME_MS / PERIOD_MS) % 1.0
        self.update()

    def paintEvent(self, _event):  # pylint: disable=invalid-name
        dot = self._dot()
        rise = self.height() - dot  # how high a dot jumps
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme_qcolor("brand"))
        left = (self.width() - dot * (2 * DOTS - 1)) / 2
        for index in range(DOTS):
            phase = (self._phase - index * LAG) % 1.0
            # Up and down in the first half of its period, resting in the second
            height = math.sin(phase * 2 * math.pi) if phase < 0.5 else 0.0
            top = rise - rise * height
            painter.drawEllipse(QRectF(left + index * 2 * dot, top, dot, dot))
        painter.end()
