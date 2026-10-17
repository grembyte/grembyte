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
    from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module,import-outside-toplevel

    return QApplication.instance() or QApplication([])


@pytest.fixture(name="translations")
def fixture_translations():
    return load_translations("de")


@pytest.fixture(name="repository")
def fixture_repository(tmp_path, translations):
    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    repository = HookRepository(sessionmaker(bind=engine))
    repository.ensure_templates(example_templates(translations))
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
    assert dialog.template_list.count() == 3
    assert dialog.name_input.text() == dialog.template_list.item(0).text() == "Begrenzen"
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
    assert dialog.template_list.count() == 4
    assert dialog.template_list.currentItem().text() == "Verdoppeln"
    assert not dialog.is_dirty()


def test_edit_choice_parameter(dialog, repository):
    # pylint: disable=protected-access
    row = next(
        i for i in range(dialog.template_list.count())
        if dialog.template_list.item(i).text() == "Multiplikator"
    )
    dialog.template_list.setCurrentRow(row)
    _set_cell(dialog, 1, 5, "increase=Mehr; both=Immer")

    assert dialog.save()

    saved = next(t for t in repository.list_templates() if t.name == "Multiplikator")
    assert saved.parameters[1].choices == ("increase", "both")
    assert saved.parameters[1].choice_labels == ("Mehr", "Immer")


@pytest.mark.parametrize(
    ("name", "param_name", "default", "message"),
    [
        ("", "factor", "1", "Namen"),
        ("Multiplikator", "factor", "1", "bereits"),
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
    from PyQt6.QtWidgets import QMessageBox  # pylint: disable=import-error,no-name-in-module

    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes
    )
    dialog._delete_template()

    assert sorted(t.name for t in repository.list_templates()) == [
        "Fester Wert", "Multiplikator"
    ]
    assert dialog.template_list.count() == 2
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

