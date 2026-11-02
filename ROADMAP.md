# Roadmap and ideas

A collection of extensions that would help find addresses and hook entry points, and of
open points from the Wine/Proton and Unity work.
As of November 2026.

## Starting point

Available independent of any engine:

- Value scan: exact, range, unknown initial value, changed/unchanged, greater/less,
  differences, fuzzy floats
- Pointer scan for stable pointer chains
- Write watch: which instruction writes an address, optionally also which reads it
- Hooks on write instructions, with caller learning
- Memory view, freezing
- Native call diff trace and native function monitor

For Unity games with Mono there are also the catalog, the trace (with preselection
filters), the method monitor, method hooks and game scripts, through the engine adapter layer
(`hermes/engines/`). For Unreal Engine games there are the catalog, the trace, the method
monitor, method hooks, calling UFunctions (dialog and scripts), game scripts and the GAS
attributes window.

Methods and functions found in the catalog, the trace or the native trace can be marked
with ★ (first table column). Favorites are saved per program at once, so they survive a
frozen or crashed game; View → "Favorites…" lists them with a note and hands them to the
method or native function monitor.

View → "Assemblies…" lists the assemblies a Mono game has loaded; the ticked ones are
saved per program and limit the catalog and the Mono trace to them. Without a selection
both use all game assemblies, as before.

## Proposals and active work, independent of any engine

Upcoming proposals are sorted by benefit relative to effort. Implemented work remains here
with its follow-ups.

### 1. Native call diff trace (implemented; follow-ups open)

The engine-independent native counterpart of the Mono trace. Frida's **Stalker** records which
functions or basic blocks a thread executes. Comparing a baseline with a scenario finds the
functions called newly or more often when, for example, paying or taking damage.

- Works without metadata, so for C++ games, own engines, Unreal, Godot, Source
- Reusable: trace dialog, `analyze`, the "new/more often" verdicts
- Model: Cheat Engine's "Code Filter" / "Ultimap"
- Verified with the test game under Proton and Baldur's Gate under Proton; see the findings below.

**Status (implemented, 2026-10-19 onward):** View → "Native trace…", independent of any engine.
`hermes/features/native_trace.py` (`NativeTraceWorker`) and `native_trace.js` (agent),
`hermes/ui/widgets/native_trace.py` (window). Session logic shared with the Mono trace in
`AgentWorker` (`hermes/features/trace.py`); `analyze` and the verdicts are the same.

- Stalker follows the chosen threads with a CModule `transform` (since 2026-10-22, before
  that `events: {call: true}` and `onCallSummary`): before every `call` of the followed code
  sits a callout in C that works out the target from the registers (direct, register, memory
  operand including `rip`-relative) and counts it in a hash table, in the game's thread. The
  order of the first calls is exact and knows the call site. x86/x64 only; a `call` through
  `fs:`/`gs:` is not counted. Modules not chosen run natively through `Stalker.exclude` (only
  calls *into* them count). Preselection: the modules in the game's folder, the threads
  running right now (CPU time or context switches in `/proc`). Under Wine (2026-10-29) the
  game's folder comes from `GetModuleHandleW(NULL)`: Frida's `Process.mainModule` is just
  the first module listed there (`netapi32.dll` in Dark Souls III), which preselected the
  system DLLs and not the game. The agent measures the threads itself with
  `GetThreadTimes`, since /proc does not know Windows thread ids; before, every thread was
  preselected. Dark Souls III: 5 game modules, 20 of 86 threads.
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
- Learned (2026-10-29): Baldur's Gate under Proton froze on the second "Stop following"
  (two agents, so two Stalkers in the process): the followed main thread looped in Frida's
  own code, calling `NtSetContextThread` with a broken context (a write to `0x2d9`), and
  Proton's log grew by about 40 MB/s with the access violations. Under Windows the threads
  now stop following themselves, as they start: `stop` sets a flag, and the C callout
  calls `leave()` (`Stalker.unfollow()` of its own thread) at the thread's next call;
  threads that do not run meanwhile stay followed and are reported (`staying`), never
  taken over from outside. One agent serves every trace; a fresh one is loaded only when
  a module to follow was excluded before. Checked with a Windows test game under Proton
  (stop, second trace, dispose); the freeze itself did not occur there with the old code
  either, so the cause is not proven. Setting the trace's flag needs no exclusions to
  change: Stalker's exclusions must not change while threads are followed (a module
  observer doing so crashed Baldur's Gate and Dark Souls III).

Next steps:

- Try it in a real native Linux game (load, playability, quality of the hits)
- **Done (2026-10-22):** freezing under Wine/Proton fixed, see above. Confirmed with Baldur's
  Gate under Proton Experimental (1 thread, `Baldur.exe`: 3.2 million calls in the baseline,
  nothing lost, ended cleanly). The 32-bit path of the native trace under Wine is untested
  (Shadowrun Returns is Mono, where native tools ask first: they crashed it); the gadget itself runs in
  32-bit games (Mono trace in Shadowrun Returns).
- Callers: the sequence shows the call site of the first call per thread. A list of all
  callers per function (a count per call site and target) is still missing
- Connection to existing tools: selecting a function opens it in the memory viewer or hands it
  to the native function monitor. Still missing: a disassembler view and a hook on the function;
  update-proof byte signatures are covered in point 5.
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
  floats, ints, strings) with a guessed type. For Mono objects the object inspector already
  shows the fields with their types and offsets; native memory has nothing like it yet.
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
  provide function names. The native trace and monitor already use them (`DebugSymbol`, see
  point 1); the scanner, watch and hooks still show `module+0x…`.

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
| **Unreal** | Reflection through GNames/GObjects | **catalog, trace, method monitor/hooks, UFunction calls, scripts and GAS attributes done (2026-10-30/31)**; Blueprint call graph open, see below |
| **IL2CPP** (Unity without Mono) | `global-metadata.dat` + the `il2cpp_*` API | like Mono, but compiled ahead of time; shares the .NET model of the JS contract |
| **Godot** | ClassDB / object properties | scene tree and variables |
| **Lua engines** | hook the `lua_*` API | tables and variables of the script state |
| **RPG Maker MV/MZ, Ren'Py** | JavaScript or Python runtime | game variables directly in the script state |

**Unreal adapter (2026-10-30):** `hermes/engines/unreal/` (`UnrealAdapter`, agent in
`agent.js`). Recognised by the executable's name (`<Project>-Win64-Shipping.exe`, also Linux
and modular builds). The agent finds `FNamePool` and `GUObjectArray` in the module's
writable data by their shape (pool: `CurrentBlock`, `Blocks[]`, first entries `None` and
`ByteProperty`; objects: `MaxElements`/`NumElements` matching `MaxChunks`/`NumChunks`), then
measures the offsets of UStruct, FField/FProperty and UFunction on objects every game has
(CoreUObject's `Class`, `Object`, `Vector`, `Transform`, `Object:ExecuteUbergraph`).

- Verified in **Avowed** (UE5, Proton Experimental): found in 0.8 s, 260 000 objects read in
  0.4 s. Catalog: 168 package groups, `/Script/Alabama` with 1573 classes and 4831 UFunctions.
  Trace over `/Script/Alabama` + `/Game/Blueprints`: 4935 hooks in 0.65 s, baseline and
  scenario recorded, Blueprint functions restored on stop, game stayed up.
- Learned: Avowed's `FUObjectItem` is 0x14 bytes (packed), not 0x18; FField's owner is a
  tagged 8-byte pointer (Next at 0x18, name at 0x20), older UE5 has 16 bytes there. Hence
  item size and offsets are measured, not assumed.
- Learned: on Linux most of `.bss` is an anonymous mapping right after the module, which
  Frida's `Module.enumerateRanges` leaves out; the agent adds the ranges that follow the
  module's own.
- Learned: the trace's selection (`[[assembly, token], ...]`) passed 128 KiB for 5000
  methods and cut the gadget's connection under Wine; it now goes to the agent in parts
  (`choose`) before `start`. That affected large Mono traces under Wine as well.
- Catalog tokens are a hash of package, class and function name (object indexes change
  between runs); the catalog cache's build id is a hash of the declared functions.
- Trace: native UFunctions are hooked at their exec thunk (`Func`); every Blueprint function
  shares the interpreter (`ProcessInternal`) as `Func`, so each gets a stub of its own
  (16 nops, `jmp [rip]`, allocated with `allocateCode`, never freed) as `Func`, which
  `releaseCode` undoes after the trace. Calls the trace cannot see: C++ calling C++, and
  Blueprint code calling a Blueprint function directly (`CallFunction` runs the script
  without `Func`).

Next steps:

- **Done (2026-10-30):** method monitor and object inspector. `prepare` answers
  `frame: true`; `callFrame` reads the `FFrame` (Node 0x10, Object 0x18, Code 0x20, Locals
  0x28, checked in Avowed): through ProcessEvent `Node` is the function and `Locals` its
  parameters; from Blueprint bytecode `Code` points at the argument expressions, of which
  local and instance variables, constants and `self` are decoded. The result is read from
  the frame's `Result` pointer. Avowed: player, companions, encounters (`AActor*`, enums,
  floats) decoded live. Learned: for interface functions `Context` points into the object
  (the interface part, e.g. +0x318); `Stack.Object` is the object, from bytecode the agent
  searches back for the object table's entry.
- **Done (2026-10-30):** callers in the monitor. From bytecode the frame passed is the
  caller's (`Node` + `Code` = `Class:Function+0x…` in its bytecode); through ProcessEvent the
  agent scans up to 256 KiB of the thread's stack for FFrame's vtable (learned from the
  first frame it sees) and takes the nearest frame whose Node is a UFunction, Object alive
  and Code inside Node's bytecode. Avowed: `MinimapIconInterface:DetermineMinimapIconType`
  ← `WBP_InGameHUDWindow_C:Tick+0x35`. Without a script frame above, the native return
  address stays. The trace's callers could use the same.
- Monitor gaps: `EX_LocalOutVariable` (needs `FFrame::OutParms`) and nested expressions show
  as `?`; FText, TMap, TSet and delegates show their type only
- **Done (2026-10-30):** method hooks for UFunctions (change arguments in `Locals`, the
  result at `Result`).
- **Done (2026-10-30):** calling UFunctions and scripts. `UObject::ProcessEvent` is the one
  virtual function of `Default__Object`'s vtable whose code calls `[reg + Func]` (Avowed:
  slot 77); calls run on the game thread (the main thread: Avowed 348), queued until it
  next enters ProcessEvent, where a temporary interceptor runs them. The parameter block
  is laid out from the function's properties; FStrings point at buffers of the agent (the
  game copies them), FNames are looked up among the objects' names, static functions run
  on the class default object. Avowed: `BP_CheatManager_C` (an `AlabamaCheatManager`) is
  the player controller's `CheatManager` (+0x400); 147 of its 152 functions have code in
  the shipping build (`GiveHealth`, `AddCurrency`, `ToggleUnlimitedStamina` checked live).
  Scripts (hermes/features/scripts.py) run such calls by time. Hooks on functions a call
  reaches do not fire for it (Frida's interceptor ignores the thread inside a callback).
- **Done (2026-10-31):** scripts in the game export.
- **Done (2026-10-31):** Unreal in the **Call method** dialog. FString and FName as text, bools
  as a checkbox, enums by name; a struct whose fields are all numbers (FVector) is typed field
  by field (`1 2 3` or `X=1; Z=3`), its bytes built in Python from the fields `describe_call`
  adds (Mono's structs of numbers get them too). Outputs (`CPF_OutParm` without
  `CPF_ReferenceParm`, Blueprint's rule) need no input and are read from the parameter block
  after the call. An instance UFunction of the catalog or the favorites runs on a live object
  of its class (`call_found`, capability `CALL_FOUND`): the first one `findObject` gives, or one
  named or at an address (checked against the class). Not typed in: TArray, TMap, TSet,
  FText, delegates, structs of structs.
- **Done (2026-10-31):** GAS attributes window (View → "GAS attributes …"): the player's
  attribute sets, base values set through the game's own setter, held by a script. Setter
  discovery supports the standard AbilitySystemComponent/FGameplayAttribute pattern and
  AngelscriptGAS's `TrySetAttributeBaseValue(FName, float)` pattern (Gothic 1 Remake).
- Possible extension: a trusted Unreal code-plugin interface for games with non-standard GAS
  setters. The current agent has no external plugin loader; a setter with a genuinely different
  signature or call sequence needs code, not just another method name in configuration.
- Call graph from Blueprint bytecode (`UStruct::Script`)
- UE4 before 4.23 (`TNameEntryArray` instead of `FNamePool`) and 32-bit games are not
  supported

### 7. Smaller additions to the search

- **Group search:** several values at fixed distances, e.g. HP 120 and max HP 150 four bytes
  further on. Finds structures at once.
- **Save game diff:** compare two save games to recognise values and layout, then search
  memory for them.

### 8. Register hooks at any code address

Today a hook needs a write instruction: it relocates the write and hands old and new value
to the transform. Some values are better changed elsewhere, before they are written:

- **Write sites that cannot be hooked:** in Dark Souls III the souls are stored by a tiny
  setter (`mov [rcx+0x74], eax; ret` at `DarkSoulsIII.exe+0x465f13`), too short for the 5-byte
  jump. The short jump needs int3 padding nearby, which Arxan overwrites at run time, and Arxan
  restores the setter's code anyway (see "Write hooks under anti-tamper protection" below).
  The short jump exists as the setting `hook_short_jump` (experimental), for games without
  such protection.
- **The value is computed before it is stored:** the native function monitor showed at the
  return address `+0x5a5436` in the caller: `rax` = new souls, `rbx` = souls gained,
  `rsi` = old souls. A hook there could set `rax = rsi + rbx * factor` before the setter runs.

Proposal: a hook type on any instruction (not only writes) whose script gets the registers
(`ctx.regs.rax`, …, also flags and xmm if needed) and may change them before the instruction
runs; optionally memory through `ctx.read`/`ctx.write`.

- Reuses the trampoline of the write hooks (relocation, saved registers, short jump), the
  caller learning, the debug window and saving as module + offset
- Created from the native function monitor (call site or return address of a logged call, with
  the registers seen there) and from the disassembler view
- Templates in the same system as value templates (`kind: "registers"`), e.g. "scale register
  difference": `rax = rsi + (rax - rsi) * factor`
- Open: write back the registers from the block of the trampoline (today they are only read);
  instructions that cannot be relocated (`rip`-relative jumps are handled by Frida's relocator)

### Recommendation

**1 (Stalker diff trace)** and **2 (accesses of an instruction)** bring the most
independent of any engine and use much of what exists; 2 is the next larger step. Among the
adapters, **Unreal** has catalog, trace, method monitor, hooks and calls now; the call graph
from Blueprint bytecode is left there. **IL2CPP** is the most obvious one for Unity games
without Mono.

## Write hooks under anti-tamper protection (2026-10-27)

Found with Dark Souls III (Arxan/GuardIT):

- **Patched sites are repaired:** Arxan restores the original code of checked functions, the
  hook then stops without an error. The agent compares every site with the bytes it wrote once
  a second and reports `site-state` (hook status "Removed by game"); with the setting
  `hook_repatch` it patches a site whose original code is back again. Arxan also decrypts and
  encrypts code at run time: a site may read as "changed" for a while, and int3 padding used as
  a short jump cave can be overwritten (the cave is then looked for anew).
- **Debug registers are detected:** with debug registers set on all threads at an address that
  is never executed, the game crashed about 11 s later, jumping to the same unmapped address
  (`0x7ff7ab63b120`) as in the two crashes before (a breakpoint hook and a write watch). So
  hook installation as hardware breakpoint (`HookSpec.breakpoint`, leaves the code untouched)
  is no option there, and write watches must stay short. When a game ends within 30 s after
  Hermes used debug registers in it, the main window says so.
- **Performance:** a hooked write cost ~18 µs in a test process, mostly JavaScript work: all
  16 registers read into an object (~7 µs) and the address computed with BigInt (~3.5 µs).
  Now registers are read on demand, the address with NativePointer arithmetic from a plan made
  once per site, and caller signatures took the stack slots of the last scan first
  (`siteSignature`, since replaced by the C scan below): ~8 µs, caller check +1.5 µs instead of +3.5 µs. The rest is the transform
  and its context (~3 µs), the two calls into JS with the saved state (~1.5 µs) and reading
  the old value.
- **Frame drops came from the agent's lock, not the average cost:** measured in Dark Souls III
  (setting "Hooks: measure their cost"): 100–350 writes/s per site, most of them skipped
  (other callers), but again and again a hooked thread waited ~28–37 ms (twice a Windows
  scheduler slice), once up to 400 ms: a game thread descheduled while it held the agent's
  JavaScript lock stalls every other hooked thread.
- **C fast path (2026-10-27):** the trampoline calls a CModule (`FAST_SOURCE` in
  `hermes/hooks/engine.js`). It checks address, switch and learned callers and runs multiply, freeze and
  clamp itself (`native_spec` in `transforms.py` recognises them by their code, also copies
  such as "Clamp (Import)"). Writes pending between before and after are kept per thread in a
  table per site. A hooked write now costs ~0.1 µs instead of ~8–18 µs in the test process; a
  foreign caller is rejected in C after its first write (3003 of 3004 writes without JS).
- Learned (DS3, first try of the fast path): caching one stack layout per site (the slots of
  the last scan) did not work there: the scan finds the return address at different distances
  from rsp (stale return addresses in the locals of a large frame), the layout changed on nearly
  every write and the TP hook still ran in JavaScript. Now C runs its own copy of the scan
  (`native_frames`): slot by slot, executable values only, with a table of what the JavaScript
  scan decided about each (value, function) pair (`fast_teach_verdict`, `fast_teach_skip`).
  `GetCurrentThreadStackLimits` keeps it on the thread's stack; without it (Linux) callers are
  checked in JavaScript. Caller check ~0.1 µs; in the test a caller whose return address lay
  deeper (through `qsort`) needed JavaScript once in 56 032 writes. Dark Souls III: steady frame
  time, "0 in JS" for TP and FP. A fixed-address hook that learns callers still runs in
  JavaScript.
- Learned: the rare writes that still need JavaScript (new stack values, e.g. at a bonfire)
  took 60–120 ms each in Dark Souls III and dropped a frame: `Process.findRangeByAddress` costs
  11 ms there (it walks all ranges of the process) and the scan asked it for every stack value.
  Now both scans use one sorted snapshot of executable memory (`isExecutable`, refreshed every
  5 s, ~25 ms).

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

## Settings

`hermes/core/settings.py` (definitions, values in this process), table `settings` in
`hermes.db` (`hermes/db/repository/settings.py`), window `hermes/ui/widgets/settings_dialog.py`
under Settings → General …: activity log, the trace's method limit under Wine/Proton
(`wine_trace_limit`, formerly the constant `WINE_TRACE_LIMIT`), from how many methods the trace
asks first (`large_trace`), entries in the traces' call sequence (`trace_sequence`), the refresh
interval of tracked addresses, automatic Wine/Proton gadget downloads (off by default), the
language, and since 2026-10-26/27 the hook options `hook_short_jump`, `hook_repatch` and
`hook_profiling` (see above). The watch backend stays in the menu and in `QSettings`, since it is
chosen automatically on attach.

## Binary distribution (PyInstaller) and licenses

Open, nothing is built yet. Hermes Hook is MIT-licensed; a frozen bundle contains
third-party code whose licenses add duties that a source release through pip does not have:

- **Qt (LGPL-3.0):** users must be able to replace the bundled Qt libraries with their own
  build. Build in one-folder mode (`--onedir`), not `--onefile`, so the Qt shared libraries
  stay separate, replaceable files. Ship the LGPL text and point to the source of the exact Qt
  version (`download.qt.io`), or offer it in writing.
- **PySide6 (LGPL-3.0-only, GPL-2.0-only or GPL-3.0-only):** the Qt for Python bindings offer
  an LGPL route as well as GPL and commercial terms. A frozen bundle must meet the chosen
  license's obligations; the project's own MIT license remains unchanged.
- **License files in the bundle:** PyInstaller does not collect `LICENSE`, `LICENSES/` and
  `THIRD_PARTY_NOTICES`; add them as data files, next to the executable.
- **Inventory the bundle:** PyInstaller pulls in more than `requirements.txt` lists — the Python
  runtime (PSF-2.0), OpenSSL (Apache-2.0), libffi, zlib, Qt plugins, ICU and others. List
  what the finished bundle contains and extend `THIRD_PARTY_NOTICES` and `LICENSES/`.
- **Smaller inventory:** exclude Qt modules and plugins Hermes does not use (e.g. QtWebEngine,
  QtMultimedia, QML). That is not required by the licenses, but each excluded module is one
  license fewer to check and makes the bundle smaller.
- **Frida gadgets:** they are downloaded at runtime and not part of the bundle. If a bundle
  ever ships them, their wxWindows licence text must be in it as well (`LICENSES/Frida.txt`).

## Open points from the Wine/Proton work

- **Gadget downloads and version check (implemented):** the matching Windows gadget
  is cached per Frida version and architecture. Automatic downloads are opt-in and off by
  default; selecting a Wine/Proton process with a missing gadget asks once and saves approval.
  On Linux, File → "Check Frida Gadget Version …" checks the installed Frida version locally
  and offers a one-time download; it does not check GitHub for a newer Frida release. The
  status bar marks a missing matching gadget while Frida is using the Wine gadget.
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
  though; Mono features always go through the gadget. The reason is the hook engine, which
  only handles x64 code (`installSite`); the method monitor follows the x86 convention there
  (see above).
- **Gadget through the Windows helper (implemented):** `hermes/core/inject/inject.c`, built
  with `build.sh` (MinGW or zig) as `hermes-inject-x86_64.exe` or `-x86.exe` into
  `build/inject/` (not in git; `helper_path` runs `build.sh` once if they are missing), loads the gadget
  with `VirtualAllocEx`/`WriteProcessMemory`/`CreateRemoteThread` + `LoadLibraryW`.
  `hermes/core/wine_helper.py` starts it with the game's `wine` (beside or above its
  preloader) and its `WINEPREFIX` and sync variables. It finds the Windows PID by exe name and
  creation time. Beforehand it checks that the prefix's wineserver socket exists in `/tmp`, so
  that no second server starts. `load_gadget` tries the helper first and falls back to the
  ptrace loader for 64 bit.
  - The gadget drops messages from Hermes larger than 128 KiB; larger agents are sent in
    parts and put together in the game (`frida_attach.py`, 2026-10-27).
  - Checked in a prefix of its own with Proton's Wine (64- and 32-bit `ping.exe`, several
    processes of the same name).
  - Open: a real Steam game in the Steam Linux Runtime container (Kathy Rain 2). The helper
    runs outside the container and needs the shared `/tmp` and `/dev/shm`. If that fails,
    `nsenter` into the game's namespaces would be the next step.
- **Gadget loader (ptrace, fallback):** catches a thread with a software breakpoint at a
  frequently called Windows function (message pump, timing). Games that call none of these
  functions need `hot_points` of their own; `load_gadget` takes them, but neither the UI nor
  the settings can set them yet.
- **Resolved (2026-10-29, test-suite core dumps):** a window released a worker in its
  `finished` signal, which Qt emits before the thread has ended; destroying a running QThread
  aborted the process. Workers are now kept until their thread has ended
  (`hermes/ui/workers.py`).
