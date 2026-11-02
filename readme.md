# Hermes Hook

Hermes Hook is a desktop application built with Python and PySide6 for inspecting and manipulating the memory of running processes such as games. It runs on Linux, including Windows games under Wine/Proton, and on Windows. The project is under development.

## Disclaimer
This tool is intended solely for educational, research, and single-player game modding purposes. It is not designed or intended to bypass anti-cheat mechanisms or be used in online multiplayer games. The developer assumes no responsibility or liability for any account bans, suspensions, or damages resulting from the use or misuse of this software.

Use of the feature for restoring memory modifications may be detected by anti-tamper systems (such as Arxan) and, when used online, will inevitably result in account bans or soft bans. Use it at your own risk.

## Features

- **Process selection** showing the command line and working directory.
- **Memory scan** for `int8`, `int16`, `int32`, `int64`, their unsigned variants `uint8` to `uint64`, and `float` and `double`: exact value, greater/less than, range, unknown initial value, and follow-up scans for changed, unchanged, increased or decreased, and for "increased/decreased/changed by X". Exact and by-X scans accept a tolerance (±); `float` and `double` values can be compared exactly, rounded or truncated to the decimal places entered (`12.3` rounded finds 12.25 to 12.35). "Guess automatically" tries `int32`, `int64`, `float` and `double`; smaller types match almost everywhere and have to be chosen explicitly.
- **Tracked addresses** with live values; values can be written straight into the process or frozen. The scan area above them folds away (**▲ Hide address search** next to the tabs, **View → Address search** or Ctrl+Shift+F), and stays so at the next start. Like that, every window keeps its size (windows that are not modal also their position), its splitters and the column widths you set.
- **Write and read watch**: a hardware watchpoint shows which code writes an address or, with "Address watch (all accesses)", also reads it. The "Reads" and "Writes" columns show what a row's instruction does with the value; hooks can only be set on writing instructions. Watch and hooks run through Frida by default; **Settings → Backend (watch & hooks)** switches to ptrace, which needs no injection (at most 4 addresses, no training or caller learning). Hermes picks ptrace by itself for 32-bit Wine/Proton games.
- **Pointer scan**: finds pointer chains through which tracked addresses and hooks find the value again after the program restarts.
- **Hooks**: every write at a code location that was found is intercepted and the written value is manipulated, e.g. with a multiplier, a fixed value, bounds or a JavaScript function of your own. Hooks are stored per program and installed again the next time it starts.
- **Hook templates**: manipulations are kept in the database as JavaScript functions with parameters and can be tested without a game process.
- **Hook debug**: a debug window logs every call of a hook with its values, caller, output and errors.
- **Settings** (menu **Settings → General …**, stored in `hermes.db`): activity log on/off, the trace's method limit under Wine/Proton (default 5000, also no limit), from how many methods the trace asks first, entries in the traces' call sequence, the refresh interval of tracked addresses, the language, automatic downloads of missing Wine/Proton Frida gadgets (off by default), and three hook options: a short jump in tight spots (experimental, see [limitations](#how-a-hook-works)), patching hooks again that the game removed (for games that repair their code, e.g. Dark Souls III), and measuring the cost of hooks.
- **UI color themes** (**Settings → Color theme …**): choose a bundled theme or edit its colors. Color changes preview immediately; **Save** stores the selected palette as `hermes_themes_<name>.json` and activates it, while **Cancel** discards the preview. New themes can be created by entering a name in the theme selector. The bundled themes include Default, Solarized-Dark and Solarized-Light.
- **Activity log**: `activity.jsonl` in the log folder (`$XDG_STATE_HOME/hermes-hook`, by default `~/.local/state/hermes-hook`; on Windows `%LOCALAPPDATA%\hermes-hook\logs`; `HERMES_LOG_DIR` sets another folder) records the course of every run: the attached process with its details (32/64 bit, native or Wine/Proton with the Proton version, engine, how the gadget was loaded), sessions and agents in the order they started and ended, every RPC with its duration and outcome (frequent ones summed up), clicks, and why a Frida session ended, with Frida's crash report. Values from the game's memory are not in it. It can be read in the **Log** window under **Activity**; **Export bug report …** packs the activity, the log and `crash.log` into a ZIP file.

### More tools

- **File menu**: export the saved addresses, hooks, method hooks, favorites and scripts of a game to a JSON file and import them again, delete saved entries or whole processes, compact the database, and check whether the Wine/Proton gadget matching the installed Frida version is cached.
- **Memory viewer** for the memory around an address, and the **Sessions** window, which lists the connections to processes (Frida, ptrace) and can cut one that does not respond.
- **Native trace** (**View → Native trace…**, any engine): Frida's Stalker counts which functions the chosen threads call, first in a baseline, then in a scenario such as paying gold; the functions that are new or called more often in the scenario are the candidates. **Monitor native function …** logs the arguments, result and caller of a function and compares the memory behind pointer arguments before and after the call.
- **Favorites** (**View → Favorites …**): methods and functions marked with ★ in the catalog or a trace, saved per program with a note.
- **Unity games with Mono**:
  - The **catalog** lists the classes and methods of the game's assemblies, also offline for games read before.
  - **Assemblies …** limits the catalog and the trace to chosen assemblies.
  - The **trace** finds the methods a scenario calls, with preselection filters.
  - The **method monitor** logs calls with arguments and results; **method hooks** change arguments, fields or results with templates of their own.
  - The **call graph** shows which methods a method calls and which call it, from the IL code.
  - The **object inspector** shows the fields of C# objects, and **Call method** runs a static method on the game's main thread.
- **Unreal Engine games** (4.23 and later, 64-bit; e.g. Avowed under Proton): Hermes finds the engine's reflection data (`FNamePool`, `GUObjectArray`) in memory, without signatures or a mod loader.
  - The **catalog** lists the classes and UFunctions with C++ parameter types, grouped by package: `/Script/<Module>` for C++ classes, the first folder for Blueprints (`/Game/Blueprints`).
  - **Assemblies …** (here: packages) and the **trace** work as for Mono. The trace hooks the exec thunks of native UFunctions and gives every Blueprint function a hookable stub of its own; it sees calls from Blueprints and through `ProcessEvent` (events, delegates), not C++ calling C++ directly (the native trace sees those).
  - The **method monitor** logs UFunction calls with the object they run on (with its UObject name), arguments and result; the object inspector shows the UProperties of objects and structs. Arguments come from the parameters ProcessEvent passes, or, when a Blueprint calls a native function, from the bytecode of the call (variables and constants; other arguments show as `?`).
  - **Method hooks** change arguments (from ProcessEvent's parameters, or ref arguments from Blueprint code) and fields before a call, or the result after it. Their scripts can call UFunctions: `obj.call("Function", ...args)` runs it through `UObject::ProcessEvent` on the game thread, `ctx.find("ClassName")` gives a live object of a class, e.g. the game's cheat manager.
  - **GAS attributes** (**View → GAS attributes …**, games with Unreal's Gameplay Ability System): the player's attribute sets with base and current value, live. A base value set there goes through the game's own setter (found by its parameters, in Avowed `AlabamaAbilitySystemBlueprintLibrary:SetFloatAttributeBaseFromAbilitySystemComponent`), so the active effects keep counting: in Avowed `DamageOutgoingMult` 3 with a +40 % bonus deals 4.2 times the damage. Writing such values into memory does not last, as GAS computes the current value anew. **Hold** keeps a value against the game resetting it (loading, levelling up) by a script in the **Scripts** tab; scripts can do the same with `ctx.gas.get("Might")` and `ctx.gas.setBase("AttributeSetCore.Might", 14)`.
  - **Call method** runs a UFunction through `ProcessEvent` on the game thread: a static one on its class default object, an instance one (from the catalog or favorites) on a live object of its class – the first one found, or the one whose name or address you type in, e.g. `AlabamaCheatManager:GiveHealth` in Avowed. FString and FName are typed as text, bools as a checkbox, enums by name, structs of numbers such as FVector as `1 2 3` or `X=1; Z=3`; outputs need no input and are shown after the call. Not possible: TArray, TMap, TSet, FText and delegates as arguments.
  - Not yet: the call graph.
- **Scripts** (tab next to **Hooks**): JavaScript that calls game code on a schedule or by hand; scripts are stored per program and restored when it is attached. See [Scripts](#scripts).

### Scripts

Scripts are JavaScript function bodies that run on the game's main thread and can call its methods. Each script is stored per program and restored when that program is attached. Choose when it runs:

- **Once**: after the configured delay, each time the script starts.
- **Repeat**: every configured interval; the next interval starts after the previous run finishes, so runs do not overlap.
- **Manual**: only when you click **Run**.

Scripts can have **parameters** (text, number, yes/no or choice). A manual run asks for their values; scheduled runs use their defaults. A choice parameter gets its options from a short JavaScript function evaluated in the game, returning values or `{label, value}` objects. The list filters as you type.

The Scripts tab's `ctx` is separate from the value-hook `ctx` described under [Hook templates](#hook-templates-and-manipulations-of-your-own):

| Field | Meaning |
| --- | --- |
| `ctx.find(className[, objectName])` | Find a live object in Unity or Unreal. Unreal also accepts the object's name within its outer; class default objects and Widget Blueprint templates are not returned. |
| `ctx.findClass(className)` | Unreal: get a class object, for example for a `TSubclassOf` parameter. |
| `ctx.forEach(className, callback)` | Call `callback` for each live object of the class; stop early by returning `false`. |
| `ctx.outerName(object)` | Unreal: get an object's name including its outer, useful with `ctx.find`. |
| `ctx.call(method, ...args)` | Call a static game method, using a dotted name or the `Namespace.Class:Method` form shown in catalogs. |
| `ctx.gas` | Unreal games with Gameplay Ability System: `attributes()` lists sets; `get(name)` returns `{base, current}` or `null` if there is no player; `setBase(name, value)` changes the base through the game's code, preserving active effects. |
| `ctx.args` | Values of the script's parameters, by name. |
| `ctx.lang` | Current app language: `"de"` or `"en"`. |
| `ctx.log(...)` | Write to the script log. |
| `ctx.state`, `ctx.runs`, `ctx.scriptId` | Per-script state kept between runs, the number of earlier runs, and the script's ID. |

Objects returned by `ctx.find` are wrappers: call instance methods with `.call(name, ...args)`, access fields and properties as properties, and use `.field(name)` when needed. A returned object is shown as a result and can be opened in the object inspector. For example, Avowed's `AlabamaCheatManager` can restore 50 health with `ctx.find("AlabamaCheatManager").call("GiveHealth", 50)`.

### Freeze or hook?

**❄ Freeze** holds the current value of a tracked address: Hermes writes it back every 100 ms. Clicking again releases it; **✎ Write value** changes the frozen value. For entries with a pointer chain or a static address the state is stored and still applies after the program restarts. Entries without a chain are released when they are loaded again, because their address need no longer be right then. Freezing needs no write watch, but may flicker briefly, since the game may change the value between two passes. A hook with the "Fixed value" template, by contrast, acts directly at the write instruction.

## Requirements

- GNU/Linux or Windows on x86-64; the instructions below are for Linux
- Python 3.11 or newer
- `venv` and `pip` for the chosen Python version
- A graphical session with X11 or Wayland for the Qt interface

On Debian or Ubuntu, Python and the tools can be installed from the distribution if needed:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
```

The Python dependencies, including PySide6, are then installed with `pip` from `pyproject.toml`. If Qt reports a missing platform plugin at start, the distribution's Qt/XCB runtime packages have to be added.

## Installation

In the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

The editable install also sets up the console command `hermes`.

## Start

With the virtual environment activated:

```bash
hermes
```

Alternatively the application can be started straight from the project directory:

```bash
./.venv/bin/python main.py
```

`scripts/hermes-hook.sh` activates the virtual environment (`.venv` in the project, or `$HERMES_VENV`) and starts the application from any working directory. To start Hermes from the desktop menu (KDE, GNOME, …), install a menu entry for the current user; it appears under Development and Games:

```bash
./scripts/install-desktop-entry.sh              # install or update the entry
./scripts/install-desktop-entry.sh --uninstall  # remove it
```

The entry points to the project directory, so run the script again after moving the repository.

On Windows, `scripts\hermes-hook.cmd` starts the application the same way. For the Start menu and the taskbar, install a shortcut for the current user, then right click "Hermes Hook" in the Start menu and choose "Pin to taskbar":

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows-shortcut.ps1             # install or update
powershell -ExecutionPolicy Bypass -File scripts\install-windows-shortcut.ps1 -Uninstall  # remove it
```

The language is German or English depending on the system locale (English for any other locale). It can be chosen under **Settings → General …**, or for one start with an environment variable, which applies while the setting is "as the system":

```bash
HERMES_LANGUAGE=en hermes
HERMES_LANGUAGE=de hermes
```

At first start the SQLite database is created at `~/.local/share/hermes-hook/hermes.db` (or under `$XDG_DATA_HOME/hermes-hook/`), on Windows at `%LOCALAPPDATA%\hermes-hook\hermes.db`. `HERMES_DB=/path/to/file.db` uses another file. If there is no database there yet but one from earlier versions at `data/hermes.db` in the project directory, it is copied once; the original stays. Catalogs read from Mono and Unreal games are cached beside it in `hermes-hook/cache` (or `$HERMES_CACHE_DIR`). User themes are saved in `~/.config/hermes-hook/themes/` (or `$XDG_CONFIG_HOME/hermes-hook/themes/`), and on Windows in `%APPDATA%\hermes-hook\themes\`; each file is named `hermes_themes_<name>.json`. Bundled themes ship in `hermes/ui/themes/`. The process list only shows processes whose information the current user may access. When the attached process exits, Hermes resets the window within a second: scans, watches, freezing and hooks stop; stored addresses and hooks are kept and loaded again the next time it attaches.

## Linux permissions for memory access

Scans and writes use `/proc/<pid>/mem`; the write watch and hooks attach to the target process through [Frida](https://frida.re). Both are subject to Linux's protections, in particular Yama (`kernel.yama.ptrace_scope`):

- `0`: processes of the same user may be inspected.
- `1` (the default of many distributions): only one's own child processes. Hermes then cannot access a game started separately.

Do not run the application as root just in case, and do not loosen `ptrace_scope` permanently system-wide. For a test session a temporary change is enough, which lapses at the next reboot:

```bash
sudo sysctl kernel.yama.ptrace_scope=0
```

### Wine and Proton games

`frida.attach` crashes Wine processes. Instead, Hermes loads Frida's Windows gadget into the game: `frida-gadget-<Frida version>-windows-<x86|x86_64>.dll`, cached under `~/.local/share/hermes-hook/gadget/` (or `$XDG_DATA_HOME/hermes-hook/gadget/`). Automatic downloads are **off by default**. When the matching gadget is missing as a Wine/Proton process is selected, Hermes asks whether to download it from GitHub if automatic downloads are off. Approving downloads it and turns the setting on for future missing gadgets; declining leaves automatic downloads off. With the setting enabled, missing gadgets download without another prompt. You can also enable it yourself in advance under **Settings → General → Automatically download the Wine/Proton Frida gadget**.

On Linux, **File → Check Frida Gadget Version …** checks locally for the gadget matching the installed Frida version. With a Wine/Proton process selected it checks that process's architecture; otherwise it checks both x86 and x86_64. If a matching file is missing, Hermes asks whether to download it now. This manual check is a one-time action and does not enable automatic downloads. It does not query GitHub for a newer Frida release. While Frida is using a Wine gadget, the status bar adds **(Update available)** when the matching gadget file is missing.

A small Windows program, `hermes-inject-x86_64.exe` or `hermes-inject-x86.exe` (source `hermes/core/inject/inject.c`), loads the gadget; Hermes starts it with the game's Wine and prefix. If that fails, Hermes uses ptrace as a fallback for 64-bit games. The executables are not in the repository: `hermes/core/inject/build.sh` builds them into `build/inject/` (needs `mingw64-gcc` and `mingw32-gcc`, or `pip install ziglang`). Hermes runs it by itself the first time a Wine game needs the helper; `HERMES_INJECT_DIR` points to executables elsewhere.

```bash
sh hermes/core/inject/build.sh
```

## Example: gold multiplier for the test game

`test_app/dummy_game.c` is a minimal GTK game with buttons for "Gold +100", "Gold -100", "New game", and mana, experience and a treasure hunt for testing fuzzy scans (see [below](#fuzzy-scans-tolerance-and-rounded-floats)). As in real games, the player lives on the heap behind pointers (`g_game → player → gold`), so its address changes with every start and every "New game". The game prints its PID, the pointer chain and the current address of the gold, so that the results of the scanner and the pointer scan can be checked. Several instances may run at once.

```bash
mkdir -p build
gcc -O2 test_app/dummy_game.c -o build/dummy_game.exe $(pkg-config --cflags --libs gtk+-3.0)
./build/dummy_game.exe
```

This needs `gcc` and the GTK 3 development packages (`gtk3-devel` on Fedora, `libgtk-3-dev` on Debian/Ubuntu).

1. In Hermes, **Select process** → `dummy_game.exe`.
2. **First scan** for the gold value shown (`int32`), change the gold in the game, **Next scan** with the new value until one address is left. Take it into the tracked addresses with **+ Track** and give it a description such as "Gold".
3. Right-click the address → **Address watch (writes)**.
4. Click **Gold +100** in the game. The watch window shows the code location that raises the gold (e.g. `dummy_game+0x…`). "Gold -100" produces a second row of its own.
5. Select the row of the increase → **Create hook**, template **Multiplier**, factor `10`, "Increases only".
6. The hook appears in the **Hooks** tab. Every click on "Gold +100" now brings 1000 gold; the "Last" column shows old → written by the game ⇒ stored.

In the **Hooks** tab hooks can be edited, paused, debugged and removed. When a hook is removed, when the process changes and when Hermes exits, the original bytes are restored.

### Fuzzy scans: tolerance and rounded floats

The game shows mana (`float`) with one decimal place and experience (`double`) with none. Mana regenerates by 0.13 every second, a spell costs 7.5; a fight brings 10–15 XP with decimal places, a treasure hunt 20–30 gold. The game prints the addresses every round.

- **Mana**: data type `float`, **Exact value** with the value shown (e.g. `20.1`) and float comparison **Rounded**; with "Exact" the address is hard to find, because the stored value is e.g. 20.13. Then click **Cast spell** and scan **Decreased by …** `7.5` with tolerance `0.5` – the regeneration between the scans would make a comparison without tolerance fail.
- **Experience**: data type `double`, **Exact value** with the number shown and **Rounded**, after **Fight** **Increased by …** `12` with tolerance `3`.
- **Gold**: after **Find treasure** **Increased by …** `25` with tolerance `5` (`int32`).

### Hooks after a restart

Hooks are stored per program, as module + offset of the write instruction. When the program is selected again after a restart, Hermes installs the stored hooks automatically. If the bytes at that location changed, e.g. after an update of the program, the hook is not installed. If the write instruction lies in no module, e.g. in Mono/Unity JIT code, which is generated anew and elsewhere at every start, it depends on the target:

- **With a pointer chain** the hook is stored with the bytes of the write instruction and the surrounding code. In Mono games it also remembers the C# method of the write: after a restart Mono compiles that method and the hook is installed in it right away. Otherwise, or if that fails, it is in **Training**: Hermes watches the chain's target with a hardware watchpoint of its own and installs the hook at the first write from matching code. It takes effect from the next write on. The tooltip of the status column shows how many writes from other code were discarded. At most three hooks can train at a time; hooks installed as a hardware breakpoint take one of these three slots each.
- **Without a pointer chain** the hook cannot be stored, because its target is unknown after a restart.

After a restart the original target address is no longer valid. How the hook recognises the right writes depends on how it was created:

- **With a pointer chain** (see below): the hook resolves the chain at every write and only acts on the address it leads to at that moment. This is the most reliable variant.
- **Without a pointer chain**: while running, the hook learns the callers, i.e. the return addresses on the stack, that write the target address. After a restart it only acts on writes from these callers. How many return addresses identify a caller is set by **Caller levels** in the hook dialog (default `1`, at most `4`; `off` learns no callers). More levels help when the player and other objects are changed through the same helper function. If a learned caller also writes other addresses, Hermes points it out.

### Finding the gold again after a restart (pointer scan)

Tracked addresses are stored per program. For "Gold" to be right again after the game restarts, the entry needs a pointer chain:

1. Right-click "Gold" → **Find pointers …** → **New scan**. Among others it finds `dummy_game+0x…  → 0x28 → 0x0` (`g_game` → player → gold), plus some chance hits.
2. Click **New game** in the game (or restart the game) and find the gold again in the scanner – the pointer dialog can stay open meanwhile. Select the result in the scanner, then **Take from scan** and **Filter with this address** in the pointer dialog. Chance hits that only matched because of the layout in memory drop out; a plain restart is often not enough for that, since the layout stays the same at start.
3. Select the remaining chain → **Use pointer**. From now on the address list shows the gold correctly after every restart and every "New game".
4. If the write watch is started from this entry, a new hook gets the chain as its target: it follows the player also after "New game" and after a restart.

If an address lies in the program's static memory (a global variable), Hermes stores it as module + offset right away when tracking it; a pointer scan is not needed then, and the pointer dialog says so.

### How a hook works

The write watch reports the instruction *after* the write (hardware watchpoints only fire afterwards). From it Hermes determines the actual write instruction and replaces it with a jump into a code cave:

```
before(regs)       save the old value if the instruction really writes the target address
<write instruction, relocated>
after(regs)        call the manipulation and store the result
<remaining relocated bytes>
jmp back
```

Since Hermes only works with the old and the new value, it does not matter whether the game uses `add [mem], reg`, `mov [mem], reg` or `sub`. If the same instruction is also used for other objects (e.g. the enemies' gold), the hook only acts on its target: the watched address, the address of its pointer chain or, after a restart without a chain, the learned callers. Several hooks can share a write instruction; they run in the order they were created.

`before` and `after` are C functions (a Frida CModule): they check the target address and, on Windows and under Wine, the learned callers, and run the built-in manipulations (multiply, fixed value, clamp, also as copies in templates) themselves, so a hooked write costs a fraction of a microsecond. On Linux the callers are checked in JavaScript. Only scripts of your own, the debug mode, caller learning and pointer chains run in JavaScript. The agent's JavaScript runs one thread at a time: a game thread that has to wait there can drop frames.

Limitations:

- x86-64 processes only.
- The write instruction and the bytes up to the length of a jump (5 bytes) are overwritten. If other code jumps into the middle of this range, the target crashes; Frida hooks and Cheat Engine's code injection carry the same risk.
- If the write sits in a tiny function with less than 5 bytes before the next one, the setting "Hooks: short jump in tight spots" overwrites only 2 bytes with a short jump to int3 padding nearby. The padding is found by a heuristic; if it guesses wrong, the target crashes.
- **Hardware breakpoint (code unchanged)** in the hook dialog leaves the code as it is and uses one of the three debug registers instead. Some anti-tamper protections detect debug registers (Dark Souls III crashed); Hermes points it out when a game ends shortly after debug registers were used.
- If the connection to Hermes breaks off unexpectedly while a hook is active, the target may crash.
- `int64` and `uint64` values are exact in manipulations only up to 2^53.

## Hook templates and manipulations of your own

A manipulation is a JavaScript function that runs in the target process after every write. It receives a `ctx` object and returns the value to store; `undefined` or `null` keeps the game's value. Integer results are truncated and limited to the range of the type.

| Field | Meaning |
| --- | --- |
| `ctx.old`, `ctx.new`, `ctx.delta` | Value before and after the write, difference |
| `ctx.params` | The template's parameters with the hook's values |
| `ctx.state` | Object kept between calls (e.g. for counters) |
| `ctx.this` | Method hooks only: the instance the method runs on; `null` for static methods |
| `ctx.valueType`, `ctx.address`, `ctx.writePc`, `ctx.threadId`, `ctx.hookId` | Context of the write |
| `ctx.log(…)` | Output in the debug window; no effect outside debug mode |

### Maintaining templates

Manipulations are stored in the database as **hook templates** and managed under **View → Hook templates…**. At first start Hermes creates the examples **Multiplier**, **Fixed value** and **Clamp**; they can be changed or deleted like templates of your own. Their texts are English whatever the language of the interface, since templates are data that exports share.

A template consists of a name, an optional description, the JavaScript function and parameters. Each parameter has a name (`ctx.params.<name>` in the script), a label, a type (**Number**, **Choice** or **Code** for multi-line text), a default and whether it is optional; choice values are given as `value=Label; value=Label`. The hook dialog builds its input form from the parameters. Example of a template with the number parameter `threshold`:

```js
ctx => ctx.delta >= ctx.params.threshold ? ctx.new : ctx.old
```

**▶ Test** runs the function without a game process: enter the value type, the old value (`ctx.old`) and the value written by the game (`ctx.new`) and run it. It shows the stored value, output of `ctx.log`/`console.log`, and errors. `ctx.state` is kept between test runs until it is reset. The test runs in Qt's JavaScript engine rather than in Frida, so Frida APIs such as `ptr()` or `Memory` are not available there.

A hook stores a **copy** of the template it was created from. Later changes to the template therefore do not affect existing hooks. In the **Edit** dialog Hermes points out a changed template and offers **Use changed template**. If the template is deleted, the hook keeps working with its copy.

### Debugging hooks

Below the hook table, a log shows what hook scripts write with `ctx.log` or `console.log` and the errors of hooks, also without debug mode (at most 100 lines per 0.1 s; more are counted as dropped).

Several hooks can be selected at once (Ctrl/Shift-click): **Pause**, **◉ Debug** and **Remove** act on all of them, **Edit** on one. **◉ Debug** in the **Hooks** tab puts the selected hooks into debug mode. The debug window then logs every call with time, thread, address, caller, old value, game value, result, `ctx.log` output and errors with their stack. Writes the hook skips appear too, with the reason, such as "writes another address" or "caller not learned". Debug mode costs time in the game; with very many calls the agent drops entries and only counts them. Closing the window ends debug mode.

### Example templates in the code

The example templates come from subclasses of `ValueTransform` in `hermes/hooks/transforms.py` whose `kind` is in `EXAMPLE_TEMPLATE_KINDS`. If an example is missing at start (same seed key or name), Hermes creates it. Name, description and parameter labels come from `EXAMPLE_TEXTS` in the same file, under `<kind>`, `<kind>.description`, `<kind>.<parameter>` and `choice.<value>`; where they are missing, `title`, the parameter name or the choice value is used. Examples created by earlier versions with German names are renamed through `GERMAN_EXAMPLE_TEXTS`. Manipulations of your own do not need this: they are created as templates directly in the interface.

## Tests

pytest comes with the `test` extra:

```bash
./.venv/bin/python -m pip install -e ".[test]"
./.venv/bin/python -m pytest
```

Many tests compile a small C program and attach to it with Frida (hooks, watch, native trace and monitor, the Mono features against a stand-in Mono runtime, the Unreal features against a stand-in Unreal game). They are skipped if `gcc` is missing, `kernel.yama.ptrace_scope` is not `0` or the machine is not x86-64. Tests of the Windows helper need MinGW, some need Linux.

## License

Copyright (C) 2026 Frank Hoogen

Hermes Hook is distributed under the MIT License. It is provided without any warranty; see [LICENSE](LICENSE).

The third-party components and their licenses are listed in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES); the full license texts are in [LICENSES](LICENSES/).
