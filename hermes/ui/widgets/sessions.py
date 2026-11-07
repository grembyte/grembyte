"""Windows for the running sessions: an overview, and waiting for them on exit."""

import time

from PySide6.QtCore import Qt, QTimer  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from hermes.core.sessions import SESSIONS, SessionSignals, frida_agents, frida_gadgets
from hermes.ui.widgets.icons import set_button_icon
from hermes.ui.widgets.table import copy_menu, resizable_columns, set_row_height
from hermes.ui.widgets.labels import DocsHint


# How long closing Hermes waits for the sessions before it cuts them, and
# then for the cut sessions before it exits anyway.
SHUTDOWN_TIMEOUT_S = 10
DISCONNECT_GRACE_S = 3

_KIND, _PID, _RUNNING, _STATUS = range(4)


def duration_text(seconds):
    """``42 s``, ``3:07 min``, ``1:02 h``."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d} min"
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d} h"


def status_text(translations, session, now):
    if session.detaching:
        return translations["sessions.status_detaching"]
    if session.stopping is None:
        return translations["sessions.status_active"]
    key = "sessions.status_stuck" if session.stuck(now) else "sessions.status_stopping"
    return translations[key].format(duration=duration_text(now - session.stopping))


class SessionsDialog(QDialog):
    """The workers attached to processes, with a way to end them.

    ``current_pid`` returns the process Hermes is attached to (or None), to
    count the Frida agents loaded there.
    """

    def __init__(self, translations, current_pid, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.current_pid = current_pid
        self._shown = []
        self.setWindowTitle(translations["sessions.title"])
        self.resize(760, 380)
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self._signals = SessionSignals(self)
        self._signals.changed.connect(self.refresh)
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        hint = DocsHint(self.translations, self.translations["sessions.hint"], "guide/tools/#sessions")
        layout.addWidget(hint)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([
            self.translations["sessions.column_kind"],
            self.translations["sessions.column_pid"],
            self.translations["sessions.column_running"],
            self.translations["sessions.column_status"],
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        header = self.table.horizontalHeader()
        for column in range(4):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch if column in (_KIND, _STATUS)
                else QHeaderView.ResizeMode.ResizeToContents)
        resizable_columns(self.table)
        copy_menu(self.table, self.translations)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        self.agents_label = QLabel("")
        self.agents_label.setWordWrap(True)
        layout.addWidget(self.agents_label)

        buttons = QHBoxLayout()
        self.stop_button = QPushButton(self.translations["sessions.stop"])
        set_button_icon(self.stop_button, "stop")
        self.stop_button.setToolTip(self.translations["sessions.stop_tooltip"])
        self.stop_button.clicked.connect(self._stop_selected)
        buttons.addWidget(self.stop_button)
        self.disconnect_button = QPushButton(self.translations["sessions.disconnect"])
        self.disconnect_button.setObjectName("btn_danger")
        set_button_icon(self.disconnect_button, "link_off")
        self.disconnect_button.setToolTip(self.translations["sessions.disconnect_tooltip"])
        self.disconnect_button.clicked.connect(self._disconnect_selected)
        buttons.addWidget(self.disconnect_button)
        buttons.addStretch()
        self.stop_all_button = QPushButton(self.translations["sessions.stop_all"])
        set_button_icon(self.stop_all_button, "stop_circle")
        self.stop_all_button.clicked.connect(SESSIONS.stop_all)
        buttons.addWidget(self.stop_all_button)
        layout.addLayout(buttons)

    def refresh(self):
        now = time.monotonic()
        selected = self.selected_session()
        self._shown = SESSIONS.sessions()
        self.table.setRowCount(len(self._shown))
        for row, session in enumerate(self._shown):
            values = (
                self.translations[f"sessions.kind_{session.kind}"],
                str(session.pid),
                duration_text(now - session.started),
                status_text(self.translations, session, now),
            )
            for column, text in enumerate(values):
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, column, item)
                item.setText(text)
                if column == _PID:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if session is selected:
                self.table.selectRow(row)
        self.stop_all_button.setEnabled(bool(self._shown))
        self._show_agents()
        self._update_buttons()

    def _show_agents(self):
        pid = self.current_pid()
        if pid is None:
            self.agents_label.setText("")
            return
        count = frida_agents(pid)
        # All sessions of this Hermes share one agent; a gadget (Wine games)
        # is shared by every Hermes run, so it is never stale.
        ours = 1 if any(s.pid == pid and s.frida_session is not None for s in self._shown) else 0
        ours = max(ours, frida_gadgets(pid))
        text = self.translations["sessions.agents"].format(pid=pid, count=count)
        if count > ours:
            text += " " + self.translations["sessions.agents_stale"].format(stale=count - ours)
        self.agents_label.setText(text)

    def selected_session(self):
        row = self.table.currentRow()
        selected = self.table.selectionModel().hasSelection()
        return self._shown[row] if selected and 0 <= row < len(self._shown) else None

    def _update_buttons(self):
        session = self.selected_session()
        self.stop_button.setEnabled(session is not None and session.stopping is None)
        self.disconnect_button.setEnabled(
            session is not None and session.frida_session is not None and not session.detaching)

    def _stop_selected(self):
        session = self.selected_session()
        if session is not None:
            SESSIONS.stop(session)

    def _disconnect_selected(self):
        session = self.selected_session()
        if session is not None:
            SESSIONS.disconnect(session)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._timer.stop()
        super().closeEvent(event)


class ShutdownDialog(QDialog):
    """Shown while Hermes exits and sessions still run.

    Asks them to stop, cuts their Frida sessions after SHUTDOWN_TIMEOUT_S (or
    when asked to), and gives up DISCONNECT_GRACE_S later. ``exec`` returns
    Accepted once all have ended, Rejected if Hermes must exit regardless.
    """

    def __init__(self, translations, parent=None, timeout_s=SHUTDOWN_TIMEOUT_S,
                 grace_s=DISCONNECT_GRACE_S):
        super().__init__(parent)
        self.setModal(True)
        self.translations = translations
        self.timeout_s = timeout_s
        self.grace_s = grace_s
        self._began = time.monotonic()
        self._cut = None
        self.setWindowTitle(translations["shutdown.title"])
        self.resize(480, 220)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(translations["shutdown.waiting"]))
        self.sessions_label = QLabel("")
        self.sessions_label.setWordWrap(True)
        layout.addWidget(self.sessions_label, 1)
        self.countdown_label = QLabel("")
        self.countdown_label.setWordWrap(True)
        layout.addWidget(self.countdown_label)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.now_button = QPushButton(translations["shutdown.now"])
        self.now_button.setObjectName("btn_danger")
        set_button_icon(self.now_button, "power_settings_new")
        self.now_button.setToolTip(translations["shutdown.now_tooltip"])
        self.now_button.clicked.connect(self._cut_now)
        buttons.addWidget(self.now_button)
        layout.addLayout(buttons)
        SESSIONS.stop_all()
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._check)
        self._timer.start()
        self._check()

    def _check(self):
        running = SESSIONS.sessions()
        if not running:
            self._timer.stop()
            self.accept()
            return
        now = time.monotonic()
        if self._cut is None and now - self._began >= self.timeout_s:
            self._cut_all(now)
        if self._cut is not None and now - self._cut >= self.grace_s:
            self._timer.stop()
            self.reject()
            return
        self.sessions_label.setText("\n".join(
            f"• {self.translations[f'sessions.kind_{s.kind}']} (PID {s.pid}): "
            f"{status_text(self.translations, s, now)}" for s in running))
        if self._cut is None:
            self.countdown_label.setText(self.translations["shutdown.countdown"].format(
                seconds=max(0, int(self.timeout_s - (now - self._began) + 0.999))))

    def _cut_all(self, now):
        self._cut = now
        SESSIONS.disconnect_all()
        self.now_button.setEnabled(False)
        self.countdown_label.setText(self.translations["shutdown.disconnecting"])

    def _cut_now(self):
        # One more short wait: a cut session usually ends right away.
        self.grace_s = min(self.grace_s, 1)
        self._cut_all(time.monotonic())

    def reject(self):
        self._timer.stop()
        super().reject()
