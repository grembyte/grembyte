"""Changing fields in the object inspector (grembyte.ui.widgets.field_value)."""

# pylint: disable=import-outside-toplevel,import-error,no-name-in-module,unused-argument

import ctypes
import os

import pytest

from tests.test_hooks import fixture_qt_app, fixture_translations  # pylint: disable=unused-import


def _buffer(data):
    buffer = ctypes.create_string_buffer(bytes(data), len(data))
    return buffer, ctypes.addressof(buffer)


def test_numbers_and_bools_are_written_into_the_process():
    from grembyte.ui.widgets.field_value import write_field

    buffer, address = _buffer([0] * 8)
    write_field(os.getpid(), {"address": hex(address), "valueType": "int32"}, "-2")
    assert int.from_bytes(buffer.raw[:4], "little", signed=True) == -2
    write_field(os.getpid(), {"address": hex(address + 4), "valueType": "uint8", "bool": True},
                True)
    assert buffer.raw[4] == 1
    with pytest.raises(ValueError):
        write_field(os.getpid(), {"address": hex(address), "valueType": "uint8"}, "300")


def test_a_bitfield_bool_changes_only_its_bit():
    from grembyte.ui.widgets.field_value import write_field

    buffer, address = _buffer([0b1010_0001])
    edit = {"address": hex(address), "valueType": "uint8", "bool": True, "mask": 0b0000_0100}
    write_field(os.getpid(), edit, True)
    assert buffer.raw[0] == 0b1010_0101
    write_field(os.getpid(), {**edit, "mask": 0b1000_0000}, False)
    assert buffer.raw[0] == 0b0010_0101


def test_the_dialog_offers_what_fits_the_field(qt_app, translations):
    from grembyte.ui.widgets.field_value import FieldValueDialog

    number = FieldValueDialog(translations, "Gold", "System.Int32", "120",
                              {"address": "0x10", "valueType": "int32"})
    assert number.input.text() == "120" and number.choice is None
    number.input.setText("lots")
    number._accept()  # pylint: disable=protected-access
    assert number.value() is None and not number.error_label.isHidden()
    number.input.setText("0x20")
    number._accept()  # pylint: disable=protected-access
    assert number.value() == "32"

    flag = FieldValueDialog(translations, "m_isDead", "System.Boolean", "false",
                            {"address": "0x10", "valueType": "uint8", "bool": True})
    assert flag.choice.currentText() == "false"
    flag.choice.setCurrentIndex(0)
    flag._accept()  # pylint: disable=protected-access
    assert flag.value() is True

    enum = FieldValueDialog(translations, "Type", "Game.DamageType", "2 (Frost)",
                            {"address": "0x10", "valueType": "int32",
                             "enumValues": {"0": "Physical", "1": "Fire", "2": "Frost"}})
    assert [enum.choice.itemText(i) for i in range(enum.choice.count())] == [
        "Physical (0)", "Fire (1)", "Frost (2)"]
    assert enum.choice.currentText() == "Frost (2)"
    enum.choice.setCurrentIndex(1)
    enum._accept()  # pylint: disable=protected-access
    assert enum.value() == "1"
    enum.choice.setEditText("3")  # flags combine several values
    enum._accept()  # pylint: disable=protected-access
    assert enum.value() == "3"
    for dialog in (number, flag, enum):
        dialog.close()


def test_a_live_inspector_changes_fields_a_recorded_one_does_not(qt_app, translations,
                                                                monkeypatch):
    from PySide6.QtWidgets import QDialog

    from grembyte.ui.widgets import field_value
    from grembyte.ui.widgets.object_inspector import ObjectInspectorDialog

    buffer, address = _buffer([0] * 8)
    contents = {"name": "Game.Hero", "address": hex(address), "fields": [
        {"name": "Gold", "type": "System.Int32", "offset": 0, "static": False,
         "value": {"text": "0"}, "address": hex(address), "valueType": "int32",
         "edit": {"address": hex(address), "valueType": "int32"}},
        {"name": "Name", "type": "System.String", "offset": 8, "static": False,
         "value": {"text": '"Miranda"'}, "address": hex(address + 8), "valueType": None,
         "edit": None},
    ]}
    requests = []

    def request(_target, inspector):
        requests.append(inspector)

    dialog = ObjectInspectorDialog(translations, lambda *_: None, target={"address": hex(address)},
                                   request=request, pid=os.getpid())
    recorded = ObjectInspectorDialog(translations, lambda *_: None, contents=contents,
                                     captured="16:58:56.791", pid=os.getpid())
    monkeypatch.setattr(field_value.FieldValueDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(field_value.FieldValueDialog, "value", lambda self: "250")
    try:
        dialog.show_contents(contents)
        assert dialog.field_edit(0) is not None
        assert dialog.field_edit(1) is None  # a string
        assert recorded.field_edit(0) is None  # values recorded at a call
        requests.clear()
        dialog.table.cellDoubleClicked.emit(0, 2)
        assert int.from_bytes(buffer.raw[:4], "little") == 250
        assert dialog.status_label.text() == "Gold ist jetzt 250."
        assert requests == [dialog]  # read again at once
    finally:
        dialog.close()
        recorded.close()
