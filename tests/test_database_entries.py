"""Deleting entries saved for processes, and its window."""

from sqlalchemy import select

from grembyte.db.models import Address, AssemblySelectionRecord, Hook, PointerScan, Process

from tests.test_game_export import (  # pylint: disable=unused-import
    PROTON_EXP,
    PROTON_GE,
    _process_id,
    fixture_qt_app,
    fixture_repository,
    fixture_sessions,
    fixture_translations,
)


def _count(sessions, model, *where):
    with sessions() as session:
        return len(list(session.scalars(select(model).where(*where))))


def test_deleting_addresses_takes_their_pointer_scans(sessions, repository):
    souls = _process_id(sessions, "DarkSoulsIII.ex", PROTON_GE)
    assert repository.delete_entries(souls, ["addresses"]) == {"addresses": 1, "pointer_scans": 1}
    assert _count(sessions, PointerScan) == 0
    assert _count(sessions, Process, Process.id == souls) == 1


def test_only_the_chosen_kinds_go(sessions, repository):
    souls = _process_id(sessions, "DarkSoulsIII.ex", PROTON_GE)
    assert repository.delete_entries(souls, ["pointer_scans"]) == {"pointer_scans": 1}
    assert _count(sessions, Address, Address.process_id == souls) == 1


def test_removing_a_process_takes_everything_saved_for_it(sessions, repository):
    other = _process_id(sessions, "DarkSoulsIII.ex", PROTON_EXP)
    with sessions() as session:
        session.add(AssemblySelectionRecord(process_id=other, name="Assembly-CSharp"))
        session.commit()
    deleted = repository.delete_entries(other, [], remove_process=True)
    assert deleted["hooks"] == 1 and deleted["process"] == 1
    assert _count(sessions, Process, Process.id == other) == 0
    assert _count(sessions, Hook) == 0
    assert _count(sessions, AssemblySelectionRecord) == 0
    # The game's other process stays.
    assert _count(sessions, Address) == 1


def test_dialog_ticks_rows_and_deletes(qt_app, translations, sessions, repository, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtWidgets import QMessageBox  # pylint: disable=import-error,no-name-in-module
    from grembyte.ui.widgets.database_entries import (
        KIND_COLUMNS,
        PROCESS_COLUMN,
        ROW_COLUMN,
        DeleteEntriesDialog,
    )

    souls = _process_id(sessions, "DarkSoulsIII.ex", PROTON_GE)
    dialog = DeleteEntriesDialog(translations, repository, current=souls)
    rows = {process.process_id: row for row, process in enumerate(dialog.processes)}
    row = rows[souls]
    table = dialog.table
    assert not dialog.delete_button.isEnabled()
    # The attached process cannot be removed; counts of 0 cannot be ticked.
    assert not table.item(row, PROCESS_COLUMN).flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert not table.item(row, KIND_COLUMNS["hooks"]).flags() & Qt.ItemFlag.ItemIsUserCheckable

    # Addresses take their pointer scans along, and all boxes tick the row's box.
    table.item(row, KIND_COLUMNS["addresses"]).setCheckState(Qt.CheckState.Checked)
    assert table.item(row, KIND_COLUMNS["pointer_scans"]).checkState() == Qt.CheckState.Checked
    assert table.item(row, ROW_COLUMN).checkState() == Qt.CheckState.Checked
    table.item(row, ROW_COLUMN).setCheckState(Qt.CheckState.Unchecked)
    assert dialog.selection() == []

    other = rows[_process_id(sessions, "DarkSoulsIII.ex", PROTON_EXP)]
    table.item(other, PROCESS_COLUMN).setCheckState(Qt.CheckState.Checked)
    assert table.item(other, KIND_COLUMNS["hooks"]).checkState() == Qt.CheckState.Checked
    table.item(row, KIND_COLUMNS["pointer_scans"]).setCheckState(Qt.CheckState.Checked)
    assert dialog.delete_button.isEnabled()

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    dialog.delete_button.click()
    assert dialog.deleted[souls] == {"pointer_scans": 1}
    assert dialog.deleted[dialog.processes[other].process_id]["process"] == 1
    assert _count(sessions, PointerScan) == 0 and _count(sessions, Address) == 1


def test_space_steps_the_selected_rows(qt_app, translations, sessions, repository):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtGui import QShortcut  # pylint: disable=import-error,no-name-in-module
    from grembyte.ui.widgets.database_entries import (
        KIND_COLUMNS,
        PROCESS_COLUMN,
        ROW_COLUMN,
        DeleteEntriesDialog,
    )

    souls = _process_id(sessions, "DarkSoulsIII.ex", PROTON_GE)
    other_id = _process_id(sessions, "DarkSoulsIII.ex", PROTON_EXP)
    dialog = DeleteEntriesDialog(translations, repository, current=souls)
    rows = {process.process_id: row for row, process in enumerate(dialog.processes)}
    table = dialog.table
    (space,) = table.findChildren(QShortcut)
    assert space.key().toString() == "Space"
    assert space.context() == Qt.ShortcutContext.WidgetShortcut

    def ticked(row):
        return [column for column in (ROW_COLUMN, *KIND_COLUMNS.values(), PROCESS_COLUMN)
                if table.item(row, column).checkState() == Qt.CheckState.Checked]

    entries = [column for column in KIND_COLUMNS.values()
               if table.item(rows[other_id], column).flags() & Qt.ItemFlag.ItemIsUserCheckable]
    other = rows[other_id]
    table.selectRow(other)
    space.activated.emit()  # the box in front: all entries
    assert ticked(other) == [ROW_COLUMN, *entries]
    space.activated.emit()  # the process too
    assert ticked(other) == [ROW_COLUMN, *entries, PROCESS_COLUMN]
    assert dialog.selection()[0][2]
    space.activated.emit()  # nothing
    assert ticked(other) == [] and dialog.selection() == []
    assert not dialog.delete_button.isEnabled()

    # The attached process cannot be removed: the second step is left out.
    attached = rows[souls]
    table.selectRow(attached)
    space.activated.emit()
    assert ROW_COLUMN in ticked(attached) and PROCESS_COLUMN not in ticked(attached)
    space.activated.emit()
    assert ticked(attached) == []

    # Several rows go together, by the furthest one.
    table.selectAll()
    table.item(other, PROCESS_COLUMN).setCheckState(Qt.CheckState.Checked)
    space.activated.emit()
    assert all(ticked(row) == [] for row in range(table.rowCount()))
    space.activated.emit()
    assert ROW_COLUMN in ticked(other) and ROW_COLUMN in ticked(attached)

