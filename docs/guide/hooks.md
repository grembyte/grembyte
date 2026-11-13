# Hooks

A value hook changes writes to a target address: with the Frida backend it intercepts writes at a specific instruction; with `ptrace` it watches the address and corrects its value after each write. Hooks are stored per program and installed again the next time it starts.

<figure markdown="span">
  ![Hooks tab](../assets/screenshots/hooks-tab-dark.webp#only-dark)
  ![Hooks tab](../assets/screenshots/hooks-tab-light.webp#only-light)
  <figcaption>The Hooks tab with its log below the table.</figcaption>
</figure>

## Creating a hook

1. Find the instruction that writes the value with the [write watch](write-watch.md).
2. Select its row and click **Create hook …**.
3. Give it a **Description**, e.g. *Gold*: it names the hook in the table and in the messages of [grembyte-cli](launch-option.md).
4. Choose a [template](templates.md) and fill in its parameters. The built-in ones are **Multiplier**, **Fixed value** and **Clamp**.

The top of the dialog names the target: its address and type, and the write site, the code location as `module+offset`. A hook created from a tracked address with a [pointer chain](pointer-scan.md) follows the chain.

<figure markdown="span">
  ![Create hook](../assets/screenshots/hook-dialog-dark.webp#only-dark){ width="640" }
  ![Create hook](../assets/screenshots/hook-dialog-light.webp#only-light){ width="640" }
</figure>

Further options in the dialog:

**Caller levels**
:   For hooks without a pointer chain: how many return addresses identify a caller that writes the target (default `1`, at most `4`, `off` learns no callers). See [after a restart](#after-a-restart).

**Install as**
:   With the Frida backend: **Jump in the code (fast)** writes a jump to the hook into the game's code. **Hardware breakpoint (code unchanged)** leaves the code as it is and uses one of the CPU's debug registers to stop at the write instruction instead (at most three at a time). Useful where code must not change; some anti-tamper protections detect debug registers, though. This is different from the `ptrace` backend, which watches the target data address.

## The Hooks tab

Several hooks can be selected at once (Ctrl/Shift-click): **Pause**, **Debug** and **Remove** act on all of them, **Edit** on one. The columns:

- **Address**: the target – an address, the pointer chain it follows (`↳ …`), *dynamic · N callers* for a hook that changes only the writes of its learned callers, or *dynamic (all writes)* for one that changes every write of its code location. Method hooks show *C# method*.
- **Status**: *Active*, *Paused*, *Training* (waiting for a write, see [after a restart](#after-a-restart)), *Failed* (could not be installed), *Script error* (its script threw), *Removed by game* (the game restored or replaced the code at the write site, see [limitations](#limitations)) or *Disconnected* (its session ended).
- **Applied**: how often the hook ran. With **Hooks: measure their cost** in the [settings](settings.md), its tooltip adds calls per second, skipped writes and the time they cost.
- **Last (old → game ⇒ new)**: the last call as old value → value written by the game ⇒ value stored.

Below the table a log shows what hook scripts write with `ctx.log` or `console.log`, and the errors of hooks, also without debug mode; at most 100 lines per 0.1 s, more are counted as dropped. With the `ptrace` backend it shows only the errors (see the [hook context](../reference/hook-context.md)).

For Frida hooks, when a hook is removed, when you select another process and when Grembyte exits, the original code bytes are restored. `ptrace` hooks do not patch game code.

## Debugging hooks

**Debug** puts the selected hooks into debug mode and opens the debug window. It logs every call with time, thread, address, caller, old value, game value, result, `ctx.log` output and errors with their stack. Writes the hook skips appear too, with the reason, such as "writes another address" or "caller not learned".

<figure markdown="span">
  ![Hook debug window](../assets/screenshots/hook-debug-dark.webp#only-dark)
  ![Hook debug window](../assets/screenshots/hook-debug-light.webp#only-light)
</figure>

Debug mode costs time in the game; with very many calls the agent drops entries and only counts them. Closing the window ends debug mode.

## After a restart

Module-based hooks are stored per program as module + offset of the write instruction. When the program is attached again after a restart, Grembyte reinstalls them if the bytes at that location still match. If they changed, e.g. after a game update, the hook is not installed.

If the write instruction lies in no module – e.g. in Mono/Unity JIT code, which is generated anew and elsewhere at every start – it depends on the target:

- **With a pointer chain** the hook is stored with the bytes of the write instruction and the surrounding code. In Mono games it also remembers the C# method of the write: after a restart Mono compiles that method and the hook is installed in it right away. Otherwise, or if that fails, the hook is in **Training**: Grembyte watches the chain's target with a hardware watchpoint of its own and installs the hook at the first write from matching code. It takes effect from the next write on. The tooltip of the status column shows how many writes from other code were discarded. At most three hooks can train at a time; hooks installed as a hardware breakpoint take one of these three slots each.
- **JIT code without a pointer chain** cannot be stored as a value hook, because its write site and target cannot be found again after a restart.

The original target address is no longer valid after a restart. How the hook recognises the right writes depends on how it was created:

- **With a pointer chain**: the hook resolves the chain at every write and only acts on the address it leads to at that moment. This is the most reliable variant.
- **Without a pointer chain on a module-based write site**: while running, the hook learns the callers (with **Caller levels** `off` it learns none: "Callers are not learned"), i.e. the return addresses on the stack, that write the target address. After a restart it only acts on writes from these callers. More caller levels help when the player and other objects are changed through the same helper function. If a learned caller also writes other addresses, Grembyte points it out.

## How a hook works

### Frida backend

Grembyte replaces the write instruction with a jump to the hook, which runs right after the game's write and stores the result. Since the hook only works with the old and the new value, it does not matter how the game writes (`mov`, `add`, `sub` …). If the same instruction also writes other objects, e.g. the enemies' gold, the hook only acts on its target: the watched address, the address of its pointer chain or, after a restart without a chain, the learned callers. Several hooks can share a write instruction; they run in the order they were created.

The built-in templates cost a fraction of a microsecond per write. Scripts of your own, debug mode and caller learning run in JavaScript, one call at a time: a slow script can make the game drop frames. The [implementation notes](../development/implementation-notes.md#write-hooks) describe the details.

### ptrace backend

`ptrace` does not patch the write instruction. It sets a hardware data watchpoint on the target address. After the game stores a value, the thread stops; Grembyte reads the new value, runs the transform in its local JavaScript engine, and writes the result back before resuming the thread. The game's unmodified value is briefly present in memory.

This backend watches addresses rather than instructions: shared code is distinguished by the watched address, and a hook needs a fixed address or a pointer chain. It supports at most four distinct watched addresses and does not provide JIT training, caller learning or per-call hook debug events. See the [backend comparison](write-watch.md#backends-frida-or-ptrace).

## Limitations

- x86-64 code only.
- The write instruction and the bytes up to the length of a jump (5 bytes) are overwritten. If other code jumps into the middle of this range, the game crashes; Frida hooks and Cheat Engine's code injection carry the same risk.
- If the write sits in a tiny function with less than 5 bytes before the next one, the setting *Hooks: short jump in tight spots* overwrites only 2 bytes with a short jump to `int3` padding nearby. The padding is found by a heuristic; if it guesses wrong, the game crashes.
- If the connection to Grembyte breaks off unexpectedly while a hook is active, the game may crash.
- `int64` and `uint64` values are exact in manipulations only up to 2^53.
- Some games repair their code. The setting *Hooks: patch hooks the game removed again* reinstalls them (e.g. for Dark Souls III); see the [warning about anti-tamper systems](../index.md).

!!! tip "Hooks on methods"
    In Unity and Unreal games you can also hook a whole method instead of a single write: change its arguments, the fields of its object, or its result. See [method hooks](../engines/index.md#method-hooks).
