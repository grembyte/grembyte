"""Tests for the process memory scanner."""

# pylint: disable=protected-access

import builtins
import io
import struct

import pytest
import numpy as np

from hermes.core.scanner import MemorySnapshot, ScanWorker, ValueRefreshWorker


class FakeMemory:
    """Small seekable memory image addressed by virtual address."""

    def __init__(self, base_address, contents):
        self.base_address = base_address
        self.contents = contents
        self.position = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def seek(self, address):
        self.position = address - self.base_address

    def read(self, size):
        if self.position < 0 or self.position >= len(self.contents):
            return b""
        result = self.contents[self.position:self.position + size]
        self.position += len(result)
        return result


def install_fake_process(monkeypatch, maps, base_address, contents):
    """Route this worker's /proc file opens to an in-memory process image."""
    def fake_open(path, mode="r", **_kwargs):
        if path == "/proc/123/maps":
            return io.StringIO(maps)
        if path == "/proc/123/mem":
            assert mode == "rb"
            return FakeMemory(base_address, contents)
        raise AssertionError(f"Unexpected file open: {path}")

    monkeypatch.setattr(builtins, "open", fake_open)


@pytest.mark.parametrize(
    ("mode", "current", "target", "upper", "previous", "expected"),
    [
        ("unknown_start", 5, None, None, None, True),
        ("exact", 7, 7, None, None, True),
        ("greater_than", 8, 7, None, None, True),
        ("less_than", 6, 7, None, None, True),
        ("between", 5, 2, 7, None, True),
        ("changed", 8, None, None, 7, True),
        ("unchanged", 7, None, None, 7, True),
        ("increased", 8, None, None, 7, True),
        ("decreased", 6, None, None, 7, True),
        ("increased", 7, None, None, 7, False),
        ("decreased", 8, None, None, 7, False),
        ("exact", 6, 7, None, None, False),
        ("changed", 7, None, None, 7, False),
    ],
)
def test_matches_scan_modes(mode, current, target, upper, previous, expected):
    worker = ScanWorker(123, "int32", mode)

    assert worker._matches(current, target, upper, previous) is expected


@pytest.mark.parametrize(
    ("mode", "target", "upper", "expected"),
    [
        ("exact", 7, None, [False, True, False]),
        ("greater_than", 7, None, [False, False, True]),
        ("less_than", 7, None, [True, False, False]),
        ("between", 4, 8, [False, True, False]),
        ("unknown_start", None, None, [True, True, True]),
    ],
)
def test_vectorized_filters(mode, target, upper, expected):
    worker = ScanWorker(123, "int32", mode)
    values = np.array([2, 7, 10], dtype=np.int32)

    assert worker._matches_array(values, target, upper).tolist() == expected


def test_parse_scan_value_uses_selected_numeric_format():
    assert ScanWorker._parse_scan_value("0x10", "<i") == 16
    assert ScanWorker._parse_scan_value("0.1", "<f") == struct.unpack(
        "<f", struct.pack("<f", 0.1)
    )[0]


@pytest.mark.parametrize("value", ["not-a-number", "2147483648"])
def test_parse_scan_value_rejects_invalid_or_out_of_range_values(value):
    with pytest.raises(ValueError, match="Invalid scan value"):
        ScanWorker._parse_scan_value(value, "<i")


def test_first_scan_finds_exact_value_in_readable_mappings(monkeypatch):
    base_address = 0x1000
    contents = struct.pack("<iii", 1, 7, 9)
    maps = (
        "1000-100c rw-p 00000000 00:00 0\n"
        "2000-200c ---p 00000000 00:00 0\n"
    )
    install_fake_process(monkeypatch, maps, base_address, contents)
    worker = ScanWorker(123, "int32", "exact", "7")
    emitted = []
    progress = []
    worker.results_ready.connect(emitted.append)
    worker.progress_changed.connect(progress.append)

    worker.run()

    assert emitted == [[{"address": base_address + 4, "type": "int32", "value": 7}]]
    assert progress[-1] == 100


def test_first_scan_finds_values_crossing_chunk_boundary(monkeypatch):
    base_address = 0x1000
    contents = b"\0" * 3 + struct.pack("<i", 42) + b"\0" * 4
    maps = "1000-100b rw-p 00000000 00:00 0\n"
    install_fake_process(monkeypatch, maps, base_address, contents)
    worker = ScanWorker(123, "int32", "exact", "42", aligned=False)
    worker._CHUNK_SIZE = 4

    assert worker._first_scan() == [
        {"address": base_address + 3, "type": "int32", "value": 42}
    ]


def test_first_scan_does_not_duplicate_candidates_at_chunk_boundary(monkeypatch):
    base_address = 0x1000
    contents = struct.pack("<iii", 42, 42, 42)
    maps = "1000-100c rw-p 00000000 00:00 0\n"
    install_fake_process(monkeypatch, maps, base_address, contents)
    worker = ScanWorker(123, "int32", "exact", "42")
    worker._CHUNK_SIZE = 4

    results = worker._first_scan()

    assert [result["address"] for result in results] == [0x1000, 0x1004, 0x1008]


def test_first_scan_keeps_private_writable_mappings():
    maps = io.StringIO(
        "1000-1004 rw-p 00000000 00:00 0\n"
        "2000-2004 rw-p 00000000 00:00 0 [heap]\n"
        "3000-3004 rw-p 00000000 00:00 0 [stack]\n"
        "4000-4004 rw-p 00000000 00:00 0 [anon:runtime]\n"
        "5000-5004 r--p 00000000 00:00 0\n"
        "6000-6004 rw-p 00000000 00:00 0 /usr/lib/example.so\n"
        "7000-7004 rw-s 00000000 00:00 0 /memfd:shared\n"
        "8000-8004 rw-p 00000000 00:00 0 /dev/dri/card0\n"
    )

    assert ScanWorker._writable_mappings(maps) == [
        (0x1000, 0x1004),
        (0x2000, 0x2004),
        (0x3000, 0x3004),
        (0x4000, 0x4004),
        (0x6000, 0x6004),
    ]


def test_auto_first_scan_returns_matching_typed_candidates(monkeypatch):
    base_address = 0x1000
    contents = struct.pack("<ii", 7, 7)
    maps = "1000-1008 rw-p 00000000 00:00 0\n"
    install_fake_process(monkeypatch, maps, base_address, contents)
    worker = ScanWorker(123, "auto", "exact", "7")

    results = worker._first_scan()

    assert results == [
        {"address": base_address, "type": "int32", "value": 7},
        {"address": base_address + 4, "type": "int32", "value": 7},
    ]


def test_auto_datatype_requires_value_based_initial_scan():
    worker = ScanWorker(123, "auto", "unknown_start")

    with pytest.raises(ValueError, match="value-based first scan"):
        worker._first_scan()


def test_first_scan_discards_non_finite_float_candidates(monkeypatch):
    base_address = 0x1000
    contents = struct.pack("<fff", 1.5, float("nan"), float("inf"))
    maps = "1000-100c rw-p 00000000 00:00 0\n"
    install_fake_process(monkeypatch, maps, base_address, contents)
    worker = ScanWorker(123, "float", "unknown_start")

    results = worker._first_scan()

    assert base_address in [result["address"] for result in results]
    assert base_address + 4 not in [result["address"] for result in results]
    assert base_address + 8 not in [result["address"] for result in results]


@pytest.mark.parametrize(
    ("mode", "expected_addresses"),
    [
        ("changed", [0x1004]),
        ("unchanged", [0x1000, 0x1008]),
        ("increased", [0x1004]),
        ("decreased", []),
    ],
)
def test_refine_scan_compares_current_and_previous_values(
    monkeypatch, mode, expected_addresses
):
    current_values = struct.pack("<iii", 10, 16, 25)
    maps = "1000-100c r--p 00000000 00:00 0\n"
    install_fake_process(monkeypatch, maps, 0x1000, current_values)
    previous = [
        {"address": 0x1000, "type": "int32", "value": 10},
        {"address": 0x1004, "type": "int32", "value": 15},
        {"address": 0x1008, "type": "int32", "value": 25},
    ]
    worker = ScanWorker(123, "int32", mode, previous_results=previous)
    progress = []
    worker.progress_changed.connect(progress.append)

    results = worker._refine_scan()

    assert [result["address"] for result in results] == expected_addresses
    assert progress[-1] == 100


def test_aligned_first_scan_skips_unaligned_addresses(monkeypatch):
    contents = b"\0" * 3 + struct.pack("<i", 42) + struct.pack("<i", 42)
    maps = "1000-100b rw-p 00000000 00:00 0\n"
    install_fake_process(monkeypatch, maps, 0x1000, contents)

    aligned = ScanWorker(123, "int32", "exact", "42")._first_scan()
    unaligned = ScanWorker(123, "int32", "exact", "42", aligned=False)._first_scan()

    assert [result["address"] for result in aligned] == []
    assert [result["address"] for result in unaligned] == [0x1003, 0x1007]


def test_unknown_start_scan_returns_snapshot_instead_of_result_list(monkeypatch):
    contents = struct.pack("<iii", 10, 15, 25)
    maps = "1000-100c rw-p 00000000 00:00 0\n"
    install_fake_process(monkeypatch, maps, 0x1000, contents)

    snapshot = ScanWorker(123, "int32", "unknown_start")._first_scan()

    assert isinstance(snapshot, MemorySnapshot)
    assert len(snapshot) == 3
    assert snapshot[:2] == [
        {"address": 0x1000, "type": "int32", "value": 10},
        {"address": 0x1004, "type": "int32", "value": 15},
    ]


@pytest.mark.parametrize(
    ("mode", "value", "expected_addresses"),
    [
        ("changed", "", [0x1004, 0x1008]),
        ("unchanged", "", [0x1000]),
        ("increased", "", [0x1004]),
        ("decreased", "", [0x1008]),
        ("exact", "16", [0x1004]),
        ("greater_than", "11", [0x1004, 0x1008]),
    ],
)
def test_refine_snapshot_compares_with_current_memory(
    monkeypatch, mode, value, expected_addresses
):
    previous = struct.pack("<iii", 10, 15, 25)
    current = struct.pack("<iii", 10, 16, 24)
    snapshot = MemorySnapshot("int32", 4, [(0x1000, previous, None)])
    install_fake_process(monkeypatch, "", 0x1000, current)
    worker = ScanWorker(123, "int32", mode, value, previous_results=snapshot)

    results = worker._refine_scan()

    assert [result["address"] for result in results] == expected_addresses
    assert all(result["value"] in {10, 16, 24} for result in results)


def test_refine_snapshot_keeps_snapshot_for_large_candidate_sets(monkeypatch):
    contents = struct.pack("<iiii", 1, 2, 3, 4)
    snapshot = MemorySnapshot("int32", 4, [(0x1000, contents, None)])
    install_fake_process(monkeypatch, "", 0x1000, contents)
    worker = ScanWorker(123, "int32", "unchanged", previous_results=snapshot)
    worker._LIST_THRESHOLD = 2

    results = worker._refine_scan()

    assert isinstance(results, MemorySnapshot)
    assert len(results) == 4


def test_run_emits_scan_error_signal_for_invalid_input():
    worker = ScanWorker(123, "int32", "exact", "invalid")
    errors = []
    worker.scan_failed.connect(errors.append)

    worker.run()

    assert errors == ["Invalid scan value: 'invalid'"]


def test_run_emits_cancelled_signal_when_interruption_is_requested(monkeypatch):
    worker = ScanWorker(123, "int32", "exact", "7")
    results = []
    cancelled = []
    worker.results_ready.connect(results.append)
    worker.scan_cancelled.connect(lambda: cancelled.append(True))
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: True)

    worker.run()

    assert results == []
    assert cancelled == [True]


def test_value_refresh_worker_reads_requested_addresses(monkeypatch):
    base_address = 0x1000
    memory = struct.pack("<id", 27, 3.5)
    install_fake_process(monkeypatch, "", base_address, memory)
    worker = ValueRefreshWorker(
        123,
        [
            {"address": base_address, "type": "int32"},
            {"address": base_address + 4, "type": "double"},
        ],
    )
    emitted = []
    worker.values_ready.connect(emitted.append)

    worker.run()

    assert emitted == [{base_address: 27, base_address + 4: 3.5}]