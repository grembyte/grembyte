"""Export of the entries saved per game as JSON, its import, and their windows."""

import json
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from grembyte.db import models  # noqa: F401  pylint: disable=unused-import
from grembyte.db.base import Base
from grembyte.db.models import (
    Address,
    FavoriteRecord,
    GameNote,
    GameScript,
    Hook,
    HookTemplate,
    MethodHook,
    PointerScan,
    Process,
)
from grembyte.db.repository.game_export import (
    EXPORT_FORMAT,
    GameExportRepository,
    ImportFormatError,
    export_file_name,
    game_platform,
    read_export,
)

from tests.test_hooks import fixture_qt_app, fixture_translations  # pylint: disable=unused-import

PROTON_GE = "/steam/compatibilitytools.d/GE-Proton11-7/files/lib/wine/i386-unix/wine64-preloader"
PROTON_EXP = "/steam/common/Proton - Experimental/files/lib/wine/i386-unix/wine64-preloader"
TODAY = date(2026, 10, 26)


@pytest.fixture(name="sessions")
def fixture_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'grembyte.db'}")
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
            GameScript(process_id=ds_exp.id, label="Souls", source="ctx.log(1);",
                       mode="repeat", seconds=2.5, enabled=False),
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
        == "grembyte_Windows_DarkSoulsIII.ex_2026-10-26.json"
    assert export_file_name("Linux", 'a/b:c*', TODAY) == "grembyte_Linux_a_b_c__2026-10-26.json"


def test_games_are_grouped_by_name_and_empty_ones_left_out(repository):
    games = {game.name: game for game in repository.list_games()}
    assert list(games) == ["DarkSoulsIII.ex", "PillarsOfEternity"]
    souls = games["DarkSoulsIII.ex"]
    assert souls.paths == [PROTON_GE, PROTON_EXP] and souls.platform == "Windows"
    assert souls.counts == {"addresses": 1, "hooks": 1, "method_hooks": 0, "favorites": 0,
                            "scripts": 1, "notes": 0, "pointer_scans": 1}


def test_export_holds_all_processes_of_the_game_and_their_templates(repository):
    export = repository.export_game("DarkSoulsIII.ex")
    assert export["format"] == EXPORT_FORMAT and export["platform"] == "Windows"
    assert [process["path"] for process in export["processes"]] == [PROTON_GE, PROTON_EXP]
    [souls] = export["processes"][0]["addresses"]
    assert souls["label"] == "Souls" and souls["pointer"] == {"module": "DarkSoulsIII.exe"}
    assert "pointer_scan" not in souls and "process_id" not in souls
    assert export["processes"][1]["hooks"][0]["transform_params"] == {"maximum": 450}
    [script] = export["processes"][1]["scripts"]
    assert (script["label"], script["source"], script["mode"], script["seconds"],
            script["enabled"]) == ("Souls", "ctx.log(1);", "repeat", 2.5, False)
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
    from grembyte.ui.widgets.game_export import GameExportDialog

    dialog = GameExportDialog(translations, repository, current="PillarsOfEternity",
                              today=TODAY)
    assert dialog.selected_game.name == "PillarsOfEternity"
    assert dialog.file_name.text() == "grembyte_Linux_PillarsOfEternity_2026-10-26.json"
    # No pointer scans saved: nothing to tick.
    assert not dialog.pointer_scans.isChecked() and not dialog.pointer_scans.isEnabled()

    dialog.table.selectRow(0)
    assert dialog.file_name.text() == "grembyte_Windows_DarkSoulsIII.ex_2026-10-26.json"
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


def _process_id(sessions, name, path):
    with sessions() as session:
        return session.scalars(
            select(Process.id).where(Process.name == name, Process.path == path)).one()


def _labels(sessions, model, process_id):
    with sessions() as session:
        return [record.label for record in session.scalars(
            select(model).where(model.process_id == process_id).order_by(model.id))]


def test_import_mixes_in_what_the_target_does_not_hold(sessions, repository):
    export = repository.export_game("DarkSoulsIII.ex", pointer_scans=True)
    # A third Proton version: a new process, as attaching saves it.
    with sessions() as session:
        target = Process(name="DarkSoulsIII.ex", path="/steam/Proton 12/wine64-preloader")
        session.add(target)
        session.commit()
        target_id = target.id

    result = repository.import_export(export, target_id)
    assert result.added == {"addresses": 1, "hooks": 1, "method_hooks": 0, "favorites": 0,
                            "scripts": 1, "notes": 0, "pointer_scans": 1}
    assert result.templates == 0  # "Begrenzen" exists with the same code
    with sessions() as session:
        [hook] = session.scalars(select(Hook).where(Hook.process_id == target_id))
        assert session.get(HookTemplate, hook.template_id).name == "Begrenzen"
        [address] = session.scalars(select(Address).where(Address.process_id == target_id))
        scan = session.scalars(
            select(PointerScan).where(PointerScan.address_id == address.id)).one()
        # Imported scans are kept packed like scanned ones.
        assert scan.chains == [] and scan.packed is not None
        assert scan.chain_list() == [
            {"module": "DarkSoulsIII.exe", "base_offset": 0, "offsets": [8]}]
        assert session.get(Process, target_id).path == "/steam/Proton 12/wine64-preloader"
        [script] = session.scalars(select(GameScript).where(GameScript.process_id == target_id))
        assert (script.label, script.mode, script.seconds, script.enabled) == (
            "Souls", "repeat", 2.5, False)

    again = repository.import_export(export, target_id)
    assert sum(again.added.values()) == 0
    assert again.skipped == {"addresses": 1, "hooks": 1, "method_hooks": 0, "favorites": 0,
                             "scripts": 1, "notes": 0}

    # Exports made before scripts were exported hold none.
    for process in export["processes"]:
        del process["scripts"]
    assert sum(repository.import_export(export, target_id).added.values()) == 0


def test_import_replaces_the_targets_entries(sessions, repository):
    poe = _process_id(sessions, "PillarsOfEternity", "/steam/common/PoE/PillarsOfEternity")
    export = repository.export_game("DarkSoulsIII.ex", pointer_scans=True)
    export["processes"][0]["addresses"][0]["label"] = "Souls (imported)"

    result = repository.import_export(export, poe, replace=True)
    assert result.added["addresses"] == 1 and result.added["pointer_scans"] == 1
    assert _labels(sessions, Address, poe) == ["Souls (imported)"]
    assert _labels(sessions, MethodHook, poe) == []
    with sessions() as session:
        assert not list(session.scalars(
            select(FavoriteRecord).where(FavoriteRecord.process_id == poe)))
    # Again: the pointer scan of the replaced address is gone as well.
    repository.import_export(export, poe, replace=True)
    assert _labels(sessions, Address, poe) == ["Souls (imported)"]
    with sessions() as session:
        assert len(list(session.scalars(select(PointerScan)))) == 2


def test_a_changed_template_is_added_under_a_new_name(sessions, repository):
    export = repository.export_game("DarkSoulsIII.ex")
    export["templates"][0]["source"] = "function (ctx) { return 1; }"
    target = _process_id(sessions, "PillarsOfEternity", "/steam/common/PoE/PillarsOfEternity")

    assert repository.import_export(export, target).templates == 1
    assert repository.import_export(export, target).templates == 0
    with sessions() as session:
        [hook] = session.scalars(select(Hook).where(Hook.process_id == target))
        assert session.get(HookTemplate, hook.template_id).name == "Begrenzen (Import)"


def _note_bodies(sessions, process_id):
    with sessions() as session:
        return [(note.title, note.body) for note in session.scalars(
            select(GameNote).where(GameNote.process_id == process_id).order_by(GameNote.id))]


def test_notes_are_exported_and_a_game_with_only_notes_counts(sessions, repository):
    empty = _process_id(sessions, "abrt-dbus", "/usr/sbin/abrt-dbus")
    with sessions() as session:
        session.add(GameNote(process_id=empty, body="## Gold\n`AddGold(int)`"))
        session.commit()
    games = {game.name: game for game in repository.list_games()}
    assert games["abrt-dbus"].counts["notes"] == 1
    [process] = repository.export_game("abrt-dbus")["processes"]
    assert [note["body"] for note in process["notes"]] == ["## Gold\n`AddGold(int)`"]
    assert repository.delete_entries(empty, ["notes"])["notes"] == 1
    assert _note_bodies(sessions, empty) == []


def test_empty_notes_do_not_count_or_export(sessions, repository):
    empty = _process_id(sessions, "abrt-dbus", "/usr/sbin/abrt-dbus")
    with sessions() as session:
        session.add(GameNote(process_id=empty, body=" \n "))
        session.commit()

    process = next(row for row in repository.list_processes() if row.process_id == empty)
    assert process.counts["notes"] == 0
    assert "abrt-dbus" not in {game.name for game in repository.list_games()}
    assert repository.export_game("abrt-dbus")["processes"] == []


def test_notes_count_and_export_alike_when_only_other_space_is_in_them(sessions, repository):
    # SQL's trim() and the export agree on what an empty note is
    empty = _process_id(sessions, "abrt-dbus", "/usr/sbin/abrt-dbus")
    with sessions() as session:
        session.add(GameNote(process_id=empty, body="\u00a0\f"))
        session.commit()
    process = next(row for row in repository.list_processes() if row.process_id == empty)
    [exported] = repository.export_game("abrt-dbus")["processes"]
    assert process.counts["notes"] == len(exported["notes"]) == 1


def test_imported_notes_are_merged_and_never_deleted(sessions, repository):
    souls = _process_id(sessions, "DarkSoulsIII.ex", PROTON_GE)
    poe = _process_id(sessions, "PillarsOfEternity", "/steam/common/PoE/PillarsOfEternity")
    with sessions() as session:
        session.add_all([GameNote(process_id=souls, body="Souls: `PlayerGameData`"),
                         GameNote(process_id=poe, body="Gold: `AddGold`")])
        session.commit()
    export = repository.export_game("DarkSoulsIII.ex")
    assert export["processes"][0]["notes"][0]["body"] == "Souls: `PlayerGameData`"

    result = repository.import_export(export, poe, replace=True)
    assert result.added["notes"] == 1
    assert _note_bodies(sessions, poe) == [
        ("", "Gold: `AddGold`\n\n---\n\nSouls: `PlayerGameData`")]
    # Text the note holds already is not appended again
    result = repository.import_export(export, poe)
    assert result.added["notes"] == 0 and result.skipped["notes"] == 1
    assert len(_note_bodies(sessions, poe)) == 1

    # A process without notes takes them as they are; an older export has none
    empty = _process_id(sessions, "abrt-dbus", "/usr/sbin/abrt-dbus")
    repository.import_export(export, empty)
    assert _note_bodies(sessions, empty) == [("", "Souls: `PlayerGameData`")]
    for process in export["processes"]:
        del process["notes"]
    assert repository.import_export(export, empty).added["notes"] == 0


def test_only_exports_of_grembyte_are_read(tmp_path, repository):
    path = tmp_path / "export.json"
    repository.write_export("DarkSoulsIII.ex", path)
    assert read_export(path)["game"] == "DarkSoulsIII.ex"
    for content in ("{", "[]", '{"format": "other"}',
                    json.dumps({"format": EXPORT_FORMAT, "version": 99, "processes": []})):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ImportFormatError):
            read_export(path)



def test_exports_from_before_the_rename_are_read(tmp_path, repository):
    path = tmp_path / "export.json"
    repository.write_export("DarkSoulsIII.ex", path)
    export = json.loads(path.read_text(encoding="utf-8"))
    export["format"] = "hermes-hook-export"
    path.write_text(json.dumps(export), encoding="utf-8")
    assert read_export(path)["game"] == "DarkSoulsIII.ex"

def test_import_dialog_selects_the_target_and_imports(qt_app, translations, sessions,
                                                      repository, tmp_path, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtWidgets import QMessageBox  # pylint: disable=import-error,no-name-in-module
    from grembyte.ui.widgets.game_export import GameImportDialog

    path = tmp_path / "export.json"
    repository.write_export("PillarsOfEternity", path)
    souls = _process_id(sessions, "DarkSoulsIII.ex", PROTON_EXP)
    dialog = GameImportDialog(translations, repository, current=souls)
    assert not dialog.import_button.isEnabled()
    assert dialog.selected_process.process_id == souls

    dialog.load_file(path)
    # The attached process is of another game: the file's game is selected.
    assert dialog.selected_process.name == "PillarsOfEternity"
    assert dialog.merge.isChecked() and dialog.import_button.isEnabled()
    dialog.table.selectRow(
        [process.process_id for process in dialog.processes].index(souls))
    assert "PillarsOfEternity" in dialog.status.text()

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *_args: QMessageBox.StandardButton.Yes)
    dialog.replace.setChecked(True)
    dialog.import_button.click()
    assert dialog.process_id == souls
    assert dialog.result.added["method_hooks"] == 1 and dialog.result.added["favorites"] == 1
    assert _labels(sessions, Hook, souls) == []
