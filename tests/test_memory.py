"""Tests for process memory helpers."""

import builtins
import struct

import pytest

from hermes.core.memory import write_process_value


class FakeMemory:
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

    def write(self, value):
        end = self.position + len(value)
        self.contents[self.position:end] = value
        self.position = end
        return len(value)


@pytest.mark.parametrize(
    ("value_type", "value", "expected"),
    [
        ("int32", "0x2a", 42),
        ("double", "3.5", 3.5),
        ("uint8", "255", 255),
        ("int16", "-2", -2),
    ],
)
def test_write_process_value_encodes_and_writes_typed_data(
    monkeypatch, value_type, value, expected
):
    base_address = 0x1000
    contents = bytearray(16)
    fake_memory = FakeMemory(base_address, contents)
    opened = []

    def fake_open(path, mode, buffering):
        opened.append((path, mode, buffering))
        return fake_memory

    monkeypatch.setattr(builtins, "open", fake_open)

    result = write_process_value(123, base_address + 4, value, value_type)

    formats = {"int32": "<i", "double": "<d", "uint8": "<B", "int16": "<h"}
    expected_bytes = struct.pack(formats[value_type], expected)
    assert result == expected
    assert contents[4:4 + len(expected_bytes)] == expected_bytes
    assert opened == [("/proc/123/mem", "r+b", 0)]


@pytest.mark.parametrize(
    ("value", "value_type"),
    [("invalid", "int32"), ("2147483648", "int32"), ("1", "unknown"),
     ("256", "uint8"), ("-1", "uint32")],
)
def test_write_process_value_rejects_invalid_input(value, value_type):
    with pytest.raises(ValueError):
        write_process_value(123, 0x1000, value, value_type)