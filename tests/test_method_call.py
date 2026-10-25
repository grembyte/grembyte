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

        self.signals = Signals()
        for name in ("call_described", "called", "call_failed", "worker_failed"):
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
