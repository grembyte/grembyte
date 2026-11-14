"""Reference graph: which objects hold an object, and which hold those; or,
the other way, which objects an object points to, and those in turn.

The pointer scan's pointer map (grembyte.core.pointer) has every aligned
pointer of the game's writable memory, sorted by value. The locations that
hold an object's address are therefore one binary search away
(``ReferenceIndex.referrers``). The map is read once per graph, a snapshot
of that moment; the graph grows from it as the user unfolds its nodes, and
"read again" takes a new one.

A location tells where it lies (``place``): a module's static memory or a
thread's stack ends a chain there (a root), and heap memory goes to the
engine adapter's ``locateReferences`` (through the agent's
``resolveReferences``), which names the object and field holding it, a
class's static field (another root), or nothing it knows (native memory,
e.g. Unity's own objects or the blocks of Unreal's containers).

The other way (``TARGETS``) takes the pointers lying inside an object
(``ReferenceIndex.pointers_in``, the object's size from the agent) and keeps
those to the start of an object, labelled with the field holding them.

``ReferenceGraph`` keeps what was found: nodes by key, edges from the holder
to the object it holds, labelled with the field. It knows nothing of Qt;
the window (grembyte.ui.widgets.reference_graph) draws it.
"""

from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QThread, Signal  # pylint: disable=import-error,no-name-in-module

from grembyte.core import logs, memory_regions, messages
from grembyte.core.pointer import PointerMap, ScanCancelled, module_bases
from grembyte.platform import read_mappings, thread_stack_pointers

# Node kinds. OBJECT and the holders the agent names; the rest end a chain.
OBJECT = "object"
STATIC = "static"      # a class's static field (Mono)
MODULE = "module"      # static memory of a module: module + offset
STACK = "stack"        # a thread's stack: a local variable
UNKNOWN = "unknown"    # heap memory no object of the engine covers
ROOTS = (STATIC, MODULE, STACK)
# Which way a graph grows from its object: to what holds it, or to what it
# points to
HOLDERS, TARGETS = "holders", "targets"
# Why a node is left out of the graph (see ReferenceGraph.hidden_reason)
KIND, BY_USER, FILTERED = "kind", "user", "filtered"
# Locations looked at per node at most; the rest are counted. Many may lie
# in native memory, at lower addresses than the engine's objects, so far
# more are looked at than shown.
MAX_REFERRERS = 5000
# Holders shown per node at most: roots first, then objects (the windows pass
# the setting graph_max_nodes)
MAX_HOLDERS = 100
# How the holders are ordered when there are more than MAX_HOLDERS
HOLDER_ORDER = {STATIC: 0, MODULE: 1, OBJECT: 2, STACK: 3, UNKNOWN: 4}
# Objects unfolded at most by one search for the way to a root
MAX_PATH_STEPS = 200


class ReferenceIndex:
    """Where the game's memory holds a given address (see the module doc)."""

    def __init__(self, pointer_map, regions):
        """``regions`` the writable memory as ``memory_regions.classify`` gives it."""
        self.pointer_map = pointer_map
        self.regions = regions
        self._region_starts = [region.start for region in regions]
        self._bases = module_bases(pointer_map.mappings)
        self._by_location = None

    @classmethod
    def build(cls, pid, main_module=None, progress=None, cancelled=None):
        """Read the memory of process ``pid``; ``progress(percent)`` reports it.

        Raises ScanCancelled when ``cancelled()`` turns true.
        """
        mappings = read_mappings(pid)
        regions = memory_regions.classify(mappings, main_module, thread_stack_pointers(pid))
        pointer_map = PointerMap.build(pid, progress=progress, cancelled=cancelled)
        return cls(pointer_map, regions)

    def referrers(self, address, limit=MAX_REFERRERS):
        """Return ``(locations, total)``: up to ``limit`` locations holding
        ``address``, in address order, and how many there are."""
        values = self.pointer_map.values
        target = np.uint64(address)
        low = int(np.searchsorted(values, target, side="left"))
        high = int(np.searchsorted(values, target, side="right"))
        locations = np.sort(self.pointer_map.locations[low:high])
        return [int(location) for location in locations[:limit]], high - low

    def pointers_in(self, start, end, limit=MAX_REFERRERS):
        """Return ``(pairs, total)``: up to ``limit`` ``(location, value)`` of
        the pointers lying in ``start`` to ``end`` (excluded), in address
        order, and how many there are."""
        if self._by_location is None:
            # Sorted by location once, the first time it is asked
            order = np.argsort(self.pointer_map.locations, kind="stable")
            self._by_location = (self.pointer_map.locations[order],
                                 self.pointer_map.values[order])
        locations, values = self._by_location
        low = int(np.searchsorted(locations, np.uint64(start), side="left"))
        high = int(np.searchsorted(locations, np.uint64(end), side="left"))
        pairs = [(int(location), int(value)) for location, value in
                 zip(locations[low:min(high, low + limit)], values[low:min(high, low + limit)])]
        return pairs, high - low

    def place(self, location):
        """Return ``(kind, text)`` for a root (MODULE with "module+0x…", STACK)
        or ``(None, None)`` for heap memory the agent has to name."""
        index = _last_at_most(self._region_starts, location)
        region = self.regions[index] if index >= 0 else None
        if region is None or location >= region.end:
            return None, None
        if region.category == memory_regions.STACK:
            return STACK, None
        if region.module is not None:
            offset = location - self._bases.get(region.module, region.start)
            return MODULE, f"{region.module}+0x{offset:x}"
        return None, None


def _last_at_most(values, value):
    """Index of the last of the sorted ``values`` not above ``value``, or -1."""
    low, high = 0, len(values)
    while low < high:
        middle = (low + high) // 2
        if values[middle] <= value:
            low = middle + 1
        else:
            high = middle
    return low - 1


@dataclass
class Node:
    """One holder in the graph: an object or a root."""

    key: str
    kind: str
    # The object's address (OBJECT), else the location
    address: int
    class_name: str = ""
    # The object's own name (Unity objects, strings, Unreal objects), the
    # field (STATIC) or "module+0x…" (MODULE)
    name: str | None = None
    klass: str | None = None
    # Whether its holders were looked up, how many there are, how many shown
    expanded: bool = False
    total: int = 0
    shown: int = 0
    # The locations of a STACK or UNKNOWN node (it stands for all of one object's)
    locations: list = field(default_factory=list)
    # An object's size in bytes, once the agent said it (the other way, TARGETS)
    size: int | None = None
    # The static fields of an object's class, once the agent said them (TARGETS):
    # [(field, location, class name)]
    statics: list | None = None

    @property
    def is_root(self):
        return self.kind in ROOTS

    @property
    def can_expand(self):
        return self.kind == OBJECT and not self.expanded

    def target(self):
        """The object inspector's target of an object node, else None."""
        if self.kind != OBJECT:
            return None
        return {"address": hex(self.address)}


def static_fields(entry):
    """``[(field, location, class name)]`` of the ``statics`` in an answer of the
    agent's ``resolveReferences``: the static fields of an object's class (only
    Mono has them); None when the answer is not about the object's start, so
    that it does not tell them."""
    location, start = entry.get("location"), entry.get("object")
    if not location or not start or int(location, 16) != int(start, 16):
        return None
    return [(field, int(at, 16), class_name)
            for field, at, class_name in entry.get("statics") or ()]


@dataclass(frozen=True)
class Edge:
    """``holder`` (a node key) holds ``held`` in ``label`` (its field)."""

    holder: str
    held: str
    label: str


@dataclass
class ReferenceGraph:
    """The object asked about (``target``) and the holders found so far.

    Objects can be hidden one by one (``hidden_keys``) or by class
    (``hidden_classes``, the class names), or all but those of some classes
    shown (``only_classes``, the classes' ``klass``; None: all): the graph
    leaves them out, and the way to a root goes around them. The target is
    never hidden, nor are roots.
    """

    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    target: str = ""
    # HOLDERS: it grows to what holds the target; TARGETS: to what it points to
    direction: str = HOLDERS
    hidden_keys: set = field(default_factory=set)
    hidden_classes: set = field(default_factory=set)
    only_classes: set | None = None

    @classmethod
    def for_object(cls, address, class_name="", name=None, klass=None, direction=HOLDERS):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        graph = cls(direction=direction)
        node = Node(object_key(address), OBJECT, address, class_name, name, klass)
        graph.nodes[node.key] = node
        graph.target = node.key
        return graph

    def node_for(self, held, location, entry=None, place=(None, None)):
        """The node of what holds node ``held`` at ``location``: ``place`` from
        ``ReferenceIndex.place`` or an answer of the agent's
        ``resolveReferences`` (``entry``); added if new. The stack and unknown
        locations of one object share a node, which counts them."""
        kind, text = place
        if kind == MODULE:
            node = Node(f"module:{text}", MODULE, location, name=text)
        elif kind == STACK:
            node = Node(f"stack:{held}", STACK, location)
        elif entry is not None and entry.get("kind") == STATIC:
            name = f"{entry.get('className', '?')}.{entry.get('field', '?')}"
            node = Node(f"static:{name}", STATIC, location, entry.get("className", ""),
                        entry.get("field"), entry.get("klass"))
        elif entry is not None and entry.get("object"):
            address = int(entry["object"], 16)
            node = Node(object_key(address), OBJECT, address, entry.get("className", ""),
                        entry.get("name"), entry.get("klass"), size=entry.get("size"),
                        statics=static_fields(entry))
        else:
            node = Node(f"unknown:{held}", UNKNOWN, location)
        node = self.nodes.setdefault(node.key, node)
        if node.kind in (STACK, UNKNOWN):
            node.locations.append(location)
        return node

    def add_holders(self, key, holders, total, limit=MAX_HOLDERS):
        """Record what node ``key`` leads to: its holders (HOLDERS) or the
        objects it points to (TARGETS), ``holders`` [(Node, label)], one per
        location looked at, among ``total`` locations. Of more than ``limit``
        the roots and objects are kept; the nodes left out are forgotten
        unless something else already led to them. ``shown`` counts the
        locations of those kept."""
        node = self.nodes[key]
        node.expanded = True
        node.total = total
        kept = []
        # Shown only some classes: their objects before the others
        order = sorted(holders, key=lambda pair: (
            HOLDER_ORDER[pair[0].kind], self.hidden_reason(pair[0].key) == FILTERED))
        for holder, _label in order:
            if holder.key not in kept and len(kept) < limit:
                kept.append(holder.key)
        kept = set(kept)
        connected = {edge.holder for edge in self.edges} | {edge.held for edge in self.edges}
        for holder, _label in holders:
            if holder.key not in kept and holder.key not in connected and holder.key != key:
                self.nodes.pop(holder.key, None)
        holders = [(holder, label) for holder, label in holders if holder.key in kept]
        node.shown = len(holders)
        known = set(self.edges)
        for holder, label in holders:
            if holder.key == key:
                continue  # it holds itself
            edge = (Edge(holder.key, key, label) if self.direction == HOLDERS
                    else Edge(key, holder.key, label))
            if edge not in known:
                known.add(edge)
                self.edges.append(edge)

    def add_static_target(self, key, field, location, class_name, target):
        """The other way (TARGETS): node ``key`` reaches node ``target`` through the
        static field ``field`` of its class ``class_name`` at ``location``, drawn as
        that field's node between them (as the holders show it). Returns the
        field's node; ``add_holders`` links ``key`` to it."""
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        node = self.node_for(key, location, entry={
            "kind": STATIC, "className": class_name, "field": field})
        node.expanded = True
        node.total = node.shown = 1
        edge = Edge(node.key, target.key, "")
        if edge not in self.edges:
            self.edges.append(edge)
        return node

    def collapse(self, key):
        """Fold node ``key`` in again: forget what unfolding it found, and every
        node only reached through that. Returns the keys of the nodes removed."""
        node = self.nodes[key]
        node.expanded = False
        node.total = node.shown = 0
        leaving = {edge for edge, _other in self.following(key)}
        self.edges = [edge for edge in self.edges if edge not in leaving]
        reached = {self.target}
        frontier = [self.target]
        while frontier:
            following = []
            for current in frontier:
                for _edge, other in self.following(current):
                    if other not in reached:
                        reached.add(other)
                        following.append(other)
            frontier = following
        removed = [other for other in self.nodes if other not in reached]
        for other in removed:
            del self.nodes[other]
        self.edges = [edge for edge in self.edges
                      if edge.holder in reached and edge.held in reached]
        return removed

    def holders_of(self, key):
        return [edge for edge in self.edges if edge.held == key]

    def following(self, key):
        """``[(edge, key)]``: the nodes ``key`` leads to, away from the target."""
        if self.direction == HOLDERS:
            return [(edge, edge.holder) for edge in self.edges if edge.held == key]
        return [(edge, edge.held) for edge in self.edges if edge.holder == key]

    def leading(self, key):
        """The keys of the nodes that lead to ``key``, toward the target."""
        if self.direction == HOLDERS:
            return [edge.held for edge in self.edges if edge.holder == key]
        return [edge.holder for edge in self.edges if edge.held == key]

    def path_edges(self, path):
        """``[(holder, held)]`` of the edges along ``path`` (as ``path_from``
        gives it: from a node to the target)."""
        pairs = list(zip(path or (), (path or ())[1:]))
        # The other way, a node is held by the next one toward the target
        return pairs if self.direction == HOLDERS else [(holder, held) for held, holder in pairs]

    def hidden_reason(self, key, kinds=()):
        """Why node ``key`` is left out, or None: KIND (of one of the node
        kinds ``kinds``), BY_USER (hidden one by one or by class) or FILTERED
        (not of ``only_classes``)."""
        if key == self.target:
            return None
        node = self.nodes[key]
        if node.kind in kinds:
            return KIND
        if key in self.hidden_keys or (node.kind == OBJECT
                                       and node.class_name in self.hidden_classes):
            return BY_USER
        if (self.only_classes is not None and node.kind == OBJECT
                and node.klass not in self.only_classes):
            return FILTERED
        return None

    def is_hidden(self, key, kinds=()):
        """Whether node ``key`` is left out (see ``hidden_reason``)."""
        return self.hidden_reason(key, kinds) is not None

    def hidden_by_user(self):
        """How many of the nodes are hidden by the user (one by one or by class)."""
        return sum(1 for key in self.nodes if self.hidden_reason(key) == BY_USER)

    def depths(self, hidden=()):
        """``{key: steps from the target}`` along the edges (holders deeper),
        without the holders of the node kinds ``hidden``."""
        depth = {self.target: 0}
        frontier = [self.target]
        while frontier:
            following = []
            for key in frontier:
                for _edge, other in self.following(key):
                    if self.is_hidden(other, hidden):
                        continue
                    if other not in depth:
                        depth[other] = depth[key] + 1
                        following.append(other)
            frontier = following
        return depth

    def hidden_locations(self, key, hidden, reason=KIND):
        """How many of the locations holding node ``key`` belong to holders
        left out for ``reason`` (see ``hidden_reason``; ``hidden`` the node
        kinds left out). STACK and UNKNOWN nodes count their locations."""
        return sum(max(len(self.nodes[other].locations), 1)
                   for _edge, other in self.following(key)
                   if self.hidden_reason(other, hidden) == reason)

    def path_to_root(self):
        """Keys from a root to the target along the edges, the shortest found
        around the hidden nodes, or None (always the other way, TARGETS)."""
        if self.direction != HOLDERS:
            return None
        return self._path(lambda key: self.nodes[key].is_root)

    def path_from(self, start):
        """Keys from node ``start`` to the target along the edges, the
        shortest around the hidden nodes, or None."""
        return self._path(lambda key: key == start)

    def _path(self, found):
        previous = {self.target: None}
        frontier = [self.target]
        while frontier:
            following = []
            for key in frontier:
                if found(key):
                    path = [key]
                    while previous[path[-1]] is not None:
                        path.append(previous[path[-1]])
                    return path
                for _edge, other in self.following(key):
                    if other not in previous and not self.is_hidden(other):
                        previous[other] = key
                        following.append(other)
            frontier = following
        return None

    def next_to_expand(self):
        """The unfolded object nearest the target, for the way to a root, or None."""
        depth = self.depths()
        waiting = [self.nodes[key] for key in depth if self.nodes[key].can_expand]
        if not waiting:
            return None
        return min(waiting, key=lambda node: (depth[node.key], node.address)).key


def object_key(address):
    return f"object:{address:x}"


class ReferenceIndexWorker(QThread):
    """Read the game's memory for a ReferenceIndex outside the GUI thread."""

    progress = Signal(int)
    # the ReferenceIndex
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, pid, main_module=None, parent=None):
        super().__init__(parent)
        logs.log_failures(self)
        self.pid = pid
        self.main_module = main_module

    def run(self):
        try:
            index = ReferenceIndex.build(
                self.pid, self.main_module, progress=self.progress.emit,
                cancelled=self.isInterruptionRequested)
        except ScanCancelled:
            return
        except (OSError, MemoryError) as error:
            if not self.isInterruptionRequested():
                self.failed.emit(messages.describe(error))
            return
        if not self.isInterruptionRequested():
            self.ready.emit(index)
