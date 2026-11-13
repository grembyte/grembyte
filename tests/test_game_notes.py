"""The notes window per saved game, its choice of a game, and their storage."""

from grembyte.db.models import GameNote
from grembyte.db.repository.game_export import GameExportRepository
from grembyte.db.repository.notes import NoteRepository, merged_text

from tests.test_game_export import (  # pylint: disable=unused-import
    PROTON_GE,
    _note_bodies,
    _process_id,
    fixture_qt_app,
    fixture_repository,
    fixture_sessions,
    fixture_translations,
)

POE = "/steam/common/PoE/PillarsOfEternity"


def test_merged_text_appends_only_what_is_new():
    assert merged_text("", "Gold") == "Gold"
    assert merged_text("Gold\n", "XP") == "Gold\n\n---\n\nXP"
    assert merged_text("Gold\n\n---\n\nXP", "XP") is None
    assert merged_text("Gold and XP", "XP") == "Gold and XP\n\n---\n\nXP"
    assert merged_text("Gold", "  \n") is None


def test_merged_text_keeps_rules_and_adds_only_new_sections():
    # A rule the user wrote looks like the separator: re-importing adds nothing
    note = "Gold\n\n---\n\nXP"
    assert merged_text(note, note) is None
    assert merged_text(note, "XP\n\n---\n\nGold\n") is None
    # A note merged on another computer brings back only what is new there
    assert merged_text("Gold", "Gold\n\n---\n\nXP") == "Gold\n\n---\n\nXP"
    assert merged_text("Gold\n\n---\n\nXP", "Gold\n\n---\n\nXP\n\n---\n\nItems") == (
        "Gold\n\n---\n\nXP\n\n---\n\nItems")
    assert merged_text("Gold", "XP\n\n---\n\nXP") == "Gold\n\n---\n\nXP"


def test_notes_synced_back_and_forth_do_not_grow(sessions, repository):
    souls = _process_id(sessions, "DarkSoulsIII.ex", PROTON_GE)
    other = _process_id(sessions, "abrt-dbus", "/usr/sbin/abrt-dbus")
    notes = NoteRepository(sessions)
    notes.save_first_note(souls, "Gold\n\n---\n\nXP")
    repository.import_export(repository.export_game("DarkSoulsIII.ex"), other)
    notes.save_first_note(other, notes.first_note(other).body + "\n\n---\n\nItems")
    for _round in range(2):
        repository.import_export(repository.export_game("abrt-dbus"), souls)
        repository.import_export(repository.export_game("DarkSoulsIII.ex"), other)
    expected = "Gold\n\n---\n\nXP\n\n---\n\nItems"
    assert _note_bodies(sessions, souls) == [("", expected)]
    assert _note_bodies(sessions, other) == [("", expected)]


def test_the_first_note_is_saved_and_kept(sessions, repository):
    # pylint: disable=unused-argument
    notes = NoteRepository(sessions)
    poe = _process_id(sessions, "PillarsOfEternity", POE)
    assert notes.first_note(poe) is None
    notes.save_first_note(poe, "Gold")
    notes.save_first_note(poe, "Gold: `AddGold`")
    assert _note_bodies(sessions, poe) == [("", "Gold: `AddGold`")]


def test_empty_notes_are_not_created_and_clearing_removes_the_note(sessions, repository):
    # pylint: disable=unused-argument
    notes = NoteRepository(sessions)
    poe = _process_id(sessions, "PillarsOfEternity", POE)
    assert notes.save_first_note(poe, "") is None
    assert notes.save_first_note(poe, " \n ") is None
    assert _note_bodies(sessions, poe) == []

    notes.save_first_note(poe, "Gold")
    assert notes.save_first_note(poe, " \n ") is None
    assert _note_bodies(sessions, poe) == []


def test_clearing_the_note_keeps_it_when_the_process_has_more(sessions, repository):
    # pylint: disable=unused-argument
    notes = NoteRepository(sessions)
    poe = _process_id(sessions, "PillarsOfEternity", POE)
    notes.save_first_note(poe, "Gold")
    with sessions() as session:
        session.add(GameNote(process_id=poe, title="Imported", body="Souls"))
        session.commit()
    assert notes.save_first_note(poe, "") is None
    # The window keeps showing its (now empty) note, not the imported one
    assert _note_bodies(sessions, poe) == [("", ""), ("Imported", "Souls")]
    assert notes.first_note(poe).body == ""
    counted = next(row for row in repository.list_processes() if row.process_id == poe)
    assert counted.counts["notes"] == 1


def _dialog(translations, sessions, process_id, game="PillarsOfEternity"):
    # pylint: disable=import-outside-toplevel
    from grembyte.ui.widgets.game_notes import GameNotesDialog

    return GameNotesDialog(translations, process_id, game, repository=NoteRepository(sessions))


def test_notes_save_after_typing_with_the_button_and_on_closing(qt_app, translations, sessions,
                                                                  repository):
    # pylint: disable=unused-argument,import-outside-toplevel
    from grembyte.ui.widgets.code_editor import code_theme_colors

    poe = _process_id(sessions, "PillarsOfEternity", POE)
    dialog = _dialog(translations, sessions, poe)
    try:
        assert dialog.windowTitle() == translations["notes.title"].format(game="PillarsOfEternity")
        # The button saves even when nothing changed, without creating an empty entry.
        dialog.save_button.click()
        assert _note_bodies(sessions, poe) == []
        assert not dialog.dirty
        # Colouring the Markdown again (a theme change) changes no text
        dialog.editor.markdown_highlighter.set_colors(code_theme_colors())
        assert not dialog.dirty and not dialog._autosave.isActive()  # pylint: disable=protected-access

        dialog.editor.setPlainText("## Gold\n- `AddGold(int)`")
        assert dialog.dirty and dialog._autosave.isActive()  # pylint: disable=protected-access
        dialog._autosave.timeout.emit()  # pylint: disable=protected-access
        assert _note_bodies(sessions, poe) == [("", "## Gold\n- `AddGold(int)`")]

        dialog.tabs.setCurrentIndex(1)
        preview = dialog.preview.toPlainText()
        assert "Gold" in preview and "#" not in preview and "`" not in preview

        dialog.editor.setPlainText("Escape keeps this")
        dialog.reject()
        assert _note_bodies(sessions, poe) == [("", "Escape keeps this")]
    finally:
        dialog.close()


def test_open_notes_follow_the_attached_game(qt_app, translations, sessions, repository,
                                             monkeypatch):
    # pylint: disable=unused-argument,import-outside-toplevel,protected-access
    from PySide6.QtWidgets import QDialog  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui import main_window
    from grembyte.ui.widgets.game_notes import GameNotesDialog

    poe = _process_id(sessions, "PillarsOfEternity", POE)
    souls = _process_id(sessions, "DarkSoulsIII.ex", PROTON_GE)
    with sessions() as session:
        session.add(GameNote(process_id=poe, body="Gold"))
        session.commit()

    class ProcessPicker:  # pylint: disable=too-few-public-methods
        selected_pid = 12345
        selected_name = "DarkSoulsIII.ex"

        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window, "ProcessSelectDialog", ProcessPicker)
    monkeypatch.setattr(main_window.activity, "record", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_window.activity, "describe_process", lambda *_args: {})
    monkeypatch.setattr(main_window, "is_wine_process", lambda _pid: False)
    monkeypatch.setattr(main_window, "process_start_time", lambda _pid: 0)
    monkeypatch.setattr(main_window.MainWindow, "_take_over_from_cli",
                        lambda *_args: (True, None))
    monkeypatch.setattr(main_window.MainWindow, "_prepare_wine_gadget",
                        lambda *_args: (True, False))
    monkeypatch.setattr(main_window.MainWindow, "_known_process_id", lambda _self, _dlg: souls)
    for method in ("_stop_scan_and_refresh", "_stop_write_monitor", "_stop_freeze_worker",
                   "_close_process_tools", "_switch_pointer_dialogs", "_auto_select_backend",
                   "_load_saved_addresses", "_update_catalog_button"):
        monkeypatch.setattr(main_window.MainWindow, method, lambda *_args: None)
    monkeypatch.setattr(main_window, "GameNotesDialog", lambda *args, **kwargs: GameNotesDialog(
        *args, repository=NoteRepository(sessions), **kwargs))

    window = main_window.MainWindow("en")
    try:
        window._process_id = poe
        window._process_name = "PillarsOfEternity"
        dialog = GameNotesDialog(translations, poe, "PillarsOfEternity",
                                 repository=NoteRepository(sessions), parent=window)
        window._notes_dialog = dialog
        window._open_process_dialog()
        assert dialog.process_id == souls
        assert dialog.game == "DarkSoulsIII.ex"
        assert dialog.text() == ""
        assert _note_bodies(sessions, poe) == [("", "Gold")]
    finally:
        window.close()


class _FailingRepository(NoteRepository):
    """Notes that can be read but not saved (a locked database)."""

    def save_first_note(self, process_id, body):
        # pylint: disable=import-outside-toplevel
        from sqlalchemy.exc import OperationalError

        raise OperationalError("UPDATE", {}, Exception("database is locked"))


def test_notes_that_cannot_be_saved_are_not_dropped_without_asking(
        qt_app, translations, sessions, repository, monkeypatch):
    # pylint: disable=unused-argument,import-outside-toplevel
    from grembyte.ui.widgets.game_notes import GameNotesDialog

    poe = _process_id(sessions, "PillarsOfEternity", POE)
    souls = _process_id(sessions, "DarkSoulsIII.ex", PROTON_GE)
    dialog = GameNotesDialog(translations, poe, "PillarsOfEternity",
                             repository=_FailingRepository(sessions))
    asked = []
    answer = [False]

    def confirm_discard(error):
        asked.append(str(error))
        return answer[0]

    monkeypatch.setattr(dialog, "confirm_discard", confirm_discard)
    try:
        dialog.show()
        dialog.editor.setPlainText("Gold")
        # Cancel: the dialog stays with its text, on its game
        assert not dialog.close()
        assert dialog.isVisible() and dialog.text() == "Gold" and dialog.dirty
        dialog.reject()
        assert dialog.isVisible()
        assert not dialog.set_process(souls, "DarkSoulsIII.ex")
        assert dialog.process_id == poe and dialog.text() == "Gold"
        assert len(asked) == 3 and "locked" in asked[0]
        assert "locked" in dialog.status.text()
        # Discard: it goes
        answer[0] = True
        assert dialog.set_process(souls, "DarkSoulsIII.ex")
        assert dialog.process_id == souls and dialog.text() == ""
        dialog.editor.setPlainText("Souls")
        assert dialog.close()
        assert len(asked) == 5
    finally:
        answer[0] = True
        dialog.close()


def test_notes_change_game_and_save_the_previous_one(qt_app, translations, sessions, repository):
    # pylint: disable=unused-argument
    poe = _process_id(sessions, "PillarsOfEternity", POE)
    souls = _process_id(sessions, "DarkSoulsIII.ex", PROTON_GE)
    with sessions() as session:
        session.add(GameNote(process_id=souls, body="Souls"))
        session.commit()
    dialog = _dialog(translations, sessions, poe)
    try:
        dialog.editor.setPlainText("Gold")
        dialog.set_process(souls, "DarkSoulsIII.ex")
        assert _note_bodies(sessions, poe) == [("", "Gold")]
        assert dialog.text() == "Souls" and not dialog.dirty
    finally:
        dialog.close()


def test_saved_games_dialog_lists_every_saved_process(qt_app, translations, sessions, repository):
    # pylint: disable=unused-argument,import-outside-toplevel
    from grembyte.ui.widgets.saved_games import SavedGamesDialog

    poe = _process_id(sessions, "PillarsOfEternity", POE)
    NoteRepository(sessions).save_first_note(poe, "Gold")
    dialog = SavedGamesDialog(translations, GameExportRepository(sessions), current=poe)
    try:
        names = [dialog.table.item(row, 0).text() for row in range(dialog.table.rowCount())]
        assert names == ["abrt-dbus", "DarkSoulsIII.ex", "DarkSoulsIII.ex", "PillarsOfEternity"]
        row = names.index("PillarsOfEternity")
        assert dialog.table.currentRow() == row
        assert dialog.table.item(row, 2).text() == "1"
        dialog.table.cellDoubleClicked.emit(row, 0)
        assert dialog.process.process_id == poe
    finally:
        dialog.close()


def test_main_window_opens_the_picked_games_notes_and_closes_them_with_the_game(
        qt_app, translations, sessions, repository, monkeypatch):
    # pylint: disable=unused-argument,import-outside-toplevel,protected-access
    from PySide6.QtWidgets import QDialog  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui import main_window
    from grembyte.ui.widgets.game_notes import GameNotesDialog

    poe = _process_id(sessions, "PillarsOfEternity", POE)
    picked = GameExportRepository(sessions).list_processes()[-1]

    class Picker:  # pylint: disable=too-few-public-methods
        process = picked

        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window, "SavedGamesDialog", Picker)
    monkeypatch.setattr(main_window, "GameNotesDialog", lambda *args, **kwargs: GameNotesDialog(
        *args, repository=NoteRepository(sessions), **kwargs))
    window = main_window.MainWindow("en")
    try:
        window._open_notes()  # nothing attached: the picker's game
        dialog = window._notes_dialog
        assert dialog.process_id == poe and dialog.game == "PillarsOfEternity"
        dialog.editor.setPlainText("Gold")
        window._save_notes()
        assert _note_bodies(sessions, poe) == [("", "Gold")]

        dialog.editor.setPlainText("not saved: the process is gone")
        window._notes_changed({poe}, removed={poe})
        qt_app.processEvents()
        assert window._notes_dialog is None
        assert _note_bodies(sessions, poe) == [("", "Gold")]
    finally:
        window.close()


def test_notes_preview_is_formatted_in_the_theme_colors(qt_app, translations, sessions,
                                                         repository):
    # pylint: disable=unused-argument,import-outside-toplevel
    from PySide6.QtGui import QTextCharFormat  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.styles.themes import ThemeManager
    from grembyte.ui.widgets.code_editor import apply_code_theme

    manager = ThemeManager(user_theme_dir="/tmp/no-grembyte-themes")
    colors = manager.load_theme("Solarized-Light")["colors"]
    poe = _process_id(sessions, "PillarsOfEternity", POE)
    dialog = _dialog(translations, sessions, poe)
    try:
        dialog.editor.setPlainText(
            "## Gold\n- call `AddGold` in [the wiki](https://example.com)\n\n"
            "```js\nconst gold = 42;\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n")
        dialog.tabs.setCurrentIndex(1)
        apply_code_theme(colors)
        document = dialog.preview.document()

        def format_at(token):
            cursor = document.find(token)
            cursor.setPosition(cursor.selectionStart() + 1)
            return QTextCharFormat(cursor.charFormat())

        assert format_at("Gold").foreground().color().name() == colors["code_keyword"]
        assert format_at("AddGold").foreground().color().name() == colors["code_string"]
        assert format_at("AddGold").background().color().name() == colors["surface"]
        assert format_at("the wiki").foreground().color().name() == colors["accent"]
        assert format_at("const").foreground().color().name() == colors["code_keyword"]
        assert format_at("42").foreground().color().name() == colors["code_number"]
        code_frame = document.find("const").currentFrame()
        assert code_frame.frameFormat().background().color().name() == colors["surface"]
        assert dialog.preview.toPlainText().count("`") == 0
    finally:
        apply_code_theme(manager.load_theme("Github-Dark")["colors"])
        dialog.close()


def test_fenced_code_lines_count_each_block():
    # pylint: disable=import-outside-toplevel
    from grembyte.ui.widgets.markdown_preview import fenced_code_lines

    assert fenced_code_lines("```js\na\n```\n\n```js\nb\nc\n```\n") == [1, 2]
    assert fenced_code_lines("```\n```\n> ```c\n> int x;\n> ```\n- item\n\n  ~~~\n  a\n\n  ~~~\n") == [
        1, 2]
    assert fenced_code_lines("````\n```\n````\n```js\nopen") == [1, 1]


def test_preview_frames_each_code_block_of_one_language(qt_app, translations, sessions,
                                                         repository):
    # pylint: disable=unused-argument
    poe = _process_id(sessions, "PillarsOfEternity", POE)
    dialog = _dialog(translations, sessions, poe)
    try:
        dialog.editor.setPlainText("```js\nconst a = `open\n```\n\n```js\nconst b = 1;\n```\n")
        dialog.tabs.setCurrentIndex(1)
        document = dialog.preview.document()
        first = document.find("const a").currentFrame()
        second = document.find("const b").currentFrame()
        assert first is not second
        assert len(document.rootFrame().childFrames()) == 2
        # The string left open in the first block does not colour the second
        cursor = document.find("const b")
        cursor.setPosition(cursor.selectionStart() + 1)
        assert cursor.charFormat().fontWeight() > 400
    finally:
        dialog.close()
