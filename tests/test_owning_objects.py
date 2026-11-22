"""The window naming the objects addresses lie in (grembyte.ui.widgets.owning_objects)."""

from grembyte.ui.localization import load_translations
from grembyte.ui.widgets.owning_objects import describe

from tests.test_scripts import _FakeScriptWorker, fixture_qt_app  # pylint: disable=unused-import


def test_answers_are_described_as_class_object_and_field():
    translations = load_translations("en")
    assert describe({"kind": "field", "className": "UIW_ProfileItem_Ergo_C",
                     "name": "UIW_ProfileItem_Ergo_C_0", "field": "PrevSoul"},
                    translations) == ("UIW_ProfileItem_Ergo_C", "UIW_ProfileItem_Ergo_C_0",
                                      "PrevSoul")
    # Without a name the object's address; a namespace is cut off
    assert describe({"kind": "element", "className": "Game.Item[]", "object": "0x10",
                     "field": "[3]"}, translations) == ("Item[]", "0x10", "[3]")
    assert describe({"kind": "static", "className": "Game.Bank", "field": "Gold"},
                    translations) == ("Bank", "static field", "Gold")
    assert describe({"kind": "unknown"}, translations) is None


def test_window_looks_up_the_addresses_and_opens_their_objects(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.ui.widgets import owning_objects, scripts

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    monkeypatch.setattr(owning_objects, "RESOLVE_BATCH", 2)
    translations = load_translations("de")
    panel = scripts.ScriptPanel(translations)
    panel.set_process(4242)
    entries = [{"address": 0x1070, "value": "2476"}, {"address": 0x5000, "value": "2476"},
               {"address": 0x2010, "value": "2476"}]
    dialog = owning_objects.OwningObjectsDialog(translations, panel, entries,
                                                framework=("mscorlib",), truncated=7)
    try:
        assert dialog.windowTitle() == "Objekte an 3 Adressen"
        assert dialog.status_label.text() == "Suche die Objekte von 3 Adressen …"
        worker = panel._worker
        first, second = worker.requests[-2:]
        assert first == ("resolve_references", [0x1070, 0x5000], ("mscorlib",))
        assert second == ("resolve_references", [0x2010], ("mscorlib",))
        worker.references_resolved.emit(len(worker.requests) - 1, [
            {"location": "0x1070", "kind": "field", "object": "0x1000",
             "className": "UIW_ProfileItem_Ergo_C", "name": "UIW_ProfileItem_Ergo_C_0",
             "field": "PrevSoul"},
            {"location": "0x5000", "kind": "unknown"}])
        assert dialog.status_label.text() == "Suche die Objekte von 3 Adressen …"
        worker.references_resolved.emit(len(worker.requests), [
            {"location": "0x2010", "kind": "static", "className": "Game.Bank", "field": "Gold"}])

        assert dialog.status_label.text() == (
            "2 von 3 Adressen liegen in einem Objekt. "
            "Nur die ersten 3 Treffer wurden gesucht, 7 weitere nicht.")
        table = dialog.table
        texts = [[table.item(row, column).text() for column in range(5)] for row in range(3)]
        assert texts == [
            ["0x1070", "2476", "UIW_ProfileItem_Ergo_C", "UIW_ProfileItem_Ergo_C_0", "PrevSoul"],
            ["0x5000", "2476", "in keinem Objekt", "", ""],
            ["0x2010", "2476", "Bank", "statisches Feld", "Gold"]]
        # Addresses in no object are hidden until asked for
        assert [table.isRowHidden(row) for row in range(3)] == [False, True, False]
        dialog.only_objects.setChecked(False)
        assert not table.isRowHidden(1)

        # A live object opens in the inspector; a static field has none
        opened = []
        monkeypatch.setattr(panel.inspections, "inspect",
                            lambda target, name="", instance_name=None: opened.append(
                                (target, name, instance_name)))
        table.selectRow(2)
        assert not dialog.inspect_button.isEnabled()
        table.selectRow(0)
        assert dialog.inspect_button.isEnabled()
        # Without the main window no reference graph
        assert not dialog.references_button.isEnabled()
        dialog.inspect()
        assert opened == [({"address": "0x1000"}, "UIW_ProfileItem_Ergo_C",
                           "UIW_ProfileItem_Ergo_C_0")]
    finally:
        dialog.close()


def test_window_says_so_when_the_lookup_fails(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.ui.widgets import owning_objects, scripts

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    translations = load_translations("en")
    panel = scripts.ScriptPanel(translations)
    detached = owning_objects.OwningObjectsDialog(translations, panel, [{"address": 0x10}])
    assert detached.status_label.text() == "Not attached to a game."
    detached.close()

    panel.set_process(4242)
    dialog = owning_objects.OwningObjectsDialog(translations, panel, [{"address": 0x10}])
    worker = panel._worker
    worker.references_failed.emit(len(worker.requests), "no Unreal runtime")
    assert dialog.status_label.text() == "no Unreal runtime"
    dialog.close()


def test_scan_results_offer_their_objects_in_the_context_menu(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from PySide6.QtCore import QPoint  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtWidgets import QMenu  # pylint: disable=import-error,no-name-in-module

    from grembyte.engines import base as engine_base
    from grembyte.ui import main_window
    from grembyte.ui.widgets import owning_objects, scripts

    class Engine:  # pylint: disable=too-few-public-methods
        framework_prefixes = ("mscorlib",)

        @staticmethod
        def supports(feature):
            return feature == engine_base.OBJECT_REFERENCES

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    monkeypatch.setattr(owning_objects, "MAX_ADDRESSES", 2)
    monkeypatch.setattr(main_window, "MAX_ADDRESSES", 2)
    menus = []

    class Menu(QMenu):  # pylint: disable=too-few-public-methods
        """Records itself instead of showing (exec would wait for a click)."""

        def exec(self, *_args):  # pylint: disable=arguments-differ
            menus.append(self)

    monkeypatch.setattr(main_window, "QMenu", Menu)
    window = main_window.MainWindow("en")
    window.show()
    try:
        window._on_scan_results([{"address": address, "type": "int32", "value": 2476}
                                 for address in (0x1070, 0x2070, 0x3070)])
        table = window.result_table

        def actions():
            menus.clear()
            window._show_result_context_menu(QPoint(5, table.rowViewportPosition(1) + 2))
            return {action.text(): action for action in menus[0].actions()}

        # Only for engines that name objects
        offered = actions()
        assert not offered["Find the object it lies in"].isEnabled()
        assert not offered["Find the objects of all results"].isEnabled()

        window._current_pid = 4242
        window._engine = Engine()
        window.script_panel.set_process(4242)
        offered = actions()
        offered["Find the object it lies in"].trigger()
        worker = window.script_panel._worker
        assert worker.requests[-1] == ("resolve_references", [0x2070], ("mscorlib",))
        actions()["Find the objects of all results"].trigger()
        assert worker.requests[-1] == ("resolve_references", [0x1070, 0x2070], ("mscorlib",))
        dialog = next(widget for widget in qt_app.topLevelWidgets()
                      if isinstance(widget, owning_objects.OwningObjectsDialog)
                      and widget.isVisible() and widget.truncated)
        assert dialog.truncated == 1
        assert dialog.table.item(0, 1).text() == "2476"
    finally:
        for widget in qt_app.topLevelWidgets():
            if isinstance(widget, owning_objects.OwningObjectsDialog):
                widget.close()
        window._current_pid = None
        window.close()


def test_a_field_of_an_object_becomes_the_target_of_an_entry(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.ui.widgets import owning_objects, scripts

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    translations = load_translations("en")
    panel = scripts.ScriptPanel(translations)
    panel.set_process(4242)
    answer = [{"location": "0x1070", "kind": "field", "object": "0x1000",
               "className": "UIW_ProfileItem_Ergo_C", "name": "UIW_ProfileItem_Ergo_C_0",
               "field": "PrevSoul"},
              {"location": "0x2010", "kind": "static", "className": "Game.Bank",
               "field": "Gold"}]
    entries = [{"address": 0x1070, "value": "2476", "type": "int32"},
               {"address": 0x2010, "value": "5", "type": "int32"}]
    dialog = owning_objects.OwningObjectsDialog(translations, panel, entries)
    requested = []
    dialog.target_requested.connect(requested.append)
    try:
        assert dialog.target_button.text() == "Add to address list"
        worker = panel._worker
        worker.references_resolved.emit(len(worker.requests), answer)
        dialog.table.selectRow(1)  # a static field: no object to find again
        assert not dialog.target_button.isEnabled()
        dialog.table.selectRow(0)
        assert dialog.target_button.isEnabled()
        dialog.target_button.click()
        assert requested == [{
            "address": 0x1070, "object": "0x1000", "className": "UIW_ProfileItem_Ergo_C",
            "field": "PrevSoul", "name": "UIW_ProfileItem_Ergo_C_0", "type": "int32",
            "label": "UIW_ProfileItem_Ergo_C.PrevSoul", "entry_id": None}]
    finally:
        dialog.close()

    # From the address list: that entry gets the target, under its own description
    listed = owning_objects.OwningObjectsDialog(translations, panel, [
        {"address": 0x1070, "value": "2476", "type": "int32", "entry_id": 7, "label": "Ergo"}])
    listed.target_requested.connect(requested.append)
    try:
        assert listed.target_button.text() == "Locate the entry by this field"
        worker.references_resolved.emit(len(worker.requests), answer[:1])
        listed.table.selectRow(0)
        listed.request_target()
        assert (requested[-1]["entry_id"], requested[-1]["label"]) == (7, "Ergo")
    finally:
        listed.close()


def test_the_address_list_finds_field_targets_again(qt_app, monkeypatch, tmp_path):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access,too-many-locals
    import os

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from grembyte.core import pointer
    from grembyte.db.base import Base
    from grembyte.db.repository.addresses import AddressRepository, address_pointer
    from grembyte.db.repository.hooks import HookRepository
    from grembyte.engines import base as engine_base
    from grembyte.ui import main_window
    from grembyte.ui.widgets import scripts

    class Engine:  # pylint: disable=too-few-public-methods
        framework_prefixes = ()

        @staticmethod
        def supports(feature):
            return feature == engine_base.OBJECT_REFERENCES

    database = create_engine(f"sqlite:///{tmp_path / 'grembyte.db'}")
    Base.metadata.create_all(database)
    sessions = sessionmaker(bind=database)
    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    monkeypatch.setattr(pointer, "_managed_bases", {})
    pid = os.getpid()
    window = main_window.MainWindow("en")
    try:
        repository = AddressRepository(sessions)
        window._address_repository = repository
        window._process_id = HookRepository(sessions).get_or_create_process("game", "/g/game")
        window._current_pid = pid
        window._engine = Engine()
        window.script_panel.set_process(pid)
        worker = window.script_panel.worker()

        def saved_pointers():
            return [address_pointer(entry) for entry in
                    repository.list_addresses(window._process_id)]

        # From the objects window: a new entry located by the field
        window.add_field_target({
            "address": 0x1070, "object": "0x1000", "className": "UIW_ProfileItem_Ergo_C",
            "field": "PrevSoul", "name": "UIW_ProfileItem_Ergo_C_0", "type": "int32",
            "label": "Ergo", "entry_id": None})
        chain = pointer.object_chain("UIW_ProfileItem_Ergo_C", "PrevSoul",
                                     "UIW_ProfileItem_Ergo_C_0")
        assert saved_pointers() == [chain]
        assert window.addr_table.item(0, 5).text() == "UIW_ProfileItem_Ergo_C.PrevSoul"
        assert pointer.managed_bases(pid) == {chain.module: 0x1070}

        # Checked by the engine, with the object found last; one request at a time
        window._request_object_bases([chain, None])
        assert worker.requests[-1] == ("locate_fields", [{
            "className": "UIW_ProfileItem_Ergo_C", "field": "PrevSoul",
            "name": "UIW_ProfileItem_Ergo_C_0", "hint": "0x1000"}])
        count = len(worker.requests)
        window._object_bases_next = 0.0
        window._request_object_bases([chain])
        assert len(worker.requests) == count
        # Gone (a level changed): the base is forgotten, looked for less often
        worker.fields_located.emit(count, [None])
        assert pointer.managed_bases(pid) == {}
        assert window._object_bases_next > main_window.time.monotonic() + 3
        window._object_bases_next = 0.0
        window._request_object_bases([chain])
        assert worker.requests[-1][1][0]["hint"] is None
        worker.fields_located.emit(len(worker.requests),
                                   [{"address": "0x2070", "object": "0x2000"}])
        assert pointer.managed_bases(pid) == {chain.module: 0x2070}

        # From the object inspector: once the engine names the object, by its field
        window._add_inspected_address({"address": 0x3070, "value_type": "int32",
                                       "label": "Hero.Health"})
        assert worker.requests[-1] == ("resolve_references", [0x3070], ())
        worker.references_resolved.emit(len(worker.requests), [{
            "location": "0x3070", "kind": "field", "object": "0x3000",
            "className": "BP_Hero_C", "name": "BP_Hero_C_0", "field": "Health"}])
        health = pointer.object_chain("BP_Hero_C", "Health", "BP_Hero_C_0")
        assert saved_pointers() == [chain, health]
        assert window.addr_table.item(1, 0).text() == "Hero.Health"
        assert window.addr_table.item(1, 5).text() == "BP_Hero_C.Health"
    finally:
        window._refresh_timer.stop()
        window._current_pid = None
        window.close()
        database.dispose()
