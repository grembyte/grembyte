# Roadmap and ideas

Extensions that would help find addresses and hook entry points, and open points from the
work so far. As of November 2026.

What Hermes can do today is described in the [documentation](docs/index.md); what was learned
while building it is in the [implementation notes](docs/development/implementation-notes.md).

## Proposals, independent of any engine

Sorted by benefit relative to effort.

### Next: code explorer and pseudocode

To be tackled next, in this order. Matching PDBs are rarely shipped with games, so names
have to come from the file itself or from the engine's own reflection.

1. **Names without PDB**
   - **C++ RTTI:** done (see the [code explorer](docs/guide/code-explorer.md#class-names-from-c-rtti)):
     vtables and virtual methods named from MSVC and Itanium RTTI. Open: template
     arguments (shown as `<…>`), names of classes for objects in memory (point 4).
   - **IL2CPP and Unreal:** done (see the [code explorer](docs/guide/code-explorer.md#names-from-the-engine)):
     **Engine names** adds IL2CPP methods and Unreal exec thunks. Open: a test in
     V Rising and Avowed; remember the names per module build so they are there
     without the game.
   - **Unreal property descriptors:** strings like `PrimaryAttackPower` (Queen's Domain)
     show only as "pointer in .rdata": the pointer is the `NameUTF8` field of a UHT
     `FPropertyParams` record that the engine reads once at start-up to build its
     reflection, so no code uses the string. Recognise these records and show
     "UPROPERTY PrimaryAttackPower, float, offset 0x…" instead. UE4 layout (to verify per
     version): +0x00 name, +0x08 RepNotify name or 0, +0x10 PropertyFlags (8 bytes),
     +0x18 EPropertyGenFlags (type, 1 byte), +0x1c ObjectFlags, +0x20 ArrayDim,
     +0x24 offset in the object. Follow the record up through the `PropPointers` array and
     `FClassParams` to `Z_Construct_UClass_…` to name the owning class, and link to the
     object inspector so the value can be read on a live object.
2. **Own names and structure fields**
   - Rename functions, globals and arguments by hand, saved per module (ELF build ID, PE
     time stamp and checksum) and shown everywhere: disassembly, pseudocode, callers,
     trace, write watch.
   - Name structure fields: `*(int16_t *)(rbx + 0x790)` becomes `rbx->strength`; fits the
     pointer chains the pseudocode already shows.
   - "Who writes this field?": all instructions writing `[reg + offset]`, the static
     counterpart of the write watch, also finding writes that do not run at the moment.
3. **More readable pseudocode**
   - `if`/`else` and loops from the control flow graph instead of `goto` (if-then,
     if-else, backward jump as `while`/`do`).
   - `switch` from jump tables; also removes the odd instructions the linear sweep shows
     for them.
   - Local variables: `[rsp+0x28]` becomes `var_28`.
4. **Link to the running game**
   - Mark the functions the native trace saw running, with their call count.
   - Show the register and argument values of a monitor hit in the pseudocode.
5. **Smaller additions**
   - Copy a function's pseudocode to the clipboard.
   - The control flow graph as a graph view.
   - Carry names from a game's Linux build over to its Windows build through shared strings
     and the call graph (a small BinDiff; mainly for Baldur's Gate EE, whose Linux build is
     not stripped).

### 1. Native call diff trace: follow-ups

The native trace and the native function monitor exist (see the
[documentation](docs/guide/native-tools.md)). Open:

- Try it in a real native Linux game (load, playability, quality of the hits).
- The 32-bit path of the native trace under Wine is untested (Shadowrun Returns is Mono, where
  native tools ask first: they crashed it).
- Callers: the sequence shows the call site of the first call per thread. A list of all
  callers per function (a count per call site and target) is still missing.
- A disassembler view and a hook on a function found in the trace; update-proof byte
  signatures are point 5.
- Threads created during the trace are not followed.
- The native function monitor has not been tried in Baldur's Gate (native, Linux) yet.

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

- **String references:** done for the files of native modules (the
  [code explorer](docs/guide/code-explorer.md)): strings, the instructions and pointers
  referring to them, functions and callers. Open: strings only present at run time (built or
  decrypted in memory), and Mono/IL2CPP metadata strings.
- **C++ RTTI:** type names (MSVC `.?AV…@@`, Itanium `typeinfo`) lead to vtables, i.e. to
  class names for objects in memory. An address then becomes "`CPlayerStats`+0x48".
- **Symbols:** native Linux games are often not fully stripped. The native trace and monitor
  already use exported and debug symbols (`DebugSymbol`); the scanner, watch and hooks still
  show `module+0x…`.

### 5. Update-proof hooks through byte signatures

For native code, build a pattern (AOB) from the bytes around the write instruction, with
wildcards for addresses and offsets, and search for it again automatically after a game
update. For JIT code the method identity already does this; for native games it is missing.

Scripts can already find code by such a pattern (`ctx.native.scan`, see
[scripts in native games](docs/guide/scripts.md#scripts-in-native-games)); hooks and tracked
addresses cannot yet. Open for native scripts:

- A frame function of the script's own choosing, for games that export none of the known ones
  (SDL linked in statically, only `IDXGISwapChain::Present` through its vtable).
- Building the pattern for a function found by the native trace or monitor in one click (the
  code explorer has it; the trace and monitor windows could open it there).
- Only tried in Underkeep (own engine, SDL3, under Proton) and a test program so far.

### 6. Register hooks at any code address

Today a hook needs a write instruction: it relocates the write and hands old and new value
to the transform. Some values are better changed elsewhere, before they are written:

- **Write sites that cannot be hooked:** in Dark Souls III the souls are stored by a tiny
  setter (`mov [rcx+0x74], eax; ret` at `DarkSoulsIII.exe+0x465f13`), too short for the 5-byte
  jump. The short jump needs int3 padding nearby, which Arxan overwrites at run time, and Arxan
  restores the setter's code anyway (see the
  [implementation notes](docs/development/implementation-notes.md#write-hooks-under-anti-tamper-protection)).
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

### 7. Smaller additions to the search

- **Group search:** several values at fixed distances, e.g. HP 120 and max HP 150 four bytes
  further on. Finds structures at once.
- **Save game diff:** compare two save games to recognise values and layout, then search
  memory for them.

### 8. Hooks: remaining JavaScript paths

The C fast path handles most hooked writes (see the implementation notes). Still in
JavaScript:

- On Linux, callers are checked in JavaScript (no `GetCurrentThreadStackLimits`).
- A fixed-address hook that learns callers.

### Recommendation

The **code explorer and pseudocode** list above comes next, starting with names without PDB.
After that, **2 (accesses of an instruction)** is the next larger step independent of any
engine and uses much of what exists. Among the adapters, the call graph from Blueprint bytecode is left for
**Unreal**, and the call dialog for **IL2CPP**.

## Engines

The adapter layer is described in the [architecture](docs/development/architecture.md#adding-an-engine).

### Data type explorer (Mono/IL2CPP and Unreal)

The catalog lists methods; the types behind them are only seen one at a time, as an
argument in **Call method** or an object in the inspector. A type explorer would browse all
types the adapters know, searchable like the catalog:

- **Classes and structs:** fields with type and offset (inherited ones marked), base class
  and derived classes, the methods (linking to the catalog). Static fields with their current
  value and the address of their class's static data (as the pointer scan's chain bases).
- **Enums:** names and values, the base type, `[Flags]` shown as such. The adapters already
  read them for **Call method** (Mono from the constants' metadata, Unreal from `UEnum`).
- **Unreal:** `UClass`, `UScriptStruct` and `UEnum` from reflection, with property flags
  (`BlueprintReadWrite`, `Replicated`, ...) and the Blueprint classes of loaded packages.
- **Links:** open a live object of the type in the object inspector (`findObjects`, or the
  object browser below), copy a
  type as a C struct for the structure view (proposal 3), and give offsets to the pointer
  chain dialog, so `GameState.s_playerCharacter → m_Inventory → currencyTotalValue` can be
  entered by field names instead of hex offsets.
- Type names as C# writes them (`StrongholdAdventure.Type`), as the catalog shows them.

### Object browser (Unreal, then Mono/IL2CPP)

Today a live object is reached through `ctx.find`/`ctx.forEach` or a link in the inspector,
which needs its class name first. An object browser shows which objects exist: a tree with
the classes on the first level (with their number of instances) and below them the instances
as `"name"@address`, opened in the object inspector from there (double click or context
menu, also "copy address"). Above the tree: a dropdown to focus on one assembly or package
(also "game only", without the framework ones), a filter on class and instance names, and a
checkbox for singletons only. It is the instance side of the type explorer above; both share
the class tree, the dropdown and the filter, and can become two views of one window.

- **Unreal (first):** `GUObjectArray` holds every UObject, so one pass over the object table
  (as `findObjects` reads it) gives all of them, grouped by their exact class. The dropdown
  lists the packages as the catalog groups them (`/Script/<Module>`, `/Game/<Folder>`). Class
  default objects, archetypes and the reflection objects themselves (classes, functions,
  packages) are left out unless asked for. Games keep hundreds of thousands of objects: the
  classes come first with their counts, the instances of a class only when it is opened, and
  the filter runs in the agent (a class matches by its name or by those of its instances).
  `FName`s repeat, so the outer name (`ctx.outerName`) shows in the tooltip. Singletons:
  objects of the engine's single-instance classes (`GameInstance`, subsystems, `GameModeBase`,
  `GameStateBase`, `WorldSettings`, `GameUserSettings`), and game classes with exactly one
  live instance.
- **Mono/IL2CPP:** `FindObjectsOfType` only gives `UnityEngine.Object`s (GameObjects,
  components, ScriptableObjects), of active GameObjects, on the main thread. One call for
  `UnityEngine.Object`, grouped by class, gives those; `Resources.FindObjectsOfTypeAll`
  would add inactive objects and assets (a checkbox). Plain C# objects, where much game state
  lives (`GameState.s_playerCharacter → m_Inventory`), are not in it. A heap walk
  (`mono_gc_walk_heap`, Boehm GC) would find them but stops the game and is risky in old
  Monos (PoE2), so they are reached through static fields instead.
- **Singletons (Mono/IL2CPP):** a static field whose type is its own class or a subclass and
  which holds an object (`Instance`, `s_instance`, `<Instance>k__BackingField`), also through
  generic bases such as `Singleton<T>`. This reaches plain C# objects too, and is what the
  pointer scan wants as chain bases.
- Objects die: the tree is a snapshot with a refresh button, and an instance is checked again
  before the inspector opens it.

### More adapters

| Engine | Approach | Result |
|---|---|---|
| **Godot** | ClassDB / object properties | scene tree and variables |
| **Lua engines** | hook the `lua_*` API | tables and variables of the script state |
| **RPG Maker MV/MZ, Ren'Py** | JavaScript or Python runtime | game variables directly in the script state |

### Unreal: open

- Call graph from Blueprint bytecode (`UStruct::Script`).
- Monitor gaps: `EX_LocalOutVariable` (needs `FFrame::OutParms`) and nested expressions show
  as `?`; FText, TMap, TSet and delegates show their type only.
- The trace's callers could use the FFrame stack scan of the monitor.
- Call method: TArray, TMap, TSet, FText, delegates and structs of structs cannot be typed in.
- A trusted code-plugin interface for games with non-standard GAS setters. The agent has no
  external plugin loader; a setter with a genuinely different signature or call sequence needs
  code, not just another method name in configuration.
- UE4 before 4.23 (`TNameEntryArray` instead of `FNamePool`) and 32-bit games are not
  supported.

### IL2CPP: open

- The **Call method** dialog. Unity calls `il2cpp_runtime_invoke` on the main thread about
  17,000 times a second (also on job threads, to be filtered out), so the Mono way of running
  work on the main thread carries over. Arguments: IL2CPP passes a hidden `MethodInfo*` last.
- The call graph would need disassembly instead of IL.
- ECS games run most gameplay as Burst code (530 of 538 `OnUpdate` methods in V Rising), with
  their data in ECS chunks rather than objects; method hooks do not see it.

## Wine and Proton: open points

- **Method monitor and method hooks under Wine:** arguments follow the platform's calling
  convention (System V, Win64, x86). Still to check on a real game: Win64 with Kathy Rain 2,
  x86 with Shadowrun Returns, especially methods with structs and floats. Under 32 bit, float
  results (st0) and 64-bit results (edx:eax) are read but cannot be replaced.
- **Large traces under Wine:** with tens of thousands of methods the game froze while
  compiling (Mono's garbage collector hung in `SuspendThread`) or crashed while hooking in
  Frida's code (a write to the header of `OPENGL32.dll` at `0x7BEB0000`). Worked around with
  preselection filters and a limit (the `wine_trace_limit` setting), not fixed. A candidate
  for a bug report to Frida with a minidump.
- **32-bit Wine games:** the watch and hooks take ptrace, because the hook engine only handles
  x64 code (`installSite`).
- **Steam Linux Runtime:** a real Steam game in the container (Kathy Rain 2) is untested. The
  Windows helper runs outside the container and needs the shared `/tmp` and `/dev/shm`. If
  that fails, `nsenter` into the game's namespaces would be the next step.
- **Gadget loader (ptrace, fallback):** catches a thread with a software breakpoint at a
  frequently called Windows function (message pump, timing). Games that call none of these
  functions need `hot_points` of their own; `load_gadget` takes them, but neither the UI nor
  the settings can set them yet.

## Activity log: open

- The ptrace backends (watch, hooks) only report the start and end of the session, no single
  steps.

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
