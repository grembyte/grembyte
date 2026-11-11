"""Dialogs docked in the main window, as tabs.

Windows of their own cannot be kept next to the main window (Wayland does
not let programs place them), so tools that stay in use while the game runs
can be docked instead: a dialog with a ``PinButton`` (``dialog.pin_button``)
goes into one of the main window's two docking areas, ``RIGHT`` for narrow
tools and ``BOTTOM`` for wide tables. Each area is a ``DockHost`` holding
its dialogs as tabs; the pin button floats a dialog again (there is no
dragging out, which Wayland does not support well).

``DockManager`` (the main window's ``dock_manager``) knows the kinds of
dialogs: their area and whether the last one was docked, which the next one
of that kind follows (kept in the settings under ``docking/<kind>``). Code
far from the main window finds it with ``find_dock_manager``.
"""

import shiboken6  # pylint: disable=import-error,no-name-in-module
from PySide6.QtCore import QEvent, QObject, Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QDialog,
    QDockWidget,
    QPushButton,
    QTabWidget,
    QWidget,
)

from hermes.ui import window_state
from hermes.ui.widgets.icons import set_button_icon

RIGHT = "right"
BOTTOM = "bottom"
_AREAS = {
    RIGHT: (Qt.DockWidgetArea.RightDockWidgetArea,
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea),
    BOTTOM: (Qt.DockWidgetArea.BottomDockWidgetArea,
             Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea),
}
# A dialog's short title for its tab (a Qt property), if its window title is long
TAB_TITLE = "tabTitle"
# A docked dialog's window before it was docked, to give it back when floated
_OWNER = "_hermes_dock_owner"


# The slot closing a docked dialog with the dialog it came from
_OWNER_SLOT = "_hermes_dock_owner_slot"


def _alive(widget):
    return widget is not None and getattr(shiboken6, "isValid")(widget)


def close_dialog(dialog):
    """Close ``dialog`` as its window's close button would, docked or not.

    ``close()`` runs the dialog's closeEvent, where the tools stop their
    workers and timers; but QDialog emits ``finished`` only for a visible
    dialog, and a docked one in a tab behind is hidden. ``reject()`` alone
    emits ``finished`` without the closeEvent. So: close, then reject if
    ``finished`` did not come (and the dialog did not refuse to close).
    """
    if not _alive(dialog):
        return
    finished = []

    def note(_result):
        finished.append(True)

    dialog.finished.connect(note)
    closed = dialog.close()
    if not _alive(dialog):
        return
    try:
        dialog.finished.disconnect(note)
    except (RuntimeError, TypeError):
        pass
    if closed and not finished:
        dialog.reject()


class PinButton(QPushButton):
    """The button that docks its dialog in the main window or floats it again.

    ``toggled(bool)`` says what the user asked for; ``set_pinned`` shows
    what happened without asking again.
    """

    def __init__(self, translations, tooltip_key="dock.pin_tooltip", parent=None):
        super().__init__(translations["dock.pin"], parent)
        self._translations = translations
        set_button_icon(self, "push_pin")
        self.setCheckable(True)
        self.setToolTip(translations[tooltip_key])
        self.toggled.connect(self._show)

    def _show(self, pinned):
        self.setText(self._translations["dock.unpin" if pinned else "dock.pin"])

    def set_pinned(self, pinned):
        """Show whether the dialog is docked, without asking to change it."""
        self.blockSignals(True)
        self.setChecked(pinned)
        self.blockSignals(False)
        self._show(pinned)


class DockHost(QDockWidget):
    """One docking area of the main window: its dialogs as tabs.

    A tab's close button (or a middle click on it) closes its dialog, and a
    dialog that finishes leaves its tab. Empty, the dock hides. The dock has
    no title bar: the tabs name their dialogs.
    """

    def __init__(self, window, area=RIGHT):
        super().__init__(window)
        self.setObjectName(f"dock_{area}")
        place, allowed = _AREAS[area]
        self.setAllowedAreas(allowed)
        # The tabs name the dialogs and close them: no title bar above them
        self.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self.setTitleBarWidget(QWidget())
        self._window = window
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideMiddle)
        # Long names are shortened in the middle rather than widening the tab bar
        self.tabs.setStyleSheet("QTabBar::tab { max-width: 260px; }")
        self.tabs.tabCloseRequested.connect(lambda index: self._close_tab(index))
        self.tabs.currentChanged.connect(lambda _index: self._show_title())
        self.tabs.tabBar().installEventFilter(self)
        self.setWidget(self.tabs)
        window.addDockWidget(place, self)
        self.hide()

    def dialogs(self):
        """The docked dialogs, in the order of their tabs."""
        return [self.tabs.widget(index) for index in range(self.tabs.count())]

    def dialog(self):
        """The dialog of the tab in front, or None."""
        return self.tabs.currentWidget()

    def holds(self, dialog):
        return self.tabs.indexOf(dialog) >= 0

    def pin(self, dialog):
        """Dock ``dialog`` as a tab in front (or bring its tab to the front)."""
        if not self.holds(dialog):
            owner = dialog.parentWidget()
            setattr(dialog, _OWNER, owner)
            dialog.setParent(self.tabs, Qt.WindowType.Widget)
            self.tabs.addTab(dialog, "")
            self._show_tab_title(dialog)
            dialog.installEventFilter(self)
            if isinstance(owner, QDialog):
                # Docked, it would outlive the dialog it came from (whose
                # worker it asks): it goes with it, as it would have floating
                slot = self._owner_closed(dialog)
                setattr(dialog, _OWNER_SLOT, slot)
                owner.finished.connect(slot)
        pinned = getattr(dialog, "pin_button", None)
        if pinned is not None:
            pinned.set_pinned(True)
        dialog.show()
        self.tabs.setCurrentWidget(dialog)
        self._show_title()
        self.show()
        self.raise_()

    def unpin(self, dialog):
        """Give ``dialog`` a window of its own again."""
        if not self._take(dialog):
            return
        owner = getattr(dialog, _OWNER, None)
        dialog.setParent(owner if _alive(owner) else self._window, Qt.WindowType.Dialog)
        pinned = getattr(dialog, "pin_button", None)
        if pinned is not None:
            pinned.set_pinned(False)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def release(self, dialog):
        """``dialog`` finished: remove its tab."""
        self._take(dialog)

    def _take(self, dialog):
        index = self.tabs.indexOf(dialog)
        if index < 0:
            return False
        dialog.removeEventFilter(self)
        owner = getattr(dialog, _OWNER, None)
        slot = getattr(dialog, _OWNER_SLOT, None)
        if slot is not None and _alive(owner):
            try:
                owner.finished.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        setattr(dialog, _OWNER_SLOT, None)
        self.tabs.removeTab(index)
        if self.tabs.count() == 0:
            self.hide()
        else:
            self._show_title()
        return True

    @staticmethod
    def _owner_closed(dialog):
        return lambda _result: close_dialog(dialog)

    def _close_tab(self, index):
        dialog = self.tabs.widget(index)
        if dialog is not None:
            close_dialog(dialog)

    def _show_title(self):
        dialog = self.dialog()
        self.setWindowTitle(dialog.windowTitle() if dialog is not None else "")

    def eventFilter(self, watched, event):  # pylint: disable=invalid-name
        if watched is self.tabs.tabBar():
            if (event.type() == QEvent.Type.MouseButtonRelease
                    and event.button() == Qt.MouseButton.MiddleButton):
                index = watched.tabAt(event.position().toPoint())
                if index >= 0:
                    self._close_tab(index)
                    return True
            return False
        if event.type() == QEvent.Type.WindowTitleChange or (
                event.type() == QEvent.Type.DynamicPropertyChange
                and bytes(event.propertyName()).decode() == TAB_TITLE):
            self._show_tab_title(watched)
            if watched is self.dialog():
                self._show_title()
        return False

    def _show_tab_title(self, dialog):
        """A tab shows the dialog's TAB_TITLE property if it has one (a short
        name), else its window title; the window title is the tooltip."""
        index = self.tabs.indexOf(dialog)
        if index < 0:
            return
        self.tabs.setTabText(index, dialog.property(TAB_TITLE) or dialog.windowTitle())
        self.tabs.setTabToolTip(index, dialog.windowTitle())

    def closeEvent(self, event):  # pylint: disable=invalid-name
        """Closing the dock closes its dialogs."""
        for dialog in self.dialogs():
            close_dialog(dialog)
        super().closeEvent(event)


class DockManager(QObject):
    """The main window's docking areas and the kinds of dialogs docked there."""

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._hosts = {}

    def host(self, area):
        """The DockHost of ``area`` (RIGHT or BOTTOM), created on first use."""
        if area not in self._hosts:
            self._hosts[area] = DockHost(self._window, area)
        return self._hosts[area]

    def host_of(self, dialog):
        """The DockHost holding ``dialog``, or None."""
        return next((host for host in self._hosts.values() if host.holds(dialog)), None)

    @staticmethod
    def remembered(kind):
        """Whether the last dialog of ``kind`` was docked."""
        return window_state.settings().value(f"docking/{kind}", False, type=bool)

    def add(self, dialog, kind, area=RIGHT, dock=None):
        """Make ``dialog`` dockable and show it: docked if the last one of
        ``kind`` was (or as ``dock`` says), else as a window of its own.

        The dialog's ``pin_button`` (a PinButton) docks and floats it.
        """
        button = getattr(dialog, "pin_button", None)
        if button is not None and not dialog.property("hermesDockable"):
            dialog.setProperty("hermesDockable", True)
            button.toggled.connect(lambda pinned: self.set_docked(dialog, kind, area, pinned))
            dialog.finished.connect(lambda _result: self._released(dialog))
        if self.remembered(kind) if dock is None else dock:
            self.host(area).pin(dialog)
        else:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

    def show(self, dialog):
        """Bring ``dialog`` to the front, docked or not."""
        host = self.host_of(dialog)
        if host is not None:
            host.pin(dialog)
        else:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

    def set_docked(self, dialog, kind, area, docked):
        """Dock or float ``dialog``; the next dialog of ``kind`` follows."""
        window_state.settings().setValue(f"docking/{kind}", bool(docked))
        if docked:
            self.host(area).pin(dialog)
        else:
            host = self.host_of(dialog)
            if host is not None:
                host.unpin(dialog)

    def _released(self, dialog):
        for host in self._hosts.values():
            host.release(dialog)


def find_dock_manager(widget):
    """The DockManager of the main window ``widget`` belongs to, or None."""
    node = widget
    while node is not None:
        manager = getattr(node, "dock_manager", None)
        if isinstance(manager, DockManager):
            return manager
        node = node.parentWidget() if isinstance(node, QWidget) else node.parent()
    return None
