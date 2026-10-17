"""Tests for write-watchpoint configuration."""

import pytest

from hermes.core.write_watch import (
    _build_agent_source,
    watch_size_for_type,
)


@pytest.mark.parametrize(
    ("value_type", "expected_size"),
    [
        ("int8", 1),
        ("uint16", 2),
        ("int32", 4),
        ("float", 4),
        ("int64", 8),
        ("double", 8),
    ],
)
def test_watch_size_matches_scan_value_type(value_type, expected_size):
    assert watch_size_for_type(value_type) == expected_size


def test_watch_size_rejects_unknown_value_type():
    with pytest.raises(ValueError, match="Unsupported watch data type"):
        watch_size_for_type("string")


def test_agent_installs_exception_handler_before_watchpoints():
    source = _build_agent_source(0x1234, 4)

    assert "ptr('0x1234')" in source
    assert "const targetSize = 4;" in source
    assert source.index("Process.setExceptionHandler") < source.index(
        "thread.setHardwareWatchpoint"
    )
