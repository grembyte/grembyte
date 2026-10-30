"""Engine adapters and their registry (no target process needed)."""

import pytest

from hermes.engines import base
from hermes.engines.mono.adapter import MonoAdapter
from hermes.engines.unreal.adapter import UnrealAdapter
from hermes.engines.registry import (
    ENGINE_AGENT_SOURCE, ENGINES, detect_engine, engine_by_id, resolve_engine,
)
from tests.test_hooks import linux_only


@pytest.mark.parametrize("name", [
    "libmonobdwgc-2.0.so", "libmonosgen-2.0.so", "libmono.so",
    "mono-2.0-bdwgc.dll", "mono-2.0-sgen.dll", "mono.dll", "MONO-2.0-BDWGC.DLL",
])
def test_mono_recognizes_its_runtime_on_linux_and_wine(name):
    assert MonoAdapter().is_runtime_file(name)


@pytest.mark.parametrize("name", ["libmono-native.so", "MonoPosixHelper.dll", "libc.so.6"])
def test_mono_ignores_other_libraries(name):
    assert not MonoAdapter().is_runtime_file(name)


@pytest.mark.parametrize("name", [
    "Avowed-Win64-Shipping.exe", "EVERSPACE-Win64-Shipping.exe", "Game-Linux-Shipping",
    "Game-Win64-Test.exe", "UE4-CoreUObject-Win64-Shipping.dll", "UnrealEditor-CoreUObject.dll",
])
def test_unreal_recognizes_shipping_builds_and_modular_engines(name):
    assert UnrealAdapter().is_runtime_file(name)


@pytest.mark.parametrize("name", ["Avowed.exe", "Shipping.exe", "UnityPlayer.dll", "libc.so.6"])
def test_unreal_ignores_launchers_and_other_engines(name):
    assert not UnrealAdapter().is_runtime_file(name)


def test_unreal_adapter_capabilities_and_framework_filter():
    unreal = engine_by_id("unreal")
    for capability in (base.CATALOG, base.TRACE, base.MONITOR):
        assert unreal.supports(capability)
    # Not yet: method hooks, the call graph, calling methods
    for capability in (base.METHOD_HOOKS, base.CALL_GRAPH, base.CALL):
        assert not unreal.supports(capability)
    for package in ("/Script/CoreUObject", "/Script/Engine", "/Script/UMG",
                    "/Script/GameplayAbilities", "/Engine/EngineMaterials"):
        assert unreal.is_framework_assembly(package)
    for package in ("/Script/Alabama", "/Script/OEIItemSystemRuntime", "/Game/Blueprints"):
        assert not unreal.is_framework_assembly(package)
    assert unreal.native_tools


def _maps(tmp_path, pid, *paths):
    maps = tmp_path / str(pid) / "maps"
    maps.parent.mkdir()
    maps.write_text("".join(f"7f00-7f10 r-xp 00000000 08:01 7 {path}\n" for path in paths))


@linux_only
def test_detects_the_engine_from_the_mapped_runtime(tmp_path):
    _maps(tmp_path, 41, "/usr/lib/libc.so.6",
          "/games/Kathy Rain 2/MonoBleedingEdge/EmbedRuntime/mono-2.0-bdwgc.dll")
    _maps(tmp_path, 42, "/usr/lib/libc.so.6")
    _maps(tmp_path, 44, "/games/Avowed/Alabama/Binaries/Win64/Avowed-Win64-Shipping.exe")
    assert detect_engine(41, proc_root=str(tmp_path)).id == "mono"
    assert detect_engine(44, proc_root=str(tmp_path)).id == "unreal"
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
    for capability in (base.NAMING, base.CATALOG, base.TRACE, base.MONITOR, base.METHOD_HOOKS,
                       base.CALL_GRAPH, base.CALL):
        assert mono.supports(capability)
    assert mono.is_framework_assembly("UnityEngine.CoreModule")
    assert mono.is_framework_assembly("Rewired_Core") and mono.is_framework_assembly("Steamworks")
    assert not mono.is_framework_assembly("Assembly-CSharp")
    # The native trace crashed a Mono game: hermes keeps both native tools away.
    assert not mono.native_tools and base.EngineAdapter.native_tools


def test_agent_source_registers_adapters_before_the_facade():
    source = ENGINE_AGENT_SOURCE
    prelude = source.index("function registerEngine")
    adapter = source.index("registerEngine(mono);")
    unreal = source.index("registerEngine(unreal);")
    facade = source.index("const engine = (() => {")
    assert source.index("function pauseThreads") < prelude < adapter < unreal < facade
