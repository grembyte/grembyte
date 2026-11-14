# Implementation notes

What was learned while building the features: how they work inside, what broke in which game, and why the code does what it does. Open work is in the [roadmap](https://github.com/grembyte/grembyte/blob/main/ROADMAP.md).

## Attaching and Wine/Proton

- **Method monitor and method hooks under Wine:** `grembyte/features/monitor.py` now assigns
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
- **Gadget through the Windows helper:** `grembyte/attach/inject/inject.c`, built
  with `build.sh` (MinGW or zig) as `grembyte-inject-x86_64.exe` or `-x86.exe` into
  `build/inject/` (not in git; `helper_path` runs `build.sh` once if they are missing), loads the gadget
  with `VirtualAllocEx`/`WriteProcessMemory`/`CreateRemoteThread` + `LoadLibraryW`.
  `grembyte/attach/wine_helper.py` starts it with the game's `wine` (beside or above its
  preloader) and its `WINEPREFIX` and sync variables. It finds the Windows PID by exe name and
  creation time. Beforehand it checks that the prefix's wineserver socket exists in `/tmp`, so
  that no second server starts. `load_gadget` tries the helper first and falls back to the
  ptrace loader for 64 bit.
  - The gadget drops messages from Grembyte larger than 128 KiB; larger agents are sent in
    parts and put together in the game (`frida_attach.py`, 2026-10-27).
  - Checked in a prefix of its own with Proton's Wine (64- and 32-bit `ping.exe`, several
    processes of the same name).

- **Two Grembyte processes on one game (2026-11-13):** Vampire Survivors crashed three
  times (a jump to `0x100000000`, `mono_runtime_invoke` back to its original bytes) while a
  second Grembyte process browsed objects. Each host process injects its own Frida agent,
  and the agents do not know each other's hooks. `onMainThread` attaches to
  `mono_runtime_invoke` and detaches when its work is done. When agent A hooks on top of B
  and B detaches first, B writes back the original bytes and frees its trampoline, while A's
  trampoline still jumps there. A minimal C program with four threads calling a hooked
  function dies the same way (SIGSEGV in the first round). Sessions of one host process
  share one agent and its Interceptor and survive the same sequence, so one Grembyte with
  several features is safe; two Grembyte processes (or Grembyte and another Frida tool) on
  the same game are not. `attach_alone` (`grembyte/attach/frida_attach.py`) therefore
  refuses to attach next to another host's runtime (`error.attach.foreign_frida`). Every
  host's agent is its own memfd, so `/proc/<pid>/maps` shows one `frida-agent` inode per
  host. An inode Grembyte did not note after its own attach counts as foreign if
  attaching adds a new agent. If no new one appears, the agent belongs to Grembyte's own
  process (e.g. a session opened with `frida.attach` directly). Native Linux games only:
  under Wine all connections share Grembyte's gadget, and on Windows the check is not
  implemented.

## Write hooks

The write watch reports the instruction *after* the write (hardware watchpoints fire afterwards). From it Grembyte determines the actual write instruction and replaces it with a jump into a code cave:

```text
before(regs)       save the old value if the instruction really writes the target address
<write instruction, relocated>
after(regs)        call the manipulation and store the result
<remaining relocated bytes>
jmp back
```

Since the hook only works with the old and the new value, the kind of instruction (`add [mem], reg`, `mov [mem], reg`, `sub` …) does not matter. Several hooks can share a write instruction; they run in the order they were created.

`before` and `after` are C functions (a Frida CModule): they check the target address and, on Windows and under Wine, the learned callers, and run the built-in manipulations themselves, so a hooked write costs a fraction of a microsecond. On Linux the callers are checked in JavaScript. Only scripts of your own, debug mode, caller learning and pointer chains run in JavaScript. The agent's JavaScript runs one thread at a time: a game thread that has to wait there can drop frames.

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
  Grembyte used debug registers in it, the main window says so.
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
  `grembyte/hooks/engine.js`). It checks address, switch and learned callers and runs multiply, freeze and
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
  Learned: a return probe runs *before* the instructions it covers. In `Vec.Length` of the
  IL2CPP test target (`cvtsi2sd xmm0, eax; pop rbp; ret`) the result only reached `xmm0`
  after the probe, the "after" hook saw a stale result and its own was overwritten. The
  covered instructions may therefore write neither a result register (rax, rdx, xmm0,
  xmm1 and their parts; `vzeroupper` is allowed) nor memory (`leavesResult`); otherwise
  that method falls back to `onLeave`.
  Test: `test_monitor_leaves_exceptions_thrown_through_the_method_to_the_game` with
  `Rumors.Parse` in HermesQuest (fails without it: Unity logs the unhandled exception).

- **Main thread of a game out of focus (2026-11-12):** Might & Magic X – Legacy (Unity 4.2,
  32-bit, Proton) stops running frames when its window loses focus. Its main thread then
  sits in a single `GetMessageA` call and calls nothing else, not even `mono_runtime_invoke`
  (about 40,000 calls a second while it runs), so every main-thread request timed out after
  3 s. `wakeMainThread` (`grembyte/agent/platform.js`) hooks `GetMessageA`/`GetMessageW` while a
  request waits and posts the main thread a `WM_NULL` (`PostThreadMessageW`); the work runs in
  the next `GetMessage` call, between two passes of the message loop. Mono and IL2CPP use it
  beside their `runtime_invoke` hook. Learned: Frida waits for every thread to leave a hooked
  function before a script unloads (it replaces the return address also for `onEnter` only,
  see above), and the sleeping main thread stays inside the hooked `GetMessage`: unloading
  hung until the game got a message. Detaching therefore posts a second `WM_NULL`.

## Unity Mono adapter

- **Object browser:** Unity's Mono has no way to walk its heap: `mono_gc_walk_heap` belongs to Mono's SGen collector and does nothing in the Boehm collector Unity uses. The browser uses Unity's own liveness calculation instead (`mono_unity_liveness_*`, which `Resources.UnloadUnusedAssets` uses), from the static fields and from all Unity objects, with the world stopped. Both generations of its API work: `calculation_begin`/`end` (Unity 4 to 2020) and `allocate_struct` (Unity 2021 and later, including Unity 6). The calculation runs on the game's main thread.
- **Object browser in 32-bit Mono games (2026-11-12):** the callbacks of Unity's liveness
  calculation are written as machine code (no JavaScript may run while the world is
  stopped); there was only an x64 version, and on 32 bit the writer failed inside
  `Memory.patchCode`, which swallowed the error (`Cannot read property 'collect' of
  undefined`). `writeLivenessCode32` is the cdecl version; the buffer keeps its layout, with
  pointers in the low half of 64-bit slots. Checked in Might & Magic X – Legacy.
- **Object browser in Unity 6 (2026-11-12):** Unity 6's Mono (Vampire Survivors,
  6000.0.62) no longer exports `mono_unity_liveness_stop/start_gc_world`; the general
  `mono_unity_stop/start_gc_world` take Mono's locks and then call the same
  `GC_stop/start_world_external`, so the agent falls back to them. Its GC handles are
  pointers (`mono_gchandle_new_v2`); the old `mono_gchandle_new` returns them cut to
  32 bits, and `mono_gchandle_free` of such a handle is an access violation. The agent uses
  the `_v2` functions where they exist. Vampire Survivors keeps about 2.45 million objects
  alive (19.7 MB of the 128 MB pool), most of them Newtonsoft JSON, more than the buffer
  keeps. Running the pass over the Unity objects first does not help: the Unity objects
  reach the same JSON, and the result had fewer Unity objects and a third of the classes.
  Either order is safe, though: both passes add to the same list of marked objects
  (`from_statics` only resets the work list), and `finalize` clears every mark.
- **Static fields as chain bases (pointer scan):** Mono gives each class a block of its own for its static fields, which stays put while the game runs. Grembyte looks the blocks up through the Mono runtime at the start of a scan, and again after each restart of the game for the classes the saved chains need. A class whose static constructor has not run yet has no block; it is looked for again every 30 seconds.
- **Cut UTF-16 strings (2026-11-12):** Frida's `readUtf16String` throws `invalid string`
  for a surrogate without its partner, for example an emoji cut at a length limit. In
  Vampire Survivors this broke the object browser's instance list. The agents read game
  strings through `readUtf16Text` (`grembyte/engines/engine_prelude.js`), which turns such
  surrogates into U+FFFD.

## IL2CPP adapter

**IL2CPP adapter (2026-11-03):** `grembyte/engines/il2cpp/` (`Il2CppAdapter`, agent in
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

**Unreal adapter (2026-10-30):** `grembyte/engines/unreal/` (`UnrealAdapter`, agent in
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
- Learned (2026-11-14): **Fatekeeper** (a newer UE5, Proton) changed three layouts the
  agent had assumed. `FChunkedFixedUObjectArray` has no `PreAllocatedObjects` and the counts
  come first: Objects, NumElements, MaxElements, NumChunks, MaxChunks. `FUObjectItem`
  (0x18) starts with the 64-bit `FlagsAndRefCount`, so the object pointer is at +8, not +0;
  SerialNumber stays at 0x10. `FFieldClass` has a vtable, so its name is at +8. The agent
  now tries both object table layouts and both places of the object pointer, and measures
  the name offset of `FFieldClass` on `Vector.X`. Without that, Grembyte could not find
  `GUObjectArray`, and then the inspector and the GAS window failed with "access violation
  accessing 0x6aa0" (the vtable read as a name index). The stand-in game in
  `tests/test_unreal.py` builds this layout with `FATEKEEPER_LAYOUT`.
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
  Scripts (grembyte/features/scripts.py) run such calls by time. Hooks on functions a call
  reaches do not fire for it (Frida's interceptor ignores the thread inside a callback).
  Learned (2026-11-08, Queen's Domain, UE4): the search read 0x800 bytes from each slot and
  found `call [reg + 0xd8]` in the functions *after* slots 16 and 65 ("2 candidates"), while
  ProcessEvent (slot 68) calls a non-inlined `UFunction::Invoke`. On Windows each function is
  now read up to its end from the executable's `.pdata` (functions it does not list are
  leaves and call nothing); unbounded as before only where that finds none.
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

## Native call diff trace

The engine-independent native counterpart of the Mono trace. Frida's **Stalker** records which
functions or basic blocks a thread executes. Comparing a baseline with a scenario finds the
functions called newly or more often when, for example, paying or taking damage.

- Model: Cheat Engine's "Code Filter" / "Ultimap"
- Verified with the test game under Proton and Baldur's Gate under Proton; see the findings below.

**Implementation (2026-10-19 onward):** View → "Native trace …", independent of any engine.
`grembyte/features/native_trace.py` (`NativeTraceWorker`) and `native_trace.js` (agent),
`grembyte/ui/widgets/native_trace.py` (window). Session logic shared with the Mono trace in
`AgentWorker` (`grembyte/features/trace.py`); `analyze` and the verdicts are the same.

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
  run with Proton's `files/bin/wine` in a `WINEPREFIX` of its own; Grembyte connects through
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
  function monitor (`grembyte/features/native_monitor.py`): an Interceptor on a function from
  the trace or by address/module+offset/symbol; logs arguments, result and caller, and
  compares the memory behind pointer arguments before and after the call (for C++ methods:
  which fields of `this` are set). Not yet tried in Baldur's Gate (native, Linux).

## Scripts

- **Native games:** without an engine adapter scripts run when a function the game calls every frame returns on its main thread (`grembyte/features/script_native.js`). The agent hooks the ones the game exports, in this order: `SDL_PollEvent`, `SDL_PumpEvents`, `SDL_GL_SwapWindow`, `glfwPollEvents`, `glfwSwapBuffers`, `PeekMessageW`, `PeekMessageA`, `SwapBuffers`, `wglSwapBuffers`, `glXSwapBuffers`, `eglSwapBuffers`, `vkQueuePresentKHR`, and for GTK and other GLib programs `g_main_context_iteration`. The first of them that runs on the main thread within half a second is kept (if none does, the first that runs on another thread). Games that export none of them, for example with SDL linked in statically, are reported.
- **Hotkeys are read by Grembyte, not in the game** (`core/hotkeys.py`, `platform/keyboard.py`). On Linux `XQueryKeymap` is polled about 30 times a second and the window with the input focus is matched to the game by `_NET_WM_PID` (or that of the first parent that has one). This needs no code in the game and works for native and Proton games alike: XWayland only knows the keys while one of its windows has the focus, which for a game is exactly when they count. On a plain X11 session the keymap is global, so the focus check is what keeps hotkeys to the game. Keys cannot be taken away from the game this way. Polling the keyboard inside the game (`GetAsyncKeyState` in the Frida agent) would only have covered Wine games. Verified with `grembyte-cli` and the window closed in Pillars of Eternity (native Linux) and Avowed (Unreal, Proton).

## Pointer scan

- **Thread stacks** (off by default as chain passages): on Linux, Wine included, a thread's stack is the memory around its stack pointer (`thread_stack_pointers`), on Windows the memory above its guard page.
- **Saved candidates are packed** (`db/models/pointer_scan.py`): as a JSON list of `{module, base_offset, offsets}` objects a scan of up to a million chains took about 50 MB in the database (four scans: 198 MB). The chains are now `[module index, base offset, [offsets]]` with a module table, zlib-compressed in `packed`; 1 million random chains take 11 MB, real ones less. The old `chains` column stays (required in older databases) and is emptied; `init_db` packs older scans once and runs VACUUM. `chain_count` lets the check for obsolete scans (`AddressRepository.obsolete_pointer_scans`) do without unpacking. Exports keep the JSON objects.

## Activity log

`grembyte/core/activity.py`, `grembyte/ui/activity_recorder.py`: `activity.jsonl` beside
`grembyte.log`, one JSON line per event with a run id. Agents, RPCs, script errors and the end
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

`grembyte/core/settings.py` (definitions, values in this process), table `settings` in
`grembyte.db` (`grembyte/db/repository/settings.py`), window `grembyte/ui/widgets/settings_dialog.py`
under Settings → General …; the settings themselves are listed in the
[user guide](../guide/settings.md). The window shows one page per category
(`_SETTING_CATEGORIES`), chosen in a list on the left; a page scrolls when it gets long, so new
settings do not make the window taller. The watch backend stays in the menu and in `QSettings`,
since it is chosen automatically on attach.

## Qt and PySide

- **Workers released too early (2026-10-29):** core dumps of the test suite: a window released a worker in its
  `finished` signal, which Qt emits before the thread has ended; destroying a running QThread
  aborted the process. Workers are now kept until their thread has ended
  (`grembyte/ui/workers.py`).
- **Widgets without a parent (2026-11-13):** give widgets their parent when they go into a layout that is not yet
  attached to a widget (`QHBoxLayout()` added to the dialog's layout afterwards). Qt reparents
  them later, but PySide still counts them as Python's own and deletes them a second time when
  the application is torn down: the settings window, left as a child of the main window, crashed
  Grembyte at exit (segfault in `QWidgetPrivate::invalidateBackingStore`, `crash.log`).

## grembyte-cli and the window

- **Handing a game over.** Frida sessions belong to the process that opened them, so the window cannot reuse those of `grembyte-cli`. Instead `grembyte-cli` gives the game up (`core/cli_link.py`). The window connects to its Unix socket and sends `claim`. `grembyte-cli` stops its workers, which removes their hooks, and answers only once the game is free. The open connection is the window's claim: when it closes, whether the window detached, was closed or crashed, `grembyte-cli` attaches again. A crash therefore never leaves the game without hooks.
- **Waiting for Frida's agent.** A host's Frida agent unloads only shortly after its last session detached. `attach_alone` refuses a new agent next to one it does not know (`error.attach.foreign_frida`). So for native games `grembyte-cli` waits until its agent has left the game before it answers `claim`, and waits for the window's agent before it attaches again (up to 10 s each). Under Wine the gadget stays loaded and is shared, so there is nothing to wait for.
- **Without the coordination**, the window would be refused with `foreign_frida` on a native game. Under Wine it would connect to the same gadget and install every saved hook a second time.
