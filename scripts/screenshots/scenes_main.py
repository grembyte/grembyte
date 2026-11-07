"""The main window: scan results, value search and the tracked addresses."""

from hermes.core.pointer import PointerChain
from studio import scene, settle

# A second scan in the test game: three addresses still hold the gold.
_RESULTS = [
    {"address": 0x5615_3A2C_41F8, "type": "int32", "value": 1350},
    {"address": 0x5615_3A2C_9B40, "type": "int32", "value": 1350},
    {"address": 0x7F3B_1C80_2E14, "type": "int32", "value": 1350},
]

# Offsets as in the Character struct of test_app/dummy_game.c.
_TRACKED = (
    ("Gold", 0x5615_3A2C_41F8, "int32", "1350", None,
     PointerChain("dummy_game.exe", 0x4040, (0x28, 0x0))),
    ("HP", 0x5615_3A2C_41FC, "int32", "100", "100",
     PointerChain("dummy_game.exe", 0x4040, (0x28, 0x4))),
    ("Mana", 0x5615_3A2C_420C, "float", "20.13", None,
     PointerChain("dummy_game.exe", 0x4040, (0x28, 0x14))),
    ("Experience", 0x5615_3A2C_4210, "double", "412.6", None,
     PointerChain("dummy_game.exe", 0x4040, (0x28, 0x18))),
)


def _fill(window):
    window._on_scan_results(_RESULTS)  # pylint: disable=protected-access
    window.input_value.setText("1350")
    for description, address, value_type, value, frozen, pointer in _TRACKED:
        window._add_address_row(  # pylint: disable=protected-access
            description, address, value_type, value, frozen_value=frozen, pointer=pointer)
    window.btn_next_scan.setEnabled(True)
    window.btn_reset.setEnabled(True)
    window.addr_table.selectRow(0)


@scene("main-window", size=(1280, 800),
       description="Main window with scan results and tracked addresses")
def main_window(studio):
    """The whole window after a few scans for the gold of the test game."""
    window = studio.show_attached()
    _fill(window)
    window.result_table.selectRow(0)
    # Room for every tracked address below the scan area.
    window._scan_area.parentWidget().setSizes([360, 300])  # pylint: disable=protected-access
    settle()
    return window
