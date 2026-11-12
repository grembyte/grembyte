"""Pointer scan: find pointer chains from static memory to an address.

Absolute addresses of heap objects change with every start. A pointer chain
starts at a static location (inside a module's writable data, which stays at
the same module + offset) and follows pointers to the target::

    address = module_base + base_offset
    for offset in offsets:
        address = read_pointer(address) + offset

The scan reads all writable memory of the process once and keeps every
aligned pointer-sized value (8 bytes, 4 in 32-bit programs) that points into
writable memory (the pointer map, sorted by value). By default only values
that are multiples of the pointer size count:
real pointers to objects are, while numbers and bit masks that merely look
like an address (e.g. ``0x8860004``) usually are not. Starting at the target it then searches backwards: which
locations hold a pointer at most ``max_offset`` below the current address?
Locations in static memory end a chain, all others become the addresses of
the next level, up to ``max_depth`` levels.

The backwards search grows exponentially, and most addresses it would follow
never lead to static memory. So it first computes forwards, for every
pointer, how many pointers away from static memory its location is at least,
and only follows locations that can still reach static memory in the depth
that is left.

Chains are collected shortest first, so a result limit drops the longest
ones. ``base_modules`` restricts where chains may start, e.g. to the program
itself; static memory of other modules then ends no chain and is not followed.

A first scan usually finds many chains. Chains that still lead to the value
after the game was restarted (``filter_chains``) are the stable ones.

In Mono games the static fields of C# classes lie in no module: Mono keeps
each class's static data in a block of its own, which stays put while the
game runs. Such a block can be a chain base too (``static_areas``), named
``mono:<assembly>:<class>`` (``managed_base``) instead of a module. Its
address is only known by asking the runtime, so it is looked up once per run
of the game and remembered here (``remember_managed_bases``);
``chain_bases`` has module and managed bases alike for ``resolve_chain``.
"""

from dataclasses import dataclass

import numpy as np

from grembyte.platform import (
    ProcessMemory,
    is_wine_process,
    process_start_time,
    program_bits,
    read_mappings,
)
from grembyte.platform.wine import wine_bits


POINTER_SIZE = 8
_CHUNK_SIZE = 16 * 1024 * 1024
# Pointers kept by one search (about 24 bytes each), which bounds its memory.
MAX_EDGES = 20_000_000
# Pointers looked up at once; bounds the temporary arrays of a search level.
_EDGE_BATCH = 1_000_000
# Neither Linux (vm.mmap_min_addr) nor Windows maps the lowest 64 KiB.
_MIN_ADDRESS = 0x10000


class ScanCancelled(Exception):
    """Raised when a pointer scan is cancelled."""


@dataclass(frozen=True)
class PointerChain:
    """``module + base_offset``, then dereference and add each offset."""

    module: str
    base_offset: int
    offsets: tuple = ()

    def text(self):
        """Readable form, e.g. ``game+0x2a1f0 → 0x18 → 0x40``.

        Managed bases read ``[Assembly-CSharp] Game.GameState+0x18``.
        """
        managed = split_managed(self.module)
        base = self.module if managed is None else f"[{managed[0]}] {managed[1]}"
        parts = [f"{base}+0x{self.base_offset:x}"]
        parts += [f"{offset:#x}" if offset >= 0 else f"-{-offset:#x}" for offset in self.offsets]
        return " → ".join(parts)

    def to_json(self):
        return {"module": self.module, "base_offset": self.base_offset,
                "offsets": list(self.offsets)}

    @classmethod
    def from_json(cls, data):
        return cls(data["module"], int(data["base_offset"]),
                   tuple(int(offset) for offset in data.get("offsets", ())))


MANAGED_PREFIX = "mono:"


def managed_base(assembly, class_name):
    """The chain base name of the static data of a class (see the module doc)."""
    return f"{MANAGED_PREFIX}{assembly}:{class_name}"


def split_managed(name):
    """``(assembly, class)`` of a managed base name, else None."""
    if not name.startswith(MANAGED_PREFIX):
        return None
    assembly, separator, class_name = name[len(MANAGED_PREFIX):].partition(":")
    return (assembly, class_name) if separator and assembly and class_name else None


# pid -> (start time, {managed base name: address}) of the running game
_managed_bases = {}


def remember_managed_bases(pid, bases):
    """Keep the addresses of managed bases (``{name: address}``) for this run of ``pid``."""
    started = process_start_time(pid)
    known = _managed_bases.get(pid)
    merged = dict(known[1]) if known is not None and known[0] == started else {}
    merged.update(bases)
    _managed_bases[pid] = (started, merged)


def managed_bases(pid):
    """The managed bases remembered for the current run of ``pid``."""
    known = _managed_bases.get(pid)
    if known is None or pid is None or known[0] != process_start_time(pid):
        return {}
    return dict(known[1])


def missing_managed_bases(pid, chains):
    """``{assembly: {class, ...}}`` of the managed bases of ``chains`` not known yet."""
    known = managed_bases(pid)
    missing = {}
    for chain in chains:
        managed = split_managed(chain.module)
        if managed is not None and chain.module not in known:
            missing.setdefault(managed[0], set()).add(managed[1])
    return missing


def chain_bases(pid, mappings):
    """Bases ``resolve_chain`` finds chains by: the modules', then the managed ones."""
    bases = module_bases(mappings)
    bases.update(managed_bases(pid))
    return bases


def module_bases(mappings):
    """Return ``{module name: load address}`` of the loaded modules."""
    bases = {}
    for mapping in mappings:
        if mapping.module is not None:
            bases[mapping.module] = min(bases.get(mapping.module, mapping.start), mapping.start)
    return bases


def static_regions(mappings):
    """Return ``[(start, end, module)]`` of writable module memory (.data, .bss)."""
    return [
        (mapping.start, mapping.end, mapping.module)
        for mapping in sorted(mappings, key=lambda m: m.start)
        if mapping.writable and mapping.module is not None
    ]


def _separate_areas(areas, regions):
    """The ``areas`` that overlap neither ``regions`` nor each other, as regions.

    Static data of a class lies on the heap; one that would lie in a module's
    memory or in another area could not tell where a chain starts.
    """
    taken = sorted((start, end) for start, end, _name in regions)
    result = []
    for start, end, name in sorted(areas):
        if any(low < end and start < high for low, high in taken):
            continue
        taken.append((start, end))
        taken.sort()
        result.append((start, end, name))
    return result


def static_location(regions, address):
    """Return the module whose static memory contains ``address``, or None."""
    for start, end, module in regions:
        if start <= address < end:
            return module
    return None


# pid -> (start time, pointer size): finding the bits of a Wine program reads
# its images, too slow for every refresh.
_pointer_sizes = {}


def pointer_size(pid):
    """Size of a pointer in process ``pid``: 4 for 32-bit programs, else 8."""
    if pid is None:
        return POINTER_SIZE
    started = process_start_time(pid)
    known = _pointer_sizes.get(pid)
    if known is not None and known[0] == started:
        return known[1]
    if is_wine_process(pid):
        bits = wine_bits(pid)
    else:
        bits = program_bits(pid)
    size = 4 if bits == 32 else POINTER_SIZE
    if started is not None:
        _pointer_sizes[pid] = (started, size)
    return size


def user_address_limit(size):
    """End of user space for pointers of ``size`` bytes.

    x86-64 programs get the lower 47 bits on Linux and Windows alike, 32-bit
    programs at most 4 GiB.
    """
    return 1 << 47 if size == 8 else 1 << 32


def resolve_chain(memory, bases, chain):
    """Follow ``chain`` in a process; return the final address or None.

    A step that leaves user space ends it with None: once the game frees an
    object, its pointer slot may hold anything, and such an address can be
    neither read, written, nor saved (SQLite integers have 63 bits).
    """
    base = bases.get(chain.module)
    if base is None:
        return None
    size = pointer_size(getattr(memory, "pid", None))
    limit = user_address_limit(size)
    address = base + chain.base_offset
    for offset in chain.offsets:
        pointer = memory.read_pointer(address, size)
        if not pointer or not _MIN_ADDRESS <= pointer < limit:
            return None
        address = pointer + offset
    return address if _MIN_ADDRESS <= address < limit else None


def chain_for_static_address(mappings, address):
    """Return a chain without offsets if ``address`` is static, else None.

    Global variables live in a module's data, so module + offset finds them
    again after a restart without any pointer scan.
    """
    regions = static_regions(mappings)
    module = static_location(regions, address)
    if module is None:
        return None
    return PointerChain(module, address - module_bases(mappings)[module], ())


class PointerMap:
    """All aligned pointers into writable memory, sorted by value."""

    def __init__(self, values, locations, mappings, location_rank=None, static_areas=()):
        # pylint: disable=too-many-arguments
        """``static_areas`` ``[(start, end, name)]`` are further chain bases
        (managed static data, see the module doc)."""
        self.values = values
        self.locations = locations
        # Position of each pointer when sorted by location (computed if None).
        self._location_rank = location_rank
        self.mappings = mappings
        self.bases = module_bases(mappings)
        regions = static_regions(mappings)
        regions = sorted(regions + _separate_areas(static_areas, regions))
        self.bases.update((name, start) for start, _end, name in static_areas
                          if any(region[2] == name for region in regions))
        self._static_starts = np.array([r[0] for r in regions], dtype=np.uint64)
        self._static_ends = np.array([r[1] for r in regions], dtype=np.uint64)
        self._static_modules = [r[2] for r in regions]
        # Whether the last find_chains stopped at its max_edges limit; if so,
        # its chains with up to ``complete_depth`` pointers are still complete.
        self.truncated = False
        self.complete_depth = None

    def __len__(self):
        return len(self.values)

    @classmethod
    def build(cls, pid, progress=None, cancelled=None, select=None, location_step=None,
              value_alignment=None, static_areas=()):
        # pylint: disable=too-many-arguments,too-many-locals
        """Read the process memory; ``progress(percent)`` reports the reading.

        ``select(mappings)`` may return the sorted ``[(start, end)]`` to read
        instead of all writable memory (see grembyte.core.memory_regions);
        pointers into other memory are left out then, too.

        Pointers are looked for every ``location_step`` bytes, and only values
        that are multiples of ``value_alignment`` count (1: any value); both
        default to the pointer size. A step of 4 in a 64-bit program also
        finds pointers in packed structures, at twice the work.
        ``static_areas`` see ``__init__``.
        """
        size_of_pointer = pointer_size(pid)
        dtype = "<u8" if size_of_pointer == 8 else "<u4"
        step = location_step or size_of_pointer
        if size_of_pointer % step:
            raise ValueError(f"location step {step} does not divide the pointer size")
        alignment = np.uint64(value_alignment or size_of_pointer)
        # Bytes read past a block, for pointers that start in it at a step
        # smaller than the pointer size.
        overlap = size_of_pointer - step
        mappings = read_mappings(pid)
        if select is not None:
            ranges = select(mappings)
        else:
            ranges = sorted((m.start, m.end) for m in mappings if m.writable)
        starts = np.array([start for start, _end in ranges], dtype=np.uint64)
        ends = np.array([end for _start, end in ranges], dtype=np.uint64)
        total = sum(end - start for start, end in ranges) or 1
        done = 0
        values, locations = [], []
        with ProcessMemory(pid) as memory:
            for range_start, range_end in ranges:
                position = range_start
                while position < range_end:
                    if cancelled is not None and cancelled():
                        raise ScanCancelled()
                    size = min(_CHUNK_SIZE, range_end - position)
                    block = memory.read_available(
                        position, size + min(overlap, range_end - position - size))
                    block_values, block_locations = [], []
                    for shift in range(0, size_of_pointer, step):
                        count = min((len(block) - shift) // size_of_pointer,
                                    (size - shift + size_of_pointer - 1) // size_of_pointer)
                        if count <= 0:
                            continue
                        words = np.frombuffer(block, dtype=dtype, count=count,
                                              offset=shift).astype(np.uint64)
                        index = np.searchsorted(starts, words, side="right") - 1
                        valid = (index >= 0) & (words < ends[np.maximum(index, 0)])
                        if alignment > 1:
                            valid &= words % alignment == 0
                        hits = np.flatnonzero(valid)
                        if hits.size:
                            block_values.append(words[hits])
                            block_locations.append(
                                np.uint64(position + shift)
                                + hits.astype(np.uint64) * np.uint64(size_of_pointer)
                            )
                    if len(block_values) > 1:
                        # Interleaved shifts: back into address order
                        joined = np.concatenate(block_locations)
                        order = np.argsort(joined, kind="stable")
                        block_locations = [joined[order]]
                        block_values = [np.concatenate(block_values)[order]]
                    values += block_values
                    locations += block_locations
                    position += size
                    done += size
                    if progress is not None:
                        progress(done * 100 // total)
        if values:
            values = np.concatenate(values)
            order = np.argsort(values, kind="stable")
            values = values[order]
            # Memory was read in address order, so ``order`` maps each pointer
            # to its position by location.
            locations = np.concatenate(locations)[order]
            location_rank = order.astype(np.uint32) if len(order) < 2**32 else order
            del order
        else:
            values = np.empty(0, dtype=np.uint64)
            locations = np.empty(0, dtype=np.uint64)
            location_rank = np.empty(0, dtype=np.uint32)
        return cls(values, locations, mappings, location_rank, static_areas)

    def static_modules(self, locations):
        """Return the module index per location, -1 for dynamic memory."""
        if not len(self._static_starts):
            return np.full(len(locations), -1)
        index = np.searchsorted(self._static_starts, locations, side="right") - 1
        inside = (index >= 0) & (locations < self._static_ends[np.maximum(index, 0)])
        return np.where(inside, index, -1)

    def find_chains(self, target, max_depth=5, max_offset=0x1000, max_results=10000,
                    max_edges=MAX_EDGES, progress=None, cancelled=None, base_modules=None):
        # pylint: disable=too-many-arguments
        """Return chains from static memory to ``target``, shortest first.

        If there are more than ``max_results`` chains, the longest are left
        out. ``base_modules`` (module names) limits where chains may start.
        ``progress(level)`` is called for each searched level. At most
        ``max_edges`` pointers are kept for the whole search, which bounds
        its memory; ``truncated`` tells whether that limit cut it short, and
        ``complete_depth`` up to which length the chains are complete then.
        """
        # Module index per static region; True where chains must not start.
        excluded = np.array(
            [base_modules is not None and name not in base_modules
             for name in self._static_modules],
            dtype=bool,
        )
        reach = self._reach(max_depth, max_offset, excluded)
        graph = self._search(target, max_depth, max_offset, max_edges, excluded, reach,
                             progress, cancelled)
        distance = graph.distances(max_depth)
        chains = []
        for length in range(1, max_depth + 1):
            if len(chains) >= max_results:
                break
            self._collect(graph, 0, length, (), distance, chains, max_results, set(),
                          cancelled)
        chains.sort(key=lambda chain: (len(chain.offsets), chain.module, chain.base_offset,
                                       chain.offsets))
        return chains

    def location_rank(self):
        """Return each pointer's position when the pointers are sorted by location."""
        if self._location_rank is None:
            self._location_rank = np.argsort(np.argsort(self.locations, kind="stable"))
        return self._location_rank

    def _reach(self, max_depth, max_offset, excluded):
        """Return, by location order, the fewest pointers from static memory to each pointer.

        0 means the pointer lies in (allowed) static memory itself, ``max_depth``
        that its location cannot be reached within the search depth.
        """
        rank = self.location_rank()
        locations = np.empty_like(self.locations)
        locations[rank] = self.locations
        values = np.empty_like(self.values)
        values[rank] = self.values
        modules = self.static_modules(locations)
        reach = np.full(len(locations), max_depth, dtype=np.int8)
        static = modules >= 0
        if excluded.any():
            static &= ~excluded[np.maximum(modules, 0)]
        reach[static] = 0
        dynamic = np.flatnonzero(modules == -1)
        del modules
        current = static
        for distance in range(1, max_depth):
            # Addresses reachable now: [value, value + max_offset] of the last step.
            starts = np.sort(values[current])
            if not starts.size or not dynamic.size:
                break
            ends = starts + np.uint64(max_offset)
            begins = np.concatenate(([True], starts[1:] > ends[:-1]))
            starts = starts[begins]
            ends = ends[np.concatenate((begins[1:], [True]))]
            candidates = locations[dynamic]
            index = np.searchsorted(starts, candidates, side="right") - 1
            inside = (index >= 0) & (candidates <= ends[np.maximum(index, 0)])
            current = np.zeros(len(locations), dtype=bool)
            current[dynamic[inside]] = True
            reach[current] = distance
            dynamic = dynamic[~inside]
        return reach

    def _search(self, target, max_depth, max_offset, max_edges, excluded, reach,
                progress, cancelled):
        # pylint: disable=too-many-arguments,too-many-locals
        """Search backwards from ``target``: which pointers lead to each address?"""
        self.truncated = False
        self.complete_depth = None
        frontier = np.array([target], dtype=np.uint64)
        seen = frontier.copy()
        nodes, counts, locations, offsets, modules = [], [], [], [], []
        edges_left = max_edges
        for level in range(max_depth):
            if cancelled is not None and cancelled():
                raise ScanCancelled()
            if progress is not None:
                progress(level + 1)
            if not frontier.size:
                break
            low = np.searchsorted(
                self.values,
                np.where(frontier > np.uint64(max_offset), frontier - np.uint64(max_offset), 0)
                .astype(np.uint64),
                side="left",
            )
            raw_counts = np.searchsorted(self.values, frontier, side="right") - low
            # Pointers at a dynamic location may follow if static memory is
            # at most this many pointers away from it.
            depth_left = max_depth - 1 - level
            level_counts, followed = [], []
            searched = 0
            for begin, end in _batches(np.cumsum(raw_counts), _EDGE_BATCH):
                batch_counts, *batch = self._edges(
                    frontier[begin:end], low[begin:end], raw_counts[begin:end],
                    excluded, reach, depth_left,
                )
                if len(batch[0]) > edges_left:
                    # Chains through the rest of this level are missing.
                    self.truncated = True
                    self.complete_depth = level
                    break
                edges_left -= len(batch[0])
                level_counts.append(batch_counts)
                locations.append(batch[0])
                offsets.append(batch[1])
                modules.append(batch[2])
                followed.append(np.unique(batch[0][batch[2] == -1]))
                searched = end
                if cancelled is not None and cancelled():
                    raise ScanCancelled()
            nodes.append(frontier[:searched])
            counts.append(
                np.concatenate(level_counts) if level_counts else np.empty(0, dtype=np.int64)
            )
            if self.truncated or not followed:
                break
            following = np.unique(np.concatenate(followed))
            following = np.setdiff1d(following, seen, assume_unique=True)
            seen = np.union1d(seen, following)
            frontier = following
        return _SearchGraph.build(nodes, counts, locations, offsets, modules)

    def _edges(self, nodes, low, counts, excluded, reach, depth_left):
        # pylint: disable=too-many-arguments
        """Return the pointers to ``nodes`` that may be part of a chain.

        Returns the number per node and, per pointer, location, offset and
        module (-1 for dynamic memory).
        """
        total = int(counts.sum())
        index = np.arange(total, dtype=np.int64) + np.repeat(
            low.astype(np.int64) - (np.cumsum(counts) - counts), counts
        )
        locations = self.locations[index]
        modules = self.static_modules(locations).astype(np.int32)
        if excluded.any():
            modules[(modules >= 0) & excluded[np.maximum(modules, 0)]] = -2
        useful = modules >= 0
        if depth_left > 0:
            useful |= (modules == -1) & (reach[self.location_rank()[index]] <= depth_left)
        owner = np.repeat(np.arange(len(nodes)), counts)[useful]
        offsets = (nodes[owner] - self.values[index[useful]]).astype(np.int64)
        return (np.bincount(owner, minlength=len(nodes)), locations[useful], offsets,
                modules[useful])

    def _collect(self, graph, node, remaining, suffix, distance, chains, limit, path,
                 cancelled):
        # pylint: disable=too-many-arguments
        """Add the chains to ``node`` with exactly ``remaining`` pointers."""
        begin, end = int(graph.first[node]), int(graph.first[node + 1])
        if begin == end or len(chains) >= limit:
            return
        if cancelled is not None and cancelled():
            raise ScanCancelled()
        path.add(node)
        modules = graph.modules[begin:end]
        offsets = graph.offsets[begin:end]
        if remaining == 1:
            for edge in np.flatnonzero(modules >= 0).tolist():
                if len(chains) >= limit:
                    break
                name = self._static_modules[modules[edge]]
                location = int(graph.locations[begin + edge])
                chains.append(PointerChain(
                    name, location - self.bases[name], (int(offsets[edge]),) + suffix
                ))
        else:
            children = graph.children[begin:end]
            useful = children >= 0
            useful[useful] = distance[children[useful]] <= remaining - 1
            for edge in np.flatnonzero(useful).tolist():
                if len(chains) >= limit:
                    break
                child = int(children[edge])
                if child not in path:
                    self._collect(graph, child, remaining - 1,
                                  (int(offsets[edge]),) + suffix, distance, chains,
                                  limit, path, cancelled)
        path.discard(node)


def _batches(ends, size):
    """Split nodes with cumulative edge counts ``ends`` into batches of ~``size`` edges."""
    if not ends.size:
        return []
    splits = np.searchsorted(ends, np.arange(size, int(ends[-1]), size), side="right")
    bounds = np.unique(np.concatenate(([0], splits, [len(ends)])))
    return list(zip(bounds[:-1].tolist(), bounds[1:].tolist()))


@dataclass
class _SearchGraph:
    """Pointers found by a search, stored per address in flat arrays.

    Node ``i`` (node 0 is the target) has the edges ``first[i]:first[i + 1]``:
    a pointer at ``locations[e]`` plus ``offsets[e]`` leads to the node. Its
    ``modules[e]`` is the static module index, -1 for dynamic memory and -2
    for excluded modules; ``children[e]`` is the node of a dynamic location,
    or -1 if it was not searched.
    """

    first: np.ndarray
    locations: np.ndarray
    offsets: np.ndarray
    modules: np.ndarray
    children: np.ndarray

    @classmethod
    def build(cls, nodes, counts, locations, offsets, modules):
        """Join the arrays of all levels and link dynamic locations to their nodes."""
        def joined(parts, dtype):
            return np.concatenate(parts) if parts else np.empty(0, dtype=dtype)

        nodes = joined(nodes, np.uint64)
        first = np.concatenate(([0], np.cumsum(joined(counts, np.int64))))
        locations = joined(locations, np.uint64)
        offsets = joined(offsets, np.int64)
        modules = joined(modules, np.int32)
        children = np.full(len(locations), -1, dtype=np.int64)
        if len(nodes):
            order = np.argsort(nodes, kind="stable")
            ordered = nodes[order]
            dynamic = np.flatnonzero(modules == -1)
            position = np.searchsorted(ordered, locations[dynamic])
            position = np.minimum(position, len(ordered) - 1)
            found = ordered[position] == locations[dynamic]
            children[dynamic[found]] = order[position[found]]
        return cls(first, locations, offsets, modules, children)

    def distances(self, max_depth):
        """Return the fewest pointers from static memory to each node.

        Used to skip nodes no chain leads to; more than ``max_depth`` means none.
        """
        node_count = len(self.first) - 1
        unreachable = max_depth + 1
        distance = np.full(node_count, unreachable, dtype=np.int64)
        if not node_count:
            return distance
        owner = np.repeat(np.arange(node_count), np.diff(self.first))
        distance[np.unique(owner[self.modules >= 0])] = 1
        dynamic = np.flatnonzero(self.children >= 0)
        sources = owner[dynamic]
        targets = self.children[dynamic]
        del owner
        if not len(dynamic):
            return distance
        # Edges are grouped by their source node.
        starts = np.flatnonzero(np.concatenate(([True], sources[1:] != sources[:-1])))
        grouped = sources[starts]
        for _ in range(max_depth):
            best = np.minimum.reduceat(distance[targets] + 1, starts)
            updated = np.minimum(distance[grouped], best)
            if np.array_equal(updated, distance[grouped]):
                break
            distance[grouped] = updated
        return np.minimum(distance, unreachable)


def filter_chains(pid, chains, target):
    """Return the chains that lead to ``target`` in the (restarted) process."""
    bases = chain_bases(pid, read_mappings(pid))
    with ProcessMemory(pid) as memory:
        return [chain for chain in chains if resolve_chain(memory, bases, chain) == target]
