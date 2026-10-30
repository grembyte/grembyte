"""Tests for the process and hook repository."""

import sys

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
    assert not saved.breakpoint


def test_update_and_delete_hook(repository):
    process_id = repository.get_or_create_process("game", "/opt/game/game")
    hook_id = _add(repository, process_id, MultiplyTransform(10))

    repository.update_hook(hook_id, label="Mana", transform=ScriptTransform("ctx => 1"))
    repository.update_hook(hook_id, enabled=False)
    repository.update_hook(hook_id, callers=[[("game", 0x2000)]])
    repository.update_hook(hook_id, breakpoint=True)
    [saved] = repository.list_hooks(process_id)

    assert saved.label == "Mana"
    assert saved.transform_name == "script"
    assert saved.transform_source == "ctx => 1"
    assert saved.transform_params == {}
    assert not saved.enabled
    assert saved.callers == [[["game", 0x2000]]]
    assert saved.breakpoint

    repository.delete_hook(hook_id)
    assert repository.list_hooks(process_id) == []


def test_example_templates_are_created_once(repository):
    examples = example_templates()

    repository.ensure_templates(examples)
    repository.ensure_templates(examples)

    templates = repository.list_templates()
    assert sorted(t.name for t in templates) == [
        "Clamp", "Fixed value", "Method script", "Multiplier", "Result × factor"]
    multiply = next(t for t in templates if t.name == "Multiplier")
    assert multiply == examples["multiply"]
    assert multiply.parameters[1].choice_labels[0] == "Increases only"
    assert multiply.kind == "value"
    # Method hook examples keep their kind.
    method = next(t for t in templates if t.name == "Result × factor")
    assert method.kind == "method" and method == examples["method_multiply"]


def test_example_templates_respect_existing_names(repository):
    examples = example_templates()
    repository.save_template(TransformDefinition("Multiplier", "ctx => 1"))

    repository.ensure_templates(examples)

    templates = repository.list_templates()
    assert len(templates) == 5
    assert next(t for t in templates if t.name == "Multiplier").source == "ctx => 1"


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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="XDG_DATA_HOME is Linux only")
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


def test_vacuum_frees_the_space_of_deleted_rows(tmp_path):
    # pylint: disable=import-outside-toplevel
    from sqlalchemy import text

    from hermes.db.engine import database_size, vacuum_db

    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE junk (data BLOB)"))
        connection.execute(text("INSERT INTO junk VALUES (zeroblob(2000000))"))
        connection.execute(text("DROP TABLE junk"))
    size, free = database_size(engine)
    assert free > 1_900_000

    before, after = vacuum_db(engine)
    assert before == size and after < 200_000
    assert database_size(engine)[1] == 0
    engine.dispose()


def test_german_examples_and_their_hooks_become_english(repository):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.db.models import Hook, HookTemplate, MethodHook
    from hermes.hooks.transforms import GERMAN_EXAMPLE_TEXTS

    examples = example_templates()
    multiply = examples["multiply"].parameters_json()
    multiply[0]["label"] = "Faktor"
    multiply[1]["label"] = "Anwenden auf"
    multiply[1]["choices"] = [["increase", "Nur Erhöhungen"], ["decrease", "Nur Verringerungen"],
                              ["both", "Alle Änderungen"]]
    script = examples["method_script"].parameters_json()
    script[0]["label"] = "Skript"
    script[0]["default"] = GERMAN_EXAMPLE_TEXTS["method_script.code_default"][1]
    process_id = repository.get_or_create_process("game", "/opt/game/game")
    with repository._sessions() as session:
        german = HookTemplate(name="Multiplikator", source=examples["multiply"].source,
                              parameters=multiply, seed_key="multiply", description="")
        own_script = HookTemplate(name="Mein Skript", source=examples["method_script"].source,
                                  parameters=script, seed_key="method_script",
                                  description="Selbst geschrieben", kind="method")
        mine = HookTemplate(name="Multi_2", source="ctx => 1", parameters=multiply)
        session.add_all([german, own_script, mine])
        session.flush()
        session.add_all([
            Hook(process_id=process_id, label="Gold", module="game", module_offset=1,
                 instruction_bytes="01", value_type="int32", template_id=german.id,
                 transform_name="Multiplikator", transform_source=german.source,
                 transform_parameters=multiply, transform_params={"factor": 2}),
            # Its template was deleted: found by the name and the unchanged code.
            Hook(process_id=process_id, label="Clamped", module="game", module_offset=2,
                 instruction_bytes="01", value_type="int32", template_id=99,
                 transform_name="Begrenzen", transform_source=examples["clamp"].source,
                 transform_parameters=examples["clamp"].parameters_json(),
                 transform_params={}),
            MethodHook(process_id=process_id, label="DoDamage", managed={"token": 1},
                       template_id=own_script.id, transform_name="Methoden-Skript",
                       transform_source=own_script.source, transform_parameters=script,
                       transform_params={"code": "// mein Code"}),
        ])
        session.commit()

    assert repository.translate_examples() == 5
    assert repository.translate_examples() == 0
    templates = {t.name: t for t in repository.list_templates()}
    assert templates["Multiplier"].parameters == examples["multiply"].parameters
    # A name and a description the user wrote stay; the German labels go.
    assert templates["Mein Skript"].description == "Selbst geschrieben"
    assert templates["Mein Skript"].parameters == examples["method_script"].parameters
    assert templates["Multi_2"].parameters[0].label == "Faktor"
    with repository._sessions() as session:
        names = {hook.label: hook for hook in session.query(Hook)}
        assert names["Gold"].transform_name == "Multiplier"
        assert names["Gold"].transform_parameters[1]["choices"][0] == ["increase", "Increases only"]
        assert names["Clamped"].transform_name == "Clamp"
        method = session.query(MethodHook).one()
        assert method.transform_name == "Method script"
        assert method.transform_parameters[0]["label"] == "Script"
        assert method.transform_params == {"code": "// mein Code"}


def test_an_english_name_taken_by_the_user_keeps_the_german_example(repository):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.db.models import HookTemplate

    examples = example_templates()
    with repository._sessions() as session:
        session.add_all([
            HookTemplate(name="Multiplikator", source=examples["multiply"].source,
                         parameters=examples["multiply"].parameters_json(),
                         seed_key="multiply", description=""),
            HookTemplate(name="Multiplier", source="ctx => ctx.new * 3", parameters=[]),
        ])
        session.commit()

    repository.translate_examples()
    repository.ensure_templates(examples)
    names = {t.name: t for t in repository.list_templates()}
    assert names["Multiplier"].source == "ctx => ctx.new * 3"
    assert names["Multiplikator"].source == examples["multiply"].source
    # The other examples are created all the same.
    assert {"Fixed value", "Clamp"} <= set(names)


def test_init_db_sets_aside_a_table_of_the_first_versions(tmp_path):
    # pylint: disable=import-outside-toplevel
    import sqlite3

    from hermes.db.engine import init_db
    from hermes.db.repository.addresses import AddressRepository

    path = tmp_path / "first.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE processes (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL);
        INSERT INTO processes (name) VALUES ('game');
        CREATE TABLE addresses (id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL,
                                label VARCHAR, address VARCHAR NOT NULL,
                                value_type VARCHAR, frozen INTEGER, last_value VARCHAR);
        INSERT INTO addresses (game_id, label, address) VALUES (1, 'Gold', '0x5000');
        CREATE TABLE pointer_scans (id INTEGER PRIMARY KEY,
                                    address_id INTEGER NOT NULL UNIQUE
                                        REFERENCES addresses (id) ON DELETE CASCADE,
                                    max_depth INTEGER NOT NULL, max_offset INTEGER NOT NULL,
                                    chains JSON NOT NULL, rounds INTEGER NOT NULL DEFAULT 0);
    """)
    connection.commit()
    connection.close()

    engine = create_engine(f"sqlite:///{path}")
    init_db(engine)
    init_db(engine)

    connection = sqlite3.connect(path)
    tables = {row[0]: row[1] for row in connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
    # The old rows are kept; the process keeps its id and gets a path.
    assert connection.execute("SELECT label FROM addresses_old").fetchall() == [("Gold",)]
    assert connection.execute("SELECT id, name, path FROM processes").fetchall() == [
        (1, "game", "")]
    assert "addresses_old" not in tables["pointer_scans"]
    connection.close()

    sessions = sessionmaker(bind=engine)
    repository = AddressRepository(sessions)
    gold = repository.add_address(1, label="Gold", value_type="int32", address=0x5000)
    assert [(a.label, a.address) for a in repository.list_addresses(1)] == [("Gold", 0x5000)]
    # Foreign keys are on: removing the process removes its addresses.
    with engine.begin() as db:
        db.exec_driver_sql("DELETE FROM processes WHERE id = 1")
    assert repository.list_addresses(1) == [] and gold
    engine.dispose()


def test_scripts_are_saved_changed_and_deleted_per_process(tmp_path):
    # pylint: disable=import-outside-toplevel
    from hermes.db.repository.scripts import ScriptRepository
    from hermes.features.scripts import ScriptSpec

    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    try:
        hooks = HookRepository(sessions)
        scripts = ScriptRepository(sessions)
        game = hooks.get_or_create_process("game", "/games/game")
        other = hooks.get_or_create_process("other", "/games/other")
        heal = scripts.add_script(game, label="Heal", source="ctx.log(1);", mode="repeat",
                                  seconds=60)
        scripts.add_script(other, label="Gold", source="return 1;", mode="manual", seconds=0)

        scripts.update_script(heal, seconds=30, enabled=False)
        [saved] = scripts.list_scripts(game)
        assert (saved.label, saved.source, saved.mode, saved.seconds, saved.enabled) == (
            "Heal", "ctx.log(1);", "repeat", 30, False)
        spec = ScriptSpec(saved.source, saved.mode, saved.seconds, saved.enabled, saved.label)
        assert spec.to_agent(3) == {"id": 3, "source": "ctx.log(1);", "mode": "repeat",
                                    "seconds": 30, "enabled": False}

        scripts.delete_script(heal)
        assert scripts.list_scripts(game) == []
        assert [script.label for script in scripts.list_scripts(other)] == ["Gold"]
    finally:
        engine.dispose()


def test_script_spec_rejects_unknown_modes_and_busy_loops():
    # pylint: disable=import-outside-toplevel
    from hermes.features.scripts import ScriptSpec

    with pytest.raises(ValueError):
        ScriptSpec("return 1;", "hourly", 60)
    with pytest.raises(ValueError):
        ScriptSpec("return 1;", "repeat", 0)
    with pytest.raises(ValueError):
        ScriptSpec("return 1;", "once", -1)
    assert ScriptSpec("return 1;", "once", 0).mode == "once"
