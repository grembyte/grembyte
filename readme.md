# Hermes Hook

Hermes Hook is a Linux desktop application built with Python and PyQt6 for inspecting and manipulating the memory of running processes such as games. The project is under development.

## Features

- **Process selection** showing the command line and working directory.
- **Memory scan** for `int8`, `int16`, `int32`, `int64`, their unsigned variants `uint8` to `uint64`, and `float` and `double`: exact value, greater/less than, range, unknown initial value, and follow-up scans for changed, unchanged, increased or decreased, and for "increased/decreased/changed by X". Exact and by-X scans accept a tolerance (±); `float` and `double` values can be compared exactly, rounded or truncated to the decimal places entered (`12.3` rounded finds 12.25 to 12.35). "Guess automatically" tries `int32`, `int64`, `float` and `double`; smaller types match almost everywhere and have to be chosen explicitly.
- **Tracked addresses** with live values; values can be written straight into the process or frozen.
- **Write and read watch**: a hardware watchpoint shows which code writes an address or, with "Address watch (all accesses)", also reads it. The "Reads" and "Writes" columns show what a row's instruction does with the value; hooks can only be set on writing instructions.
- **Pointer scan**: finds pointer chains through which tracked addresses and hooks find the value again after the program restarts.
- **Hooks**: every write at a code location that was found is intercepted and the written value is manipulated, e.g. with a multiplier, a fixed value, bounds or a JavaScript function of your own. Hooks are stored per program and installed again the next time it starts.
- **Hook templates**: manipulations are kept in the database as JavaScript functions with parameters and can be tested without a game process.
- **Hook debug**: a debug window logs every call of a hook with its values, caller, output and errors.
- **Settings** (menu **Settings → General …**, stored in `hermes.db`): activity log on/off, the trace's method limit under Wine/Proton (default 5000, also no limit), from how many methods the trace asks first, entries in the traces' call sequence, the refresh interval of tracked addresses, and the language.
- **Activity log**: `activity.jsonl` in the log folder (`~/.local/state/hermes-hook`) records the course of every run: the attached process with its details (32/64 bit, native or Wine/Proton with the Proton version, engine, how the gadget was loaded), sessions and agents in the order they started and ended, every RPC with its duration and outcome (frequent ones summed up), clicks, and why a Frida session ended, with Frida's crash report. Values from the game's memory are not in it. It can be read in the **Log** window under **Activity**; **Export bug report …** packs the activity, the log and `crash.log` into a ZIP file.

### Freeze or hook?

**❄ Freeze** holds the current value of a tracked address: Hermes writes it back every 100 ms. Clicking again releases it; **✎ Write value** changes the frozen value. For entries with a pointer chain or a static address the state is stored and still applies after the program restarts. Entries without a chain are released when they are loaded again, because their address need no longer be right then. Freezing needs no write watch, but may flicker briefly, since the game may change the value between two passes. A hook with the "Fixed value" template, by contrast, acts directly at the write instruction.

## Requirements

- GNU/Linux on x86-64
- Python 3.11 or newer
- `venv` and `pip` for the chosen Python version
- A graphical session with X11 or Wayland for the Qt interface

On Debian or Ubuntu, Python and the tools can be installed from the distribution if needed:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
```

The Python dependencies, including PyQt6, are then installed with `pip` from `pyproject.toml`. If Qt reports a missing platform plugin at start, the distribution's Qt/XCB runtime packages have to be added.

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

The language is German or English depending on the system locale (English for any other locale). It can be chosen under **Settings → General …**, or for one start with an environment variable, which applies while the setting is "as the system":

```bash
HERMES_LANGUAGE=en hermes
HERMES_LANGUAGE=de hermes
```

At first start the SQLite database is created at `~/.local/share/hermes-hook/hermes.db` (or under `$XDG_DATA_HOME/hermes-hook/`). `HERMES_DB=/path/to/file.db` uses another file. If there is no database there yet but one from earlier versions at `data/hermes.db` in the project directory, it is copied once; the original stays. The process list only shows processes whose information the current user may access. When the attached process exits, Hermes resets the window within a second: scans, watches, freezing and hooks stop; stored addresses and hooks are kept and loaded again the next time it attaches.

## Linux permissions for memory access

Scans and writes use `/proc/<pid>/mem`; the write watch and hooks attach to the target process through [Frida](https://frida.re). Both are subject to Linux's protections, in particular Yama (`kernel.yama.ptrace_scope`):

- `0`: processes of the same user may be inspected.
- `1` (the default of many distributions): only one's own child processes. Hermes then cannot access a game started separately.

Do not run the application as root just in case, and do not loosen `ptrace_scope` permanently system-wide. For a test session a temporary change is enough, which lapses at the next reboot:

```bash
sudo sysctl kernel.yama.ptrace_scope=0
```

### Wine and Proton games

`frida.attach` crashes Wine processes. Instead, hermes loads Frida's Windows gadget (`frida-gadget.dll`, downloaded once to `~/.local/share/hermes-hook/gadget/`) into the game. A small Windows program, `hermes-inject-x86_64.exe` or `hermes-inject-x86.exe` (source `hermes/core/inject/inject.c`), does that; hermes starts it with the game's Wine and prefix. If that fails, hermes uses ptrace as a fallback for 64-bit games. The executables are not in the repository: `hermes/core/inject/build.sh` builds them into `build/inject/` (needs `mingw64-gcc` and `mingw32-gcc`, or `pip install ziglang`). Hermes runs it by itself the first time a Wine game needs the helper; `HERMES_INJECT_DIR` points to executables elsewhere.

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

- **With a pointer chain** the hook is stored with the bytes of the write instruction and the surrounding code. After a restart it is in **Training**: Hermes watches the chain's target with a hardware watchpoint of its own and installs the hook at the first write from matching code. It takes effect from the next write on. The tooltip of the status column shows how many writes from other code were discarded. At most three hooks can train at a time.
- **Without a pointer chain** the hook cannot be stored, because its target is unknown after a restart.

After a restart the original target address is no longer valid. How the hook recognises the right writes depends on how it was created:

- **With a pointer chain** (see below): the hook resolves the chain at every write and only acts on the address it leads to at that moment. This is the most reliable variant.
- **Without a pointer chain**: while running, the hook learns the callers, i.e. the return addresses on the stack, that write the target address. After a restart it only acts on writes from these callers. How many return addresses identify a caller is set by **Caller levels** in the hook dialog (default `1`; `off` learns no callers). More levels help when the player and other objects are changed through the same helper function. If a learned caller also writes other addresses, Hermes points it out.

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

Limitations:

- x86-64 processes only.
- The write instruction and the bytes up to the length of a jump (5 bytes) are overwritten. If other code jumps into the middle of this range, the target crashes; Frida hooks and Cheat Engine's code injection carry the same risk.
- If the connection to Hermes breaks off unexpectedly while a hook is active, the target may crash.
- `int64` and `uint64` values are exact in manipulations only up to 2^53.

## Hook templates and manipulations of your own

A manipulation is a JavaScript function that runs in the target process after every write. It receives a `ctx` object and returns the value to store; `undefined` or `null` keeps the game's value. Integer results are truncated and limited to the range of the type.

| Field | Meaning |
| --- | --- |
| `ctx.old`, `ctx.new`, `ctx.delta` | Value before and after the write, difference |
| `ctx.params` | The template's parameters with the hook's values |
| `ctx.state` | Object kept between calls (e.g. for counters) |
| `ctx.valueType`, `ctx.address`, `ctx.writePc`, `ctx.threadId`, `ctx.hookId` | Context of the write |
| `ctx.log(…)` | Output in the debug window; no effect outside debug mode |

### Maintaining templates

Manipulations are stored in the database as **hook templates** and managed under **View → Hook templates…**. At first start Hermes creates the examples **Multiplier**, **Fixed value** and **Clamp** in the language of the interface; they can be changed or deleted like templates of your own.

A template consists of a name, an optional description, the JavaScript function and parameters. Each parameter has a name (`ctx.params.<name>` in the script), a label, a type (**Number** or **Choice**), a default and whether it is optional; choice values are given as `value=Label; value=Label`. The hook dialog builds its input form from the parameters. Example of a template with the number parameter `threshold`:

```js
ctx => ctx.delta >= ctx.params.threshold ? ctx.new : ctx.old
```

**▶ Test** runs the function without a game process: enter the value type, the old value (`ctx.old`) and the value written by the game (`ctx.new`) and run it. It shows the stored value, output of `ctx.log`/`console.log`, and errors. `ctx.state` is kept between test runs until it is reset. The test runs in Qt's JavaScript engine rather than in Frida, so Frida APIs such as `ptr()` or `Memory` are not available there.

A hook stores a **copy** of the template it was created from. Later changes to the template therefore do not affect existing hooks. In the **Edit** dialog Hermes points out a changed template and offers **Use changed template**. If the template is deleted, the hook keeps working with its copy.

### Debugging hooks

**◉ Debug** in the **Hooks** tab puts the selected hook into debug mode. The debug window then logs every call with time, thread, address, caller, old value, game value, result, `ctx.log` output and errors with their stack. Writes the hook skips appear too, with the reason, such as "writes another address" or "caller not learned". Debug mode costs time in the game; with very many calls the agent drops entries and only counts them. Closing the window ends debug mode.

### Example templates in the code

The example templates come from subclasses of `ValueTransform` in `hermes/hooks/transforms.py` whose `kind` is in `EXAMPLE_TEMPLATE_KINDS`. If an example is missing at start (same seed key or name), Hermes creates it. Name, description and parameter labels come from `hermes/ui/localization/*.json` under `transform.<kind>`, `transform.<kind>.description`, `transform.<kind>.<parameter>` and `transform.choice.<value>`; where they are missing, `title`, the parameter name or the choice value is used. Manipulations of your own do not need this: they are created as templates directly in the interface.

## Tests

```bash
./.venv/bin/python -m pytest
```

The hook tests compile a small C program and attach to it with Frida. They are skipped if `gcc` is missing or `kernel.yama.ptrace_scope` is not `0`.
