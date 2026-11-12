"""Calling static methods: reading the arguments typed in, the call dialog."""

import os
import struct

import pytest

from grembyte.features.catalog import CatalogMethod
from grembyte.features.method_call import (
    BOOLEAN,
    CHAR,
    I4,
    R4,
    R8,
    STRING,
    U1,
    VALUETYPE,
    ArgumentError,
    argument_text,
    can_call,
    parse_argument,
    supported,
)
from grembyte.ui.localization import load_translations


GIVE_ITEM = CatalogMethod("Assembly-CSharp", "Scripts", "GiveItem", "(string,int)", "void",
                          True, 0x06000010, "Scripts:GiveItem (string,int)")
UPDATE = CatalogMethod("Assembly-CSharp", "Game.Loop", "Update", "()", "void",
                       False, 0x06000011, "Game.Loop:Update ()")
GIVE_HEALTH = CatalogMethod("/Script/Alabama", "AlabamaCheatManager", "GiveHealth",
                            "(float,int32&)", "void", False, 0x1234,
                            "AlabamaCheatManager:GiveHealth (float,int32&)", "unreal")


def _type(kind, name="T", **extra):
    return {"kind": kind, "byref": False, "name": name, **extra}


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    # pylint: disable=import-outside-toplevel
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    return QApplication.instance() or QApplication([])


@pytest.fixture(name="translations")
def fixture_translations():
    return load_translations("de")


def test_arguments_are_read_by_their_type():
    assert parse_argument(_type(STRING), " Sword ") == " Sword "
    assert parse_argument(_type(I4), " 1_000 ") == 1000
    assert parse_argument(_type(I4), "0x10") == 16
    assert parse_argument(_type(I4), "-5") == -5
    assert parse_argument(_type(R4), "1,5") == 1.5
    assert parse_argument(_type(BOOLEAN), "Ja") is True
    assert parse_argument(_type(CHAR), "x") == "x"
    assert parse_argument(_type(0x12), "null") is None
    assert parse_argument(_type(0x12), "0x7f00") == {"address": "0x7f00"}
    assert parse_argument(_type(I4, enumName="Game.DamageType"), "2") == 2
    attribute = _type(U1, enumName="EPrimaryAttribute",
                      enumValues={"0": "Might", "2": "Dexterity"})
    assert parse_argument(attribute, " dexterity ") == 2
    assert parse_argument(attribute, "0") == 0
    with pytest.raises(ArgumentError):
        parse_argument(attribute, "Charm")


def test_enums_with_known_names_are_chosen_from_a_list(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.ui.widgets import method_call
    from grembyte.ui.widgets.method_call import MethodCallDialog

    dialog = MethodCallDialog.__new__(MethodCallDialog)  # pylint: disable=no-value-for-parameter
    dialog.translations, dialog._editors = translations, []
    values = {"0": "Might", "1": "Constitution", "2": "Dexterity"}
    attribute = _type(U1, "EPrimaryAttribute", enumName="EPrimaryAttribute", enumValues=values)

    editor = dialog._editor(attribute, None)
    assert [editor.itemText(i) for i in range(editor.count())] == [
        "Might (0)", "Constitution (1)", "Dexterity (2)"]
    assert editor.currentText() == "" and editor.lineEdit().placeholderText() == (
        translations["call.placeholder_enum_choice"])
    editor.setCurrentIndex(2)
    assert method_call._editor_text(editor) == "Dexterity"
    assert parse_argument(attribute, method_call._editor_text(editor)) == 2
    editor.setEditText(" 7 ")  # a value without a name
    assert parse_argument(attribute, method_call._editor_text(editor)) == 7
    # The last choice comes back, a number typed in too.
    assert dialog._editor(attribute, "Constitution").currentText() == "Constitution (1)"
    assert dialog._editor(attribute, "7").currentText() == "7"
    # Mono's nested classes read as C# writes them, Unreal's paths stay.
    assert method_call._type_name("StrongholdAdventure/Type") == "StrongholdAdventure.Type"
    assert method_call._type_name("/Script/Engine.Actor") == "/Script/Engine.Actor"


def test_unreal_types_are_typed_in_as_text_and_bools():
    fstring = _type(0, "FString", engine="string")
    fname = _type(0, "FName", engine="name")
    flag = _type(0, "bool", engine="bool")
    assert all(supported(param_type) for param_type in (fstring, fname, flag))
    assert parse_argument(fstring, " Bob ") == " Bob "
    assert parse_argument(fname, "Sword_3") == "Sword_3"
    assert parse_argument(flag, "wahr") is True
    # Containers, FText and delegates cannot be typed in.
    for engine in ("array", "map", "set", "text", "weak", "interface", "other"):
        assert not supported(_type(0, "TArray<int32>", engine=engine))


def _vector(kind=R8):
    return _type(VALUETYPE, "FVector", structSize=24, fields=[
        {"name": name, "kind": kind, "offset": 8 * i} for i, name in enumerate("XYZ")])


def test_structs_of_numbers_are_typed_in_by_their_fields():
    vector = _vector()
    assert supported(vector)
    in_order = parse_argument(vector, "1, 2,5 -3")
    assert in_order["struct"] == struct.pack("<ddd", 1, 2.5, -3).hex()
    assert argument_text(in_order) == "{X=1.0, Y=2.5, Z=-3.0}"
    # By name, in any case and order; the fields not named are 0
    named = parse_argument(vector, "z = 3; x=1")
    assert named["struct"] == struct.pack("<ddd", 1, 0, 3).hex()
    # Integers and enum names, at their offsets in a larger struct
    point = _type(VALUETYPE, "FIntPoint", structSize=12, fields=[
        {"name": "X", "kind": I4, "offset": 0},
        {"name": "Kind", "kind": U1, "offset": 8, "enumValues": {"2": "Dexterity"}}])
    assert parse_argument(point, "-1 dexterity")["struct"] == "ffffffff0000000002000000"
    # Without its fields (a struct of structs, Mono's Vector3 unread) it is not supported.
    assert not supported(_type(VALUETYPE, "FTransform", structSize=96))


@pytest.mark.parametrize("text, key", [
    ("1 2", "call.error_struct_count"),
    ("X=1 2", "call.error_struct_mixed"),
    ("W=1", "call.error_struct_field"),
    ("1 zwei 3", "call.error_number"),
])
def test_wrong_struct_values_say_why(text, key, translations):
    with pytest.raises(ArgumentError) as raised:
        parse_argument(_vector(), text)
    assert raised.value.key == key
    assert translations[key].format(**raised.value.values)


@pytest.mark.parametrize("param_type, text, key", [
    (_type(I4), "viel", "call.error_integer"),
    (_type(U1), "256", "call.error_range"),
    (_type(I4), "2147483648", "call.error_range"),
    (_type(R4), "nan", "call.error_number"),
    (_type(BOOLEAN), "vielleicht", "call.error_bool"),
    (_type(CHAR), "ab", "call.error_char"),
    (_type(0x12), "Schwert", "call.error_address"),
    (_type(VALUETYPE, "UnityEngine.Vector3"), "1,2,3", "call.error_struct"),
])
def test_wrong_arguments_say_why(param_type, text, key, translations):
    with pytest.raises(ArgumentError) as raised:
        parse_argument(param_type, text)
    assert raised.value.key == key
    assert translations[key].format(**raised.value.values)


def test_guids_are_passed_in_the_byte_order_of_dotnet():
    guid = _type(VALUETYPE, "System.Guid")
    value = parse_argument(guid, " {29EA711E-B1E9-47D6-BEDA-C89DCE74B62C} ")
    # Data1 (int), Data2 and Data3 (short) little-endian, the last 8 bytes as they are
    assert value == {"struct": "1e71ea29e9b1d647bedac89dce74b62c",
                     "guid": "29ea711e-b1e9-47d6-beda-c89dce74b62c"}
    assert argument_text(value) == "29ea711e-b1e9-47d6-beda-c89dce74b62c"
    with pytest.raises(ArgumentError) as raised:
        parse_argument(guid, "pollaxe_exceptional")
    assert raised.value.key == "call.error_guid"


def test_arguments_are_logged_as_written_in_code():
    assert [argument_text(value) for value in ("Sword", 3, True, None, {"address": "0x10"})] \
        == ['"Sword"', "3", "true", "null", "0x10"]


class _FakeWorker:
    """Stands in for MethodMonitorWorker: records the requests; the test answers."""

    def __init__(self, _pid):
        # pylint: disable=import-outside-toplevel
        from PySide6.QtCore import QObject, Signal  # pylint: disable=import-error,no-name-in-module

        class Signals(QObject):  # pylint: disable=too-few-public-methods
            call_described = Signal(int, object)
            called = Signal(int, object)
            call_failed = Signal(int, str)
            worker_failed = Signal(str)
            inspected = Signal(int, object)
            inspect_failed = Signal(int, str)
            evaluated = Signal(int, object)
            evaluate_failed = Signal(int, str)
            methods_listed = Signal(int, object)
            methods_failed = Signal(int, str)
            text_fetched = Signal(int, object)
            text_failed = Signal(int, str)

        self.signals = Signals()
        self.call_described = self.signals.call_described
        self.called = self.signals.called
        self.call_failed = self.signals.call_failed
        self.worker_failed = self.signals.worker_failed
        self.inspected = self.signals.inspected
        self.inspect_failed = self.signals.inspect_failed
        self.evaluated = self.signals.evaluated
        self.evaluate_failed = self.signals.evaluate_failed
        self.methods_listed = self.signals.methods_listed
        self.methods_failed = self.signals.methods_failed
        self.text_fetched = self.signals.text_fetched
        self.text_failed = self.signals.text_failed
        self.requests = []

    def start(self):
        pass

    def describe_call(self, identity, instance=False):
        self.requests.append(("describe", identity, instance) if instance
                             else ("describe", identity))
        return len(self.requests)

    def call_found(self, identity, class_name, object_text, arguments):
        self.requests.append(("call_found", identity, class_name, object_text, arguments))
        return len(self.requests)

    def call(self, identity, arguments):
        self.requests.append(("call", identity, arguments))
        return len(self.requests)

    def inspect(self, target):
        self.requests.append(("inspect", target))
        return len(self.requests)

    def evaluate(self, target, names):
        self.requests.append(("evaluate", target, names))
        return len(self.requests)

    def list_methods(self, target):
        self.requests.append(("methods", target))
        return len(self.requests)

    def describe_method(self, method):
        self.requests.append(("describe_method", method))
        return len(self.requests)

    def call_on(self, target, method, arguments):
        self.requests.append(("call_on", target, method, arguments))
        return len(self.requests)

    def requestInterruption(self):  # pylint: disable=invalid-name
        pass

    def isRunning(self):  # pylint: disable=invalid-name
        return False


def test_call_dialog_reads_arguments_and_logs_results(qt_app, translations, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.ui.widgets import method_call

    monkeypatch.setattr(method_call, "MethodMonitorWorker", _FakeWorker)
    monkeypatch.setattr(method_call, "_last_arguments", {})
    dialog = method_call.MethodCallDialog(translations, 1234)
    worker = dialog._worker
    dialog.set_method(GIVE_ITEM)
    assert worker.requests == [("describe", GIVE_ITEM.identity())]
    assert not dialog.call_button.isEnabled()
    worker.call_described.emit(1, {"params": [
        {"name": "itemName", "type": _type(STRING, "System.String")},
        {"name": "count", "type": _type(I4, "System.Int32")}],
        "returns": _type(0x01, "System.Void")})
    assert dialog.form.rowCount() == 2 and dialog.call_button.isEnabled()

    name, count = dialog._editors
    name.setText("Sword")
    count.setText("drei")
    dialog.call_button.click()
    assert len(worker.requests) == 1
    assert dialog.status_label.text() == "count: „drei“ ist keine Ganzzahl"

    count.setText("3")
    count.returnPressed.emit()
    assert worker.requests[1] == ("call", GIVE_ITEM.identity(), ["Sword", 3])
    assert not dialog.call_button.isEnabled()  # until the game answered
    worker.called.emit(2, {"result": None})
    assert dialog.log.toPlainText().endswith('Scripts:GiveItem("Sword", 3)  → (void)')
    assert dialog.call_button.isEnabled()

    dialog.call_button.click()
    worker.called.emit(3, {"exception": "System.NullReferenceException"})
    assert dialog.log.toPlainText().endswith("⚠ System.NullReferenceException")

    # The arguments are kept for the next time the method is shown.
    dialog.set_method(GIVE_ITEM)
    worker.call_described.emit(4, {"params": [
        {"name": "itemName", "type": _type(STRING)}, {"name": "count", "type": _type(I4)}],
        "returns": _type(0x01)})
    assert [editor.text() for editor in dialog._editors] == ["Sword", "3"]

    # A struct cannot be typed in.
    dialog.set_method(GIVE_ITEM)
    worker.call_described.emit(5, {"params": [
        {"name": "position", "type": _type(VALUETYPE, "UnityEngine.Vector3")}],
        "returns": _type(0x01)})
    assert not dialog.call_button.isEnabled()
    dialog.close()


def test_unreal_instance_functions_run_on_a_found_object(qt_app, translations, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from PySide6.QtWidgets import QCheckBox  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets import method_call

    monkeypatch.setattr(method_call, "MethodMonitorWorker", _FakeWorker)
    monkeypatch.setattr(method_call, "_last_arguments", {})
    assert can_call(GIVE_HEALTH) and not can_call(UPDATE)  # Mono finds no objects for it
    dialog = method_call.MethodCallDialog(translations, 1234)
    worker = dialog._worker
    dialog.set_method(GIVE_HEALTH)
    assert worker.requests == [("describe", GIVE_HEALTH.identity(), True)]
    assert "AlabamaCheatManager" in dialog.hint_label.text()
    # An object field first, then the inputs; an output gets none.
    worker.call_described.emit(1, {"params": [
        {"name": "Amount", "type": _type(R4, "float")},
        {"name": "bQuiet", "type": _type(0, "bool", engine="bool")},
        {"name": "Healed", "type": _type(I4, "int32", byref=True), "out": True}],
        "returns": _type(0x01, "void")})
    assert dialog.form.rowCount() == 4
    assert "AlabamaCheatManager" in dialog.object_edit.placeholderText()
    amount, quiet = dialog._editors
    assert isinstance(quiet, QCheckBox)
    assert dialog.call_button.isEnabled()
    amount.setText("50")
    quiet.setChecked(True)
    dialog.call_button.click()
    assert worker.requests[1] == ("call_found", GIVE_HEALTH.identity(), "AlabamaCheatManager",
                                  "", [50.0, True, None])
    worker.called.emit(2, {"result": None, "outputs": [
        {"name": "Healed", "value": {"text": "35"}}], "object": {
            "text": 'UBP_CheatManager_C "BP_CheatManager_C_0"@0x7f00'}})
    assert dialog.log.toPlainText().endswith(
        'AlabamaCheatManager:GiveHealth(50.0, true) auf UBP_CheatManager_C '
        '"BP_CheatManager_C_0"@0x7f00  → (void); Healed = 35')

    # The object typed in goes with the call, and is kept with the arguments.
    dialog.object_edit.setText("BP_CheatManager_C_1")
    dialog.call_button.click()
    assert worker.requests[2][3] == "BP_CheatManager_C_1"
    dialog.set_method(GIVE_HEALTH)
    worker.call_described.emit(4, {"params": [
        {"name": "Amount", "type": _type(R4, "float")},
        {"name": "bQuiet", "type": _type(0, "bool", engine="bool")},
        {"name": "Healed", "type": _type(I4, "int32", byref=True), "out": True}],
        "returns": _type(0x01, "void")})
    assert dialog.object_edit.text() == "BP_CheatManager_C_1"
    assert dialog._editors[0].text() == "50" and dialog._editors[1].isChecked()

    # A TArray cannot be typed in; a struct of numbers can.
    dialog.set_method(GIVE_HEALTH)
    worker.call_described.emit(5, {"params": [
        {"name": "Items", "type": _type(0, "TArray<int32>", engine="array")}],
        "returns": _type(0x01, "void")})
    assert not dialog.call_button.isEnabled()
    dialog.set_method(GIVE_HEALTH)
    worker.call_described.emit(6, {"params": [{"name": "Where", "type": _vector()}],
                                   "returns": _type(0x01, "void")})
    assert dialog.call_button.isEnabled()
    assert dialog._editors[0].placeholderText().startswith("X Y Z, z. B. 1 2 3 oder X=1; Y=2")
    dialog.close()


def test_only_static_methods_can_be_called(qt_app, translations, tmp_path):
    # pylint: disable=import-outside-toplevel,unused-argument
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from grembyte.db import models  # noqa: F401  pylint: disable=unused-import
    from grembyte.db.base import Base
    from grembyte.db.repository.favorites import FavoriteRepository
    from grembyte.db.repository.hooks import HookRepository
    from grembyte.ui.widgets.favorites import FavoritesDialog, FavoriteStore

    engine = create_engine(f"sqlite:///{tmp_path / 'grembyte.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    store = FavoriteStore(FavoriteRepository(sessions))
    store.set_process(HookRepository(sessions).get_or_create_process("game", "/opt/game"))
    store.toggle(GIVE_ITEM, "catalog")
    store.toggle(UPDATE, "catalog")
    dialog = FavoritesDialog(translations, store)
    requested = []
    dialog.call_requested.connect(requested.append)

    dialog.table.selectRow(0)
    assert not dialog.call_button.isEnabled()  # the game does not run
    dialog.set_attached(True)
    assert dialog.call_button.isEnabled()
    dialog.call_button.click()
    assert requested == [GIVE_ITEM]
    dialog.table.selectRow(1)  # an instance method
    assert not dialog.call_button.isEnabled()
    dialog.table.selectAll()
    assert not dialog.call_button.isEnabled()
    dialog.close()
    engine.dispose()


def test_an_object_a_call_returns_opens_in_the_inspector(qt_app, translations, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from PySide6.QtCore import QUrl  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets import method_call

    monkeypatch.setattr(method_call, "MethodMonitorWorker", _FakeWorker)
    monkeypatch.setattr(method_call, "_last_arguments", {})
    dialog = method_call.MethodCallDialog(translations, 1234)
    worker = dialog._worker
    player = CatalogMethod("Assembly-CSharp", "GameState", "get_PlayerCharacter", "()",
                           "Player", True, 0x06001db7,
                           "GameState:get_PlayerCharacter ()")
    try:
        dialog.set_method(player)
        worker.call_described.emit(1, {"params": [], "returns": _type(0x12, "Player")})
        dialog.call_button.click()
        target = {"address": "0x7f4a1e16e930", "klass": "0x55aa"}
        worker.called.emit(2, {"result": {
            "text": 'Player "Player_New_Game(Clone)_1"@0x7f4a1e16e930', "target": target}})
        assert "Inspizieren" in dialog.log.toPlainText()
        [inspector] = dialog.inspections.inspectors
        assert inspector.target == target and worker.requests[-1] == ("inspect", target)
        worker.inspected.emit(3, {"name": "Player", "fields": [
            {"name": "Health", "type": "int", "offset": 16, "static": False,
             "value": {"text": "100"}}]})

        # The log's link opens it again, also after the first inspector closed.
        inspector.close()
        dialog.log.anchorClicked.emit(QUrl("inspect:0"))
        assert worker.requests[-1] == ("inspect", target)
        assert len([shown for shown in dialog.inspections.inspectors if shown.isVisible()]) == 1

        # No object, no inspector.
        opened = len(dialog.inspections.inspectors)
        dialog.call_button.click()
        worker.called.emit(5, {"result": {"text": "42"}})
        assert len(dialog.inspections.inspectors) == opened
        assert dialog.log.toPlainText().splitlines()[-1].endswith("→ 42")
    finally:
        for shown in list(dialog.inspections.inspectors):
            shown.close()
        dialog.close()


def test_the_inspector_sets_properties_and_calls_methods(qt_app, translations, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from PySide6.QtWidgets import QWidget  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui import window_state
    from grembyte.ui.widgets import method_call
    from grembyte.ui.widgets.inspection import Inspections

    monkeypatch.setattr(method_call, "_last_arguments", {})
    window = QWidget()
    worker = _FakeWorker(1234)
    inspections = Inspections(translations, worker, window)
    target = {"address": "0x7f4a1e16e930"}
    contents = {"name": "Game.Player", "address": target["address"], "fields": [], "properties": [
        {"name": "Health", "type": "System.Int32", "static": False, "setter": "0x5001"},
        {"name": "IsDead", "type": "System.Boolean", "static": False, "setter": None}]}
    inspections.inspect(target, name="player")
    [inspector] = inspections.inspectors
    try:
        assert worker.requests == [("inspect", target)]
        worker.inspected.emit(1, contents)
        # Only a property with a setter can be set.
        assert inspector.properties_table.cellWidget(0, 4) is not None
        assert inspector.properties_table.cellWidget(1, 4) is None

        # The getters fold away, and stay so; the methods are folded until
        # opened, then listed once.
        assert inspector.properties_section.is_open and not inspector.methods_section.is_open
        inspector.properties_section.fold_button.click()
        assert inspector.properties_table.isHidden()
        assert window_state.settings().value("inspector/getters_open", type=bool) is False
        inspector.methods_section.fold_button.click()
        assert worker.requests[-1] == ("methods", target)
        worker.methods_listed.emit(2, [
            {"name": "Heal", "method": "0x6001", "owner": "Game.Player",
             "params": "System.Int32 amount", "returns": "System.Void"},
            {"name": "HealAll", "method": "0x6003", "owner": "Game.Player",
             "params": "", "returns": "System.Void"},
            {"name": "Destroy", "method": "0x6002", "owner": "UnityEngine.Object",
             "params": "", "returns": "System.Void"}])

        def shown():
            return [inspector.methods_table.item(row, 0).text()
                    for row in range(inspector.methods_table.rowCount())
                    if not inspector.methods_table.isRowHidden(row)]

        # The own class's methods, base classes on request; a search narrows them.
        assert shown() == ["Heal", "HealAll"]
        inspector.base_classes_check.setChecked(True)
        assert shown() == ["Heal", "HealAll", "Destroy"]
        inspector.method_filter.setText("all")
        assert shown() == ["HealAll"]

        # A method is called on the object, through the same worker; then
        # the fields are read again.
        inspector.methods_table.cellWidget(0, 4).linkActivated.emit("#")
        dialog = inspector.findChild(method_call.MethodCallDialog)
        assert worker.requests[-1] == ("describe_method", "0x6001")
        assert "Game.Player:Heal (System.Int32 amount)" in dialog.signature_label.text()
        worker.call_described.emit(3, {"params": [
            {"name": "amount", "type": _type(I4, "System.Int32")}], "returns": _type(0x01)})
        dialog._editors[0].setText("25")
        dialog.call_button.click()
        assert worker.requests[-1] == ("call_on", target, "0x6001", [25])
        worker.called.emit(4, {"result": None})
        assert worker.requests[-1] == ("inspect", target)
        worker.inspected.emit(5, contents)

        # Setting a property calls its setter, then shows the new value.
        inspector.properties_table.cellWidget(0, 4).linkActivated.emit("#")
        assert inspector.findChild(method_call.MethodCallDialog) is dialog
        assert worker.requests[-1] == ("describe_method", "0x5001")
        worker.call_described.emit(6, {"params": [
            {"name": "value", "type": _type(I4, "System.Int32")}], "returns": _type(0x01)})
        dialog._editors[0].setText("80")
        dialog.call_button.click()
        assert worker.requests[-1] == ("call_on", target, "0x5001", [80])
        worker.called.emit(7, {"result": None})
        assert worker.requests[-2:] == [("inspect", target), ("evaluate", target, ["Health"])]
    finally:
        inspector.close()
        window.deleteLater()