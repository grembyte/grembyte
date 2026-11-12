"""Tests for the process memory scanner."""

# pylint: disable=protected-access

import builtins
import io
import struct

import pytest
import numpy as np

from grembyte.core import scanner
from grembyte.core.scanner import (
    MemorySnapshot, ScanResults, ScanWorker, ValueRange, ValueRefreshWorker,
)
from grembyte.core.value_types import VALUE_TYPES
from grembyte.platform import linux
from grembyte.platform.linux import parse_maps


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
    # The fake is /proc: use the Linux backend on every system.
    monkeypatch.setattr(scanner, "read_mappings", linux.read_mappings)
    monkeypatch.setattr(scanner, "ProcessMemory", linux.ProcessMemory)


@pytest.mark.parametrize(
    ("mode", "current", "target", "upper", "previous", "expected"),
    [
        ("unknown_start", 5, None, None, None, True),
        ("exact", 7, ValueRange(7, 7), None, None, True),
        ("greater_than", 8, 7, None, None, True),
        ("less_than", 6, 7, None, None, True),
        ("between", 5, 2, 7, None, True),
        ("changed", 8, None, None, 7, True),
        ("unchanged", 7, None, None, 7, True),
        ("increased", 8, None, None, 7, True),
        ("decreased", 6, None, None, 7, True),
        ("increased", 7, None, None, 7, False),
        ("decreased", 8, None, None, 7, False),
        ("exact", 6, ValueRange(7, 7), None, None, False),
        ("exact", 6, ValueRange(5, 9), None, None, True),
        ("increased_by", 107, ValueRange(100, 100), None, 7, True),
        ("increased_by", 106, ValueRange(98, 102), None, 7, True),
        ("increased_by", 7, ValueRange(100, 100), None, 107, False),
        ("decreased_by", 7, ValueRange(100, 100), None, 107, True),
        ("changed_by", 7, ValueRange(100, 100), None, 107, True),
        ("changed_by", 107, ValueRange(100, 100), None, 7, True),
        ("changed_by", 108, ValueRange(100, 100), None, 7, False),
        ("changed", 7, None, None, 7, False),
    ],
)
def test_matches_scan_modes(mode, current, target, upper, previous, expected):
    worker = ScanWorker(123, "int32", mode)
    if previous is not None:
        previous = np.array([previous], dtype=np.int32)

    matches = worker._matches_array(np.array([current], dtype=np.int32), target, upper, previous)

    assert matches.tolist() == [expected]


@pytest.mark.parametrize(
    ("mode", "target", "upper", "expected"),
    [
        ("exact", ValueRange(7, 7), None, [False, True, False]),
        ("exact", ValueRange(2, 7), None, [True, True, False]),
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
    maps = parse_maps(io.StringIO(
        "1000-1004 rw-p 00000000 00:00 0\n"
        "2000-2004 rw-p 00000000 00:00 0 [heap]\n"
        "3000-3004 rw-p 00000000 00:00 0 [stack]\n"
        "4000-4004 rw-p 00000000 00:00 0 [anon:runtime]\n"
        "5000-5004 r--p 00000000 00:00 0\n"
        "6000-6004 rw-p 00000000 00:00 0 /usr/lib/example.so\n"
        "7000-7004 rw-s 00000000 00:00 0 /memfd:shared\n"
        "8000-8004 rw-p 00000000 00:00 0 /dev/dri/card0\n"
    ))

    assert ScanWorker._scan_ranges(maps) == [
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
            {"address": base_address, "type": "float"},
            {"address": base_address + 4, "type": "double"},
        ],
    )
    emitted = []
    worker.values_ready.connect(emitted.append)

    worker.run()

    # One address shown as two types keeps both values.
    as_float = struct.unpack("<f", struct.pack("<i", 27))[0]
    assert emitted == [{(base_address, "int32"): 27, (base_address, "float"): as_float,
                        (base_address + 4, "double"): 3.5}]

def test_first_scan_finds_small_unsigned_values_at_any_offset(monkeypatch):
    contents = bytes([1, 200, 3, 200, 5])
    install_fake_process(monkeypatch, "1000-1005 rw-p 00000000 00:00 0\n", 0x1000, contents)
    worker = ScanWorker(123, "uint8", "exact", "200", aligned=False)

    assert [result["address"] for result in worker._first_scan()] == [0x1001, 0x1003]


def test_first_scan_rejects_values_outside_the_type_range(monkeypatch):
    install_fake_process(monkeypatch, "1000-1004 rw-p 00000000 00:00 0\n", 0x1000, bytes(4))

    with pytest.raises(ValueError, match="Invalid scan value"):
        ScanWorker(123, "uint8", "exact", "-1")._first_scan()


def test_refine_scan_reads_int16_values(monkeypatch):
    install_fake_process(
        monkeypatch, "1000-1004 rw-p 00000000 00:00 0\n", 0x1000, struct.pack("<hh", -5, 7)
    )
    previous = [
        {"address": 0x1000, "type": "int16", "value": -3},
        {"address": 0x1002, "type": "int16", "value": 7},
    ]
    worker = ScanWorker(123, "int16", "decreased", previous_results=previous)

    assert worker._refine_scan() == [{"address": 0x1000, "type": "int16", "value": -5}]


def test_auto_first_scan_ignores_small_types(monkeypatch):
    install_fake_process(
        monkeypatch, "1000-1004 rw-p 00000000 00:00 0\n", 0x1000, struct.pack("<i", 7)
    )
    results = ScanWorker(123, "auto", "exact", "7")._first_scan()

    assert {result["type"] for result in results} == {"int32"}


def scan_range(text, value_type, tolerance="", rounding="exact", delta=False):
    return ValueRange.parse(text, VALUE_TYPES[value_type], tolerance, rounding, delta)


@pytest.mark.parametrize(
    ("text", "rounding", "tolerance", "inside", "outside"),
    [
        ("12.3", "exact", "", [np.float32(12.3)], [12.31, 12.29]),
        ("12.3", "exact", "0.1", [12.2, 12.4], [12.19, 12.41]),
        ("12.3", "rounded", "", [12.25, 12.3, 12.349], [12.35, 12.249]),
        ("12", "rounded", "", [11.5, 12.49], [12.5, 11.49]),
        ("12.3", "truncated", "", [12.3, 12.399], [12.4, 12.299]),
        ("-12.3", "truncated", "", [-12.3, -12.399], [-12.4, -12.299]),
        ("12.3", "rounded", "1", [11.25, 13.349], [11.249, 13.35]),
        ("1.25e1", "rounded", "", [12.45, 12.54], [12.55, 12.44]),
    ],
)
def test_float_ranges_round_and_widen_by_tolerance(text, rounding, tolerance, inside, outside):
    value_range = scan_range(text, "float", tolerance, rounding)

    assert value_range.contains(np.array(inside, dtype=np.float32)).all()
    assert not value_range.contains(np.array(outside, dtype=np.float32)).any()


def test_integer_ranges_ignore_float_rounding():
    value_range = scan_range("100", "int32", "5", "rounded")

    assert value_range == ValueRange(95, 105)


@pytest.mark.parametrize(
    ("text", "value_type", "tolerance", "message"),
    [
        ("12", "int32", "-1", "Invalid tolerance"),
        ("12", "int32", "0.5", "Invalid tolerance"),
        ("12", "float", "abc", "Invalid tolerance"),
        ("2147483648", "int32", "", "Invalid scan value"),
        ("abc", "float", "", "Invalid scan value"),
    ],
)
def test_value_range_rejects_invalid_input(text, value_type, tolerance, message):
    with pytest.raises(ValueError, match=message):
        scan_range(text, value_type, tolerance)


def test_value_range_change_may_exceed_the_type_range():
    value_range = scan_range("200", "int8", delta=True)

    assert value_range == ValueRange(200, 200)


@pytest.mark.parametrize("dtype", [np.int8, np.int32, np.int64, np.uint8, np.uint64])
def test_integer_change_matches_scalars_and_arrays_alike(dtype):
    info = np.iinfo(dtype)
    previous = np.array([0, 5, info.max - 10, info.min + 10, 3], dtype=dtype)
    current = np.array([10, 15, info.max, info.min + 20, 3], dtype=dtype)
    value_range = ValueRange(9, 11)

    array_result = value_range.contains_change(current, previous)
    scalar_result = [
        value_range.contains_change(int(now), int(before))
        for now, before in zip(current, previous)
    ]

    assert array_result.tolist() == scalar_result == [True, True, True, True, False]


def test_float_change_uses_rounding_of_the_entered_value():
    value_range = scan_range("7.5", "float", rounding="rounded", delta=True)
    previous = np.array([20.0, 20.0, 20.0], dtype=np.float32)
    current = np.array([12.5, 12.47, 12.6], dtype=np.float32)

    assert value_range.contains_change(previous, current).tolist() == [True, True, False]


def test_first_scan_finds_rounded_float_values(monkeypatch):
    contents = struct.pack("<fff", 12.34, 12.26, 12.4)
    install_fake_process(monkeypatch, "1000-100c rw-p 00000000 00:00 0\n", 0x1000, contents)
    worker = ScanWorker(123, "float", "exact", "12.3", float_rounding="rounded")

    assert [result["address"] for result in worker._first_scan()] == [0x1000, 0x1004]


def test_first_scan_with_tolerance_finds_nearby_integers(monkeypatch):
    contents = struct.pack("<iii", 95, 100, 106)
    install_fake_process(monkeypatch, "1000-100c rw-p 00000000 00:00 0\n", 0x1000, contents)
    worker = ScanWorker(123, "int32", "exact", "100", tolerance="5")

    assert [result["value"] for result in worker._first_scan()] == [95, 100]


@pytest.mark.parametrize("mode", ["changed", "increased_by"])
def test_first_scan_requires_previous_scan_for_comparing_modes(mode):
    with pytest.raises(ValueError, match="requires a previous scan"):
        ScanWorker(123, "int32", mode, "100")._first_scan()


def test_refine_scan_finds_values_increased_by_amount(monkeypatch):
    install_fake_process(
        monkeypatch, "1000-100c rw-p 00000000 00:00 0\n", 0x1000,
        struct.pack("<iii", 200, 205, 300),
    )
    previous = [
        {"address": 0x1000 + offset, "type": "int32", "value": 100}
        for offset in (0, 4, 8)
    ]
    worker = ScanWorker(
        123, "int32", "increased_by", "100", previous_results=previous, tolerance="5"
    )

    assert [result["value"] for result in worker._refine_scan()] == [200, 205]


def test_refine_snapshot_finds_floats_decreased_by_amount(monkeypatch):
    install_fake_process(
        monkeypatch, "1000-100c rw-p 00000000 00:00 0\n", 0x1000,
        struct.pack("<fff", 20.0, 22.0, 30.0),
    )
    snapshot = ScanWorker(123, "float", "unknown_start")._first_scan()
    install_fake_process(
        monkeypatch, "1000-100c rw-p 00000000 00:00 0\n", 0x1000,
        struct.pack("<fff", 12.6, 14.0, 29.0),
    )
    worker = ScanWorker(
        123, "float", "decreased_by", "7.5", previous_results=snapshot,
        tolerance="0.5", float_rounding="rounded",
    )

    assert [result["address"] for result in worker._refine_scan()] == [0x1000, 0x1004]


class FakeRegions:
    """ProcessMemory stand-in with readable regions ``{start: bytes}`` and holes between."""

    def __init__(self, regions):
        self.regions = regions

    def __call__(self, _pid):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read_available(self, address, size):
        for start, data in self.regions.items():
            if start <= address < start + len(data):
                return data[address - start:address - start + size]
        return b""

    def read(self, address, size):
        data = self.read_available(address, size)
        return data if len(data) == size else None


@pytest.mark.parametrize("chunk_size", [8, ScanWorker._CHUNK_SIZE])
def test_refine_reads_candidates_in_blocks_and_skips_unreadable_ones(monkeypatch, chunk_size):
    monkeypatch.setattr(scanner, "ProcessMemory", FakeRegions({
        0x1000: struct.pack("<ii", 1, 2),
        0x2000: struct.pack("<ii", 3, 4),
        0x100000: struct.pack("<i", 5),
    }))
    # 0x1800 lies in the hole between the first two regions, close enough to
    # be read together with them; 0x100000 is far away.
    addresses = [0x1000, 0x1004, 0x1800, 0x2000, 0x2004, 0x100000]
    previous = ScanResults.from_dicts(
        {"address": address, "type": "int32", "value": 0} for address in addresses
    )
    worker = ScanWorker(123, "int32", "changed", previous_results=previous)
    worker._CHUNK_SIZE = chunk_size

    results = worker._refine_scan()

    assert isinstance(results, ScanResults)
    assert [(result["address"], result["value"]) for result in results] == [
        (0x1000, 1), (0x1004, 2), (0x2000, 3), (0x2004, 4), (0x100000, 5),
    ]


def test_refine_reads_candidates_as_the_newly_chosen_type(monkeypatch):
    monkeypatch.setattr(scanner, "ProcessMemory", FakeRegions({
        0x1000: struct.pack("<hhhh", 7, 1, 9, 1),
    }))
    previous = [
        {"address": 0x1000, "type": "int32", "value": 7 + (1 << 16)},
        {"address": 0x1004, "type": "int32", "value": 9 + (1 << 16)},
    ]
    worker = ScanWorker(123, "int16", "exact", "9", previous_results=previous)

    assert worker._refine_scan() == [{"address": 0x1004, "type": "int16", "value": 9}]


def test_a_newly_chosen_type_reads_previous_values_from_their_bytes(monkeypatch):
    float_one = struct.unpack("<i", struct.pack("<f", 1.0))[0]
    monkeypatch.setattr(scanner, "ProcessMemory", FakeRegions({
        0x1000: struct.pack("<ii", 5, float_one) + struct.pack("<q", 7 + (3 << 32)),
    }))
    previous = [
        # Found as int32 and as float at one address: compared once, as int32.
        {"address": 0x1000, "type": "int32", "value": 5},
        {"address": 0x1000, "type": "float", "value": 7.0e-45},
        # A float of 1.0 is the int32 0x3F800000, the same bytes.
        {"address": 0x1004, "type": "float", "value": 1.0},
        # The low half of an int64.
        {"address": 0x1008, "type": "int64", "value": 6 + (3 << 32)},
    ]
    worker = ScanWorker(123, "int32", "unchanged", previous_results=previous)

    assert worker._refine_scan() == [
        {"address": 0x1000, "type": "int32", "value": 5},
        {"address": 0x1004, "type": "int32", "value": float_one},
    ]


def test_a_wider_type_keeps_candidates_only_without_a_previous_value(monkeypatch):
    monkeypatch.setattr(scanner, "ProcessMemory", FakeRegions({
        0x1000: struct.pack("<q", 9 + (1 << 32)),
    }))
    # Only the low 2 of the 8 bytes are known from before.
    previous = [{"address": 0x1000, "type": "int16", "value": 9}]

    changed = ScanWorker(123, "int64", "changed", previous_results=previous)
    assert changed._refine_scan() == []
    exact = ScanWorker(123, "int64", "exact", str(9 + (1 << 32)), previous_results=previous)
    assert exact._refine_scan() == [{"address": 0x1000, "type": "int64", "value": 9 + (1 << 32)}]


def test_auto_refine_keeps_each_type_and_orders_by_address(monkeypatch):
    monkeypatch.setattr(scanner, "ProcessMemory", FakeRegions({
        0x1000: struct.pack("<if", 5, 2.5) + struct.pack("<q", 5),
    }))
    previous = [
        {"address": 0x1008, "type": "int64", "value": 4},
        {"address": 0x1000, "type": "int32", "value": 4},
        {"address": 0x1004, "type": "float", "value": 2.5},
    ]
    worker = ScanWorker(123, "auto", "increased", previous_results=previous)

    assert worker._refine_scan() == [
        {"address": 0x1000, "type": "int32", "value": 5},
        {"address": 0x1008, "type": "int64", "value": 5},
    ]


def test_scan_results_behave_like_a_result_list():
    entries = [
        {"address": 0x2000, "type": "int32", "value": 2},
        {"address": 0x1000, "type": "double", "value": 1.5},
        {"address": 0x3000, "type": "int32", "value": 3},
    ]
    results = ScanResults.from_dicts(entries)

    assert len(results) == 3 and results
    assert results == sorted(entries, key=lambda entry: entry["address"])
    assert results[1:] == [entries[0], entries[2]]
    assert not ScanResults() and list(ScanResults()) == []


def test_small_snapshot_becomes_scan_results():
    contents = struct.pack("<iii", 1, 2, 3)
    mask = np.array([True, False, True])
    snapshot = MemorySnapshot("int32", 4, [(0x1000, contents, mask)])

    assert snapshot.to_results() == [
        {"address": 0x1000, "type": "int32", "value": 1},
        {"address": 0x1008, "type": "int32", "value": 3},
    ]
