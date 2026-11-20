"""Change the value of a field in the object inspector.

The agent describes what can be changed (``edit`` of a field, see editOf in
grembyte/features/monitor.js): numbers, bools and enums, each with the
address of its value and its value type. Grembyte writes the new value into
the game's memory itself, as the address list does: no Frida, no game
thread. A bitfield bool (Unreal) is one bit (``mask``) of the byte at its
address; only that bit changes. Strings, objects and structs are not
changed here.
"""

import re

from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QDialog,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from grembyte.core import messages
from grembyte.core.memory import write_process_bytes, write_process_value
from grembyte.core.value_types import value_type
from grembyte.platform import ProcessMemory
from grembyte.ui.widgets.code_explorer import BoundedComboBox
from grembyte.ui.widgets.dialog_buttons import dialog_button_row

# The number an enum value is shown with: "2 (Dexterity)"
_LEADING_NUMBER = re.compile(r"\s*(-?\d+)")


def write_field(pid, edit, value):
    """Write ``value`` (bool, or a number as text) to the field ``edit`` describes.

    Raises OSError if the memory cannot be written, LocalizedValueError if
    ``value`` does not fit the field's value type.
    """
    address = int(edit["address"], 16)
    if edit.get("bool"):
        mask = edit.get("mask")
        if mask is None:
            write_process_bytes(pid, address, bytes([1 if value else 0]))
            return
        with ProcessMemory(pid) as memory:
            current = memory.read(address, 1)
        if current is None:
            raise messages.LocalizedOSError("error.inspector.unreadable", address=f"0x{address:X}")
        byte = (current[0] & ~mask & 0xFF) | (mask if value else 0)
        write_process_bytes(pid, address, bytes([byte]))
        return
    write_process_value(pid, address, str(value), edit["valueType"])


class FieldValueDialog(QDialog):
    """Ask for the new value of a field: a number, true/false, or an enum's name.

    ``value()`` is what ``write_field`` takes: a bool, or the number as text.
    An enum's list takes a number typed in too (flags combine several).
    """

    def __init__(self, translations, field, type_name, current, edit, parent=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(parent)
        self.translations = translations
        self._edit = edit
        self._value = None
        self.setWindowTitle(translations["inspector.edit_title"])
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(translations["inspector.edit_label"].format(
            field=field, type=type_name)))
        self.input = None
        self.choice = None
        if edit.get("bool"):
            self.choice = BoundedComboBox(self)
            self.choice.addItem("true", True)
            self.choice.addItem("false", False)
            self.choice.setCurrentIndex(0 if current.strip().lower() == "true" else 1)
            layout.addWidget(self.choice)
        elif edit.get("enumValues"):
            self.choice = BoundedComboBox(self)
            self.choice.setEditable(True)
            entries = sorted(edit["enumValues"].items(), key=lambda pair: int(pair[0]))
            for number, name in entries:
                self.choice.addItem(f"{name} ({number})", number)
            match = _LEADING_NUMBER.match(current)
            index = self.choice.findData(match.group(1)) if match else -1
            if index >= 0:
                self.choice.setCurrentIndex(index)
            else:
                self.choice.setEditText(match.group(1) if match else "")
            layout.addWidget(self.choice)
        else:
            self.input = QLineEdit(current.strip())
            self.input.selectAll()
            layout.addWidget(self.input)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        layout.addLayout(dialog_button_row(translations, "button.write", self._accept,
                                           self.reject))

    def value(self):
        """The value chosen (a bool, or a number as text), after ``exec`` accepted."""
        return self._value

    def _typed(self):
        if self.input is not None:
            return self.input.text()
        if self._edit.get("bool"):
            return self.choice.currentData()
        text = self.choice.currentText()
        index = self.choice.findText(text)
        return self.choice.itemData(index) if index >= 0 else text

    def _accept(self):
        value = self._typed()
        if not isinstance(value, bool):
            try:
                value = str(value_type(self._edit["valueType"]).parse(str(value)))
            except ValueError as error:
                self.error_label.setText(messages.describe(error))
                self.error_label.show()
                return
        self._value = value
        self.accept()
