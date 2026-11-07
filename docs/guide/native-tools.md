# Native trace and monitor

When a game offers no engine metadata – or the logic you look for is plain C++ – the native tools work on machine code directly, for any engine.

## Native trace

**View → Native trace …** answers *which functions run when I do this?* It uses Frida's Stalker to count the functions the chosen threads call:

1. Choose the threads and modules to follow. Preselected are the threads active right now and the modules in the game's folder.
2. **Start following**: Stalker recompiles the code of these threads and counts every function call. System libraries run unchanged; only calls into them are counted.
3. Record a **baseline** while the game idles.
4. Record a **scenario** while doing the thing you are interested in, e.g. paying gold.
5. The functions that are new in the scenario, or called more often there, are the candidates.

The game runs noticeably slower while it is followed.

<figure markdown="span">
  ![Native trace](../assets/screenshots/native-trace-dark.webp#only-dark)
  ![Native trace](../assets/screenshots/native-trace-light.webp#only-light)
  <figcaption>Candidates after a baseline and a scenario, with how often each ran.</figcaption>
</figure>

Candidates can be marked with ★ as [favorites](tools.md#favorites) or handed to the monitor.

!!! warning
    Stalker rewrites the code of the traced threads while it runs. Hermes asks before it starts, since this can slow down or, rarely, crash a game.

## Native function monitor

**Monitor native function …** logs the calls of one function: its arguments, the result and the caller. For pointer arguments it compares the memory behind them before and after the call, so you see what the function changed.

| | Arguments | Result |
| --- | --- | --- |
| Linux, x86-64 | `rdi`, `rsi`, `rdx`, `rcx`, `r8`, `r9` | `rax` |
| Windows and Wine/Proton, x86-64 | `rcx`, `rdx`, `r8`, `r9`, then the stack | `rax` |
| 32-bit programs | the stack | `eax` |

On x86-64 the first argument of a C++ method is `this`, the object it runs on. Floating-point arguments and writes to memory that no argument points to are not visible.

<figure markdown="span">
  ![Native monitor](../assets/screenshots/native-monitor-dark.webp#only-dark)
  ![Native monitor](../assets/screenshots/native-monitor-light.webp#only-light)
</figure>

Open it from a candidate of the native trace, from a favorite, or with an address you type in.
