"""Tests for the process and hook repository."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hermes.db.base import Base
from hermes.db import models  # noqa: F401  pylint: disable=unused-import
from hermes.db.repository.hooks import HookRepository
from hermes.hooks.transforms import MultiplyTransform, ScriptTransform


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
    assert saved.transform_kind == "multiply"
    assert saved.transform_params == {"factor": 10, "direction": "increase"}
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
    assert saved.transform_kind == "script"
    assert saved.transform_params == {"source": "ctx => 1"}
    assert not saved.enabled
    assert saved.callers == [[["game", 0x2000]]]

    repository.delete_hook(hook_id)
    assert repository.list_hooks(process_id) == []

