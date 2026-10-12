"""Background worker for scanning a Linux process' memory."""

import struct

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

_VALUE_FORMATS = {
    "int32": "i",
    "int64": "q",
    "float": "f",
    "double": "d",
}


class ScanWorker(QThread):
    """Scan readable process mappings without blocking the UI thread."""

    results_ready = pyqtSignal(list)
    scan_failed = pyqtSignal(str)
    scan_cancelled = pyqtSignal()
    progress_changed = pyqtSignal(int)

    _VALUE_FORMATS = _VALUE_FORMATS
    _NUMPY_DTYPES = {
        "int32": np.dtype("<i4"),
        "int64": np.dtype("<i8"),
        "float": np.dtype("<f4"),
        "double": np.dtype("<f8"),
    }
    _CHUNK_SIZE = 1024 * 1024

    def __init__(
        self,
        pid,
        value_type,
        scan_mode,
        value="",
        value2="",
        previous_results=None,
        parent=None,
    ):
        super().__init__(parent)
        self.pid = pid
        self.value_type = value_type
        self.scan_mode = scan_mode
        self.value = value
        self.value2 = value2
        self.previous_results = previous_results or []

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
            mappings = self._anonymous_mappings(maps_file)

        total_bytes = sum(end - start for start, end in mappings)
        if not total_bytes:
            self.progress_changed.emit(100)
            return results

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
                        stride = value_size if auto_types else 1
                        first_offset = (-position) % stride if auto_types else 0
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

    def _refine_scan(self):
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
            if self.scan_mode not in {"changed", "unchanged", "unknown_start"}
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
    def _anonymous_mappings(maps_file):
        mappings = []
        for line in maps_file:
            fields = line.split(maxsplit=5)
            if len(fields) < 5:
                continue
            bounds, permissions = fields[:2]
            pathname = fields[5].strip() if len(fields) == 6 else ""
            is_anonymous = (
                not pathname
                or pathname in {"[heap]", "[stack]"}
                or pathname.startswith(("[stack:", "[anon:", "[anon_shmem:"))
            )
            if is_anonymous and all(flag in permissions for flag in "rwp"):
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
        if self.scan_mode == "exact":
            return current == target
        if self.scan_mode == "greater_than":
            return current > target
        if self.scan_mode == "less_than":
            return current < target
        if self.scan_mode == "between":
            return target <= current <= upper
        raise ValueError(f"Unsupported scan mode: {self.scan_mode}")

    def _matches_array(self, values, target, upper):
        if self.scan_mode == "unknown_start":
            return np.ones(values.shape, dtype=np.bool_)
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
