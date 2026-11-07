# Write and read watch

The address watch answers the question *which code changes this value?* It sets a hardware watchpoint on the address: the processor stops the game for a moment after every write (or every access), and Hermes records the instruction responsible.

<figure markdown="span">
  ![Write watch](../assets/screenshots/write-watch-dark.webp#only-dark)
  ![Write watch](../assets/screenshots/write-watch-light.webp#only-light)
  <figcaption>The increase and the decrease of the gold each show up as a row of their own; a third location only reads it.</figcaption>
</figure>

## Starting a watch

Right-click a tracked address or a scan result:

- **Address watch (writes)** reports every instruction that writes the address.
- **Address watch (all accesses)** also reports instructions that read it. x86 watchpoints trap on writes or on any access, there is no read-only mode; Hermes decodes each instruction to tell whether it reads, writes or both.

Then do in the game what changes the value. Each code location gets one row, with how often it hit and the bytes it left at the address. The columns **Reads** and **Writes** show what the row's instruction does with the value. In Mono games a hit in JIT code also names its C# method; in IL2CPP games code in `GameAssembly` is named as the C# method it belongs to.

Select a writing row and click **Create hook** to [hook](hooks.md) it. Hooks can only be set on writing instructions.

## Backends: Frida or ptrace

The watch (and hooks) run through Frida by default. **Settings → Backend (watch & hooks)** switches to ptrace, which needs no injection into the game:

| | Frida | ptrace (Linux only) |
| --- | --- | --- |
| Injection into the game | yes | no |
| Addresses at once | several | at most 4 (debug registers) |
| Hook training and caller learning | yes | no |
| 32-bit Wine/Proton games | – | chosen automatically |

The status bar shows the backend in use.

!!! warning "Anti-tamper and debug registers"
    Some anti-tamper protections detect the debug registers that watchpoints use; Dark Souls III crashed. Hermes points it out when a game ends shortly after debug registers were used.
