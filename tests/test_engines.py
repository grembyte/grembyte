"""Engine adapters and their registry (no target process needed)."""

import pytest

from grembyte.engines import base
from grembyte.engines.il2cpp.adapter import Il2CppAdapter
from grembyte.engines.mono.adapter import MonoAdapter
from grembyte.engines.unreal.adapter import UnrealAdapter, bootstrapped_game
from grembyte.engines.registry import (
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


@pytest.mark.parametrize("name", ["GameAssembly.dll", "GameAssembly.so", "gameassembly.DLL"])
def test_il2cpp_recognizes_game_assembly(name):
    assert Il2CppAdapter().is_runtime_file(name)


@pytest.mark.parametrize("name", ["UnityPlayer.dll", "Assembly-CSharp.dll", "GameAssembly.exe"])
def test_il2cpp_ignores_other_modules(name):
    assert not Il2CppAdapter().is_runtime_file(name)


def test_il2cpp_adapter_capabilities_and_framework_filter():
    il2cpp = engine_by_id("il2cpp")
    for capability in (base.NAMING, base.CATALOG, base.TRACE, base.MONITOR, base.METHOD_HOOKS):
        assert il2cpp.supports(capability)
    # Not yet
    for capability in (base.CALL, base.CALL_GRAPH):
        assert not il2cpp.supports(capability)
    for name in ("mscorlib", "UnityEngine.CoreModule", "Unity.Entities", "__Generated"):
        assert il2cpp.is_framework_assembly(name)
    for name in ("ProjectM", "ProjectM.Gameplay.Systems", "Assembly-CSharp"):
        assert not il2cpp.is_framework_assembly(name)
    assert il2cpp.native_tools


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
    for capability in (base.CATALOG, base.TRACE, base.MONITOR, base.METHOD_HOOKS, base.CALL,
                       base.CALL_FOUND, base.CALL_GRAPH):
        assert unreal.supports(capability)
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
    _maps(tmp_path, 47, "/games/VRising/UnityPlayer.dll", "/games/VRising/GameAssembly.dll")
    assert detect_engine(41, proc_root=str(tmp_path)).id == "mono"
    assert detect_engine(47, proc_root=str(tmp_path)).id == "il2cpp"
    assert detect_engine(44, proc_root=str(tmp_path)).id == "unreal"
    assert detect_engine(42, proc_root=str(tmp_path)) is None
    assert detect_engine(43, proc_root=str(tmp_path)) is None


@linux_only
def test_an_unreal_bootstrap_of_the_games_name_is_not_the_game(tmp_path):
    # Gothic 1 Remake: the bootstrap in the game's root has the game's name.
    _maps(tmp_path, 45, "/games/Gothic 1 Remake/G1R-Win64-Shipping.exe")
    _maps(tmp_path, 46, "/games/Gothic 1 Remake/G1R/Binaries/Win64/G1R-Win64-Shipping.exe")
    assert detect_engine(45, proc_root=str(tmp_path)) is None
    assert detect_engine(46, proc_root=str(tmp_path)).id == "unreal"


def test_finds_the_game_an_unreal_bootstrap_started():
    gothic = r"S:\steamapps\common\Gothic 1 Remake"
    game = gothic + r"\G1R\Binaries\Win64\G1R-Win64-Shipping.exe"
    running = ["/usr/bin/bash", gothic + r"\G1R-Win64-Shipping.exe", game]
    assert bootstrapped_game(gothic + r"\G1R-Win64-Shipping.exe", running) == game
    # Paths differ in case under Wine; the bootstrap may have another name.
    avowed = r"S:\Avowed\Alabama\Binaries\Win64\Avowed-Win64-Shipping.exe"
    assert bootstrapped_game(r"s:\avowed\Avowed.exe", [avowed]) == avowed
    # The game itself, a bootstrap whose game is not running, other programs
    assert bootstrapped_game(game, running) is None
    assert bootstrapped_game(r"S:\Avowed\Avowed.exe", running) is None
    assert bootstrapped_game("/usr/bin/bash", running) is None
    assert bootstrapped_game("", running) is None


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
    assert not mono.supports(base.CALL_FOUND)  # finds Unity objects only
    assert mono.is_framework_assembly("UnityEngine.CoreModule")
    assert mono.is_framework_assembly("Rewired_Core") and mono.is_framework_assembly("Steamworks")
    assert not mono.is_framework_assembly("Assembly-CSharp")
    # The native trace crashed a Mono game: grembyte keeps both native tools away.
    assert not mono.native_tools and base.EngineAdapter.native_tools  # Mono: they ask first


def test_agent_source_registers_adapters_before_the_facade():
    source = ENGINE_AGENT_SOURCE
    prelude = source.index("function registerEngine")
    adapter = source.index("registerEngine(mono);")
    unreal = source.index("registerEngine(unreal);")
    facade = source.index("const engine = (() => {")
    assert source.index("function pauseThreads") < prelude < adapter < unreal < facade


def test_utf16_text_survives_a_cut_or_lone_surrogate():
    # pylint: disable=import-outside-toplevel
    import frida

    from grembyte.hooks.syntax import checker

    if not checker().available:
        pytest.skip("needs Frida's system session")
    session = frida.attach(0)
    try:
        # Frida's readUtf16String throws 'invalid string' for these (an emoji
        # cut by a length limit: Vampire Survivors' object browser)
        script = session.create_script(base.ENGINE_PRELUDE_SOURCE + """
            rpc.exports.read = (units, length) => {
              const memory = Memory.alloc(2 * units.length + 2);
              units.forEach((unit, i) => memory.add(2 * i).writeU16(unit));
              return readUtf16Text(memory, length);
            };
        """)
        script.load()
        read = script.exports_sync.read
        emoji = [0x41, 0xD83D, 0xDE00, 0x42]
        assert read(emoji, 4) == "A\U0001F600B"
        assert read(emoji, 2) == "A�"
        assert read([0xDE00, 0x41, 0xD83D, 0x42], 4) == "�A�B"
        assert read([0x41, 0, 0x42], 3) == "A"
        assert read(emoji, 0) == ""
        script.unload()
    finally:
        session.detach()
