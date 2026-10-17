"""Pointer scan: find pointer chains from static memory to an address.

Absolute addresses of heap objects change with every start. A pointer chain
starts at a static location (inside a module's writable data, which stays at
the same module + offset) and follows pointers to the target::

    address = module_base + base_offset
    for offset in offsets:
        address = read_pointer(address) + offset

The scan reads all writable memory of the process once and keeps every
aligned 8-byte value that points into writable memory (the pointer map,
sorted by value). Starting at the target it then searches backwards: which
locations hold a pointer at most ``max_offset`` below the current address?
Locations in static memory end a chain, all others become the addresses of
the next level, up to ``max_depth`` levels.

A first scan usually finds many chains. Chains that still lead to the value
after the game was restarted (``filter_chains``) are the stable ones.
"""

import os
from dataclasses import dataclass

import numpy as np


POINTER_SIZE = 8
_CHUNK_SIZE = 16 * 1024 * 1024
_EXCLUDED = {"[vvar]", "[vvar_vclock]", "[vsyscall]", "[vdso]"}


class ScanCancelled(Exception):
    """Raised when a pointer scan is cancelled."""


@dataclass(frozen=True)
class Mapping:
    """One line of /proc/<pid>/maps."""

    start: int
    end: int
    permissions: str
    path: str

    @property
    def writable(self):
        """Private writable memory, where data and pointers live."""
        return (
            "r" in self.permissions and "w" in self.permissions and "p" in self.permissions
            and not self.path.startswith("/dev/") and self.path not in _EXCLUDED
        )


@dataclass(frozen=True)
class PointerChain:
    """``module + base_offset``, then dereference and add each offset."""

    module: str
    base_offset: int
    offsets: tuple = ()

    def text(self):
        """Readable form, e.g. ``game+0x2a1f0 → 0x18 → 0x40``."""
        parts = [f"{self.module}+0x{self.base_offset:x}"]
        parts += [f"{offset:#x}" if offset >= 0 else f"-{-offset:#x}" for offset in self.offsets]
        return " → ".join(parts)

    def to_json(self):
        return {"module": self.module, "base_offset": self.base_offset,
                "offsets": list(self.offsets)}

    @classmethod
    def from_json(cls, data):
        return cls(data["module"], int(data["base_offset"]),
                   tuple(int(offset) for offset in data.get("offsets", ())))


def read_mappings(pid):
    """Parse /proc/<pid>/maps."""
    mappings = []
    with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as maps_file:
        for line in maps_file:
            fields = line.split(maxsplit=5)
            if len(fields) < 5:
                continue
            start, end = (int(bound, 16) for bound in fields[0].split("-"))
            path = fields[5].strip() if len(fields) == 6 else ""
            mappings.append(Mapping(start, end, fields[1], path))
    return mappings


def module_name(path):
    """Name used for a module in chains (the file name)."""
    return os.path.basename(path)


def module_bases(mappings):
    """Return ``{module name: load address}`` for file-backed mappings."""
    bases = {}
    for mapping in mappings:
        if mapping.path.startswith("/"):
            name = module_name(mapping.path)
            bases[name] = min(bases.get(name, mapping.start), mapping.start)
    return bases


def static_regions(mappings):
    """Return ``[(start, end, module)]`` of writable module memory.

    That is a module's file-backed writable mapping (.data) and an anonymous
    writable mapping right behind it (.bss, if it does not fit the file).
    """
    regions = []
    previous = None
    for mapping in sorted(mappings, key=lambda m: m.start):
        if mapping.writable:
            if mapping.path.startswith("/"):
                regions.append((mapping.start, mapping.end, module_name(mapping.path)))
            elif not mapping.path and previous is not None and previous.path.startswith("/") \
                    and previous.end == mapping.start:
                regions.append((mapping.start, mapping.end, module_name(previous.path)))
        previous = mapping
    return regions


def static_location(regions, address):
    """Return the module whose static memory contains ``address``, or None."""
    for start, end, module in regions:
        if start <= address < end:
            return module
    return None


class ProcessMemory:
    """Read pointers and values of a process via /proc/<pid>/mem."""

    def __init__(self, pid):
        self.pid = pid
        self._file = open(f"/proc/{pid}/mem", "rb", buffering=0)  # pylint: disable=consider-using-with

    def close(self):
        """Close the memory file."""
        self._file.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def read(self, address, size):
        """Return ``size`` bytes or None if the memory cannot be read."""
        if address < 0:
            return None
        try:
            self._file.seek(address)
            data = self._file.read(size)
        except (OSError, OverflowError, ValueError):
            return None
        return data if len(data) == size else None

    def read_pointer(self, address):
        """Return the 8-byte pointer at ``address`` or None."""
        data = self.read(address, POINTER_SIZE)
        return None if data is None else int.from_bytes(data, "little")


def resolve_chain(memory, bases, chain):
    """Follow ``chain`` in a process; return the final address or None."""
    base = bases.get(chain.module)
    if base is None:
        return None
    address = base + chain.base_offset
    for offset in chain.offsets:
        pointer = memory.read_pointer(address)
        if not pointer:
            return None
        address = pointer + offset
    return address


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

    def __init__(self, values, locations, mappings):
        self.values = values
        self.locations = locations
        self.mappings = mappings
        self.bases = module_bases(mappings)
        regions = static_regions(mappings)
        self._static_starts = np.array([r[0] for r in regions], dtype=np.uint64)
        self._static_ends = np.array([r[1] for r in regions], dtype=np.uint64)
        self._static_modules = [r[2] for r in regions]

    def __len__(self):
        return len(self.values)

    @classmethod
    def build(cls, pid, progress=None, cancelled=None):
        """Read the process memory; ``progress(percent)`` reports the reading."""
        mappings = read_mappings(pid)
        writable = sorted((m for m in mappings if m.writable), key=lambda m: m.start)
        starts = np.array([m.start for m in writable], dtype=np.uint64)
        ends = np.array([m.end for m in writable], dtype=np.uint64)
        total = sum(m.end - m.start for m in writable) or 1
        done = 0
        values, locations = [], []
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
            for mapping in writable:
                position = mapping.start
                while position < mapping.end:
                    if cancelled is not None and cancelled():
                        raise ScanCancelled()
                    size = min(_CHUNK_SIZE, mapping.end - position)
                    try:
                        memory.seek(position)
                        block = memory.read(size)
                    except OSError:
                        block = b""
                    count = len(block) // POINTER_SIZE
                    if count:
                        words = np.frombuffer(block, dtype="<u8", count=count)
                        index = np.searchsorted(starts, words, side="right") - 1
                        valid = (index >= 0) & (words < ends[np.maximum(index, 0)])
                        hits = np.flatnonzero(valid)
                        if hits.size:
                            values.append(words[hits])
                            locations.append(
                                np.uint64(position) + hits.astype(np.uint64) * np.uint64(POINTER_SIZE)
                            )
                    position += size
                    done += size
                    if progress is not None:
                        progress(done * 100 // total)
        if values:
            values = np.concatenate(values)
            locations = np.concatenate(locations)
            order = np.argsort(values, kind="stable")
            values, locations = values[order], locations[order]
        else:
            values = np.empty(0, dtype=np.uint64)
            locations = np.empty(0, dtype=np.uint64)
        return cls(values, locations, mappings)

    def static_modules(self, locations):
        """Return the module index per location, -1 for dynamic memory."""
        if not len(self._static_starts):
            return np.full(len(locations), -1)
        index = np.searchsorted(self._static_starts, locations, side="right") - 1
        inside = (index >= 0) & (locations < self._static_ends[np.maximum(index, 0)])
        return np.where(inside, index, -1)

    def find_chains(self, target, max_depth=5, max_offset=0x1000, max_results=10000,
                    max_nodes=2_000_000, progress=None, cancelled=None):
        # pylint: disable=too-many-arguments,too-many-locals
        """Return chains from static memory to ``target``, shortest first.

        ``progress(level)`` is called for each searched level. At most
        ``max_nodes`` addresses are followed per level.
        """
        # incoming[address] = (locations, offsets, module index or -1)
        incoming = {}
        frontier = np.array([target], dtype=np.uint64)
        seen = frontier.copy()
        levels = []
        for level in range(max_depth):
            if cancelled is not None and cancelled():
                raise ScanCancelled()
            if progress is not None:
                progress(level + 1)
            if not frontier.size:
                break
            levels.append(frontier)
            low = np.searchsorted(
                self.values,
                np.where(frontier > np.uint64(max_offset), frontier - np.uint64(max_offset), 0)
                .astype(np.uint64),
                side="left",
            )
            high = np.searchsorted(self.values, frontier, side="right")
            counts = high - low
            total = int(counts.sum())
            if not total:
                break
            starts = np.repeat(low, counts)
            steps = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
            index = starts + steps
            nodes = np.repeat(frontier, counts)
            locations = self.locations[index]
            offsets = (nodes - self.values[index]).astype(np.int64)
            modules = self.static_modules(locations)
            boundaries = np.cumsum(counts)
            begin = 0
            for node, end in zip(frontier.tolist(), boundaries.tolist()):
                if end > begin:
                    incoming[node] = (locations[begin:end], offsets[begin:end], modules[begin:end])
                begin = end
            following = np.unique(locations[modules < 0])
            following = following[~np.isin(following, seen, assume_unique=True)]
            if following.size > max_nodes:
                following = following[:max_nodes]
            seen = np.union1d(seen, following)
            frontier = following

        distance = self._distances(incoming, levels)
        chains = []
        self._collect(target, max_depth, (), incoming, distance, chains, max_results, set())
        chains.sort(key=lambda chain: (len(chain.offsets), chain.module, chain.base_offset,
                                       chain.offsets))
        return chains

    @staticmethod
    def _distances(incoming, levels):
        """Return the fewest pointers from static memory to each address.

        Used to skip addresses no chain leads to. Repeated until stable, since
        an address may point to one that was seen on an earlier level.
        """
        nodes = [node for frontier in reversed(levels) for node in frontier.tolist()
                 if node in incoming]
        distance = {}
        for _ in range(len(levels)):
            changed = False
            for node in nodes:
                locations, _offsets, modules = incoming[node]
                best = 1 if (modules >= 0).any() else None
                for location in locations[modules < 0].tolist():
                    below = distance.get(location)
                    if below is not None and (best is None or below + 1 < best):
                        best = below + 1
                if best is not None and best != distance.get(node):
                    distance[node] = best
                    changed = True
            if not changed:
                break
        return distance

    def _collect(self, node, remaining, suffix, incoming, distance, chains, limit, path):
        # pylint: disable=too-many-arguments
        edges = incoming.get(node)
        if edges is None or len(chains) >= limit:
            return
        path.add(node)
        locations, offsets, modules = edges
        for location, offset, module in zip(locations.tolist(), offsets.tolist(),
                                            modules.tolist()):
            if len(chains) >= limit:
                break
            chain_offsets = (offset,) + suffix
            if module >= 0:
                name = self._static_modules[module]
                chains.append(PointerChain(name, location - self.bases[name], chain_offsets))
            elif remaining > 1 and location not in path \
                    and distance.get(location, remaining) <= remaining - 1:
                self._collect(location, remaining - 1, chain_offsets, incoming, distance,
                              chains, limit, path)
        path.discard(node)


def filter_chains(pid, chains, target):
    """Return the chains that lead to ``target`` in the (restarted) process."""
    bases = module_bases(read_mappings(pid))
    with ProcessMemory(pid) as memory:
        return [chain for chain in chains if resolve_chain(memory, bases, chain) == target]
