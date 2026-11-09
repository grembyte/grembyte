"""Record what is clicked in Hermes into the activity log (hermes.core.activity).

``UiRecorder`` needs no change to the windows: every ``SCAN_INTERVAL_MS`` it
connects to the buttons, menu actions, combo boxes, tab bars and item views
that appeared since, and notes the windows and dialogs that opened or
closed. Only what was clicked is recorded, with its window; nothing typed
into fields.

Not an event filter on the application: Python event filters can receive
wrappers for Qt objects that are being deleted; accessing those wrappers
crashed Hermes in the Qt binding's object wrapper.
"""

from PySide6.QtCore import QObject, Qt, QTimer, Slot  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QComboBox,
    QTabBar,
    QToolButton,
)

from hermes.core import activity, logs


SCAN_INTERVAL_MS = 500
_MARK = "_hermes_recorded"
_PRESSED = "_hermes_pressed_text"
_WINDOWS = (Qt.WindowType.Window, Qt.WindowType.Dialog)


def _text(text):
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&").strip()


def _window(widget):
    window = widget.window()
    return {"window": window.windowTitle() or type(window).__name__}


def _name(widget):
    # The object name tells widgets of the same text apart; Qt's own are "qt_…"
    name = widget.objectName()
    return {"name": name} if name and not name.startswith("qt_") else {}


class UiRecorder(QObject):
    """See the module documentation; ``start`` it, ``stop`` it before the windows go."""

    def __init__(self, parent=None, interval_ms=SCAN_INTERVAL_MS):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.scan)
        # id of a shown window -> (title, class name)
        self._shown = {}

    def start(self):
        self.scan()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def scan(self):
        """Connect to what appeared, note the windows that opened or closed."""
        try:
            for widget in QApplication.allWidgets():
                self._connect(widget)
                for action in widget.actions():
                    self._connect_action(action)
            self._windows()
        except Exception as error:  # pylint: disable=broad-exception-caught
            # Recording must never get in the way of the window.
            logs.cleanup_failed(error)

    def _windows(self):
        shown = {}
        for widget in QApplication.topLevelWidgets():
            if widget.isVisible() and widget.windowType() in _WINDOWS:
                shown[id(widget)] = (widget.windowTitle(), type(widget).__name__)
        for key, (title, kind) in shown.items():
            if key not in self._shown:
                activity.record("window_shown", window=title, widget=kind)
        for key, (title, kind) in self._shown.items():
            if key not in shown:
                activity.record("window_hidden", window=title, widget=kind)
        self._shown = shown

    @staticmethod
    def _marked(item):
        if item.property(_MARK):
            return True
        item.setProperty(_MARK, True)
        return False

    def _connect(self, widget):
        if isinstance(widget, QAbstractButton):
            # A tool button showing an action: the action records the click.
            # Not defaultAction(): in PySide 6.12 that call invalidates the
            # wrapper of an action Qt created (a line edit's clear button), and
            # the next click on it raised "already deleted". setDefaultAction
            # adds the action to actions(), which is safe.
            if isinstance(widget, QToolButton) and widget.actions():
                return
            if not self._marked(widget):
                # The text before the click: the window's own slot may change it first.
                widget.pressed.connect(
                    lambda button=widget: button.setProperty(_PRESSED, _text(button.text())))
                widget.clicked.connect(
                    lambda checked, button=widget: self._clicked(button, checked))
        elif isinstance(widget, QComboBox):
            if not self._marked(widget):
                widget.activated.connect(lambda index, combo=widget: self._chosen(combo, index))
        elif isinstance(widget, QTabBar):
            if not self._marked(widget):
                widget.tabBarClicked.connect(lambda index, tabs=widget: self._tab(tabs, index))
        elif isinstance(widget, QAbstractItemView):
            if not self._marked(widget):
                widget.doubleClicked.connect(lambda index, view=widget: self._double(view, index))

    def _connect_action(self, action):
        if action is None or action.isSeparator() or action.menu() is not None:
            return
        if not self._marked(action):
            # The sender, not a wrapper kept from the scan: that one may be
            # invalid by the time the action fires.
            action.triggered.connect(self._action_triggered)

    @Slot(bool)
    def _action_triggered(self, checked):
        action = self.sender()
        if action is not None:
            self._triggered(action, checked)

    @staticmethod
    def _clicked(button, checked):
        text = button.property(_PRESSED)
        button.setProperty(_PRESSED, None)
        fields = {**_window(button), "widget": type(button).__name__, **_name(button),
                  "text": text or _text(button.text()) or button.toolTip()[:80]}
        if button.isCheckable():
            fields["checked"] = checked
        activity.record("click", **fields)

    @staticmethod
    def _triggered(action, checked):
        fields = {"text": _text(action.text()) or action.toolTip()[:80], **_name(action)}
        parent = action.parent()
        if parent is not None and parent.isWidgetType():
            fields.update(_window(parent))
        if action.isCheckable():
            fields["checked"] = checked
        activity.record("action", **fields)

    @staticmethod
    def _chosen(combo, index):
        activity.record("choice", **_window(combo), **_name(combo),
                        text=combo.itemText(index), index=index)

    @staticmethod
    def _tab(tabs, index):
        if index >= 0:
            activity.record("tab", **_window(tabs), text=_text(tabs.tabText(index)))

    @staticmethod
    def _double(view, index):
        activity.record("double_click", **_window(view), widget=type(view).__name__,
                        **_name(view), row=index.row(), column=index.column())
