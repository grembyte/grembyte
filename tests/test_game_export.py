"""Export of the entries saved per game as JSON, and its window."""

import json
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hermes.db import models  # noqa: F401  pylint: disable=unused-import
from hermes.db.base import Base
from hermes.db.models import (
    Address,
    FavoriteRecord,
    Hook,
    HookTemplate,
    MethodHook,
    PointerScan,
    Process,
)
from hermes.db.repository.game_export import (
    EXPORT_FORMAT,
    GameExportRepository,
    export_file_name,
    game_platform,
)

from tests.test_hooks import fixture_qt_app, fixture_translations  # pylint: disable=unused-import

PROTON_GE = "/steam/compatibilitytools.d/GE-Proton11-7/files/lib/wine/i386-unix/wine64-preloader"
PROTON_EXP = "/steam/common/Proton - Experimental/files/lib/wine/i386-unix/wine64-preloader"
TODAY = date(2026, 10, 26)


@pytest.fixture(name="sessions")
def fixture_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _hook(process_id, label, template_id=None):
    return Hook(process_id=process_id, label=label, module="DarkSoulsIII.exe",
                module_offset=0x1431225, instruction_bytes="8983d8000000",
                value_type="int32", template_id=template_id, transform_name="Begrenzen",
                transform_source="function (ctx) {}", transform_parameters=[],
                transform_params={"maximum": 450})


@pytest.fixture(name="repository")
def fixture_repository(sessions):
    with sessions() as session:
        used = HookTemplate(name="Begrenzen", source="function (ctx) {}", parameters=[])
        unused = HookTemplate(name="Unused", source="function (ctx) {}", parameters=[])
        ds_ge = Process(name="DarkSoulsIII.ex", path=PROTON_GE)
        ds_exp = Process(name="DarkSoulsIII.ex", path=PROTON_EXP)
        poe = Process(name="PillarsOfEternity", path="/steam/common/PoE/PillarsOfEternity")
        empty = Process(name="abrt-dbus", path="/usr/sbin/abrt-dbus")
        session.add_all([used, unused, ds_ge, ds_exp, poe, empty])
        session.flush()
        souls = Address(process_id=ds_ge.id, label="Souls", value_type="int32",
                        address=0x7FFF0000, pointer={"module": "DarkSoulsIII.exe"})
        session.add_all([
            souls,
            _hook(ds_exp.id, "TP", template_id=used.id),
            MethodHook(process_id=poe.id, label="Gold", managed={"token": 1},
                       transform_name="x", transform_source="", transform_parameters=[],
                       transform_params={}),
            FavoriteRecord(process_id=poe.id, kind="method", key="k", data={}),
        ])
        session.flush()
        session.add(PointerScan(address_id=souls.id, max_depth=3, max_offset=0x400,
                                chains=[{"module": "DarkSoulsIII.exe", "offsets": [8]}]))
        session.commit()
    return GameExportRepository(sessions)


def test_platform_comes_from_the_path_not_the_name():
    assert game_platform([PROTON_GE]) == "Windows"
    assert game_platform([r"C:\Games\DarkSoulsIII.exe"]) == "Windows"
    # A Linux program may be called .exe (like the dummy game of the tests).
    assert game_platform(["/home/user/build/dummy_game.exe"]) == "Linux"
    assert game_platform(["/steam/common/PoE/PillarsOfEternity"]) == "Linux"


def test_file_name_is_platform_process_name_and_date():
    assert export_file_name("Windows", "DarkSoulsIII.ex", TODAY) \
        == "hermes-hook_Windows_DarkSoulsIII.ex_2026-10-26.json"
    assert export_file_name("Linux", 'a/b:c*', TODAY) == "hermes-hook_Linux_a_b_c__2026-10-26.json"


def test_games_are_grouped_by_name_and_empty_ones_left_out(repository):
    games = {game.name: game for game in repository.list_games()}
    assert list(games) == ["DarkSoulsIII.ex", "PillarsOfEternity"]
    souls = games["DarkSoulsIII.ex"]
    assert souls.paths == [PROTON_GE, PROTON_EXP] and souls.platform == "Windows"
    assert souls.counts == {"addresses": 1, "hooks": 1, "method_hooks": 0, "favorites": 0,
                            "pointer_scans": 1}


def test_export_holds_all_processes_of_the_game_and_their_templates(repository):
    export = repository.export_game("DarkSoulsIII.ex")
    assert export["format"] == EXPORT_FORMAT and export["platform"] == "Windows"
    assert [process["path"] for process in export["processes"]] == [PROTON_GE, PROTON_EXP]
    [souls] = export["processes"][0]["addresses"]
    assert souls["label"] == "Souls" and souls["pointer"] == {"module": "DarkSoulsIII.exe"}
    assert "pointer_scan" not in souls and "process_id" not in souls
    assert export["processes"][1]["hooks"][0]["transform_params"] == {"maximum": 450}
    assert [template["name"] for template in export["templates"]] == ["Begrenzen"]
    json.dumps(export)  # JSON-ready, dates included


def test_pointer_scans_only_when_asked_for(repository):
    export = repository.export_game("DarkSoulsIII.ex", pointer_scans=True)
    scan = export["processes"][0]["addresses"][0]["pointer_scan"]
    assert scan["chains"] == [{"module": "DarkSoulsIII.exe", "offsets": [8]}]
    assert export["pointer_scans"] is True


def test_dialog_suggests_the_name_and_keeps_a_changed_one(qt_app, translations, repository,
                                                          tmp_path):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.game_export import GameExportDialog

    dialog = GameExportDialog(translations, repository, current="PillarsOfEternity",
                              today=TODAY)
    assert dialog.selected_game.name == "PillarsOfEternity"
    assert dialog.file_name.text() == "hermes-hook_Linux_PillarsOfEternity_2026-10-26.json"
    # No pointer scans saved: nothing to tick.
    assert not dialog.pointer_scans.isChecked() and not dialog.pointer_scans.isEnabled()

    dialog.table.selectRow(0)
    assert dialog.file_name.text() == "hermes-hook_Windows_DarkSoulsIII.ex_2026-10-26.json"
    assert dialog.pointer_scans.isEnabled() and not dialog.pointer_scans.isChecked()

    dialog.file_name.setText("souls-backup")
    dialog.table.selectRow(1)
    assert dialog.file_name.text() == "souls-backup"
    dialog.table.selectRow(0)
    dialog.folder.setText(str(tmp_path))
    dialog.pointer_scans.setChecked(True)
    dialog.export_button.click()
    assert dialog.exported_path == tmp_path / "souls-backup.json"
    export = json.loads(dialog.exported_path.read_text(encoding="utf-8"))
    assert export["game"] == "DarkSoulsIII.ex"
    assert export["processes"][0]["addresses"][0]["pointer_scan"] is not None
