"""Tests for the assembly selection: the rule, persistence, the window and the catalog filter."""

import os

import pytest
from PyQt6.QtCore import QObject, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hermes.db.base import Base
from hermes.db import models  # noqa: F401  pylint: disable=unused-import
from hermes.db.repository.assembly_selections import AssemblySelectionRepository
from hermes.db.repository.hooks import HookRepository
from hermes.engines.registry import ENGINES
from hermes.features.assemblies import chosen_assemblies, missing_assemblies
from hermes.features.catalog import CatalogMethod
from hermes.ui.localization import load_translations


MONO = ENGINES[0]
LOADED = ["mscorlib", "Assembly-CSharp", "UnityEngine", "Game.Data", "System.Core"]


@pytest.fixture(name="sessions")
def fixture_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture(name="process_id")
def fixture_process_id(sessions):
    return HookRepository(sessions).get_or_create_process("game", "/opt/game/game")


@pytest.fixture(name="repository")
def fixture_repository(sessions):
    return AssemblySelectionRepository(sessions)


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    # pylint: disable=import-outside-toplevel
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    return QApplication.instance() or QApplication([])


@pytest.fixture(name="translations")
def fixture_translations():
    return load_translations("de")


class FakeCatalogWorker(QObject):
    """Answers the assembly list at once instead of attaching to a game."""

    assemblies_found = pyqtSignal(list)
    catalog_failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, _pid, assemblies=None, engine=None):
        super().__init__()
        self.assemblies = assemblies
        self.engine = engine

    def start(self):
        self.assemblies_found.emit([{"name": name, "file": f"/data/Managed/{name}.dll", "classes": 3}
                                    for name in LOADED])
        self.finished.emit()

    def requestInterruption(self):  # pylint: disable=invalid-name
        pass

    def wait(self, _timeout_ms=None):
        return True


def test_without_selection_the_game_assemblies_are_chosen():
    assert chosen_assemblies(LOADED, frozenset(), MONO) == ["Assembly-CSharp", "Game.Data"]
    # A selection may name framework assemblies; the order is the game's.
    assert chosen_assemblies(LOADED, {"Game.Data", "UnityEngine", "Gone"}, MONO) == [
        "UnityEngine", "Game.Data"]
    assert missing_assemblies(LOADED, {"Game.Data", "Gone"}) == ["Gone"]


def test_repository_replaces_the_selection_per_program(repository, process_id, sessions):
    other_id = HookRepository(sessions).get_or_create_process("other", "/opt/other")
    repository.save_assemblies(process_id, ["Game.Data", "Assembly-CSharp", "Game.Data"])
    repository.save_assemblies(other_id, ["Other"])
    assert repository.list_assemblies(process_id) == ["Assembly-CSharp", "Game.Data"]
    repository.save_assemblies(process_id, ["UnityEngine"])
    assert repository.list_assemblies(process_id) == ["UnityEngine"]
    repository.save_assemblies(process_id, [])
    assert repository.list_assemblies(process_id) == []
    assert repository.list_assemblies(other_id) == ["Other"]


def test_window_selects_saves_and_clears(qt_app, repository, process_id, translations,
                                         monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module

    from hermes.ui.widgets import assemblies
    from hermes.ui.widgets.assemblies import AssemblyDialog, AssemblySelectionStore

    monkeypatch.setattr(assemblies, "CatalogWorker", FakeCatalogWorker)
    repository.save_assemblies(process_id, ["Gone"])
    store = AssemblySelectionStore(repository)
    store.set_process(process_id)
    changes = []
    store.changed.connect(lambda: changes.append(store.selection))
    dialog = AssemblyDialog(translations, 1234, store, engine=MONO)

    # The loaded assemblies, and the one selected earlier that is not loaded
    assert dialog.table.rowCount() == len(LOADED) + 1
    assert not dialog.save_button.isEnabled()

    dialog.search_input.setText("game")
    dialog.select_all_button.click()
    dialog.search_input.setText("")
    assert dialog.checked() == {"Gone", "Game.Data"}
    row = next(row for row in range(dialog.table.rowCount())
               if dialog.table.item(row, 0).text() == "Assembly-CSharp")
    dialog.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
    dialog.table.item(next(row for row in range(dialog.table.rowCount())
                           if dialog.table.item(row, 0).text() == "Gone"),
                      0).setCheckState(Qt.CheckState.Unchecked)
    assert dialog.save_button.isEnabled()
    dialog.save_button.click()
    assert repository.list_assemblies(process_id) == ["Assembly-CSharp", "Game.Data"]
    assert changes == [{"Assembly-CSharp", "Game.Data"}]
    assert dialog.table.rowCount() == len(LOADED)  # "Gone" is no longer selected

    dialog.clear_button.click()
    dialog.save()
    assert repository.list_assemblies(process_id) == [] and store.selection == frozenset()


def test_catalog_shows_only_the_selected_assemblies(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.catalog import CatalogModel

    def method(assembly, name):
        return CatalogMethod(assembly, "Game.Player", name, "()", "void", False, 1,
                             f"Game.Player:{name} ()")

    model = CatalogModel(translations, MONO)
    model.add([method("Assembly-CSharp", "AddGold"), method("Game.Data", "Load"),
               method("UnityEngine", "Update")])
    assert [m.name for m in model.visible] == ["AddGold", "Load"]
    model.set_filter("", False, {"Game.Data", "UnityEngine"})
    assert [m.name for m in model.visible] == ["Load", "Update"]
    model.set_filter("upd", False)  # keeps the selection
    assert [m.name for m in model.visible] == ["Update"]
    model.set_filter("", True, frozenset())
    assert len(model.visible) == 3
