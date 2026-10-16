"""Background worker for scanning a Linux process' memory."""

import itertools
import struct

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

_VALUE_FORMATS = {
    "int32": "i",
    "int64": "q",
    "float": "f",
    "double": "d",
}

# Scan modes that compare against the value from the previous scan.
_PREVIOUS_VALUE_MODES = {"changed", "unchanged", "increased", "decreased"}

_NUMPY_DTYPES = {
    "int32": np.dtype("<i4"),
    "int64": np.dtype("<i8"),
    "float": np.dtype("<f4"),
    "double": np.dtype("<f8"),
}


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


class ScanWorker(QThread):
    """Scan readable process mappings without blocking the UI thread."""

    # Either a list of result dicts or a MemorySnapshot.
    results_ready = pyqtSignal(object)
    scan_failed = pyqtSignal(str)
    scan_cancelled = pyqtSignal()
    progress_changed = pyqtSignal(int)

    _VALUE_FORMATS = _VALUE_FORMATS
    _NUMPY_DTYPES = _NUMPY_DTYPES
    _CHUNK_SIZE = 1024 * 1024
    # Snapshots shrinking to this many candidates become a plain result list.
    _LIST_THRESHOLD = 100_000

    def __init__(
        self,
        pid,
        value_type,
        scan_mode,
        value="",
        value2="",
        previous_results=None,
        aligned=True,
        parent=None,
    ):
        super().__init__(parent)
        self.pid = pid
        self.value_type = value_type
        self.scan_mode = scan_mode
        self.value = value
        self.value2 = value2
        self.previous_results = previous_results or []
        self.aligned = aligned

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
            raise ValueError("Auto data type requires a value-based first scan")

        value_types = tuple(self._VALUE_FORMATS) if auto_types else (self.value_type,)
        formats = {
            value_type: self._get_value_format_for(value_type)
            for value_type in value_types
        }
        targets = {
            value_type: self._parse_scan_value(self.value, value_format)
            for value_type, value_format in formats.items()
            if self.scan_mode not in {"unknown_start"}
        }
        uppers = {
            value_type: self._parse_scan_value(self.value2, value_format)
            for value_type, value_format in formats.items()
            if self.scan_mode == "between"
        }
        results = []
        if self.isInterruptionRequested():
            return None

        with open(f"/proc/{self.pid}/maps", encoding="ascii") as maps_file:
            mappings = self._writable_mappings(maps_file)

        total_bytes = sum(end - start for start, end in mappings)
        if not total_bytes:
            self.progress_changed.emit(100)
            return results

        if self.scan_mode == "unknown_start":
            return self._snapshot_scan(mappings, total_bytes)

        processed_bytes = 0
        last_progress = -1
        max_value_size = max(
            struct.calcsize(value_format) for value_format in formats.values()
        )
        with open(f"/proc/{self.pid}/mem", "rb", buffering=0) as memory:
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
                    try:
                        memory.seek(position)
                        block = memory.read(read_size)
                    except OSError:
                        block = b""

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
                        matches = self._matches_array(
                            values,
                            targets.get(value_type),
                            uppers.get(value_type),
                        )
                        if values.dtype.kind == "f":
                            matches &= np.isfinite(values)
                        offsets = np.flatnonzero(matches)
                        results.extend(
                            {
                                "address": position + first_offset + int(offset) * stride,
                                "type": value_type,
                                "value": values[offset].item(),
                            }
                            for offset in offsets
                        )

                    position += chunk_size
                    processed_bytes += chunk_size
                    progress = min(100, processed_bytes * 100 // total_bytes)
                    if progress != last_progress:
                        self.progress_changed.emit(progress)
                        last_progress = progress

        return results

    def _snapshot_scan(self, mappings, total_bytes):
        """Copy all candidate memory instead of materializing every address."""
        dtype = self._get_numpy_dtype_for(self.value_type)
        stride = dtype.itemsize if self.aligned else 1
        # Empty snapshot used only to lay out each block's candidate values.
        layout = MemorySnapshot(self.value_type, stride, [])
        snapshot_blocks = []
        processed_bytes = 0
        last_progress = -1
        with open(f"/proc/{self.pid}/mem", "rb", buffering=0) as memory:
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
            try:
                memory.seek(position)
                data = memory.read(size)
            except OSError:
                data = b""
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
        value_format = self._get_value_format_for(snapshot.value_type)
        targets = (None, None)
        if self.scan_mode not in _PREVIOUS_VALUE_MODES | {"unknown_start"}:
            targets = (
                self._parse_scan_value(self.value, value_format),
                self._parse_scan_value(self.value2, value_format)
                if self.scan_mode == "between" else None,
            )
        blocks = []
        total_bytes = sum(len(data) for _start, data, _mask in snapshot.blocks)
        processed_bytes = 0
        last_progress = -1
        with open(f"/proc/{self.pid}/mem", "rb", buffering=0) as memory:
            for start, data, mask in snapshot.blocks:
                if self.isInterruptionRequested():
                    return None
                processed_bytes += len(data)
                try:
                    memory.seek(start)
                    current_data = memory.read(len(data))
                except OSError:
                    current_data = b""
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
        return list(results) if len(results) <= self._LIST_THRESHOLD else results

    def _refine_scan(self):
        if isinstance(self.previous_results, MemorySnapshot):
            return self._refine_snapshot(self.previous_results)
        value_types = {
            entry["type"] if self.value_type == "auto" else self.value_type
            for entry in self.previous_results
        }
        formats = {
            value_type: self._get_value_format_for(value_type)
            for value_type in value_types
        }
        targets = {
            value_type: self._parse_scan_value(self.value, value_format)
            for value_type, value_format in formats.items()
            if self.scan_mode not in _PREVIOUS_VALUE_MODES | {"unknown_start"}
        }
        uppers = {
            value_type: self._parse_scan_value(self.value2, value_format)
            for value_type, value_format in formats.items()
            if self.scan_mode == "between"
        }
        results = []

        with open(f"/proc/{self.pid}/mem", "rb", buffering=0) as memory:
            total = len(self.previous_results)
            last_progress = -1
            for index, previous in enumerate(self.previous_results, start=1):
                if self.isInterruptionRequested():
                    return None
                address = previous["address"]
                value_type = (
                    previous["type"] if self.value_type == "auto" else self.value_type
                )
                value_format = formats[value_type]
                value_size = struct.calcsize(value_format)
                try:
                    memory.seek(address)
                    block = memory.read(value_size)
                    if len(block) != value_size:
                        continue
                    current = struct.unpack(value_format, block)[0]
                except OSError:
                    continue

                if value_type in {"float", "double"} and not np.isfinite(current):
                    continue
                previous_value = previous["value"]
                if self._matches(
                    current,
                    targets.get(value_type),
                    uppers.get(value_type),
                    previous_value,
                ):
                    results.append({
                        "address": address,
                        "type": value_type,
                        "value": current,
                    })
                progress = index * 100 // total
                if progress != last_progress:
                    self.progress_changed.emit(progress)
                    last_progress = progress

        if not self.previous_results:
            self.progress_changed.emit(100)

        return results

    def _get_value_format(self):
        return self._get_value_format_for(self.value_type)

    @classmethod
    def _get_value_format_for(cls, value_type):
        try:
            return "<" + cls._VALUE_FORMATS[value_type]
        except KeyError as error:
            raise ValueError(f"Unsupported data type: {value_type}") from error

    def _get_numpy_dtype(self):
        return self._get_numpy_dtype_for(self.value_type)

    @classmethod
    def _get_numpy_dtype_for(cls, value_type):
        try:
            return cls._NUMPY_DTYPES[value_type]
        except KeyError as error:
            raise ValueError(f"Unsupported data type: {value_type}") from error

    @staticmethod
    def _writable_mappings(maps_file):
        """Return private writable mappings, including the .data/.bss of binaries."""
        mappings = []
        for line in maps_file:
            fields = line.split(maxsplit=5)
            if len(fields) < 5:
                continue
            bounds, permissions = fields[:2]
            pathname = fields[5].strip() if len(fields) == 6 else ""
            # Static/global variables live in file-backed rw-p mappings of the
            # executable and its libraries, so those must be scanned as well.
            is_excluded = pathname.startswith("/dev/") or pathname in {
                "[vvar]", "[vvar_vclock]", "[vsyscall]", "[vdso]"
            }
            if not is_excluded and all(flag in permissions for flag in "rwp"):
                start, end = (int(bound, 16) for bound in bounds.split("-"))
                mappings.append((start, end))
        return mappings

    @staticmethod
    def _parse_scan_value(value, value_format):
        try:
            parsed = (
                int(value, 0)
                if value_format[-1] in {"i", "q"}
                else float(value)
            )
            return struct.unpack(value_format, struct.pack(value_format, parsed))[0]
        except (AttributeError, OverflowError, TypeError, ValueError, struct.error) as error:
            raise ValueError(f"Invalid scan value: {value!r}") from error

    def _matches(self, current, target, upper, previous=None):
        if self.scan_mode == "unknown_start":
            return True
        if self.scan_mode == "changed":
            return current != previous
        if self.scan_mode == "unchanged":
            return current == previous
        if self.scan_mode == "increased":
            return current > previous
        if self.scan_mode == "decreased":
            return current < previous
        if self.scan_mode == "exact":
            return current == target
        if self.scan_mode == "greater_than":
            return current > target
        if self.scan_mode == "less_than":
            return current < target
        if self.scan_mode == "between":
            return target <= current <= upper
        raise ValueError(f"Unsupported scan mode: {self.scan_mode}")

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
        if self.scan_mode == "exact":
            return values == target
        if self.scan_mode == "greater_than":
            return values > target
        if self.scan_mode == "less_than":
            return values < target
        if self.scan_mode == "between":
            return (values >= target) & (values <= upper)
        raise ValueError(f"Unsupported scan mode: {self.scan_mode}")


class ValueRefreshWorker(QThread):
    """Read current values for a small set of known process addresses."""

    values_ready = pyqtSignal(dict)
    refresh_failed = pyqtSignal(str)

    def __init__(self, pid, entries, parent=None):
        super().__init__(parent)
        self.pid = pid
        self.entries = entries

    def run(self):
        values = {}
        try:
            with open(f"/proc/{self.pid}/mem", "rb", buffering=0) as memory:
                for entry in self.entries:
                    if self.isInterruptionRequested():
                        return
                    value_format = self._get_value_format(entry["type"])
                    address = entry["address"]
                    try:
                        memory.seek(address)
                        block = memory.read(struct.calcsize(value_format))
                    except OSError:
                        continue
                    if len(block) == struct.calcsize(value_format):
                        values[address] = struct.unpack(value_format, block)[0]
            self.values_ready.emit(values)
        except (OSError, ValueError, struct.error) as error:
            if not self.isInterruptionRequested():
                self.refresh_failed.emit(str(error))

    @staticmethod
    def _get_value_format(value_type):
        try:
            return "<" + _VALUE_FORMATS[value_type]
        except KeyError as error:
            raise ValueError(f"Unsupported data type: {value_type}") from error
