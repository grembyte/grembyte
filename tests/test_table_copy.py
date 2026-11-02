"""Copying rows and cells of tables (see hermes.ui.widgets.table)."""

from PySide6.QtCore import QStringListModel, Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from hermes.ui.widgets import table as table_module
from hermes.ui.widgets.table import add_copy_actions, cell_text, copy_menu, row_text

from tests.test_hooks import fixture_qt_app, fixture_translations  # pylint: disable=unused-import


def _table():
    table = QTableWidget(1, 6)
    table.setItem(0, 0, QTableWidgetItem("0x1234"))
    table.setItem(0, 1, QTableWidgetItem("int32"))
    # A check box centred in a container, like the method monitor's options
    box = QWidget()
    check = QCheckBox()
    check.setChecked(True)
    QHBoxLayout(box).addWidget(check)
    table.setCellWidget(0, 2, box)
    combo = QComboBox()
    combo.addItems(["Zahl", "Code"])
    combo.setCurrentIndex(1)
    table.setCellWidget(0, 3, combo)
    table.setCellWidget(0, 4, QLabel('<a href="#">Objekt-Details</a>'))
    table.setItem(0, 5, QTableWidgetItem("secret"))
    table.setColumnHidden(5, True)
    return table


def test_cells_with_widgets_count_by_what_they_show(qt_app):
    # pylint: disable=unused-argument
    table = _table()
    assert [cell_text(table, 0, column) for column in range(6)] == [
        "0x1234", "int32", "✓", "Code", "", "secret"]
    # Hidden columns are left out of the row.
    assert row_text(table, 0) == "0x1234\tint32\t✓\tCode\t"


def test_views_with_a_model(qt_app):
    # pylint: disable=unused-argument
    view = QTableView()
    model = QStringListModel(["Game.Loot:Take", "Game.Inventory:AddGold"])
    view.setModel(model)
    assert cell_text(view, 1, 0) == "Game.Inventory:AddGold"
    assert row_text(view, 0) == "Game.Loot:Take"


def test_menu_actions_copy_the_row_and_the_cell_under_the_pointer(qt_app, translations):
    # pylint: disable=unused-argument
    table = _table()
    table.resize(800, 200)
    table.show()
    position = table.visualRect(table.model().index(0, 1)).center()
    menu = QMenu()
    menu.addAction("Watch")
    add_copy_actions(menu, table, position, translations)
    actions = [action for action in menu.actions() if not action.isSeparator()]
    assert [action.text() for action in actions] == ["Watch", "Zeile kopieren", "Zelle kopieren"]
    actions[1].trigger()
    assert QGuiApplication.clipboard().text() == "0x1234\tint32\t✓\tCode\t"
    actions[2].trigger()
    assert QGuiApplication.clipboard().text() == "int32"

    # Outside the rows there is nothing to copy.
    empty = QMenu()
    add_copy_actions(empty, table, table.viewport().rect().bottomRight(), translations)
    assert empty.isEmpty()
    table.close()


def test_tables_without_a_menu_get_one(qt_app, translations):
    # pylint: disable=unused-argument
    table = _table()
    copy_menu(table, translations)
    assert table.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu


def test_tables_with_a_remove_function_offer_to_delete_the_clicked_row(
        qt_app, translations, monkeypatch):
    # pylint: disable=unused-argument
    table = QTableWidget(3, 1)
    for row in range(3):
        table.setItem(row, 0, QTableWidgetItem(f"row {row}"))
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.resize(400, 300)
    table.show()
    deleted = []
    copy_menu(table, translations, lambda: deleted.append(
        sorted(index.row() for index in table.selectionModel().selectedRows())))
    shown = []

    class Menu(QMenu):
        """Picks its first action instead of waiting for the user."""

        def exec(self, *_args, **_kwargs):
            shown.append([action.text() for action in self.actions()
                          if not action.isSeparator()])
            chosen = self.actions()[0]
            chosen.trigger()
            return chosen

    # PySide6 ignores methods patched onto QMenu itself; a subclass is used.
    monkeypatch.setattr(table_module, "QMenu", Menu)

    # The clicked row is selected first; the delete action acts on it.
    table.customContextMenuRequested.emit(table.visualRect(table.model().index(1, 0)).center())
    assert shown[-1] == ["Zeile löschen", "Zeile kopieren", "Zelle kopieren"]
    assert deleted == [[1]]

    # A selection of several rows the click is in stays, and is named.
    table.selectAll()
    table.customContextMenuRequested.emit(table.visualRect(table.model().index(2, 0)).center())
    assert shown[-1][0] == "3 Zeilen löschen"
    assert deleted[-1] == [0, 1, 2]
    table.close()
