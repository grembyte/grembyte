"""Tests for the process and hook repository."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hermes.db.base import Base
from hermes.db import models  # noqa: F401  pylint: disable=unused-import
from hermes.db.repository.hooks import HookRepository, hook_transform
from hermes.hooks.transforms import (
    MultiplyTransform,
    ScriptTransform,
    TemplateTransform,
    TransformDefinition,
    TransformParameter,
    example_templates,
)
from hermes.ui.localization import load_translations


@pytest.fixture(name="repository")
def fixture_repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    yield HookRepository(sessionmaker(bind=engine))
    engine.dispose()


def _add(repository, process_id, transform, **overrides):
    fields = {
        "label": "Gold",
        "module": "game",
        "module_offset": 0x1139,
        "instruction_bytes": "0137",
        "instruction_text": "add dword ptr [rdi], esi",
        "value_type": "int32",
        "transform": transform,
    }
    fields.update(overrides)
    return repository.add_hook(process_id, **fields)


def test_processes_are_identified_by_name_and_path(repository):
    first = repository.get_or_create_process("game", "/opt/game/game")

    assert repository.get_or_create_process("game", "/opt/game/game") == first
    assert repository.get_or_create_process("game", "/other/game") != first


def test_hooks_round_trip(repository):
    process_id = repository.get_or_create_process("game", "/opt/game/game")
    other_id = repository.get_or_create_process("other", "/opt/other")
    hook_id = _add(repository, process_id, MultiplyTransform(10))
    _add(repository, other_id, MultiplyTransform(2))

    [saved] = repository.list_hooks(process_id)

    assert saved.id == hook_id
    assert (saved.module, saved.module_offset, saved.instruction_bytes) == (
        "game", 0x1139, "0137"
    )
    assert saved.template_id is None
    assert saved.transform_name == "multiply"
    assert saved.transform_source == MultiplyTransform.source.strip()
    assert [p["name"] for p in saved.transform_parameters] == ["factor", "direction"]
    assert saved.transform_params == {"factor": 10, "direction": "increase"}
    assert hook_transform(saved).describe() == "multiply(factor=10, direction=increase)"
    assert saved.enabled
    assert (saved.caller_depth, saved.callers) == (1, [])


def test_update_and_delete_hook(repository):
    process_id = repository.get_or_create_process("game", "/opt/game/game")
    hook_id = _add(repository, process_id, MultiplyTransform(10))

    repository.update_hook(hook_id, label="Mana", transform=ScriptTransform("ctx => 1"))
    repository.update_hook(hook_id, enabled=False)
    repository.update_hook(hook_id, callers=[[("game", 0x2000)]])
    [saved] = repository.list_hooks(process_id)

    assert saved.label == "Mana"
    assert saved.transform_name == "script"
    assert saved.transform_source == "ctx => 1"
    assert saved.transform_params == {}
    assert not saved.enabled
    assert saved.callers == [[["game", 0x2000]]]

    repository.delete_hook(hook_id)
    assert repository.list_hooks(process_id) == []


def test_example_templates_are_created_once(repository):
    examples = example_templates(load_translations("de"))

    repository.ensure_templates(examples)
    repository.ensure_templates(examples)

    templates = repository.list_templates()
    assert sorted(t.name for t in templates) == ["Begrenzen", "Fester Wert", "Multiplikator"]
    multiply = next(t for t in templates if t.name == "Multiplikator")
    assert multiply == examples["multiply"]
    assert multiply.parameters[1].choice_labels[0] == "Nur Erhöhungen"


def test_example_templates_respect_existing_names(repository):
    examples = example_templates(load_translations("de"))
    repository.save_template(TransformDefinition("Multiplikator", "ctx => 1"))

    repository.ensure_templates(examples)

    templates = repository.list_templates()
    assert len(templates) == 3
    assert next(t for t in templates if t.name == "Multiplikator").source == "ctx => 1"


def test_save_template_rejects_duplicate_names(repository):
    first = repository.save_template(TransformDefinition("A", "ctx => 1"))
    second = repository.save_template(TransformDefinition("B", "ctx => 2"))

    with pytest.raises(ValueError, match="already exists"):
        repository.save_template(TransformDefinition("A", "ctx => 3"), second)
    repository.save_template(TransformDefinition("A", "ctx => 4"), first)

    assert [t.source for t in repository.list_templates()] == ["ctx => 4", "ctx => 2"]


def test_hooks_keep_their_copy_of_a_template(repository):
    process_id = repository.get_or_create_process("game", "/opt/game/game")
    parameters = (TransformParameter("factor", default=2, label="Faktor"),)
    template_id = repository.save_template(
        TransformDefinition("Faktor", "ctx => ctx.new * ctx.params.factor", parameters)
    )
    [template] = repository.list_templates()
    hook_id = _add(repository, process_id, TemplateTransform(template, factor=3))

    repository.save_template(
        TransformDefinition("Faktor", "ctx => ctx.new * 2", parameters), template_id
    )
    [saved] = repository.list_hooks(process_id)
    copy = hook_transform(saved)

    assert copy.definition.template_id == template_id
    assert copy.source == "ctx => ctx.new * ctx.params.factor"
    assert copy.params == {"factor": 3}
    assert copy.definition != repository.list_templates()[0]

    repository.delete_template(template_id)
    [saved] = repository.list_hooks(process_id)
    assert saved.id == hook_id
    assert saved.template_id == template_id
    assert hook_transform(saved).source == "ctx => ctx.new * ctx.params.factor"
    # The id of the deleted template is not handed out again.
    assert repository.save_template(TransformDefinition("Neu", "ctx => 1")) != template_id


def test_addresses_with_pointer_scan(tmp_path):
    # pylint: disable=import-outside-toplevel
    from hermes.core.pointer import PointerChain
    from hermes.db.repository.addresses import (
        AddressRepository,
        SavedPointerScan,
        address_pointer,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'addresses.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    process_id = HookRepository(sessions).get_or_create_process("game", "/opt/game")
    repository = AddressRepository(sessions)
    chain = PointerChain("game", 0x3070, (0x18, 0x40, 0x4))

    gold = repository.add_address(process_id, label="Gold", value_type="int32",
                                  address=0x5000)
    score = repository.add_address(process_id, label="Score", value_type="int32",
                                   address=0x4010, pointer=PointerChain("game", 0x10))
    repository.update_address(
        gold, label="Gold!", address=0x6000, pointer=chain, frozen=True, frozen_value="5000"
    )
    repository.save_pointer_scan(gold, SavedPointerScan([chain], 5, 0x1000, 0))
    repository.save_pointer_scan(gold, SavedPointerScan([chain, chain], 5, 0x1000, 2))

    saved_gold, saved_score = repository.list_addresses(process_id)
    assert (saved_gold.label, saved_gold.address, saved_gold.frozen, saved_gold.frozen_value) \
        == ("Gold!", 0x6000, True, "5000")
    assert address_pointer(saved_gold) == chain
    assert address_pointer(saved_score) == PointerChain("game", 0x10)
    scan = repository.get_pointer_scan(gold)
    assert (scan.chains, scan.rounds) == ([chain, chain], 2)
    assert repository.get_pointer_scan(score) is None

    repository.update_address(score, pointer=None)
    assert address_pointer(repository.list_addresses(process_id)[1]) is None
    repository.delete_address(gold)
    assert [a.label for a in repository.list_addresses(process_id)] == ["Score"]
    assert repository.get_pointer_scan(gold) is None
    engine.dispose()


def test_init_db_adds_new_nullable_columns(tmp_path):
    # pylint: disable=import-outside-toplevel
    import sqlite3

    from hermes.db.engine import init_db

    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE processes (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)")
    connection.execute("INSERT INTO processes (name) VALUES ('game')")
    connection.commit()
    connection.close()

    engine = create_engine(f"sqlite:///{path}")
    init_db(engine)
    init_db(engine)

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(hooks)")}
    process_columns = {row[1] for row in connection.execute("PRAGMA table_info(processes)")}
    assert "pointer" in columns
    assert "created_at" in process_columns
    assert connection.execute("SELECT name FROM processes").fetchall() == [("game",)]
    connection.close()
    engine.dispose()


def test_default_db_path_follows_environment(monkeypatch, tmp_path):
    # pylint: disable=import-outside-toplevel
    from hermes.db.engine import default_db_path

    monkeypatch.delenv("HERMES_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_db_path() == tmp_path / "hermes-hook" / "hermes.db"

    monkeypatch.setenv("HERMES_DB", str(tmp_path / "custom.db"))
    assert default_db_path() == tmp_path / "custom.db"


def test_legacy_db_is_copied_once(tmp_path):
    # pylint: disable=import-outside-toplevel
    from hermes.db.engine import adopt_legacy_db

    legacy = tmp_path / "repo" / "hermes.db"
    legacy.parent.mkdir()
    legacy.write_bytes(b"old")
    path = tmp_path / "data" / "hermes.db"
    path.parent.mkdir()

    assert adopt_legacy_db(path, legacy)
    assert path.read_bytes() == b"old" and legacy.exists()

    path.write_bytes(b"new")
    assert not adopt_legacy_db(path, legacy)
    assert path.read_bytes() == b"new"
    assert not adopt_legacy_db(tmp_path / "data" / "other.db", tmp_path / "missing.db")
