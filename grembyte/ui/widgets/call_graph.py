"""Window of the call graph: what a C# method calls and who calls it, as a graph."""

from datetime import datetime

from PySide6.QtCore import Qt, Signal  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QCheckBox,
    QDialog,
    QFileDialog,
    QGraphicsScene,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
)

from grembyte import __version__
from grembyte.core import settings
from grembyte.core.programs import program_of
from grembyte.engines.registry import resolve_engine
from grembyte.features.call_graph import CallGraphWorker, async_owner
from grembyte.features.call_graph_model import (
    CALLEES,
    CALLERS,
    CallGraph,
    call_label,
)
from grembyte.ui.export_folder import remember, suggested_path
from grembyte.ui.localization import language_code
from grembyte.ui.widgets import graph_export
from grembyte.ui.widgets.busy import BusyDots
from grembyte.ui.widgets.graph_canvas import (
    GraphView,
    NodeBox,
    column_positions,
    draw_edge,
    short_class,
)
from grembyte.ui.widgets.icons import set_button_icon, theme_color, theme_qcolor
from grembyte.ui.widgets.labels import SelectableText
from grembyte.ui.workers import keep_until_finished

# The theme colour of the way highlighted from a method to the centre
_MARKED = "highlight"
# The context menu's and tooltip's texts per way: unfold, fold in, double click
_WAY_KEYS = {
    CALLERS: ("calls.expand_callers", "calls.collapse_callers", "calls.callers_hint",
              "calls.collapse_callers_hint", "calls.callers_shown"),
    CALLEES: ("calls.expand_callees", "calls.collapse_callees", "calls.callees_hint",
              "calls.collapse_callees_hint", "calls.callees_shown"),
}


def method_title(method):
    """How the graph names a method: a compiler-made ``MoveNext`` by its origin."""
    owner = async_owner(method)
    if owner is not None:
        return f"{owner[0]}:{owner[1]} (async)"
    return method.signature


def node_ways(graph, key, columns):
    """The ways node ``key`` can grow: its side of the centre, both for the centre."""
    side = graph.side(key, columns)
    return (CALLERS, CALLEES) if side is None else (side,)


class MethodItem(NodeBox):
    """A method of the graph: its class, and its name with the parameters."""

    def __init__(self, graph, key, translations, columns):
        node = graph.nodes[key]
        t = translations
        ways = node_ways(graph, key, columns)
        lines = [node.method.signature, node.method.assembly]
        if node.framework:
            lines.append(t["calls.framework_method"])
        more = 0
        for way in ways:
            expand, _collapse, hint, collapse_hint, shown = _WAY_KEYS[way]
            if node.unfolded[way]:
                hidden = graph.hidden_count(key, way)
                more += hidden
                lines.append(t[shown].format(shown=node.total[way] - hidden,
                                             total=node.total[way]))
                lines.append(t[collapse_hint])
            else:
                lines.append(t[hint])
        super().__init__(node, short_class(node.class_name),
                         f"{node.name} {node.method.parameters}", "\n".join(lines),
                         expand_left=CALLERS in ways and not node.unfolded[CALLERS],
                         expand_right=CALLEES in ways and not node.unfolded[CALLEES],
                         more=more or None)
        self._center = key == graph.center
        self.set_highlight(None)

    def border(self, mark):
        style = Qt.PenStyle.DashLine if self.node.framework else Qt.PenStyle.SolidLine
        if mark is not None:
            return theme_qcolor(mark), 2.5, style
        if self._center:
            return theme_qcolor("accent"), 2.5, style
        return theme_qcolor("border"), 1.2, style


class CallGraphDialog(QDialog):
    """Show the calls of a method (``method``, a CatalogMethod) and its callers
    as a graph: callers to the left, called methods to the right.

    A double click unfolds a method the way it stands from the centre, a
    second one folds it in again; the context menu also starts the graph
    from another method (Back and Forward return). Callers are searched in
    the game's assemblies, or those of the assembly selection
    (``selection``); the first search reads their code, later ones come from
    the catalog cache, so that from then on the centre's callers are shown
    at once.
    """

    # pylint: disable=too-many-instance-attributes

    # CatalogMethod to log the calls of
    monitor_requested = Signal(object)

    def __init__(self, translations, pid, method, parent=None, engine=None, selection=(),
                 game=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(parent)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.translations = translations
        self.pid = pid
        # Offline: a KnownGame (see grembyte.features.games), answers from the cache
        self.game = game
        self.engine = resolve_engine(pid, engine) if game is None else engine
        self.method = None
        self.graph = None
        self._back = []
        self._forward = []
        # request id -> (node key, way) of the unfoldings asked for
        self._requests = {}
        # Once callers were searched, the centre's are shown for every method gone to
        self._show_callers = False
        self._items = {}
        self._marked = None
        self.setWindowTitle(translations["calls.title"])
        self.resize(1100, 700)
        self._build_ui()
        self._worker = CallGraphWorker(pid, engine=self.engine, selection=selection, game=game)
        self._worker.callees_found.connect(self._on_callees)
        self._worker.callers_found.connect(self._on_callers)
        self._worker.request_failed.connect(self._on_failed)
        self._worker.progress.connect(self._on_progress)
        self._worker.worker_failed.connect(self._on_worker_failed)
        keep_until_finished(self._worker)
        self._worker.start()
        if game is not None:
            self.monitor_button.setEnabled(False)
            self.monitor_button.setToolTip(self.translations["catalog.offline_no_monitor"])
        self.go_to(method)

    def _build_ui(self):
        # The widgets get the window as parent at once (see object_browser)
        t = self.translations
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        layout.addLayout(bar)
        self.back_button = QPushButton(t["calls.back"], self)
        set_button_icon(self.back_button, "arrow_back")
        self.back_button.clicked.connect(self.go_back)
        self.forward_button = QPushButton(t["calls.forward"], self)
        set_button_icon(self.forward_button, "arrow_forward")
        self.forward_button.clicked.connect(self.go_forward)
        bar.addWidget(self.back_button)
        bar.addWidget(self.forward_button)
        self.title_label = SelectableText(parent=self)
        bar.addWidget(self.title_label, 1)
        self.framework_check = QCheckBox(t["calls.show_framework"], self)
        self.framework_check.setToolTip(t["calls.show_framework_tooltip"])
        self.framework_check.toggled.connect(self._show_framework)
        bar.addWidget(self.framework_check)
        self.callers_button = QPushButton(t["calls.search_callers"], self)
        set_button_icon(self.callers_button, "manage_search")
        self.callers_button.setToolTip(t["calls.search_callers_tooltip"])
        self.callers_button.clicked.connect(self.search_callers)
        bar.addWidget(self.callers_button)

        self.scene = QGraphicsScene(self)
        self.view = GraphView(self.scene, self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        self.view.node_activated.connect(self._activated)
        layout.addWidget(self.view, 1)

        actions = QHBoxLayout()
        layout.addLayout(actions)
        # While the code is read or the callers are searched
        self.busy = BusyDots(self)
        actions.addWidget(self.busy)
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        actions.addWidget(self.status_label, 1)
        self.copy_button = QPushButton(t["catalog.copy"], self)
        set_button_icon(self.copy_button, "content_copy")
        self.copy_button.setToolTip(t["calls.copy_tooltip"])
        self.copy_button.clicked.connect(self._copy_signature)
        actions.addWidget(self.copy_button)
        self.monitor_button = QPushButton(t["catalog.monitor"], self)
        self.monitor_button.setObjectName("btn_primary")
        set_button_icon(self.monitor_button, "visibility")
        self.monitor_button.setToolTip(t["calls.monitor_tooltip"])
        self.monitor_button.clicked.connect(self._request_monitor)
        actions.addWidget(self.monitor_button)

    # --- Navigation --------------------------------------------------------------
    def go_to(self, method, remember=True):
        """Start the graph from ``method``; ``remember``: Back returns here."""
        if remember and self.method is not None:
            self._back.append(self.method)
            self._forward.clear()
        self.method = method
        previous = self.graph
        self.graph = CallGraph.for_method(method, self._is_framework(method))
        if previous is not None:
            self.graph.hidden_keys = previous.hidden_keys
            self.graph.hidden_classes = previous.hidden_classes
        self.graph.show_framework = self.framework_check.isChecked()
        self._requests.clear()
        self._marked = None
        self._items = {}
        self.title_label.setText(method_title(method))
        self.setWindowTitle(self.translations["calls.title_method"].format(
            method=method_title(method)))
        self.status_label.setText(self.translations["calls.reading"])
        self.expand(self.graph.center, CALLEES)
        if self._show_callers:
            self.expand(self.graph.center, CALLERS)
        self._update_buttons()
        self._redraw()

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
        center = self.graph.nodes[self.graph.center]
        self.callers_button.setEnabled(not center.unfolded[CALLERS]
                                       and not self._asking(self.graph.center, CALLERS))
        self.busy.set_busy(bool(self._requests))

    def _is_framework(self, method):
        return self.engine is not None and self.engine.is_framework_assembly(method.assembly)

    # --- Unfolding -----------------------------------------------------------------
    def search_callers(self):
        """Search the callers of the method in the centre (and from then on of
        every method gone to)."""
        self._show_callers = True
        self.expand(self.graph.center, CALLERS)

    def _asking(self, key, way):
        return (key, way) in self._requests.values()

    def expand(self, key, way):
        """Ask for the methods node ``key`` calls (CALLEES) or that call it (CALLERS)."""
        node = self.graph.nodes.get(key)
        if node is None or node.unfolded[way] or self._asking(key, way):
            return
        ask = self._worker.callees if way == CALLEES else self._worker.callers
        self._requests[ask(node.method)] = (key, way)
        self._update_buttons()

    def collapse(self, key, way):
        """Fold the ``way`` of node ``key`` in again."""
        node = self.graph.nodes.get(key)
        if node is None or not node.unfolded[way]:
            return
        self.graph.collapse(key, way)
        self._update_buttons()
        self._redraw()

    def _activated(self, item):
        """A double click: unfold a method the way it stands from the centre
        (the centre: its callers), or fold that way in again."""
        key = item.node.key
        way = self.graph.side(key) or CALLERS
        if way == CALLERS and key == self.graph.center:
            self._show_callers = True
        if self.graph.nodes[key].unfolded[way]:
            self.collapse(key, way)
        else:
            self.expand(key, way)

    def _add(self, key, way, found):
        """Record ``found`` (``[(method, label)]``) as unfolding ``key`` the way ``way``."""
        graph = self.graph
        calls = [(graph.node_for(method, self._is_framework(method)), label)
                 for method, label in found]
        graph.add_calls(key, way, calls, settings.get("graph_max_nodes"))

    # --- Answers -----------------------------------------------------------------
    def _on_callees(self, request_id, method, calls, body):
        asked = self._requests.pop(request_id, None)
        if asked is None:
            return
        key, way = asked
        self._add(key, way, [(call.method, call_label(call.opcode)) for call in calls]
                  + [(call.method, call_label(call.opcode, True)) for call in body])
        if key == self.graph.center:
            count = len(calls) + len(body)
            status = "calls.callees_count_async" if body else "calls.callees_count"
            self.status_label.setText(self.translations[status].format(
                count=count, method=method.name))
        self._update_buttons()
        self._redraw()

    def _on_callers(self, request_id, method, calls, missing):
        asked = self._requests.pop(request_id, None)
        if asked is None:
            return
        key, way = asked
        self._add(key, way, [(call.method, call_label(call.opcode)) for call in calls])
        t = self.translations
        if missing:
            self.status_label.setText(t["calls.not_searched"].format(
                count=len(missing), assemblies=", ".join(missing)))
        elif not calls:
            self.status_label.setText(t["calls.no_callers"])
        else:
            self.status_label.setText(t["calls.callers_count"].format(
                count=len({call.method for call in calls}), method=method.name))
        self._update_buttons()
        self._redraw()

    def _on_progress(self, done, total, assembly):
        if assembly:
            self.status_label.setText(self.translations["calls.indexing"].format(
                assembly=assembly, done=done, total=total))

    def _on_failed(self, request_id, message):
        if self._requests.pop(request_id, None) is None:
            return
        self._update_buttons()
        if message == CallGraphWorker.NOT_CACHED:
            self.status_label.setText(self.translations["calls.not_cached"])
            return
        self.status_label.setText(self.translations["calls.failed"].format(message=message))

    def _on_worker_failed(self, message):
        self._requests.clear()
        self._update_buttons()
        self.status_label.setText(self.translations["calls.failed"].format(message=message))

    # --- Drawing -------------------------------------------------------------------
    def _redraw(self):
        graph = self.graph
        selected = self._selected_key()
        first = not self._items
        self.scene.clear()
        self._items = {}
        columns = graph.columns()
        # The methods called in the order of the code
        positions = column_positions(columns, graph.leading, in_order=True)
        marked = (graph.path_from(self._marked)
                  if self._marked is not None and self._marked in graph.nodes else None)
        for key, position in positions.items():
            item = MethodItem(graph, key, self.translations, columns)
            item.setPos(position)
            if marked and key in marked:
                item.set_highlight(_MARKED)
            self.scene.addItem(item)
            self._items[key] = item
            if key == selected:
                item.setSelected(True)
        marked_edges = graph.path_edges(marked)
        taken = []
        drawn = [edge for edge in graph.edges
                 if edge.caller in positions and edge.callee in positions]
        # The labels of the marked way placed first
        drawn.sort(key=lambda edge: (edge.caller, edge.callee) not in marked_edges)
        for edge in drawn:
            mark = (edge.caller, edge.callee) in marked_edges
            draw_edge(self.scene, positions[edge.caller], positions[edge.callee], edge.label,
                      theme_qcolor(_MARKED if mark else "muted"), mark, taken)
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-80, -80, 80, 80))
        if first:
            self.view.centerOn(self._items[graph.center])

    def fit(self):
        """Zoom so that the whole graph fits the window."""
        self.view.fit()

    # --- Context menu ----------------------------------------------------------------
    def _menu(self, position):
        item = self.view.node_at(position)
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        self.fill_menu(menu, None if item is None else item.node.key)
        menu.exec(self.view.viewport().mapToGlobal(position))

    def fill_menu(self, menu, key):
        """The context menu: the actions on node ``key`` (None: the
        background), then those on the graph."""
        t = self.translations
        if key is not None:
            self._node_actions(menu, key)
            menu.addSeparator()
        menu.addAction(t["references.fit"], self.fit).setToolTip(t["references.fit_tooltip"])
        if self._marked is not None:
            menu.addAction(t["references.highlight_clear"], lambda: self.highlight(None))
        hidden = self.graph.hidden_by_user()
        if hidden:
            menu.addAction(t["references.show_hidden"].format(count=hidden),
                           self.show_hidden).setToolTip(t["calls.show_hidden_tooltip"])
        menu.addAction(t["references.export"], self.export_html).setToolTip(
            t["references.export_tooltip"])

    def _node_actions(self, menu, key):
        t = self.translations
        node = self.graph.nodes[key]
        for way in (CALLERS, CALLEES):
            expand, collapse, *_hints = _WAY_KEYS[way]
            if node.unfolded[way]:
                menu.addAction(t[collapse], lambda way=way: self.collapse(key, way))
            else:
                menu.addAction(t[expand], lambda way=way: self.expand(key, way))
        if key != self.graph.center:
            menu.addAction(t["calls.set_center"], lambda: self.go_to(node.method))
        monitor = menu.addAction(t["catalog.monitor"],
                                 lambda: self.monitor_requested.emit(node.method))
        monitor.setEnabled(self.game is None)
        menu.addAction(t["catalog.copy"],
                       lambda: QGuiApplication.clipboard().setText(node.method.signature))
        if key == self.graph.center:
            return
        menu.addSeparator()
        menu.addAction(t["calls.highlight_path"], lambda: self.highlight(key))
        menu.addAction(t["references.hide_node"], lambda: self.hide_node(key))
        menu.addAction(t["references.hide_class"].format(cls=short_class(node.class_name)),
                       lambda: self.hide_class(node.class_name))

    # --- Hiding and highlighting ----------------------------------------------------
    def highlight(self, key):
        """Highlight the way from node ``key`` to the centre; None clears it."""
        self._marked = key
        if key is not None and self.graph.path_from(key) is None:
            self.status_label.setText(self.translations["calls.no_path"])
        self._redraw()

    def hide_node(self, key):
        self.graph.hidden_keys.add(key)
        self._redraw()

    def hide_class(self, class_name):
        self.graph.hidden_classes.add(class_name)
        self._redraw()

    def show_hidden(self):
        self.graph.hidden_keys.clear()
        self.graph.hidden_classes.clear()
        self._redraw()

    def _show_framework(self, shown):
        self.graph.show_framework = shown
        self._redraw()

    # --- Export --------------------------------------------------------------------
    def export_html(self):
        """Save the graph as it is drawn now as an HTML page."""
        t = self.translations
        stamp = datetime.now()
        file_name = "-".join(
            "".join(char if char.isalnum() or char in "._-" else "_" for char in part).strip("_")
            for part in ("calls", short_class(self.method.class_name), self.method.name,
                         stamp.strftime("%Y%m%d-%H%M%S"))) + ".html"
        path, _filter = QFileDialog.getSaveFileName(
            self, t["references.export"], suggested_path(file_name), t["inspector.export_filter"])
        if not path:
            return
        if not path.lower().endswith((".html", ".htm")):
            path += ".html"
        remember(path)
        try:
            with open(path, "w", encoding="utf-8") as file:
                file.write(self.render_html(stamp))
        except OSError as error:
            self.status_label.setText(t["references.export_failed"].format(error=error))
            return
        self.status_label.setText(t["inspector.exported"].format(path=path))

    def render_html(self, stamp=None):
        """The HTML page of the graph as it is drawn now (see graph_export)."""
        t = self.translations
        facts = [(t["calls.export_method"], self.method.signature),
                 (t["calls.export_assembly"], self.method.assembly)]
        program = program_of(self.pid) if self.pid is not None and self.game is None else None
        if program is not None:
            facts.append((t["inspector.export_program"],
                          t["app.process_display"].format(name=program[0], pid=self.pid)))
        facts.append((t["inspector.export_time"],
                      (stamp or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")))
        colors = {key: theme_color(key) for key in ("background", "text", "muted")}
        return graph_export.render_html(
            self.scene, self.windowTitle(), facts, colors,
            t["inspector.export_footer"].format(version=__version__),
            language_code(settings.language()))

    # --- Actions -----------------------------------------------------------------
    def _selected_key(self):
        for item in self.scene.selectedItems():
            if isinstance(item, MethodItem):
                return item.node.key
        return None

    def selected_method(self):
        """The method chosen in the graph, else the one in the centre."""
        key = self._selected_key()
        return self.graph.nodes[key].method if key is not None else self.method

    def _request_monitor(self):
        self.monitor_requested.emit(self.selected_method())

    def _copy_signature(self):
        QGuiApplication.clipboard().setText(self.selected_method().signature)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._worker.requestInterruption()
        super().closeEvent(event)
