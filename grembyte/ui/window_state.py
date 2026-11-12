"""Window sizes, splitter positions and column widths, kept between runs.

An event filter of the application (``keep_window_state``) restores, when one
of Grembyte's windows is first shown, its size and the positions of its
splitters, and saves them when the window is hidden or closed. Windows that
are not modal get their position back, too; modal dialogs only their size
(Qt centres them over their parent). Column widths the user sets are kept by
the tables' column sizer (see ``resizable_columns`` in
grembyte.ui.widgets.table), under a key built from the window and the table's
place in it (``view_key``).

Everything lives in QSettings("grembyte", "grembyte") under
``windows/``, ``splitters/`` and ``columns/``.
"""

import json

from PySide6.QtCore import QByteArray, QEvent, QObject, QSettings, QSize  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import QDialog, QSplitter, QWidget  # pylint: disable=import-error,no-name-in-module

# Set on a window once its state was restored
_RESTORED = "grembyteStateRestored"


def settings():
    """The QSettings the state is kept in (tests point it elsewhere)."""
    return QSettings("grembyte", "grembyte")


def _is_grembyte(widget):
    return type(widget).__module__.startswith("grembyte.")


def _path(widget, top):
    """Where ``widget`` sits below ``top``: class names and indexes among
    siblings of the same class, e.g. ``QSplitter0/QTableWidget1``."""
    parts = []
    node = widget
    while node is not None and node is not top:
        parent = node.parent()
        siblings = [child for child in (parent.children() if parent is not None else [])
                    if type(child) is type(node)]
        index = next((i for i, child in enumerate(siblings) if child is node), 0)
        parts.append(f"{type(node).__name__}{index}")
        node = parent
    return "/".join(reversed(parts))


def _owner(widget):
    """The dialog ``widget`` lies in, docked in the main window or not; else its window."""
    node = widget.parentWidget()
    while node is not None:
        if isinstance(node, QDialog):
            return node
        node = node.parentWidget()
    return widget.window()


def view_key(view):
    """The key a table's column widths are kept under (the same docked or not)."""
    window = _owner(view)
    return f"{type(window).__name__}/{_path(view, window)}"


def load_columns(key, count):
    """{column: width} kept for a table with ``count`` columns, else {}."""
    try:
        data = json.loads(settings().value(f"columns/{key}", "") or "{}")
    except (TypeError, ValueError):
        return {}
    if data.get("count") != count:
        return {}
    return {int(column): int(width) for column, width in data.get("widths", {}).items()}


def save_columns(key, count, widths):
    """Keep the widths ({column: width}) the user gave a table's columns."""
    settings().setValue(f"columns/{key}", json.dumps(
        {"count": count, "widths": {str(column): width for column, width in widths.items()}}))


def restore_window(window):
    store = settings()
    key = type(window).__name__
    if isinstance(window, QDialog) and window.isModal():
        size = store.value(f"windows/{key}/size")
        if isinstance(size, QSize) and size.isValid():
            window.resize(size)
    else:
        geometry = store.value(f"windows/{key}/geometry")
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            window.restoreGeometry(geometry)
    for splitter in window.findChildren(QSplitter):
        state = store.value(f"splitters/{key}/{_path(splitter, window)}")
        if isinstance(state, QByteArray) and not state.isEmpty():
            splitter.restoreState(state)


def save_window(window):
    store = settings()
    key = type(window).__name__
    store.setValue(f"windows/{key}/geometry", window.saveGeometry())
    if not window.isMaximized() and not window.isFullScreen():
        store.setValue(f"windows/{key}/size", window.size())
    for splitter in window.findChildren(QSplitter):
        store.setValue(f"splitters/{key}/{_path(splitter, window)}", splitter.saveState())


class _WindowStateKeeper(QObject):
    """Restores Grembyte's windows when first shown, saves them when hidden."""

    def eventFilter(self, watched, event):  # pylint: disable=invalid-name
        kind = event.type()
        if kind not in (QEvent.Type.Show, QEvent.Type.Hide):
            return False
        if not isinstance(watched, QWidget) or not watched.isWindow() or not _is_grembyte(watched):
            return False
        if kind == QEvent.Type.Show:
            if not watched.property(_RESTORED):
                watched.setProperty(_RESTORED, True)
                restore_window(watched)
        elif watched.property(_RESTORED):
            save_window(watched)
        return False


def keep_window_state(app):
    """Have ``app`` keep the sizes of Grembyte's windows; returns the filter."""
    keeper = _WindowStateKeeper(app)
    app.installEventFilter(keeper)
    return keeper
