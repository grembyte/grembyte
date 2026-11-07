# Implementation notes

What was learned while building the features: how they work inside, what broke in which game, and why the code does what it does. Open work is in the [roadmap](https://github.com/winterhafen/hermes-hook/blob/main/ROADMAP.md).

## Native call diff trace

The engine-independent native counterpart of the Mono trace. Frida's **Stalker** records which
functions or basic blocks a thread executes. Comparing a baseline with a scenario finds the
functions called newly or more often when, for example, paying or taking damage.

- Model: Cheat Engine's "Code Filter" / "Ultimap"
- Verified with the test game under Proton and Baldur's Gate under Proton; see the findings below.

**Implementation (2026-10-19 onward):** View → "Native trace…", independent of any engine.
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

- **(2026-10-22):** freezing under Wine/Proton fixed, see above. Confirmed with Baldur's
  Gate under Proton Experimental (1 thread, `Baldur.exe`: 3.2 million calls in the baseline,
  nothing lost, ended cleanly). The 32-bit path of the native trace under Wine is untested
  (Shadowrun Returns is Mono, where native tools ask first: they crashed it); the gadget itself runs in
  32-bit games (Mono trace in Shadowrun Returns).

- **(2026-10-19):** results sortable and filterable (name, call count, `%3`). Native
  function monitor (`hermes/features/native_monitor.py`): an Interceptor on a function from
  the trace or by address/module+offset/symbol; logs arguments, result and caller, and
  compares the memory behind pointer arguments before and after the call (for C++ methods:
  which fields of `this` are set). Not yet tried in Baldur's Gate (native, Linux).

## IL2CPP adapter

**IL2CPP adapter (2026-11-03):** `hermes/engines/il2cpp/` (`Il2CppAdapter`, agent in
`agent.js`), tried on V Rising (Unity 2022.3, metadata v31, Proton). Recognised by
`GameAssembly.dll`/`.so`; the runtime counts as started once `il2cpp_get_corlib` answers.
- Catalog: images without `.dll` as assembly names, signatures as Mono writes them, the
  PE time stamp and size of GameAssembly as MVID. 139 assemblies, 43,000 classes in V Rising.
- Naming: a table of all method code starts (230,000 methods, under a second), built on the
  agent's thread after attaching. The function holding an address comes from the unwind data
  (`RtlLookupFunctionEntry` on Windows, `.eh_frame_hdr` on Linux); it belongs to a method only
  if it starts at one. Abstract methods are left out, their pointer is a shared stub.
- Trace: `compileAll` hands out the code pointers; nothing to compile or undo.
- Shared code: about a quarter of the methods share their code with others (folded identical
  functions, shared generics; one empty constructor stands for 8,400 methods in V Rising).
- Probed for the next steps: Unity calls `il2cpp_runtime_invoke` on the main thread about
  17,000 times a second (also on job threads, to be filtered out), so the Mono way of running
  work on the main thread carries over to calls. Arguments: IL2CPP passes a hidden
  `MethodInfo*` last. The call graph would need disassembly instead of IL.
- Method monitor (2026-11-03): signatures, fields and properties as Mono gives them; the
  monitor reads a struct result's hidden pointer before `this` (`returnPointerFirst`). Static
  data: `Il2CppClass` has no getter for it; its `klass` field points at the class itself, the
  fields array follows and `static_fields` seven pointers later (Unity 2018 and later), found
  without running a static constructor. Unity names and getters run on the main thread through
  the `il2cpp_runtime_invoke` hook. Tried in V Rising: `EventSystem "EventSystem(Clone)"`,
  properties of a MonoBehaviour and of an ECS system.
- Method hooks (2026-11-03): write barrier (`il2cpp_gc_wbarrier_set_field`), `il2cpp_string_new`,
  classes by name (`il2cpp_class_from_name`), `ctx.find` through
  `FindObjectsOfType(Type)`, which code stripping removed in V Rising: there
  `FindObjectsOfType(Type, bool)` is used. Struct methods: `this` and `il2cpp_runtime_invoke`
  both take a pointer to the struct's data (tried with `Vector3.get_magnitude`), so struct
  getters work as with Mono; the monitor shows such a `this` as the struct. Tried in V Rising
  with a read-only script on `EventSystem:Update`.
- ECS games: of 538 `OnUpdate` methods in V Rising's gameplay systems, 8 ran as IL2CPP code;
  the rest are Burst-compiled. Their data lies in ECS chunks, not in objects.

## Unreal adapter

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

- **(2026-10-30):** method monitor and object inspector. `prepare` answers
  `frame: true`; `callFrame` reads the `FFrame` (Node 0x10, Object 0x18, Code 0x20, Locals
  0x28, checked in Avowed): through ProcessEvent `Node` is the function and `Locals` its
  parameters; from Blueprint bytecode `Code` points at the argument expressions, of which
  local and instance variables, constants and `self` are decoded. The result is read from
  the frame's `Result` pointer. Avowed: player, companions, encounters (`AActor*`, enums,
  floats) decoded live. Learned: for interface functions `Context` points into the object
  (the interface part, e.g. +0x318); `Stack.Object` is the object, from bytecode the agent
  searches back for the object table's entry.
- **(2026-10-30):** callers in the monitor. From bytecode the frame passed is the
  caller's (`Node` + `Code` = `Class:Function+0x…` in its bytecode); through ProcessEvent the
  agent scans up to 256 KiB of the thread's stack for FFrame's vtable (learned from the
  first frame it sees) and takes the nearest frame whose Node is a UFunction, Object alive
  and Code inside Node's bytecode. Avowed: `MinimapIconInterface:DetermineMinimapIconType`
  ← `WBP_InGameHUDWindow_C:Tick+0x35`. Without a script frame above, the native return
  address stays. The trace's callers could use the same.
- **(2026-10-30):** method hooks for UFunctions (change arguments in `Locals`, the
  result at `Result`).
- **(2026-10-30):** calling UFunctions and scripts. `UObject::ProcessEvent` is the one
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
- **(2026-10-31):** scripts in the game export.
- **(2026-10-31):** Unreal in the **Call method** dialog. FString and FName as text, bools
  as a checkbox, enums by name; a struct whose fields are all numbers (FVector) is typed field
  by field (`1 2 3` or `X=1; Z=3`), its bytes built in Python from the fields `describe_call`
  adds (Mono's structs of numbers get them too). Outputs (`CPF_OutParm` without
  `CPF_ReferenceParm`, Blueprint's rule) need no input and are read from the parameter block
  after the call. An instance UFunction of the catalog or the favorites runs on a live object
  of its class (`call_found`, capability `CALL_FOUND`): the first one `findObject` gives, or one
  named or at an address (checked against the class). Not typed in: TArray, TMap, TSet,
  FText, delegates, structs of structs.
- **(2026-10-31):** GAS attributes window (View → "GAS attributes …"): the player's
  attribute sets, base values set through the game's own setter, held by a script. Setter
  discovery supports the standard AbilitySystemComponent/FGameplayAttribute pattern and
  AngelscriptGAS's `TrySetAttributeBaseValue(FName, float)` pattern (Gothic 1 Remake).

## Hooking managed methods

- **Exceptions through hooked methods (2026-11-07):** Pillars of Eternity crashed at the
  first attack with a monitor on `Health.DoDamage`, with and without recording results.
  The game's AI threw a `NullReferenceException` below `DoDamage`; without a hook Unity
  catches it further up. Frida 17 puts a return address of its own on the stack for
  `Interceptor.attach` with a callbacks object, **also with only `onEnter`**. Mono's
  unwinder found no managed caller behind it, took the exception for unhandled
  (`UnhandledExceptionEventArgs`, the stack trace ends at the hooked method), and Unity
  ended the game. Only a probe (a function or a CModule function as callback) leaves the
  stack alone; its `returnAddress` is wrong, the caller is at `[sp]` on entry.
  `hookCalls` in `monitor.js` therefore probes the start and, for results and the "after"
  phase, the instructions right before each `ret` (found by disassembling the code; at
  least 5 bytes, no branch into them, no tail jump). Calls are matched per thread by the
  stack pointer; calls that ended by an exception are dropped. If a return cannot be
  probed, it falls back to `onLeave` and logs a warning. The trace probes as well.
  Test: `test_monitor_leaves_exceptions_thrown_through_the_method_to_the_game` with
  `Rumors.Parse` in HermesQuest (fails without it: Unity logs the unhandled exception).

## Write hooks under anti-tamper protection

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

## Activity log

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

## Wine and Proton

- **Method monitor and method hooks under Wine:** `hermes/features/monitor.py` now assigns
  arguments by the platform's convention: System V (Linux x86-64), Win64 (rcx, rdx, r8, r9 or
  xmm0–3 by position, 32 bytes of shadow space) and x86 (everything on the stack). Under
  32 bit, float results are in st0 and 64-bit results in edx:eax; they are read or reported
  as not readable, but not replaced.
- **32-bit Wine games:** the Windows helper also loads the x86 gadget into 32-bit games. The
  agents assumed 64 bit in three places: the batch structure of the Mono agent (Shadowrun
  Returns crashed in the COM interop check), `MEMORY_BASIC_INFORMATION` in `watchUnmaps`, and
  `CONTEXT` in `pauseThreads`. That is fixed and checked in a 32-bit process. The hook
  engine only handles x64 code (`installSite`), so the watch and hooks take ptrace for 32-bit
  games; Mono features always go through the gadget.
- **Gadget through the Windows helper:** `hermes/attach/inject/inject.c`, built
  with `build.sh` (MinGW or zig) as `hermes-inject-x86_64.exe` or `-x86.exe` into
  `build/inject/` (not in git; `helper_path` runs `build.sh` once if they are missing), loads the gadget
  with `VirtualAllocEx`/`WriteProcessMemory`/`CreateRemoteThread` + `LoadLibraryW`.
  `hermes/attach/wine_helper.py` starts it with the game's `wine` (beside or above its
  preloader) and its `WINEPREFIX` and sync variables. It finds the Windows PID by exe name and
  creation time. Beforehand it checks that the prefix's wineserver socket exists in `/tmp`, so
  that no second server starts. `load_gadget` tries the helper first and falls back to the
  ptrace loader for 64 bit.
  - The gadget drops messages from Hermes larger than 128 KiB; larger agents are sent in
    parts and put together in the game (`frida_attach.py`, 2026-10-27).
  - Checked in a prefix of its own with Proton's Wine (64- and 32-bit `ping.exe`, several
    processes of the same name).
- **Test-suite core dumps (2026-10-29):** a window released a worker in its
  `finished` signal, which Qt emits before the thread has ended; destroying a running QThread
  aborted the process. Workers are now kept until their thread has ended
  (`hermes/ui/workers.py`).
