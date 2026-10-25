"""Window of the call graph: what a C# method calls and who calls it."""

from PyQt6.QtCore import Qt, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hermes.engines.registry import resolve_engine
from hermes.features.call_graph import CallGraphWorker, async_owner
from hermes.ui.widgets.labels import SelectableText
from hermes.ui.widgets.table import copy_menu, resizable_columns, set_row_height

_KIND, _METHOD, _ASSEMBLY = range(3)
# Item data: the CatalogMethod of a row
_METHOD_ROLE = Qt.ItemDataRole.UserRole

# Workers still running when their window closed; kept until they finish.
_running_workers = set()


def method_title(method):
    """How the graph names a method: a compiler-made ``MoveNext`` by its origin."""
    owner = async_owner(method)
    if owner is not None:
        return f"{owner[0]}:{owner[1]} (async)"
    return method.signature


class CallGraphDialog(QDialog):
    """Show the calls of a method (``method``, a CatalogMethod) and its callers.

    A double click goes to a method, Back and Forward return. Callers are
    searched in the game's assemblies, or those of the assembly selection
    (``selection``); the first search reads their code, later ones come from
    the catalog cache.
    """

    # pylint: disable=too-many-instance-attributes

    # CatalogMethod to log the calls of
    monitor_requested = pyqtSignal(object)

    def __init__(self, translations, pid, method, parent=None, engine=None, selection=(),
                 game=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(parent)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.translations = translations
        # Offline: a KnownGame (see hermes.features.games), answers from the cache
        self.game = game
        self.engine = resolve_engine(pid, engine) if game is None else engine
        self.method = None
        # The method of the row chosen last in either table
        self._selected = None
        self._back = []
        self._forward = []
        self._callee_request = None
        self._caller_request = None
        # Once callers were searched, they are shown for every method gone to.
        self._show_callers = False
        self.setWindowTitle(translations["calls.title"])
        self.resize(1000, 700)
        self._build_ui()
        self._worker = CallGraphWorker(pid, engine=self.engine, selection=selection, game=game)
        self._worker.callees_found.connect(self._on_callees)
        self._worker.callers_found.connect(self._on_callers)
        self._worker.request_failed.connect(self._on_failed)
        self._worker.progress.connect(self._on_progress)
        self._worker.worker_failed.connect(self._on_worker_failed)
        worker = self._worker
        self._worker.finished.connect(lambda: _running_workers.discard(worker))
        _running_workers.add(self._worker)
        self._worker.start()
        if game is not None:
            self.monitor_button.setEnabled(False)
            self.monitor_button.setToolTip(self.translations["catalog.offline_no_monitor"])
        self.go_to(method)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.back_button = QPushButton(self.translations["calls.back"])
        self.back_button.clicked.connect(self.go_back)
        self.forward_button = QPushButton(self.translations["calls.forward"])
        self.forward_button.clicked.connect(self.go_forward)
        bar.addWidget(self.back_button)
        bar.addWidget(self.forward_button)
        self.title_label = SelectableText()
        bar.addWidget(self.title_label, 1)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Orientation.Vertical)
        callees = QWidget()
        callees_layout = QVBoxLayout(callees)
        callees_layout.setContentsMargins(0, 0, 0, 0)
        callees_label = QLabel(self.translations["calls.callees"])
        callees_label.setToolTip(self.translations["calls.callees_tooltip"])
        callees_layout.addWidget(callees_label)
        self.callees_table = self._table()
        callees_layout.addWidget(self.callees_table, 1)
        splitter.addWidget(callees)

        callers = QWidget()
        callers_layout = QVBoxLayout(callers)
        callers_layout.setContentsMargins(0, 0, 0, 0)
        callers_bar = QHBoxLayout()
        callers_label = QLabel(self.translations["calls.callers"])
        callers_label.setToolTip(self.translations["calls.callers_tooltip"])
        callers_bar.addWidget(callers_label)
        callers_bar.addStretch()
        self.callers_button = QPushButton(self.translations["calls.search_callers"])
        self.callers_button.setToolTip(self.translations["calls.search_callers_tooltip"])
        self.callers_button.clicked.connect(self.search_callers)
        callers_bar.addWidget(self.callers_button)
        callers_layout.addLayout(callers_bar)
        self.callers_progress = QProgressBar()
        self.callers_progress.hide()
        callers_layout.addWidget(self.callers_progress)
        self.callers_table = self._table()
        callers_layout.addWidget(self.callers_table, 1)
        splitter.addWidget(callers)
        splitter.setSizes([400, 260])
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        actions.addWidget(self.status_label, 1)
        self.copy_button = QPushButton(self.translations["catalog.copy"])
        self.copy_button.setToolTip(self.translations["calls.copy_tooltip"])
        self.copy_button.clicked.connect(self._copy_signature)
        actions.addWidget(self.copy_button)
        self.monitor_button = QPushButton(self.translations["catalog.monitor"])
        self.monitor_button.setObjectName("btn_primary")
        self.monitor_button.setToolTip(self.translations["calls.monitor_tooltip"])
        self.monitor_button.clicked.connect(self._request_monitor)
        actions.addWidget(self.monitor_button)
        layout.addLayout(actions)

    def _table(self):
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels([self.translations["calls.column_kind"],
                                         self.translations["calls.column_method"],
                                         self.translations["calls.column_assembly"]])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        set_row_height(table)
        header = table.horizontalHeader()
        header.setSectionResizeMode(_KIND, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_METHOD, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_ASSEMBLY, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(_KIND, 130)
        header.resizeSection(_ASSEMBLY, 200)
        resizable_columns(table)
        copy_menu(table, self.translations)
        table.cellDoubleClicked.connect(lambda row, _column, table=table: self._open_row(table, row))
        table.currentCellChanged.connect(
            lambda row, _column, *_previous, table=table: self._choose(table, row))
        return table

    def _choose(self, table, row):
        item = table.item(row, _METHOD) if row >= 0 else None
        if item is not None:
            self._selected = item.data(_METHOD_ROLE)

    # --- Navigation --------------------------------------------------------------
    def go_to(self, method, remember=True):
        """Show the calls of ``method``; ``remember``: Back returns here."""
        if remember and self.method is not None:
            self._back.append(self.method)
            self._forward.clear()
        self.method = method
        self._selected = None
        self.title_label.setText(method_title(method))
        self.setWindowTitle(self.translations["calls.title_method"].format(
            method=method_title(method)))
        self._fill(self.callees_table, [])
        self._fill(self.callers_table, [])
        self.status_label.setText(self.translations["calls.reading"])
        self._callee_request = self._worker.callees(method)
        self._caller_request = None
        self.callers_button.setEnabled(True)
        if self._show_callers:
            self.search_callers()
        self._update_buttons()

    def go_back(self):
        if self._back:
            self._forward.append(self.method)
            self.go_to(self._back.pop(), remember=False)

    def go_forward(self):
        if self._forward:
            self._back.append(self.method)
            self.go_to(self._forward.pop(), remember=False)

    def _update_buttons(self):
        self.back_button.setEnabled(bool(self._back))
        self.forward_button.setEnabled(bool(self._forward))

    def _open_row(self, table, row):
        item = table.item(row, _METHOD)
        if item is not None:
            self.go_to(item.data(_METHOD_ROLE))

    def search_callers(self):
        """Search the callers of the method shown (and of those gone to later)."""
        self._show_callers = True
        self.callers_button.setEnabled(False)
        self.callers_progress.setRange(0, 0)
        self.callers_progress.show()
        self._caller_request = self._worker.callers(self.method)

    # --- Answers -----------------------------------------------------------------
    def _on_callees(self, request_id, method, calls, body):
        if request_id != self._callee_request:
            return
        rows = [(call.opcode, call.method) for call in calls]
        rows += [(f"{call.opcode} · async", call.method) for call in body]
        self._fill(self.callees_table, rows)
        key = "calls.callees_count_async" if body else "calls.callees_count"
        self.status_label.setText(self.translations[key].format(
            count=len(calls) + len(body), method=method.name))

    def _on_callers(self, request_id, _method, calls, missing):
        if request_id != self._caller_request:
            return
        self.callers_progress.hide()
        self._fill(self.callers_table, [(call.opcode, call.method) for call in calls])
        if missing:
            self.status_label.setText(self.translations["calls.not_searched"].format(
                count=len(missing), assemblies=", ".join(missing)))
        elif not calls:
            self.status_label.setText(self.translations["calls.no_callers"])

    def _on_progress(self, done, total, assembly):
        self.callers_progress.setRange(0, total)
        self.callers_progress.setValue(done)
        self.callers_progress.setFormat(self.translations["calls.indexing"].format(
            assembly=assembly, done=done, total=total).replace("%", "%%"))

    def _on_failed(self, request_id, message):
        if request_id == self._caller_request:
            self.callers_progress.hide()
            self.callers_button.setEnabled(True)
        if message == CallGraphWorker.NOT_CACHED:
            self.status_label.setText(self.translations["calls.not_cached"])
            return
        self.status_label.setText(self.translations["calls.failed"].format(message=message))

    def _on_worker_failed(self, message):
        self.callers_progress.hide()
        self.status_label.setText(self.translations["calls.failed"].format(message=message))

    def _fill(self, table, rows):
        table.setRowCount(len(rows))
        for row, (kind, method) in enumerate(rows):
            title = method_title(method)
            for column, text in ((_KIND, kind), (_METHOD, title), (_ASSEMBLY, method.assembly)):
                item = QTableWidgetItem(text)
                item.setData(_METHOD_ROLE, method)
                if column == _METHOD:
                    item.setToolTip(method.signature)
                table.setItem(row, column, item)

    # --- Actions -----------------------------------------------------------------
    def selected_method(self):
        """The method of the row chosen last, else the one shown."""
        return self._selected or self.method

    def _request_monitor(self):
        self.monitor_requested.emit(self.selected_method())

    def _copy_signature(self):
        QGuiApplication.clipboard().setText(self.selected_method().signature)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._worker.requestInterruption()
        super().closeEvent(event)
