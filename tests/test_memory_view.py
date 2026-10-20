"""Tests for the memory viewer and its memory helpers."""

# pylint: disable=protected-access

import ctypes
import os
import struct

import pytest

from hermes.core.memory import describe_address, parse_address, read_memory
from hermes.platform import read_mappings
from hermes.platform.linux import parse_maps


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    # pylint: disable=import-outside-toplevel
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    return QApplication.instance() or QApplication([])


def _unmapped_gap():
    """Return an address right behind a readable mapping that is followed by a gap."""
    mappings = read_mappings(os.getpid())
    return next(
        current.end for current, following in zip(mappings, mappings[1:])
        if following.start > current.end and "r" in current.permissions
        and not current.path.startswith("[v")
    )


def test_read_memory_reads_own_process():
    buffer = ctypes.create_string_buffer(b"hermes-hook!", 16)

    data, readable = read_memory(os.getpid(), ctypes.addressof(buffer), 12)

    assert data == b"hermes-hook!"
    assert all(readable)


def test_read_memory_marks_unmapped_bytes():
    end = _unmapped_gap()

    data, readable = read_memory(os.getpid(), end - 4, 8)

    assert list(readable) == [1, 1, 1, 1, 0, 0, 0, 0]
    assert data[4:] == bytes(4)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0x10", 0x10), ("ff", 0xFF), (" 0X7F00 ", 0x7F00), ("game+0x1f0", 0x401F0),
     ("libfoo.so.6 + 10", 0x7F0010)],
)
def test_parse_address(text, expected):
    mappings = parse_maps([
        "40000-41000 r-xp 00000000 08:01 1 /opt/game/game",
        "7f0000-7f1000 r-xp 00000000 08:01 2 /usr/lib/libfoo.so.6",
    ])

    assert parse_address(text, mappings) == expected


@pytest.mark.parametrize("text", ["", "xyz", "other+0x10"])
def test_parse_address_rejects_invalid_input(text):
    with pytest.raises(ValueError):
        parse_address(text, [])


def test_describe_address():
    mappings = parse_maps([
        "40000-41000 r-xp 00000000 08:01 1 /opt/game/game",
        "41000-42000 rw-p 00001000 08:01 1 /opt/game/game",
        "50000-60000 rw-p 00000000 00:00 0 [heap]",
    ])

    assert describe_address(mappings, 0x41010) == "game+0x1010 (rw-p)"
    assert describe_address(mappings, 0x50020) == "[heap]+0x20 (rw-p)"
    assert describe_address(mappings, 0x45000) is None


def test_viewer_shows_bytes_values_and_changes(qt_app):
    # pylint: disable=import-outside-toplevel
    from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module

    from hermes.ui.localization import load_translations
    from hermes.ui.widgets.memory_view import BYTES_PER_ROW, MemoryViewerDialog

    buffer = (ctypes.c_ubyte * 64)()
    address = ctypes.addressof(buffer)
    struct.pack_into("<i", buffer, 0, 1637)
    dialog = MemoryViewerDialog(load_translations("de"), os.getpid(), address)
    dialog.live.setChecked(False)  # refreshed by the test
    dialog.show()
    qt_app.processEvents()
    try:
        model = dialog.model
        column = address % BYTES_PER_ROW
        cell = lambda offset: model.index(  # pylint: disable=unnecessary-lambda-assignment
            (column + offset) // BYTES_PER_ROW, (column + offset) % BYTES_PER_ROW)

        assert model.base == address - column
        assert model.rows > 1
        assert dialog.selected_address() == address
        assert model.data(cell(0)) == "65" and model.data(cell(1)) == "06"
        assert dialog.value_labels["int32"].text() == "1637"
        assert "[anon]" in dialog.selection_label.text() or "+" in dialog.selection_label.text()

        # A byte that changes is highlighted.
        buffer[1] = 0x07
        dialog.refresh()
        assert model.data(cell(1)) == "07"
        assert model.data(cell(1), Qt.ItemDataRole.ForegroundRole) is not None
        assert model.data(cell(2), Qt.ItemDataRole.ForegroundRole) is None
        assert dialog.value_labels["int32"].text() == str(1637 + 0x100)

        # Scrolling and typed addresses.
        dialog.scroll_rows(2)
        assert model.base == address - column + 2 * BYTES_PER_ROW
        dialog.address_input.setText(hex(address + 0x20))
        dialog._go_to_input()
        assert dialog.selected_address() == address + 0x20
        dialog.address_input.setText("kein-modul+0x10")
        dialog._go_to_input()
        assert "kein-modul" in dialog.status_label.text()
    finally:
        dialog.close()


def test_viewer_shows_unreadable_memory(qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.ui.localization import load_translations
    from hermes.ui.widgets.memory_view import BYTES_PER_ROW, MemoryViewerDialog

    end = _unmapped_gap()
    dialog = MemoryViewerDialog(load_translations("de"), os.getpid(), end - BYTES_PER_ROW)
    dialog.live.setChecked(False)
    qt_app.processEvents()
    try:
        assert dialog.model.data(dialog.model.index(0, 0)) != "??"
        assert dialog.model.data(dialog.model.index(1, 0)) == "??"
        dialog.view.setCurrentIndex(dialog.model.index(0, BYTES_PER_ROW - 2))
        assert dialog.value_labels["int32"].text() == "??"  # runs into the gap
        assert dialog.value_labels["int8"].text() != "??"

        dialog.set_process(None)
        assert "beendet" in dialog.status_label.text()
        assert dialog.model.data(dialog.model.index(0, 0)) == "??"
    finally:
        dialog.close()
