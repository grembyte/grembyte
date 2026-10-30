"""Calling static methods: reading the arguments typed in, the call dialog."""

import os

import pytest

from hermes.features.catalog import CatalogMethod
from hermes.features.method_call import (
    BOOLEAN,
    CHAR,
    I4,
    R4,
    STRING,
    U1,
    VALUETYPE,
    ArgumentError,
    argument_text,
    parse_argument,
)
from hermes.ui.localization import load_translations


GIVE_ITEM = CatalogMethod("Assembly-CSharp", "Scripts", "GiveItem", "(string,int)", "void",
                          True, 0x06000010, "Scripts:GiveItem (string,int)")
UPDATE = CatalogMethod("Assembly-CSharp", "Game.Loop", "Update", "()", "void",
                       False, 0x06000011, "Game.Loop:Update ()")


def _type(kind, name="T", **extra):
    return {"kind": kind, "byref": False, "name": name, **extra}


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    # pylint: disable=import-outside-toplevel
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

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


def test_enum_names_are_the_hint_of_an_empty_argument(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.method_call import MethodCallDialog

    dialog = MethodCallDialog.__new__(MethodCallDialog)
    dialog.translations, dialog._editors = translations, []  # pylint: disable=protected-access
    values = {"0": "Might", "1": "Constitution", "2": "Dexterity", "3": "Perception",
              "4": "Intellect"}
    editor = dialog._editor(_type(U1, "EPrimaryAttribute", enumName="EPrimaryAttribute",
                                  enumValues=values), None)  # pylint: disable=protected-access
    assert editor.placeholderText() == (
        "Might, Constitution, Dexterity, Perception, … oder Zahlenwert")
    assert editor.toolTip().splitlines()[2] == "2 = Dexterity"


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
        from PyQt6.QtCore import QObject, pyqtSignal  # pylint: disable=import-error,no-name-in-module

        class Signals(QObject):  # pylint: disable=too-few-public-methods
            call_described = pyqtSignal(int, dict)
            called = pyqtSignal(int, dict)
            call_failed = pyqtSignal(int, str)
            worker_failed = pyqtSignal(str)
            inspected = pyqtSignal(int, dict)
            inspect_failed = pyqtSignal(int, str)
            evaluated = pyqtSignal(int, list)
            evaluate_failed = pyqtSignal(int, str)
            methods_listed = pyqtSignal(int, list)
            methods_failed = pyqtSignal(int, str)
            text_fetched = pyqtSignal(int, object)
            text_failed = pyqtSignal(int, str)

        self.signals = Signals()
        for name in ("call_described", "called", "call_failed", "worker_failed", "inspected",
                     "inspect_failed", "evaluated", "evaluate_failed", "methods_listed",
                     "methods_failed", "text_fetched", "text_failed"):
            setattr(self, name, getattr(self.signals, name))
        self.requests = []

    def start(self):
        pass

    def describe_call(self, identity):
        self.requests.append(("describe", identity))
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
    from hermes.ui.widgets import method_call

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


def test_only_static_methods_can_be_called(qt_app, translations, tmp_path):
    # pylint: disable=import-outside-toplevel,unused-argument
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from hermes.db import models  # noqa: F401  pylint: disable=unused-import
    from hermes.db.base import Base
    from hermes.db.repository.favorites import FavoriteRepository
    from hermes.db.repository.hooks import HookRepository
    from hermes.ui.widgets.favorites import FavoritesDialog, FavoriteStore

    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
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
    from PyQt6.QtCore import QUrl  # pylint: disable=import-error,no-name-in-module

    from hermes.ui.widgets import method_call

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
    from PyQt6.QtWidgets import QWidget  # pylint: disable=import-error,no-name-in-module

    from hermes.ui import window_state
    from hermes.ui.widgets import method_call
    from hermes.ui.widgets.inspection import Inspections

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