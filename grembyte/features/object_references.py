"""Reference graph: which objects hold an object, and which hold those.

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
# Locations looked at per node at most; the rest are counted. Many may lie
# in native memory, at lower addresses than the engine's objects, so far
# more are looked at than shown.
MAX_REFERRERS = 5000
# Holders shown per node at most: roots first, then objects
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


@dataclass(frozen=True)
class Edge:
    """``holder`` (a node key) holds ``held`` in ``label`` (its field)."""

    holder: str
    held: str
    label: str


@dataclass
class ReferenceGraph:
    """The object asked about (``target``) and the holders found so far."""

    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    target: str = ""

    @classmethod
    def for_object(cls, address, class_name="", name=None, klass=None):
        graph = cls()
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
                        entry.get("name"), entry.get("klass"))
        else:
            node = Node(f"unknown:{held}", UNKNOWN, location)
        node = self.nodes.setdefault(node.key, node)
        if node.kind in (STACK, UNKNOWN):
            node.locations.append(location)
        return node

    def add_holders(self, key, holders, total, limit=MAX_HOLDERS):
        """Record the holders of node ``key``: ``holders`` [(Node, label)],
        one per location looked at, among ``total`` locations. Of more than
        ``limit`` holders the roots and objects are kept; the nodes left out
        are forgotten unless something else already led to them. ``shown``
        counts the locations of the holders kept."""
        node = self.nodes[key]
        node.expanded = True
        node.total = total
        kept = []
        for holder, _label in sorted(holders, key=lambda pair: HOLDER_ORDER[pair[0].kind]):
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
            edge = Edge(holder.key, key, label)
            if edge not in known:
                known.add(edge)
                self.edges.append(edge)

    def holders_of(self, key):
        return [edge for edge in self.edges if edge.held == key]

    def depths(self, hidden=()):
        """``{key: steps from the target}`` along the edges (holders deeper),
        without the holders of the node kinds ``hidden``."""
        depth = {self.target: 0}
        frontier = [self.target]
        while frontier:
            following = []
            for key in frontier:
                for edge in self.holders_of(key):
                    if self.nodes[edge.holder].kind in hidden:
                        continue
                    if edge.holder not in depth:
                        depth[edge.holder] = depth[key] + 1
                        following.append(edge.holder)
            frontier = following
        return depth

    def hidden_locations(self, key, hidden):
        """How many of the locations shown as holding node ``key`` belong to
        holders of the node kinds ``hidden`` (STACK and UNKNOWN count theirs)."""
        return sum(max(len(self.nodes[edge.holder].locations), 1)
                   for edge in self.holders_of(key) if self.nodes[edge.holder].kind in hidden)

    def path_to_root(self):
        """Keys from a root to the target along the edges, the shortest found, or None."""
        previous = {self.target: None}
        frontier = [self.target]
        while frontier:
            following = []
            for key in frontier:
                if self.nodes[key].is_root:
                    path = [key]
                    while previous[path[-1]] is not None:
                        path.append(previous[path[-1]])
                    return path
                for edge in self.holders_of(key):
                    if edge.holder not in previous:
                        previous[edge.holder] = key
                        following.append(edge.holder)
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
