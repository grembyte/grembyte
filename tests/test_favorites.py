"""Tests for the favorites: keys, persistence, the star column and the favorites window."""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hermes.db.base import Base
from hermes.db import models  # noqa: F401  pylint: disable=unused-import
from hermes.db.repository.favorites import FavoriteRepository
from hermes.db.repository.hooks import HookRepository
from hermes.features.catalog import CatalogMethod
from hermes.features.favorites import (
    CATALOG,
    METHOD,
    NATIVE,
    NATIVE_TRACE,
    favorite_from_function,
    favorite_from_method,
    favorite_key,
)
from hermes.features.native_trace import NativeFunction
from hermes.ui.localization import load_translations


GOLD = CatalogMethod("Assembly-CSharp", "Game.PlayerInventory", "AddCurrency", "(int)", "void",
                     False, 0x06001234, "Game.PlayerInventory:AddCurrency (int)")
ROLL = NativeFunction(0x55aa00001000, "game", 0x1000, "roll_dice")
JIT = NativeFunction(0x7f0000, "", None)


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
    return FavoriteRepository(sessions)


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    # pylint: disable=import-outside-toplevel
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    return QApplication.instance() or QApplication([])


@pytest.fixture(name="translations")
def fixture_translations():
    return load_translations("de")


def test_native_functions_are_known_by_module_and_offset():
    moved = NativeFunction(0x7ffd00001000, "game", 0x1000)  # the next start of the game
    assert favorite_key(moved) == favorite_key(ROLL)
    assert favorite_key(JIT) == "native:0x7f0000"
    assert favorite_from_function(ROLL).lasting
    assert not favorite_from_function(JIT).lasting


def test_favorites_rebuild_what_they_were_made_of():
    method = favorite_from_method(GOLD, CATALOG)
    assert (method.kind, method.label, method.location) == (
        METHOD, "PlayerInventory:AddCurrency", "Assembly-CSharp")
    assert method.target() == GOLD
    function = favorite_from_function(ROLL, NATIVE_TRACE)
    assert (function.kind, function.label, function.location) == (NATIVE, "roll_dice",
                                                                   "game+0x1000")
    assert function.target() == ROLL
    assert function.matches(["roll", "game+0x1"]) and not function.matches(["gold"])


def test_repository_keeps_favorites_and_their_notes(repository, process_id, sessions):
    other_id = HookRepository(sessions).get_or_create_process("other", "/opt/other")
    gold_id = repository.add_favorite(process_id, favorite_from_method(GOLD, CATALOG))
    repository.add_favorite(process_id, favorite_from_function(ROLL, NATIVE_TRACE))
    repository.add_favorite(other_id, favorite_from_function(JIT))
    repository.set_note(gold_id, "adds gold after a sale")
    # Marked again from elsewhere: still one favorite, the note stays
    assert repository.add_favorite(process_id, favorite_from_method(GOLD, "trace")) == gold_id

    gold, roll = repository.list_favorites(process_id)
    assert gold.note == "adds gold after a sale" and gold.source == "trace"
    assert gold.method() == GOLD and roll.function() == ROLL
    assert [favorite.key for favorite in repository.list_favorites(other_id)] == [
        favorite_key(JIT)]

    repository.delete_favorite(process_id, gold.key)
    assert [favorite.label for favorite in repository.list_favorites(process_id)] == [
        "roll_dice"]


def test_star_column_marks_and_unmarks(qt_app, repository, process_id, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module

    from hermes.features.trace import NEW, TraceRow
    from hermes.ui.widgets.favorites import NO_STAR, STAR, FavoriteStore
    from hermes.ui.widgets.native_trace import NativeRowModel
    from hermes.ui.widgets.trace import trace_table

    store = FavoriteStore(repository)
    store.set_process(process_id)
    model = NativeRowModel(translations)
    view = trace_table(model, 2, sortable=True, favorites=store, source=NATIVE_TRACE)
    model.set_items([ROLL, JIT], [TraceRow(0, 3, 1.0, None, NEW, 0),
                                  TraceRow(1, 7, 2.3, None, NEW, 1)])
    repaints = []
    model.dataChanged.connect(lambda *_args: repaints.append(True))

    assert model.data(model.index(0, 0)) == NO_STAR
    view.clicked.emit(model.index(0, 0))
    assert model.data(model.index(0, 0)) == STAR and repaints
    assert [favorite.label for favorite in store.favorites()] == ["roll_dice"]
    view.clicked.emit(model.index(1, 2))  # not the star column
    assert model.data(model.index(1, 0)) == NO_STAR
    model.sort(0, Qt.SortOrder.DescendingOrder)
    assert model.data(model.index(0, 2)) == "roll_dice"
    view.clicked.emit(model.index(0, 0))
    assert model.data(model.index(0, 0)) == NO_STAR and store.favorites() == []

    hidden = trace_table(NativeRowModel(translations), 2)
    assert hidden.isColumnHidden(0)


def test_space_marks_the_selected_row(qt_app, repository, process_id, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PyQt6.QtGui import QShortcut  # pylint: disable=import-error,no-name-in-module

    from hermes.features.trace import NEW, TraceRow
    from hermes.ui.widgets.favorites import FavoriteStore
    from hermes.ui.widgets.native_trace import NativeRowModel
    from hermes.ui.widgets.trace import trace_table

    store = FavoriteStore(repository)
    store.set_process(process_id)
    model = NativeRowModel(translations)
    view = trace_table(model, 2, favorites=store, source=NATIVE_TRACE)
    model.set_items([ROLL, JIT], [TraceRow(0, 3, 1.0, None, NEW, 0),
                                  TraceRow(1, 7, 2.3, None, NEW, 1)])
    (space,) = view.findChildren(QShortcut)
    assert space.key().toString() == "Space"
    assert space.context() == Qt.ShortcutContext.WidgetShortcut

    view.selectRow(1)
    space.activated.emit()
    assert store.is_favorite(JIT) and not store.is_favorite(ROLL)
    space.activated.emit()
    assert store.favorites() == []
    view.selectRow(0)
    space.activated.emit()
    assert [favorite.label for favorite in store.favorites()] == ["roll_dice"]


def test_favorites_window_annotates_and_monitors(qt_app, repository, process_id, translations):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from hermes.ui.widgets.favorites import FavoritesDialog, FavoriteStore

    store = FavoriteStore(repository)
    store.set_process(process_id)
    store.toggle(GOLD, CATALOG)
    store.toggle(ROLL, NATIVE_TRACE)
    dialog = FavoritesDialog(translations, store)
    methods, functions = [], []
    dialog.method_monitor_requested.connect(methods.extend)
    dialog.native_monitor_requested.connect(functions.extend)

    assert dialog.table.rowCount() == 2
    dialog.table.selectRow(1)
    assert not dialog.monitor_button.isEnabled()  # the game does not run
    dialog.set_attached(True)
    dialog.monitor_button.click()
    assert functions == [ROLL]

    dialog.table.item(0, 3).setText("Gold nach Verkauf")
    assert repository.list_favorites(process_id)[0].note == "Gold nach Verkauf"
    dialog.search_input.setText("verkauf")
    assert dialog.table.rowCount() == 1
    dialog.table.selectRow(0)
    dialog.monitor_button.click()
    assert methods == [GOLD]

    dialog.remove_button.click()
    assert [favorite.label for favorite in store.favorites()] == ["roll_dice"]
    assert not store.is_favorite(GOLD)


def test_favorites_window_handles_several_at_once(qt_app, repository, process_id, translations,
                                                  monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PyQt6.QtCore import QItemSelectionModel  # pylint: disable=import-error,no-name-in-module
    from PyQt6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module
    from PyQt6.QtWidgets import QMessageBox  # pylint: disable=import-error,no-name-in-module
    from hermes.ui.widgets.favorites import FavoritesDialog, FavoriteStore

    store = FavoriteStore(repository)
    store.set_process(process_id)
    store.toggle(GOLD, CATALOG)
    store.toggle(ROLL, NATIVE_TRACE)
    dialog = FavoritesDialog(translations, store)
    dialog.set_attached(True)
    methods, functions = [], []
    dialog.method_monitor_requested.connect(methods.extend)
    dialog.native_monitor_requested.connect(functions.extend)

    dialog.table.selectAll()
    assert len(dialog.selected_favorites()) == 2
    assert dialog.selection_label.text() == "2 Favoriten ausgewählt"
    dialog.monitor_button.click()
    assert methods == [GOLD] and functions == [ROLL]

    dialog.copy_button.click()
    assert QGuiApplication.clipboard().text().splitlines() == [GOLD.signature, "game+0x1000"]

    # A refresh, e.g. after a note was edited, keeps both selected.
    dialog.table.item(0, 3).setText("Gold")
    assert len(dialog.selected_favorites()) == 2

    # Removing several asks first.
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *_args: QMessageBox.StandardButton.No)
    dialog.remove_button.click()
    assert len(store.favorites()) == 2
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *_args: QMessageBox.StandardButton.Yes)
    dialog.remove_button.click()
    assert store.favorites() == []

    # One selected: its signature, no question.
    store.toggle(GOLD, CATALOG)
    dialog.table.selectionModel().select(
        dialog.table.model().index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
    assert dialog.selection_label.text() == GOLD.signature


def test_favorites_window_sorts_by_column(qt_app, repository, process_id, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from hermes.ui.widgets.favorites import FavoritesDialog, FavoriteStore

    store = FavoriteStore(repository)
    store.set_process(process_id)
    store.toggle(GOLD, CATALOG)
    store.toggle(ROLL, NATIVE_TRACE)
    dialog = FavoritesDialog(translations, store)
    header = dialog.table.horizontalHeader()
    names = lambda: [dialog.table.item(row, 1).text()  # noqa: E731
                     for row in range(dialog.table.rowCount())]

    assert names() == ["PlayerInventory:AddCurrency", "roll_dice"]  # the order added in
    dialog.table.selectRow(1)
    header.setSortIndicator(1, Qt.SortOrder.DescendingOrder)
    assert names() == ["roll_dice", "PlayerInventory:AddCurrency"]
    # The selection stays with its favorite, the rows with theirs.
    assert dialog.selected_favorite().label == "roll_dice"
    dialog.table.item(1, 3).setText("Gold")
    assert repository.list_favorites(process_id)[0].note == "Gold"
    header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
    assert names() == ["PlayerInventory:AddCurrency", "roll_dice"]


def test_catalog_sorts_by_column_and_keeps_the_selection(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PyQt6.QtCore import QItemSelectionModel, Qt  # pylint: disable=import-error,no-name-in-module
    from PyQt6.QtWidgets import QTableView  # pylint: disable=import-error,no-name-in-module
    from hermes.ui.widgets.catalog import CatalogModel

    class Engine:  # pylint: disable=too-few-public-methods
        @staticmethod
        def is_framework_assembly(_assembly):
            return False

    sell = CatalogMethod("Assembly-CSharp", "Game.Shop", "Sell", "(int)", "bool",
                         True, 0x06000001, "Game.Shop:Sell (int)")
    load = CatalogMethod("Loader", "Boot", "Load", "()", "void",
                         False, 0x06000002, "Boot:Load ()")
    model = CatalogModel(translations, Engine())
    view = QTableView()
    view.setModel(model)
    model.add([GOLD, sell])
    view.selectionModel().setCurrentIndex(
        model.index(1, 1), QItemSelectionModel.SelectionFlag.ClearAndSelect
        | QItemSelectionModel.SelectionFlag.Rows)
    classes = lambda: [model.method_at(row).class_name  # noqa: E731
                       for row in range(model.rowCount())]

    model.sort(1, Qt.SortOrder.AscendingOrder)
    assert classes() == ["Game.PlayerInventory", "Game.Shop"]
    model.sort(4, Qt.SortOrder.DescendingOrder)  # static ones first
    assert model.method_at(view.currentIndex().row()) is sell
    model.add([load])  # read later, sorted in
    assert classes() == ["Game.Shop", "Game.PlayerInventory", "Boot"]
    model.sort(1, Qt.SortOrder.AscendingOrder)
    assert classes() == ["Boot", "Game.PlayerInventory", "Game.Shop"]
    assert model.method_at(view.currentIndex().row()) is sell
    model.set_filter("game", False)
    assert classes() == ["Game.PlayerInventory", "Game.Shop"]
    model.sort(-1, Qt.SortOrder.AscendingOrder)  # the order read in
    model.set_filter("", False)
    assert classes() == ["Game.PlayerInventory", "Game.Shop", "Boot"]
