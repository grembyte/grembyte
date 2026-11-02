"""Tests for the hook template dialog."""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hermes.db import models  # noqa: F401  pylint: disable=unused-import
from hermes.db.base import Base
from hermes.db.repository.hooks import HookRepository
from hermes.hooks.transforms import TransformParameter, example_templates
from hermes.ui.localization import load_translations
from hermes.ui.widgets.templates import format_choices, parse_choices


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module,import-outside-toplevel

    return QApplication.instance() or QApplication([])


@pytest.fixture(name="translations")
def fixture_translations():
    return load_translations("de")


@pytest.fixture(name="repository")
def fixture_repository(tmp_path, translations):
    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    repository = HookRepository(sessionmaker(bind=engine))
    repository.ensure_templates(example_templates())
    yield repository
    engine.dispose()


@pytest.fixture(name="dialog")
def fixture_dialog(qt_app, translations, repository):  # pylint: disable=unused-argument
    from hermes.ui.widgets.templates import TemplateDialog  # pylint: disable=import-outside-toplevel

    return TemplateDialog(translations, repository)


def _set_cell(dialog, row, column, text):
    dialog.param_table.item(row, column).setText(text)


def test_choices_round_trip():
    parameter = TransformParameter(
        "direction", "choice", "up", ("up", "down"), choice_labels=("Hoch", "")
    )

    assert format_choices(parameter) == "up=Hoch; down=down"
    assert parse_choices("up=Hoch; down ;") == (("up", "down"), ("Hoch", "down"))


def test_dialog_lists_templates(dialog):
    assert dialog.template_list.count() == 5
    assert dialog.name_input.text() == dialog.template_list.item(0).text() == "Clamp"
    assert dialog.param_table.rowCount() == 2
    assert not dialog.is_dirty()


def test_create_template(dialog, repository):
    # pylint: disable=protected-access
    dialog._new_template()
    dialog.name_input.setText("Verdoppeln")
    dialog.source_input.setPlainText("ctx => ctx.new * ctx.params.factor")
    dialog._add_parameter_row(None)
    _set_cell(dialog, 0, 0, "factor")
    _set_cell(dialog, 0, 1, "Faktor")
    _set_cell(dialog, 0, 3, "0x2")
    assert dialog.is_dirty()

    assert dialog.save()

    saved = next(t for t in repository.list_templates() if t.name == "Verdoppeln")
    assert saved.parameters == (TransformParameter("factor", default=2, label="Faktor"),)
    assert dialog.template_list.count() == 6
    assert dialog.template_list.currentItem().text() == "Verdoppeln"
    assert not dialog.is_dirty()


def test_edit_choice_parameter(dialog, repository):
    # pylint: disable=protected-access
    row = next(
        i for i in range(dialog.template_list.count())
        if dialog.template_list.item(i).text() == "Multiplier"
    )
    dialog.template_list.setCurrentRow(row)
    _set_cell(dialog, 1, 5, "increase=Mehr; both=Immer")

    assert dialog.save()

    saved = next(t for t in repository.list_templates() if t.name == "Multiplier")
    assert saved.parameters[1].choices == ("increase", "both")
    assert saved.parameters[1].choice_labels == ("Mehr", "Immer")


@pytest.mark.parametrize(
    ("name", "param_name", "default", "message"),
    [
        ("", "factor", "1", "Namen"),
        ("Multiplier", "factor", "1", "bereits"),
        ("Neu", "1factor", "1", "kein gültiger Parametername"),
        ("Neu", "factor", "viel", "Standardwert"),
    ],
)
def test_save_reports_invalid_input(dialog, name, param_name, default, message):
    # pylint: disable=protected-access,too-many-arguments
    dialog._new_template()
    dialog.name_input.setText(name)
    dialog._add_parameter_row(None)
    _set_cell(dialog, 0, 0, param_name)
    _set_cell(dialog, 0, 3, default)

    assert not dialog.save()
    assert message in dialog.error_label.text()


def test_delete_template(dialog, repository, monkeypatch):
    # pylint: disable=protected-access,import-outside-toplevel
    from PySide6.QtWidgets import QMessageBox  # pylint: disable=import-error,no-name-in-module

    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes
    )
    dialog._delete_template()

    assert sorted(t.name for t in repository.list_templates()) == [
        "Fixed value", "Method script", "Multiplier", "Result × factor"
    ]
    assert dialog.template_list.count() == 4
    assert dialog.name_input.text() == dialog.template_list.currentItem().text()


def test_wrapped_label_reserves_height_for_its_width(qt_app):  # pylint: disable=unused-argument
    from hermes.ui.widgets.labels import WrappedLabel  # pylint: disable=import-outside-toplevel

    label = WrappedLabel("word " * 60)
    label.show()  # hidden widgets get no resize events
    label.resize(200, 10)
    narrow = label.minimumHeight()
    label.resize(800, 10)

    assert narrow == label.heightForWidth(200)
    assert narrow > label.minimumHeight() > 0
    label.setText("")
    assert label.minimumHeight() == 0


def test_template_test_dialog_runs_definition(dialog, translations):
    # pylint: disable=import-outside-toplevel
    from hermes.ui.widgets.templates import TemplateTestDialog

    row = next(
        i for i in range(dialog.template_list.count())
        if dialog.template_list.item(i).text() == "Multiplier"
    )
    dialog.template_list.setCurrentRow(row)
    test = TemplateTestDialog(translations, dialog.editor_definition(), dialog)
    test.params_form._editors["factor"][1].setText("3")  # pylint: disable=protected-access

    result = test.run()

    assert result.value == 130
    assert "130" in test.result_label.text()

    test.new_input.setText("90")
    assert test.run().kept
    assert "90" in test.result_label.text()

    test.old_input.setText("viel")
    assert test.run() is None
    assert "Fehler" in test.result_label.text()


def test_debug_window_lists_filters_and_pauses(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets import hook_debug
    from hermes.ui.widgets.hook_debug import HookDebugWindow

    window = HookDebugWindow(translations)
    window.set_hooks({1: "#1 Gold", 2: "#2 Mana"})
    window.add_events([
        {"hookId": 1, "time": 0, "old": 100, "new": 110, "result": 130.0, "logs": ["x"]},
        {"hookId": 2, "time": 0, "skipped": "caller", "address": "0x10"},
        {"hookId": 1, "time": 0, "old": 1, "new": 2, "error": "Error: boom\nstack"},
    ], {1: 5})
    window.add_console("info", "hello")

    assert window.table.rowCount() == 4
    assert window.table.item(0, 7).text() == "130"
    assert "1 Ausgaben: x" in window.table.item(0, 8).text()
    assert "Aufrufer nicht gelernt" in window.table.item(1, 8).text()
    assert window.table.item(2, 8).text() == "Fehler: Error: boom"
    assert window.table.item(3, 1).text() == "Konsole"
    assert "5 vom Agenten verworfen" in window.count_label.text()

    window.table.selectRow(2)
    assert "stack" in window.details.toPlainText()

    window.filter_combo.setCurrentIndex(window.filter_combo.findData(1))
    assert window.table.rowCount() == 2

    window.btn_pause.setChecked(True)
    window.add_events([{"hookId": 1, "time": 0}])
    assert len(window.entries()) == 4
    assert "1 während der Pause" in window.count_label.text()

    window.btn_pause.setChecked(False)
    window.filter_combo.setCurrentIndex(0)
    window.add_events([{"hookId": 1, "time": 0}] * hook_debug.MAX_ENTRIES)
    assert len(window.entries()) == hook_debug.MAX_ENTRIES
    assert window.table.rowCount() == hook_debug.MAX_ENTRIES


def test_method_templates_keep_their_kind_and_code(dialog, repository):
    # pylint: disable=protected-access
    script = next(i for i in range(dialog.template_list.count())
                  if dialog.template_list.item(i).text() == "Method script")
    dialog.template_list.setCurrentRow(script)
    assert dialog.hook_kind_combo.currentData() == "method"
    assert "ctx.result" in dialog.script_hint.text()
    # The code parameter keeps its kind and its multi-line default.
    assert dialog.param_table.cellWidget(0, 2).currentData() == "code"
    assert dialog.save()
    saved = next(t for t in repository.list_templates() if t.name == "Method script")
    assert saved.kind == "method"
    assert saved.parameters[0].kind == "code" and "attacker" in saved.parameters[0].default

    # A new template becomes a method template by choosing the kind.
    dialog._new_template()
    dialog.name_input.setText("Halbieren")
    dialog.hook_kind_combo.setCurrentIndex(dialog.hook_kind_combo.findData("method"))
    dialog.source_input.setPlainText("function (ctx) { return ctx.result / 2; }")
    assert dialog.save()
    assert next(t for t in repository.list_templates() if t.name == "Halbieren").kind == "method"


def test_method_template_test_run(qt_app, translations):  # pylint: disable=unused-argument
    # pylint: disable=import-outside-toplevel
    from hermes.ui.widgets.templates import TemplateTestDialog

    multiply = example_templates()["method_multiply"]
    dialog = TemplateTestDialog(translations, multiply)
    dialog.result_input.setText("15")
    result = dialog.run()
    assert result.value == 30
    assert "15" in dialog.result_label.text() and "30" in dialog.result_label.text()

    script = example_templates()["method_script"]
    dialog = TemplateTestDialog(translations, script)
    result = dialog.run()
    # The example reads ctx.args.attacker, which only exists in the game.
    assert result.kept

    calling = TemplateTestDialog(translations, type(script)(
        "x", 'function (ctx) { return ctx.call("A.B"); }', kind="method"))
    assert "only works in the game" in calling.run().error


def test_syntax_errors_block_saving(dialog, repository):
    # pylint: disable=protected-access
    dialog._new_template()
    dialog.name_input.setText("Kaputt")
    dialog.source_input.setPlainText("function (ctx) {\n  return ctx.new * ;\n}")
    if not dialog.syntax_hint.problem():
        pytest.skip("needs Frida's system session")
    assert dialog.syntax_hint.text().startswith("Syntaxfehler in Zeile 2")

    assert not dialog.save()
    assert dialog.error_label.text().startswith("Syntaxfehler in Zeile 2")
    assert all(t.name != "Kaputt" for t in repository.list_templates())

    # The default of a code parameter is checked as a function body.
    dialog.source_input.setPlainText("function (ctx) { return ctx.new; }")
    assert dialog.syntax_hint.problem() is None
    assert dialog.syntax_hint.text() == "✓ Syntax in Ordnung"
    dialog._add_parameter_row(None)
    _set_cell(dialog, 0, 0, "code")
    dialog.param_table.cellWidget(0, 2).setCurrentIndex(2)  # code
    _set_cell(dialog, 0, 3, "if (")
    assert not dialog.save()
    assert dialog.error_label.text().startswith("code: Syntaxfehler")

    _set_cell(dialog, 0, 3, "return 1;")
    assert dialog.save()


def test_source_that_is_no_function_gets_a_hint(dialog):
    dialog.source_input.setPlainText("ctx.new * 2")
    if dialog.syntax_hint.problem() is None and not dialog.syntax_hint.text():
        pytest.skip("needs Frida's system session")
    assert dialog.syntax_hint.text().startswith("Hinweis:")
