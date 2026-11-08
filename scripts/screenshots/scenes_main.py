"""The main window: scan results, value search and the tracked addresses."""

from hermes.core.pointer import PointerChain
from studio import DEMO_NAME, scene, settle

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


@scene("pointer-chain-dialog",
       description="Adding the gold of the test game by its pointer chain")
def pointer_chain_dialog(studio):
    """The dialog of Pointer chain … with the tutorial's gold chain entered."""
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.core.memory import parse_chain
    from hermes.platform.linux import parse_maps
    from hermes.ui.main_window import PointerChainDialog
    from scenes_tools import _GAME_BASE, _GOLD

    # The test game as mapped in the demo: dummy_game.exe at its demo base
    mappings = parse_maps([f"{_GAME_BASE:x}-{_GAME_BASE + 0x5000:x} r-xp 00000000 08:01 1 "
                           f"/opt/game/{DEMO_NAME}"], image_size=lambda _path: None)
    gold = PointerChain(DEMO_NAME, 0x4040, (0x28, 0x0))

    def resolve(text):
        chain = parse_chain(text, mappings)
        return chain, _GOLD if chain == gold else None

    window = studio.show_attached()
    dialog = studio.keep(PointerChainDialog(resolve, "int32", window, studio.translations))
    dialog.chain_input.setText(f"{DEMO_NAME}+0x4040 -> 0x28 -> 0x0")
    dialog.description_input.setText("Gold")
    dialog.show()
    settle()
    return dialog
