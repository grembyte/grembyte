"""What the call graph window shows: methods and the calls between them.

The graph starts from one method, the centre. Unfolding a method adds the
methods it calls (CALLEES, drawn to its right) or the methods that call it
(CALLERS, to its left), as grembyte.features.call_graph finds them in the IL
code. Each unfolding remembers the edges it found, so that folding it in
again takes back exactly those, and every method only reached through them.

Framework methods (List.Add, getters of Unity's classes ...) are many and
seldom what one looks for: they are left out unless asked for, as are the
methods and classes the user hid. What is left out counts in the node's +n.
"""

from dataclasses import dataclass, field

from grembyte.features.call_graph import async_owner

CALLERS, CALLEES = "callers", "callees"
# Plain calls carry no label on their edge; the others say how they call
_PLAIN = {"call", "callvirt"}
# The label of a call found in an async method's or iterator's MoveNext
ASYNC = "async"


def method_key(method):
    """The key of a method's node: its assembly and metadata token."""
    return f"{method.assembly}:{method.token:x}"


def call_label(opcode, in_body=False):
    """The label of an edge for a call by ``opcode`` (``in_body``: in the
    compiler-made MoveNext of an async method)."""
    parts = [] if opcode in _PLAIN else [opcode]
    if in_body:
        parts.append(ASYNC)
    return " · ".join(parts)


@dataclass
class MethodNode:
    """A method in the graph (``method`` a CatalogMethod); ``framework``
    whether it belongs to the engine or framework, not the game. Per way
    (CALLERS, CALLEES): whether it was unfolded, how many methods were
    found, how many of those are drawn."""

    key: str
    method: object
    framework: bool = False
    unfolded: dict = field(default_factory=lambda: {CALLERS: False, CALLEES: False})
    total: dict = field(default_factory=lambda: {CALLERS: 0, CALLEES: 0})
    shown: dict = field(default_factory=lambda: {CALLERS: 0, CALLEES: 0})

    @property
    def class_name(self):
        """The class it is in; for a MoveNext, the class of its async method."""
        owner = async_owner(self.method)
        return self.method.class_name if owner is None else owner[0]

    @property
    def name(self):
        """Its name; for a MoveNext, its async method's, marked as such."""
        owner = async_owner(self.method)
        return self.method.name if owner is None else f"{owner[1]} ({ASYNC})"


@dataclass(frozen=True)
class CallEdge:
    """``caller`` (a node key) calls ``callee``; ``label`` says how (see call_label)."""

    caller: str
    callee: str
    label: str


@dataclass
class CallGraph:
    """The method asked about (``center``) and the calls unfolded so far.

    ``edges`` maps each CallEdge to the unfoldings that found it, as
    ``(node key, way)``. Nodes are hidden one by one (``hidden_keys``), by
    class (``hidden_classes``) or, unless ``show_framework``, for being
    framework methods; the centre never is.
    """

    nodes: dict = field(default_factory=dict)
    edges: dict = field(default_factory=dict)
    center: str = ""
    hidden_keys: set = field(default_factory=set)
    hidden_classes: set = field(default_factory=set)
    show_framework: bool = False

    @classmethod
    def for_method(cls, method, framework=False):
        graph = cls()
        node = graph.node_for(method, framework)
        graph.center = node.key
        return graph

    def node_for(self, method, framework=False):
        """The node of ``method``, added if new."""
        node = MethodNode(method_key(method), method, framework)
        return self.nodes.setdefault(node.key, node)

    def add_calls(self, key, way, calls, limit):
        """Record what unfolding node ``key`` the way ``way`` found: ``calls``
        ``[(MethodNode, label)]``, one per call. Of more methods than
        ``limit``, those of the game are kept first, in the order found; the
        nodes left out are forgotten unless something else leads to them."""
        node = self.nodes[key]
        node.unfolded[way] = True
        labels = {}
        for other, label in calls:
            labels.setdefault(other.key, [])
            if label and label not in labels[other.key]:
                labels[other.key].append(label)
        found = {other.key: other for other, _label in calls}
        node.total[way] = len(found)
        # Visible ones first, so that hidden framework methods do not crowd them out
        order = sorted(found, key=lambda other: (self.is_hidden(other), found[other].framework))
        kept = set(order[:limit])
        node.shown[way] = len(kept)
        linked = self._linked()
        for other in found:
            if other not in kept and other not in linked and other != key:
                self.nodes.pop(other, None)
        for other in order[:limit]:
            label = ", ".join(labels[other])
            edge = (CallEdge(key, other, label) if way == CALLEES
                    else CallEdge(other, key, label))
            self.edges.setdefault(edge, set()).add((key, way))

    def collapse(self, key, way):
        """Fold the ``way`` of node ``key`` in again: the edges only its
        unfolding found go, with every node no longer reached from the
        centre. Returns the keys of the nodes removed."""
        node = self.nodes[key]
        node.unfolded[way] = False
        node.total[way] = node.shown[way] = 0
        for edge in list(self.edges):
            found_by = self.edges[edge]
            found_by.discard((key, way))
            if not found_by:
                del self.edges[edge]
        reached = self._reached()
        removed = [other for other in self.nodes if other not in reached]
        for other in removed:
            del self.nodes[other]
            for unfolding in [(other, CALLERS), (other, CALLEES)]:
                for edge in list(self.edges):
                    self.edges[edge].discard(unfolding)
                    if not self.edges[edge]:
                        del self.edges[edge]
        return removed

    def _linked(self):
        return {edge.caller for edge in self.edges} | {edge.callee for edge in self.edges}

    def neighbours(self, key):
        """``[(edge, other key)]`` of the edges at node ``key``."""
        found = []
        for edge in self.edges:
            if edge.caller == key:
                found.append((edge, edge.callee))
            elif edge.callee == key:
                found.append((edge, edge.caller))
        return found

    def _reached(self, skip_hidden=False):
        reached = {self.center}
        frontier = [self.center]
        while frontier:
            following = []
            for key in frontier:
                for _edge, other in self.neighbours(key):
                    if other not in reached and not (skip_hidden and self.is_hidden(other)):
                        reached.add(other)
                        following.append(other)
            frontier = following
        return reached

    def is_hidden(self, key):
        """Whether node ``key`` is left out: hidden by the user, or a
        framework method while those are not shown."""
        if key == self.center:
            return False
        node = self.nodes[key]
        return (key in self.hidden_keys or node.class_name in self.hidden_classes
                or (node.framework and not self.show_framework))

    def hidden_by_user(self):
        """How many of the nodes the user hid (one by one or by class)."""
        return sum(1 for key, node in self.nodes.items() if key != self.center
                   and (key in self.hidden_keys or node.class_name in self.hidden_classes))

    def columns(self):
        """``{key: column}`` of the nodes drawn: the centre in column 0, a
        method it calls one column right, one calling it one column left,
        and so on along the edges; hidden nodes are left out."""
        column = {self.center: 0}
        frontier = [self.center]
        while frontier:
            following = []
            for key in frontier:
                for edge, other in self.neighbours(key):
                    if other in column or self.is_hidden(other):
                        continue
                    column[other] = column[key] + (1 if edge.caller == key else -1)
                    following.append(other)
            frontier = following
        return column

    def leading(self, key):
        """The keys of the nodes at node ``key`` (see column_positions)."""
        return [other for _edge, other in self.neighbours(key)]

    def side(self, key, columns=None):
        """Which way node ``key`` grows when unfolded: CALLERS left of the
        centre, CALLEES right of it, None for the centre."""
        column = (columns or self.columns()).get(key, 0)
        if column == 0:
            return None
        return CALLEES if column > 0 else CALLERS

    def hidden_count(self, key, way):
        """How many of the methods found the way ``way`` of node ``key`` are not drawn."""
        node = self.nodes[key]
        drawn = sum(1 for edge, other in self.neighbours(key)
                    if (key, way) in self.edges[edge] and not self.is_hidden(other))
        return max(node.total[way] - drawn, 0)

    def path_from(self, start):
        """Keys from node ``start`` to the centre along the edges drawn, the
        shortest, or None."""
        previous = {self.center: None}
        frontier = [self.center]
        while frontier:
            following = []
            for key in frontier:
                if key == start:
                    path = [key]
                    while previous[path[-1]] is not None:
                        path.append(previous[path[-1]])
                    return path
                for _edge, other in self.neighbours(key):
                    if other not in previous and not self.is_hidden(other):
                        previous[other] = key
                        following.append(other)
            frontier = following
        return None

    def path_edges(self, path):
        """``{(caller, callee)}`` of the edges along ``path``."""
        pairs = set()
        for one, other in zip(path or (), (path or ())[1:]):
            for edge in self.edges:
                if {edge.caller, edge.callee} == {one, other}:
                    pairs.add((edge.caller, edge.callee))
        return pairs
