"""Engine adapters and their registry (no target process needed)."""

import pytest

from hermes.engines import base
from hermes.engines.mono.adapter import MonoAdapter
from hermes.engines.registry import (
    ENGINE_AGENT_SOURCE, ENGINES, detect_engine, engine_by_id, resolve_engine,
)


@pytest.mark.parametrize("name", [
    "libmonobdwgc-2.0.so", "libmonosgen-2.0.so", "libmono.so",
    "mono-2.0-bdwgc.dll", "mono-2.0-sgen.dll", "mono.dll", "MONO-2.0-BDWGC.DLL",
])
def test_mono_recognizes_its_runtime_on_linux_and_wine(name):
    assert MonoAdapter().is_runtime_file(name)


@pytest.mark.parametrize("name", ["libmono-native.so", "MonoPosixHelper.dll", "libc.so.6"])
def test_mono_ignores_other_libraries(name):
    assert not MonoAdapter().is_runtime_file(name)


def _maps(tmp_path, pid, *paths):
    maps = tmp_path / str(pid) / "maps"
    maps.parent.mkdir()
    maps.write_text("".join(f"7f00-7f10 r-xp 00000000 08:01 7 {path}\n" for path in paths))


def test_detects_the_engine_from_the_mapped_runtime(tmp_path):
    _maps(tmp_path, 41, "/usr/lib/libc.so.6",
          "/games/Kathy Rain 2/MonoBleedingEdge/EmbedRuntime/mono-2.0-bdwgc.dll")
    _maps(tmp_path, 42, "/usr/lib/libc.so.6")
    assert detect_engine(41, proc_root=str(tmp_path)).id == "mono"
    assert detect_engine(42, proc_root=str(tmp_path)) is None
    assert detect_engine(43, proc_root=str(tmp_path)) is None


def test_resolve_prefers_a_given_adapter_then_detection():
    given = MonoAdapter()
    assert resolve_engine(0, given) is given
    # nothing detected (no such process): the first adapter
    assert resolve_engine(-1) is ENGINES[0]


def test_identities_without_engine_are_mono():
    assert engine_by_id(None).id == "mono"
    assert engine_by_id("mono").id == "mono"
    assert engine_by_id("unknown") is None


def test_mono_adapter_capabilities_and_framework_filter():
    mono = engine_by_id("mono")
    for capability in (base.NAMING, base.CATALOG, base.TRACE, base.MONITOR, base.METHOD_HOOKS):
        assert mono.supports(capability)
    assert mono.is_framework_assembly("UnityEngine.CoreModule")
    assert mono.is_framework_assembly("Rewired_Core") and mono.is_framework_assembly("Steamworks")
    assert not mono.is_framework_assembly("Assembly-CSharp")


def test_agent_source_registers_adapters_before_the_facade():
    source = ENGINE_AGENT_SOURCE
    prelude = source.index("function registerEngine")
    adapter = source.index("registerEngine(mono);")
    facade = source.index("const engine = (() => {")
    assert source.index("function pauseThreads") < prelude < adapter < facade
