# Roadmap and ideas

A collection of extensions that would help find addresses and hook entry points, and of
open points from the Wine/Proton and Unity work.
As of October 2026.

## Starting point

Available independent of any engine:

- Value scan: exact, range, unknown initial value, changed/unchanged, greater/less,
  differences, fuzzy floats
- Pointer scan for stable pointer chains
- Write watch: which instruction writes an address, optionally also which reads it
- Hooks on write instructions, with caller learning
- Memory view, disassembler, freezing

For Unity games with Mono there are also the catalog, the trace (with preselection
filters), the method monitor and method hooks, through the engine adapter layer
(`hermes/engines/`).

Methods and functions found in the catalog, the trace or the native trace can be marked
with ★ (first table column). Favorites are saved per program at once, so they survive a
frozen or crashed game; View → "Favorites…" lists them with a note and hands them to the
method or native function monitor.

## Proposals, independent of any engine

Sorted by benefit relative to effort.

### 1. Native call diff trace

The native counterpart of the Mono trace and the biggest lever. Frida's **Stalker** records
which functions or basic blocks a thread executes. With the same logic as the Mono trace
(baseline against scenario) one finds the function that only runs when paying or taking
damage.

- Works without metadata, so for C++ games, own engines, Unreal, Godot, Source
- Reusable: trace dialog, `analyze`, the "new/more often" verdicts
- Only the agent would be new (Stalker instead of Mono hooks)
- Model: Cheat Engine's "Code Filter" / "Ultimap"
- Open: check how Stalker behaves under Wine with the Windows gadget

**Status (prototype, 2026-10-19):** View → "Native trace…", independent of any engine.
`hermes/features/native_trace.py` (agent + `NativeTraceWorker`),
`hermes/ui/widgets/native_trace.py` (window). Session logic shared with the Mono trace in
`AgentWorker` (`hermes/features/trace.py`); `analyze` and the verdicts are the same.

- Stalker follows the chosen threads with a CModule `transform` (since 2026-10-22, before
  that `events: {call: true}` and `onCallSummary`): before every `call` of the followed code
  sits a callout in C that works out the target from the registers (direct, register, memory
  operand including `rip`-relative) and counts it in a hash table, in the game's thread. The
  order of the first calls is exact and knows the call site. x86/x64 only; a `call` through
  `fs:`/`gs:` is not counted. Modules not chosen run natively through `Stalker.exclude` (only
  calls *into* them count). Preselection: the modules in the game's folder, the threads
  running right now (CPU time or context switches in `/proc`).
- Function names: exports, on Linux also symbols (`DebugSymbol`), otherwise `module+0x…`.
- Verified: test game (C) and a Python interpreter with `libpython` (~6 million calls/2 s,
  the scenario found the JSON encoder), and Baldur's Gate under Proton (see below).
- Learned: Stalker's queue holds 16 384 events per drain by default and drops the rest
  **silently**. With the CModule there is no queue any more; calls are only lost when the
  function table (2^19) is full (`lost`, warned about in the window).
- Learned (2026-10-22): on x86-64 every new 4 MB code slab of Stalker must lie within ±2 GB
  of its thread context, which Gum maps without a hint. EVERSPACE (UE4, 9.9 GB in 5700
  regions) left no such room there: `gum_memory_allocate_near` returned NULL, Stalker wrote
  to `0x18` and the game crashed 0.4 s after the start (Pillars of Eternity after 20–40 s,
  same cause). The agent now fills every gap above the highest free area of 256 MB with
  placeholders while Stalker creates the contexts (`withRoomNearby`, Linux x86-64), so they
  land at the top of that area. EVERSPACE: 34 threads followed, baseline recorded, clean
  stop. Test: `test_native_trace_survives_a_crowded_address_space` (crashes without it).
- Learned: Frida's TinyCC has no compare-and-swap (`g_atomic_*_compare_and_exchange`,
  `__atomic_*`, `__sync_*` are missing), only `g_atomic_int_add`: entries are taken through a
  claim counter. Load test with libpython (4 threads): ~11 million calls/2 s without loss,
  the scenario slightly faster than with `onCallSummary` (1.2 s instead of 1.5 s).
- Learned (2026-10-22): under Windows, `Stalker.follow(id)` takes another thread over through
  a hardware breakpoint at the instruction it stands at, nearly always the return of a system
  call in `ntdll`. Wine keeps the breakpoint after Stalker cleared it in the exception's
  context. Stalker's handler itself calls `Sleep` while taking the thread over, through the
  same return: the breakpoint hit again inside the handler, which then waited for a lock it
  already held. The game froze (winedbg: all game threads in Frida's handler, waiting on an
  SRW lock), or it crashed later with `EXCEPTION_SINGLE_STEP`. This affected the old agent
  too (test game under Proton: 3 of 5 runs froze). Removing the breakpoint from the agent
  thread is a race; `NtSetContextThread` on the thread itself from a vectored exception
  handler placed first has no effect under Wine. Now each thread follows itself (no
  breakpoint): `Thread.backtrace(…, FUZZY)` finds its next return address into a followed
  module, and a one-shot Interceptor hook there calls `Stalker.follow()` in the thread. Test
  game under Proton: 10 of 10 runs clean, and only this way is the render thread really
  followed (8379 instead of 585 calls). Threads without a return into followed code count as
  not followable; those that have not entered yet are in `waiting`. Remaining risk: the hook
  overwrites a few bytes after a `call` instruction; if code jumps into the middle of them,
  the game crashes.
- Reproducing without a game: a small Windows test game built with `x86_64-w64-mingw32-gcc`,
  run with Proton's `files/bin/wine` in a `WINEPREFIX` of its own; Hermes connects through
  the gadget as with a real game. Stacks in the frozen state: `wine winedbg --command
  "bt all" <Windows PID>` (gdb only sees Wine's Unix side).
- Learned: a followed thread that hangs in a blocking call holds on to Stalker's code;
  `script.unload()` then waits up to ~25 s (the game survives). So idle threads are not
  preselected, and "Stop following" does not unload the agent at once. A new trace gets a
  fresh agent, because Stalker cannot take exclusions back.

Next steps:

- Try it in a real native Linux game (load, playability, quality of the hits)
- **Done (2026-10-22):** freezing under Wine/Proton fixed, see above. Confirmed with Baldur's
  Gate under Proton Experimental (1 thread, `Baldur.exe`: 3.2 million calls in the baseline,
  nothing lost, ended cleanly). The 32-bit path of the native trace under Wine is untested
  (Shadowrun Returns is Mono, where native tools are blocked); the gadget itself runs in
  32-bit games (Mono trace in Shadowrun Returns).
- Callers: the sequence shows the call site of the first call per thread. A list of all
  callers per function (a count per call site and target) is still missing
- Connection to existing tools: disassembler view of the function, a hook on the function,
  later a byte signature (point 5)
- **Done (2026-10-19):** results sortable and filterable (name, call count, `%3`). Native
  function monitor (`hermes/features/native_monitor.py`): an Interceptor on a function from
  the trace or by address/module+offset/symbol; logs arguments, result and caller, and
  compares the memory behind pointer arguments before and after the call (for C++ methods:
  which fields of `this` are set). Not yet tried in Baldur's Gate (native, Linux).
- Threads created during the trace are not followed

### 2. "Which addresses does this instruction touch?"

The reverse of the write watch: hook the write instruction that was found and collect all
the addresses it accesses, each with registers and caller.

- Solves the problem of **shared code**: the same instruction lowers the HP of the player
  *and* the enemies. The comparison shows which address belongs to the player and how the
  code tells them apart (register, caller, team field).
- The hook agent can already read registers and build caller signatures.

### 3. Dissecting and comparing structures

Once an address is known, the surrounding object is of interest: max HP, mana, gold and team
usually lie next to it.

- **Structure view:** interpret memory from the start of the object as fields (pointers,
  floats, ints, strings) with a guessed type
- **Instance comparison:** player against enemy, or two points in time side by side, with
  the differences highlighted
- **Reverse search:** who points to this object? Leads to the base pointer and to stable
  pointer chains.

### 4. Entry points through strings, RTTI and symbols

- **String references:** find texts such as "Not enough gold" or "GoldAmount" in memory,
  then the code locations that refer to them. That often yields the right function directly.
- **C++ RTTI:** type names (MSVC `.?AV…@@`, Itanium `typeinfo`) lead to vtables, i.e. to
  class names for objects in memory. An address then becomes "`CPlayerStats`+0x48".
- **Symbols:** native Linux games are often not fully stripped. Exported or debug symbols
  provide function names.

### 5. Update-proof hooks through byte signatures

For native code, build a pattern (AOB) from the bytes around the write instruction, with
wildcards for addresses and offsets, and search for it again automatically after a game
update. For JIT code the method identity already does this; for native games it is missing.

### 6. More engine adapters

The adapter layer (`hermes/engines/base.py`, `registry.py`) is designed for this: a
subpackage with an `EngineAdapter` subclass and an agent part that registers itself with
`registerEngine(...)`, plus an entry in `ENGINES`.

| Engine | Approach | Result |
|---|---|---|
| **Unreal** | Reflection through GNames/GObjects | all classes, properties with offsets, UFunctions; about as powerful as Mono |
| **IL2CPP** (Unity without Mono) | `global-metadata.dat` + the `il2cpp_*` API | like Mono, but compiled ahead of time; shares the .NET model of the JS contract |
| **Godot** | ClassDB / object properties | scene tree and variables |
| **Lua engines** | hook the `lua_*` API | tables and variables of the script state |
| **RPG Maker MV/MZ, Ren'Py** | JavaScript or Python runtime | game variables directly in the script state |

### 7. Smaller additions to the search

- **Group search:** several values at fixed distances, e.g. HP 120 and max HP 150 four bytes
  further on. Finds structures at once.
- **Save game diff:** compare two save games to recognise values and layout, then search
  memory for them.

### Recommendation

**1 (Stalker diff trace)** and **2 (accesses of an instruction)** bring the most
independent of any engine and use much of what exists. Among the adapters, **Unreal** would
be the next big step, **IL2CPP** the most obvious one for Unity games without Mono.

## Activity log (implemented 2026-10-22)

`hermes/core/activity.py`, `hermes/ui/activity_recorder.py`: `activity.jsonl` beside
`hermes.log`, one JSON line per event with a run id. Agents, RPCs, script errors and the end
of a session are recorded by proxies around Frida's session and script in `attach_session`
(all Frida workers go through it); sessions by `sessions.tracked`, the gadget by
`wine_gadget.connect`, process/engine/backend by the main window, clicks by a timer that
connects new controls every 500 ms (not an event filter: for a Python filter PyQt wraps the
receiver of every event, also widgets being destroyed, and crashed in sip doing so).
Snapshots of the game's threads (`/proc/<pid>/task/*`: state, CPU, system call) 1 s after a
native trace starts, at `stop` and when a session is cut; the results of every trace phase
in numbers. From three calls of the same method within 2 s, RPCs are summed up. It can be
switched off in the settings.

- Learned: an agent whose `unload` waits for a thread blocked in Stalker's code shows up as
  `agent_unloaded … seconds=25`.
- Open: the ptrace backends (watch, hooks) only report the start and end of the session, no
  single steps.

## Settings (implemented 2026-10-22)

`hermes/core/settings.py` (definitions, values in this process), table `settings` in
`hermes.db` (`hermes/db/repository/settings.py`), window `hermes/ui/widgets/settings_dialog.py`
under Settings → General …: activity log, the trace's method limit under Wine/Proton
(`wine_trace_limit`, formerly the constant `WINE_TRACE_LIMIT`), from how many methods the trace
asks first, entries in the traces' call sequence, the refresh interval of tracked addresses,
the language. The watch backend stays in the menu and in `QSettings`, since it is chosen
automatically on attach.

## Open points from the Wine/Proton work

- **Method monitor and method hooks under Wine:** `hermes/features/monitor.py` now assigns
  arguments by the platform's convention: System V (Linux x86-64), Win64 (rcx, rdx, r8, r9 or
  xmm0–3 by position, 32 bytes of shadow space) and x86 (everything on the stack). Under
  32 bit, float results are in st0 and 64-bit results in edx:eax; they are read or reported
  as not readable, but not replaced. Still to check on a real game: Win64 with Kathy Rain 2,
  x86 with Shadowrun Returns, especially methods with structs and floats.
- **Large traces under Wine:** with tens of thousands of methods the game froze while
  compiling (Mono's garbage collector hung in `SuspendThread`) or crashed while hooking in
  Frida's code (a write to the header of `OPENGL32.dll` at `0x7BEB0000`). Worked around with
  preselection filters and a limit (the `wine_trace_limit` setting), not fixed. A candidate
  for a bug report to Frida with a minidump. In Shadowrun Returns a scenario filled the call
  sequence (100 000 entries, 223 678 dropped); the setting for the entries in the sequence
  raises that.
- **32-bit Wine games:** the Windows helper also loads the x86 gadget into 32-bit games. The
  agents assumed 64 bit in three places: the batch structure of the Mono agent (Shadowrun
  Returns crashed in the COM interop check), `MEMORY_BASIC_INFORMATION` in `watchUnmaps`, and
  `CONTEXT` in `pauseThreads`. That is fixed and checked in a 32-bit process. The automatic
  backend choice still takes ptrace for 32-bit games. That only concerns the watch and hooks,
  though; Mono features always go through the gadget. Further 64-bit assumptions (monitor
  arguments by the Win64 convention, the hook engine) are possible.
- **Gadget through the Windows helper (implemented):** `hermes/core/inject/inject.c`, built
  with `build.sh` (MinGW or zig) as `hermes-inject-x86_64.exe` or `-x86.exe` into
  `build/inject/` (not in git; `helper_path` runs `build.sh` once if they are missing), loads the gadget
  with `VirtualAllocEx`/`WriteProcessMemory`/`CreateRemoteThread` + `LoadLibraryW`.
  `hermes/core/wine_helper.py` starts it with the game's `wine` (beside or above its
  preloader) and its `WINEPREFIX` and sync variables. It finds the Windows PID by exe name and
  creation time. Beforehand it checks that the prefix's wineserver socket exists in `/tmp`, so
  that no second server starts. `load_gadget` tries the helper first and falls back to the
  ptrace loader for 64 bit.
  - Checked in a prefix of its own with Proton's Wine (64- and 32-bit `ping.exe`, several
    processes of the same name).
  - Open: a real Steam game in the Steam Linux Runtime container (Kathy Rain 2). The helper
    runs outside the container and needs the shared `/tmp` and `/dev/shm`. If that fails,
    `nsenter` into the game's namespaces would be the next step.
- **Gadget loader (ptrace, fallback):** catches a thread with a software breakpoint at a
  frequently called Windows function (message pump, timing). Games that call none of these
  functions need `hot_points` of their own.
- **Test suite:** one run once aborted with a core dump; further runs were clean. Cause not
  investigated, possibly flaky.
