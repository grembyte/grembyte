"""Background worker for scanning a process' memory."""

import itertools
import struct
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import numpy as np
from PySide6.QtCore import QThread, Signal  # pylint: disable=import-error,no-name-in-module

from grembyte.core import logs
from grembyte.core.messages import LocalizedValueError
from grembyte.core.pointer import chain_bases, resolve_chain
from grembyte.platform import ProcessMemory, read_mappings
from grembyte.core.value_types import AUTO_VALUE_TYPES, VALUE_TYPES
from grembyte.core.value_types import value_type as lookup_value_type

# struct format characters without byte order, e.g. "i" for int32.
_VALUE_FORMATS = {name: kind.struct_format[1:] for name, kind in VALUE_TYPES.items()}

# Scan modes that compare the change since the previous scan with a value.
_DELTA_MODES = {"increased_by", "decreased_by", "changed_by"}
# Scan modes that compare against the value from the previous scan.
PREVIOUS_VALUE_MODES = {"changed", "unchanged", "increased", "decreased"} | _DELTA_MODES
# Scan modes whose value may be matched with a tolerance or rounded.
FUZZY_MODES = {"exact"} | _DELTA_MODES
# How a float search value is compared: as entered, rounded or truncated to
# the decimal places entered ("12.3" matches 12.25..12.35 or 12.3..12.4).
FLOAT_ROUNDING_MODES = ("exact", "rounded", "truncated")

_NUMPY_DTYPES = {name: kind.dtype for name, kind in VALUE_TYPES.items()}


@dataclass(frozen=True)
class ValueRange:
    """Values a fuzzy scan accepts: ``low`` to ``high`` with optional open ends.

    Works on scalars and NumPy arrays alike. Float ranges are compared in the
    precision of ``float_type``, so ``12.4`` means the float stored for 12.4.
    Integer changes are compared modulo 2**64 and never overflow the type.
    """

    low: int | float
    high: int | float
    float_type: type | None = None  # np.float32 / np.float64, None for integers
    include_low: bool = True
    include_high: bool = True

    @classmethod
    def parse(cls, text, kind, tolerance="", rounding="exact", delta=False):
        """Build the range for search ``text`` of ValueType ``kind``.

        ``delta`` ranges describe a change, which may exceed the type's range.
        Raises ValueError for invalid input.
        """
        tolerance = cls._parse_tolerance(tolerance, kind)
        try:
            center = (
                (int(text, 0) if delta else kind.parse(text))
                if kind.is_integer else float(text)
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise LocalizedValueError("error.scanner.invalid_value", text=repr(text)) from error
        if kind.is_integer:
            return cls(center - tolerance, center + tolerance)

        float_type = kind.dtype.type
        if rounding == "exact":
            bounds = (center - tolerance, center + tolerance, True, True)
        elif rounding == "rounded":
            step = 10.0 ** -cls._decimal_places(text)
            bounds = (center - step / 2 - tolerance, center + step / 2 + tolerance, True, False)
        elif rounding == "truncated":
            step = 10.0 ** -cls._decimal_places(text)
            # Truncation cuts towards zero, so negative values lie below.
            if text.strip().startswith("-"):
                bounds = (center - step - tolerance, center + tolerance, False, True)
            else:
                bounds = (center - tolerance, center + step + tolerance, True, False)
        else:
            raise ValueError(f"Unsupported float rounding: {rounding}")
        low, high, include_low, include_high = bounds
        with np.errstate(over="ignore"):
            low, high = float(float_type(low)), float(float_type(high))
        return cls(low, high, float_type, include_low, include_high)

    @staticmethod
    def _parse_tolerance(text, kind):
        if not text or not text.strip():
            return 0
        try:
            tolerance = int(text, 0) if kind.is_integer else float(text)
        except ValueError as error:
            raise LocalizedValueError("error.scanner.invalid_tolerance", text=repr(text)) from error
        if not tolerance >= 0:
            raise LocalizedValueError("error.scanner.invalid_tolerance", text=repr(text))
        return tolerance

    @staticmethod
    def _decimal_places(text):
        try:
            exponent = Decimal(text.strip()).as_tuple().exponent
        except InvalidOperation:
            return 0
        return max(0, -exponent) if isinstance(exponent, int) else 0

    def contains(self, values):
        """Return whether ``values`` (scalar or array) lie in the range."""
        above = values >= self.low if self.include_low else values > self.low
        below = values <= self.high if self.include_high else values < self.high
        return above & below

    def contains_change(self, current, previous):
        """Return whether ``current - previous`` lies in the range."""
        if self.float_type is not None:
            with np.errstate(over="ignore", invalid="ignore"):
                if isinstance(current, np.ndarray):
                    return self.contains(current - previous)
                return self.contains(
                    float(self.float_type(current) - self.float_type(previous))
                )
        span = min(self.high - self.low, 2**64 - 1)
        if isinstance(current, np.ndarray):
            change = current.astype(np.uint64) - previous.astype(np.uint64)
            return change - np.uint64(self.low % 2**64) <= np.uint64(span)
        return (current - previous - self.low) % 2**64 <= span


class ScanResults:
    """Scan candidates as arrays instead of one dict per address.

    ``groups`` maps each value type to ``(addresses, values)``: ascending
    addresses (uint64) and the values found there (in the type's dtype). A
    result dict costs about 240 bytes and a microsecond to create, which made
    scans for a common value slow and memory-hungry.

    Iterating, slicing and comparing with a list behave like the list of
    result dicts ``{"address", "type", "value"}``, ordered by address.
    """

    def __init__(self, groups=None):
        self.groups = {
            value_type: group for value_type, group in (groups or {}).items() if len(group[0])
        }

    @classmethod
    def join(cls, parts):
        """Build from ``{value_type: (address arrays, value arrays)}`` collected in address order."""
        return cls({
            value_type: (np.concatenate(addresses), np.concatenate(values))
            for value_type, (addresses, values) in parts.items() if addresses
        })

    @classmethod
    def from_dicts(cls, entries):
        """Build from result dicts."""
        by_type = {}
        for entry in entries:
            by_type.setdefault(entry["type"], []).append(entry)
        groups = {}
        for value_type, typed in by_type.items():
            addresses = np.array([entry["address"] for entry in typed], dtype=np.uint64)
            values = np.array([entry["value"] for entry in typed], dtype=_NUMPY_DTYPES[value_type])
            order = np.argsort(addresses, kind="stable")
            groups[value_type] = (addresses[order], values[order])
        return cls(groups)

    def __len__(self):
        return sum(len(addresses) for addresses, _values in self.groups.values())

    def __iter__(self):
        types = list(self.groups)
        if not types:
            return
        if len(types) == 1:
            positions = zip(itertools.repeat(0), range(len(self)))
        else:
            # Merge the groups by address; equal addresses keep the type order.
            kinds = np.concatenate([
                np.full(len(addresses), kind, dtype=np.uint8)
                for kind, (addresses, _values) in enumerate(self.groups.values())
            ])
            indexes = np.concatenate([
                np.arange(len(addresses)) for addresses, _values in self.groups.values()
            ])
            order = np.argsort(
                np.concatenate([addresses for addresses, _values in self.groups.values()]),
                kind="stable",
            )
            positions = zip(kinds[order].tolist(), indexes[order].tolist())
        for kind, index in positions:
            addresses, values = self.groups[types[kind]]
            yield {
                "address": int(addresses[index]),
                "type": types[kind],
                "value": values[index].item(),
            }

    def __getitem__(self, item):
        if not isinstance(item, slice):
            raise TypeError("ScanResults only supports slicing")
        return list(itertools.islice(self, item.start, item.stop, item.step))

    def __eq__(self, other):
        if isinstance(other, (list, ScanResults)):
            return list(self) == list(other)
        return NotImplemented

    __hash__ = None


class MemorySnapshot:
    """Raw copy of process memory standing in for a huge candidate list.

    An unknown-value scan would otherwise create one result per address.
    Each block is ``(start, data, mask)``: the bytes read at ``start`` and a
    boolean array of still-valid candidates, or ``None`` if all are valid.
    Iterating yields the same dicts as a regular result list.
    """

    def __init__(self, value_type, stride, blocks):
        self.value_type = value_type
        self.stride = stride
        self.blocks = blocks
        self._count = sum(
            len(self.values(start, data)[0]) if mask is None
            else int(np.count_nonzero(mask))
            for start, data, mask in blocks
        )

    def values(self, start, data):
        """Return a block's candidate values and the address of the first one."""
        dtype = _NUMPY_DTYPES[self.value_type]
        first_offset = (-start) % self.stride
        count = max(0, (len(data) - dtype.itemsize - first_offset) // self.stride + 1)
        if not count:
            return np.empty(0, dtype=dtype), start
        values = np.ndarray(
            shape=(count,),
            dtype=dtype,
            buffer=data,
            offset=first_offset,
            strides=(self.stride,),
        )
        return values, start + first_offset

    def __len__(self):
        return self._count

    def __iter__(self):
        for start, data, mask in self.blocks:
            values, base = self.values(start, data)
            indexes = np.flatnonzero(mask) if mask is not None else range(len(values))
            for index in indexes:
                yield {
                    "address": base + int(index) * self.stride,
                    "type": self.value_type,
                    "value": values[index].item(),
                }

    def __getitem__(self, item):
        if not isinstance(item, slice):
            raise TypeError("MemorySnapshot only supports slicing")
        return list(itertools.islice(self, item.start, item.stop, item.step))

    def to_results(self):
        """Return the remaining candidates as ScanResults."""
        addresses, values = [], []
        for start, data, mask in self.blocks:
            block_values, base = self.values(start, data)
            indexes = np.flatnonzero(mask) if mask is not None else np.arange(len(block_values))
            addresses.append(np.uint64(base) + indexes.astype(np.uint64) * np.uint64(self.stride))
            values.append(block_values[indexes])
        return ScanResults.join({self.value_type: (addresses, values)})


class ScanWorker(QThread):
    """Scan readable process mappings without blocking the UI thread."""

    # ScanResults or a MemorySnapshot.
    results_ready = Signal(object)
    scan_failed = Signal(str)
    scan_cancelled = Signal()
    progress_changed = Signal(int)

    _VALUE_FORMATS = _VALUE_FORMATS
    _NUMPY_DTYPES = _NUMPY_DTYPES
    _CHUNK_SIZE = 1024 * 1024
    # Snapshots shrinking to this many candidates become ScanResults.
    _LIST_THRESHOLD = 100_000
    # Follow-up scans read candidates at most this far apart in one go.
    _GAP = 4096

    def __init__(
        self,
        pid,
        value_type,
        scan_mode,
        value="",
        value2="",
        previous_results=None,
        aligned=True,
        tolerance="",
        float_rounding="exact",
        parent=None,
    ):
        super().__init__(parent)
        logs.log_failures(self)
        self.pid = pid
        self.value_type = value_type
        self.scan_mode = scan_mode
        self.value = value
        self.value2 = value2
        self.previous_results = previous_results or []
        self.aligned = aligned
        self.tolerance = tolerance
        self.float_rounding = float_rounding

    def run(self):
        try:
            results = (
                self._refine_scan()
                if self.previous_results
                else self._first_scan()
            )
            if results is None:
                self.scan_cancelled.emit()
            else:
                self.results_ready.emit(results)
        except (OSError, ValueError, struct.error) as error:
            if self.isInterruptionRequested():
                self.scan_cancelled.emit()
            else:
                self.scan_failed.emit(str(error))

    def _first_scan(self):
        auto_types = self.value_type == "auto"
        if auto_types and self.scan_mode not in {
            "exact", "greater_than", "less_than", "between"
        }:
            raise LocalizedValueError("error.scanner.auto_needs_value")
        if self.scan_mode in PREVIOUS_VALUE_MODES:
            raise LocalizedValueError("error.scanner.needs_previous")

        value_types = AUTO_VALUE_TYPES if auto_types else (self.value_type,)
        formats = {
            value_type: self._get_value_format_for(value_type)
            for value_type in value_types
        }
        targets = {value_type: self._scan_targets(value_type) for value_type in value_types}
        found = {value_type: ([], []) for value_type in value_types}
        if self.isInterruptionRequested():
            return None

        mappings = self._scan_ranges(read_mappings(self.pid))

        total_bytes = sum(end - start for start, end in mappings)
        if not total_bytes:
            self.progress_changed.emit(100)
            return ScanResults()

        if self.scan_mode == "unknown_start":
            return self._snapshot_scan(mappings, total_bytes)

        processed_bytes = 0
        last_progress = -1
        max_value_size = max(
            struct.calcsize(value_format) for value_format in formats.values()
        )
        with ProcessMemory(self.pid) as memory:
            for start, end in mappings:
                position = start
                while position < end:
                    if self.isInterruptionRequested():
                        return None
                    chunk_size = min(self._CHUNK_SIZE, end - position)
                    read_size = min(
                        chunk_size + max_value_size - 1,
                        end - position,
                    )
                    block = memory.read_available(position, read_size)

                    for value_type, value_format in formats.items():
                        value_size = struct.calcsize(value_format)
                        stride = value_size if auto_types or self.aligned else 1
                        first_offset = (-position) % stride
                        candidate_count = min(
                            max(0, (chunk_size - 1 - first_offset) // stride + 1),
                            max(0, (len(block) - value_size - first_offset) // stride + 1),
                            max(0, (end - position - value_size - first_offset) // stride + 1),
                        )
                        if not candidate_count:
                            continue

                        values = np.ndarray(
                            shape=(candidate_count,),
                            dtype=self._get_numpy_dtype_for(value_type),
                            buffer=block,
                            offset=first_offset,
                            strides=(stride,),
                        )
                        matches = self._matches_array(values, *targets[value_type])
                        if values.dtype.kind == "f":
                            matches &= np.isfinite(values)
                        offsets = np.flatnonzero(matches)
                        if offsets.size:
                            addresses, found_values = found[value_type]
                            addresses.append(
                                np.uint64(position + first_offset)
                                + offsets.astype(np.uint64) * np.uint64(stride)
                            )
                            found_values.append(values[offsets])

                    position += chunk_size
                    processed_bytes += chunk_size
                    progress = min(100, processed_bytes * 100 // total_bytes)
                    if progress != last_progress:
                        self.progress_changed.emit(progress)
                        last_progress = progress

        return ScanResults.join(found)

    def _snapshot_scan(self, mappings, total_bytes):
        """Copy all candidate memory instead of materializing every address."""
        dtype = self._get_numpy_dtype_for(self.value_type)
        stride = dtype.itemsize if self.aligned else 1
        # Empty snapshot used only to lay out each block's candidate values.
        layout = MemorySnapshot(self.value_type, stride, [])
        snapshot_blocks = []
        processed_bytes = 0
        last_progress = -1
        with ProcessMemory(self.pid) as memory:
            for start, end in mappings:
                blocks = self._read_blocks(memory, start, end)
                if blocks is None:
                    return None
                for block_start, data in blocks:
                    mask = None
                    if dtype.kind == "f":
                        mask = np.isfinite(layout.values(block_start, data)[0])
                    snapshot_blocks.append((block_start, data, mask))
                processed_bytes += end - start
                progress = min(100, processed_bytes * 100 // total_bytes)
                if progress != last_progress:
                    self.progress_changed.emit(progress)
                    last_progress = progress

        return MemorySnapshot(self.value_type, stride, snapshot_blocks)

    def _read_blocks(self, memory, start, end):
        """Read a mapping as contiguous readable blocks, or None if cancelled."""
        blocks = []
        block_start = None
        parts = []
        position = start
        while position < end:
            if self.isInterruptionRequested():
                return None
            size = min(self._CHUNK_SIZE, end - position)
            data = memory.read_available(position, size)
            if data:
                if block_start is None:
                    block_start = position
                parts.append(data)
            if len(data) < size and parts:
                blocks.append((block_start, b"".join(parts)))
                block_start = None
                parts = []
            position += size
        if parts:
            blocks.append((block_start, b"".join(parts)))
        return blocks

    def _refine_snapshot(self, snapshot):
        """Filter a snapshot by comparing it with the current memory."""
        targets = self._scan_targets(snapshot.value_type)
        blocks = []
        total_bytes = sum(len(data) for _start, data, _mask in snapshot.blocks)
        processed_bytes = 0
        last_progress = -1
        with ProcessMemory(self.pid) as memory:
            for start, data, mask in snapshot.blocks:
                if self.isInterruptionRequested():
                    return None
                processed_bytes += len(data)
                current_data = memory.read_available(start, len(data))
                previous, _base = snapshot.values(start, data)
                current, _base = snapshot.values(start, current_data)
                count = len(current)
                matches = self._matches_array(current, *targets, previous[:count])
                if current.dtype.kind == "f":
                    matches &= np.isfinite(current)
                if mask is not None:
                    matches &= mask[:count]
                if matches.any():
                    blocks.append((start, current_data, matches))
                progress = min(100, processed_bytes * 100 // max(1, total_bytes))
                if progress != last_progress:
                    self.progress_changed.emit(progress)
                    last_progress = progress

        self.progress_changed.emit(100)
        results = MemorySnapshot(snapshot.value_type, snapshot.stride, blocks)
        return results.to_results() if len(results) <= self._LIST_THRESHOLD else results

    def _refine_scan(self):
        if isinstance(self.previous_results, MemorySnapshot):
            return self._refine_snapshot(self.previous_results)
        previous = self.previous_results
        if not isinstance(previous, ScanResults):
            previous = ScanResults.from_dicts(previous)
        candidates = self._candidates(previous)
        found = {}
        total = max(1, len(previous))
        processed = 0
        last_progress = -1
        with ProcessMemory(self.pid) as memory:
            for value_type, (addresses, previous_values) in candidates.items():
                targets = self._scan_targets(value_type)
                dtype = self._get_numpy_dtype_for(value_type)
                kept_addresses, kept_values = [], []
                for begin, current in self._read_candidates(memory, addresses, dtype):
                    if self.isInterruptionRequested():
                        return None
                    end = begin + len(current)
                    matches = self._matches_array(current, *targets, previous_values[begin:end])
                    if current.dtype.kind == "f":
                        matches &= np.isfinite(current)
                    hits = np.flatnonzero(matches)
                    kept_addresses.append(addresses[begin:end][hits])
                    kept_values.append(current[hits])
                    progress = (processed + end) * 100 // total
                    if progress != last_progress:
                        self.progress_changed.emit(progress)
                        last_progress = progress
                found[value_type] = (kept_addresses, kept_values)
                processed += len(addresses)

        self.progress_changed.emit(100)
        return ScanResults.join(found)

    def _candidates(self, previous):
        """Return ``{value type: (addresses, previous values)}`` to compare now.

        A chosen data type reads every candidate as that type; "auto" keeps
        the type each one was found as. A previous value of another type is
        read anew from its bytes: an int32 as float, the low half of an int64
        as int32. Where the chosen type is wider, part of its bytes is not
        known; such candidates are kept only for scans that need no previous
        value. Each address is compared once, with a previous value of the
        chosen type where there is one.
        """
        if self.value_type == "auto" or not previous.groups:
            return previous.groups
        dtype = np.dtype(self._get_numpy_dtype_for(self.value_type)).newbyteorder("<")
        needs_previous = self.scan_mode in PREVIOUS_VALUE_MODES
        parts = []
        # The chosen type first: np.unique below keeps the first of an address.
        for value_type in sorted(previous.groups, key=lambda name: name != self.value_type):
            addresses, values = previous.groups[value_type]
            values = np.ascontiguousarray(values, dtype=values.dtype.newbyteorder("<"))
            if values.dtype == dtype:
                converted = values
            elif values.dtype.itemsize >= dtype.itemsize:
                raw = values.view(np.uint8).reshape(-1, values.dtype.itemsize)
                converted = np.ascontiguousarray(raw[:, :dtype.itemsize]).view(dtype).ravel()
            elif needs_previous:
                continue
            else:
                converted = np.zeros(len(addresses), dtype=dtype)  # compared with nothing
            parts.append((addresses, converted))
        if not parts:
            return {}
        addresses = np.concatenate([addresses for addresses, _values in parts])
        values = np.concatenate([values for _addresses, values in parts])
        _unique, first = np.unique(addresses, return_index=True)
        return {self.value_type: (addresses[first], values[first])}

    def _read_candidates(self, memory, addresses, dtype):
        """Yield ``(begin, values)``: the current values from ``addresses[begin]`` on.

        Candidates close together are read in one go. Unreadable ones are
        left out, so the values cover only part of the remaining addresses.
        """
        size = dtype.itemsize
        # Index of the last candidate before each wide gap.
        gaps = np.flatnonzero(np.diff(addresses) > np.uint64(self._GAP))
        count = len(addresses)
        begin = 0
        while begin < count:
            start = int(addresses[begin])
            limit = min(start + max(self._CHUNK_SIZE - size, 0), 2**64 - 1)
            end = max(begin + 1, int(np.searchsorted(addresses, np.uint64(limit), side="right")))
            gap = int(np.searchsorted(gaps, begin))
            if gap < len(gaps):
                end = min(end, int(gaps[gap]) + 1)
            block = memory.read_available(start, int(addresses[end - 1]) - start + size)
            offsets = (addresses[begin:end] - np.uint64(start)).astype(np.int64)
            readable = int(np.searchsorted(offsets + size, len(block), side="right"))
            if not readable:
                begin += 1
                continue
            raw = np.frombuffer(block, dtype=np.uint8)
            current = raw[offsets[:readable, None] + np.arange(size)].view(dtype).reshape(-1)
            yield begin, current
            begin += readable

    def _scan_targets(self, value_type):
        """Return ``(target, upper)`` of the search value(s) for a value type.

        Exact and change scans use a ValueRange as target, other value scans
        plain numbers; scans against the previous value need neither.
        """
        if self.scan_mode in FUZZY_MODES:
            return ValueRange.parse(
                self.value,
                lookup_value_type(value_type),
                self.tolerance,
                self.float_rounding,
                delta=self.scan_mode in _DELTA_MODES,
            ), None
        if self.scan_mode in PREVIOUS_VALUE_MODES | {"unknown_start"}:
            return None, None
        value_format = self._get_value_format_for(value_type)
        target = self._parse_scan_value(self.value, value_format)
        upper = (
            self._parse_scan_value(self.value2, value_format)
            if self.scan_mode == "between" else None
        )
        return target, upper

    def _get_value_format(self):
        return self._get_value_format_for(self.value_type)

    @classmethod
    def _get_value_format_for(cls, value_type):
        try:
            return "<" + cls._VALUE_FORMATS[value_type]
        except KeyError as error:
            raise LocalizedValueError("error.value_types.unsupported_type",
                                      type=value_type) from error

    def _get_numpy_dtype(self):
        return self._get_numpy_dtype_for(self.value_type)

    @classmethod
    def _get_numpy_dtype_for(cls, value_type):
        try:
            return cls._NUMPY_DTYPES[value_type]
        except KeyError as error:
            raise LocalizedValueError("error.value_types.unsupported_type",
                                      type=value_type) from error

    @staticmethod
    def _scan_ranges(mappings):
        """Return ``(start, end)`` of the writable mappings, the modules' .data/.bss included."""
        return [(mapping.start, mapping.end) for mapping in mappings if mapping.writable]

    @staticmethod
    def _parse_scan_value(value, value_format):
        try:
            parsed = (
                int(value, 0)
                if value_format[-1] not in "fd"
                else float(value)
            )
            return struct.unpack(value_format, struct.pack(value_format, parsed))[0]
        except (AttributeError, OverflowError, TypeError, ValueError, struct.error) as error:
            raise LocalizedValueError("error.scanner.invalid_value", text=repr(value)) from error

    def _matches_array(self, values, target, upper, previous=None):
        if self.scan_mode == "unknown_start":
            return np.ones(values.shape, dtype=np.bool_)
        if self.scan_mode == "changed":
            return values != previous
        if self.scan_mode == "unchanged":
            return values == previous
        if self.scan_mode == "increased":
            return values > previous
        if self.scan_mode == "decreased":
            return values < previous
        if self.scan_mode == "increased_by":
            return target.contains_change(values, previous)
        if self.scan_mode == "decreased_by":
            return target.contains_change(previous, values)
        if self.scan_mode == "changed_by":
            return (
                target.contains_change(values, previous)
                | target.contains_change(previous, values)
            )
        if self.scan_mode == "exact":
            return target.contains(values)
        if self.scan_mode == "greater_than":
            return values > target
        if self.scan_mode == "less_than":
            return values < target
        if self.scan_mode == "between":
            return (values >= target) & (values <= upper)
        raise ValueError(f"Unsupported scan mode: {self.scan_mode}")


class ValueRefreshWorker(QThread):
    """Read current values for a small set of known process addresses.

    ``values_ready`` maps ``(address, type)`` to the value: an address may be
    shown as several types. An entry with a ``pointer`` (PointerChain) is
    resolved first; its address is reported via ``addresses_resolved`` under
    the entry's ``key`` (None if the chain does not resolve right now).
    """

    values_ready = Signal(object)
    addresses_resolved = Signal(object)
    refresh_failed = Signal(str)

    def __init__(self, pid, entries, parent=None):
        super().__init__(parent)
        logs.log_failures(self)
        self.pid = pid
        self.entries = entries

    def run(self):
        values = {}
        resolved = {}
        try:
            bases = None
            if any(entry.get("pointer") is not None for entry in self.entries):
                bases = chain_bases(self.pid, read_mappings(self.pid))
            with ProcessMemory(self.pid) as memory:
                for entry in self.entries:
                    if self.isInterruptionRequested():
                        return
                    value_format = self._get_value_format(entry["type"])
                    address = entry.get("address")
                    if entry.get("pointer") is not None:
                        address = resolve_chain(memory, bases, entry["pointer"])
                        resolved[entry["key"]] = address
                    if address is None:
                        continue
                    block = memory.read(address, struct.calcsize(value_format))
                    if block is not None:
                        values[address, entry["type"]] = struct.unpack(value_format, block)[0]
            if resolved:
                self.addresses_resolved.emit(resolved)
            self.values_ready.emit(values)
        except (OSError, ValueError, struct.error) as error:
            if not self.isInterruptionRequested():
                self.refresh_failed.emit(str(error))

    @staticmethod
    def _get_value_format(value_type):
        try:
            return "<" + _VALUE_FORMATS[value_type]
        except KeyError as error:
            raise LocalizedValueError("error.value_types.unsupported_type",
                                      type=value_type) from error
