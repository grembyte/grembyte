# Write and read watch

The address watch answers the question *which code changes this value?* It sets a hardware watchpoint on the address: the processor stops the game for a moment after every write (or every access), and Grembyte records the instruction responsible.

<figure markdown="span">
  ![Write watch](../assets/screenshots/write-watch-dark.webp#only-dark)
  ![Write watch](../assets/screenshots/write-watch-light.webp#only-light)
  <figcaption>The increase and the decrease of the gold each show up as a row of their own; a third location only reads it.</figcaption>
</figure>

## Starting a watch

Right-click a tracked address or a scan result:

- **Address watch (writes)** reports every instruction that writes the address.
- **Address watch (all accesses)** also reports instructions that read it. x86 watchpoints trap on writes or on any access, there is no read-only mode; Grembyte decodes each instruction to tell whether it reads, writes or both.

Then do in the game what changes the value. Each code location gets one row, with how often it hit and the bytes it left at the address. The columns **Reads** and **Writes** show what the row's instruction does with the value. In Mono games a hit in JIT code also names its C# method; in IL2CPP games code in `GameAssembly` is named as the C# method it belongs to.

**Called from** names the code that called the function the row's instruction lies in: the most frequent caller, with `(+N)` when there are more; the tooltip lists all of them with their hits. Grembyte finds the caller on the stack the way [hooks learn callers](hooks.md#after-a-restart); with the ptrace backend the column stays empty. A caller marked `(?)` could not be checked, e.g. when the function was reached through a wrapper that jumps to it: it is the nearest return address on the stack and may be a stale one.

Some writes happen in a copy routine of the C runtime: `memcpy`, `memmove` or `memset` in `VCRUNTIME140.dll`, `ucrtbase.dll`, `msvcrt.dll`, `ntdll.dll` or `libc`. Grembyte marks such a row, e.g. `VCRUNTIME140.dll+0x1dc1e (memcpy)`, and does not offer a hook for it: the routine only copies a value computed elsewhere, the whole game copies memory through it, and its code is usually too short for a hook. The caller in **Called from** is where the value comes from; look at its code or search for the object the value belongs to.

Select a writing row and click **Create hook …** to [hook](hooks.md) it. Hooks can only be set on writing instructions. **Monitor location** hands the row's code location to the [native function monitor](native-tools.md#native-function-monitor): the registers at every pass, and if wanted the function it lies in. **Stop monitoring** ends the watch.

The watch can be [docked](tools.md#docking-windows) beside the scanner, so it stays in view while you play.

Hooks and [scripts](scripts.md) keep working while a watch is set: a game method a script calls, or a value it reads or writes itself (`ptr.writeS32(…)`), runs to its end, and the watch lists the access like any other.

## Backends: Frida or ptrace

The watch (and hooks) run through **Frida (injection)** by default. **Settings → Backend (watch & hooks)** switches to **ptrace (debugger)**, which needs no injection into the game:

| | Frida | ptrace (Linux only) |
| --- | --- | --- |
| Injection into the game | yes | no |
| Addresses at once | several | at most 4 (debug registers) |
| Hook training and caller learning | yes | no |
| 32-bit Wine/Proton games | – | chosen automatically |

The status bar shows the backend in use.

With `ptrace`, a watchpoint can cover 1, 2, 4 or 8 bytes only when its address is aligned to that size. Grembyte rejects an unaligned address instead of setting the watchpoint.

!!! warning "Anti-tamper and debug registers"
    Some anti-tamper protections detect the debug registers that watchpoints use; Dark Souls III crashed. Grembyte points it out when a game ends shortly after debug registers were used.
