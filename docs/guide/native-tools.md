# Native trace and monitor

When a game offers no engine metadata – or the logic you look for is plain C++ – the native tools work on machine code directly, for any engine.

## Native trace

**View → Native trace …** answers *which functions run when I do this?* It uses Frida's Stalker to count the functions the chosen threads call:

1. Choose the threads and modules to follow. Preselected are the threads active right now and the modules in the game's folder.
2. **Start following**: Stalker recompiles the code of these threads and counts every function call. System libraries run unchanged; only calls into them are counted.
3. Record a **baseline** while the game idles.
4. Record a **scenario** while doing the thing you are interested in, e.g. paying gold.
5. The functions that are new in the scenario, or called more often there, are the candidates. **Notable functions only** shows just these, **Game code only** hides calls into system libraries. Below, the first calls per thread are listed in their exact order, without the functions of the baseline.

The game runs noticeably slower while it is followed. The native trace can be [docked](tools.md#docking-windows) at the bottom of the main window.

<figure markdown="span">
  ![Native trace](../assets/screenshots/native-trace-dark.webp#only-dark)
  ![Native trace](../assets/screenshots/native-trace-light.webp#only-light)
  <figcaption>Candidates after a baseline and a scenario, with how often each ran.</figcaption>
</figure>

Candidates can be marked with ★ as [favorites](../engines/index.md#favorites) or handed to the monitor. A function found this way can be called from a [script](scripts.md#scripts-in-native-games) through `ctx.native`. The [code explorer](code-explorer.md) shows what it does and who calls it.

!!! warning
    Stalker rewrites the code of the traced threads while it runs. Grembyte asks before it starts, since this can slow down or, rarely, crash a game. Under [Wine and Proton](../wine-proton.md) the native trace is experimental and will probably make the game freeze or crash.

## Native function monitor

**Monitor native function …** logs the calls of one function: its arguments, the result and the caller. For pointer arguments it compares the memory behind them before and after the call, so you see what the function changed. The monitor can be [docked](tools.md#docking-windows) at the bottom of the main window.

| | Arguments | Floating-point arguments | Result |
| --- | --- | --- | --- |
| Linux, x86-64 | `rdi`, `rsi`, `rdx`, `rcx`, `r8`, `r9` | `xmm0`–`xmm7` | `rax`, or `xmm0` |
| Windows and Wine/Proton, x86-64 | `rcx`, `rdx`, `r8`, `r9`, then the stack | `xmm0`–`xmm3` | `rax`, or `xmm0` |
| 32-bit programs | the stack | the stack | `eax` |

These are the [calling conventions](../reference/assembly.md#calling-conventions); on x86-64 the first argument of a C++ method is `this`, the object it runs on. The xmm registers appear in a call's details with both readings, as `float` and as `double`: which one is meant, the signature tells (below). Writes to memory that no argument points to are not visible.

For a function in a module whose file Grembyte can read (x86-64), the column **Signature (guessed)** shows which arguments the function's code reads and what callers do with its result, as the [code explorer](code-explorer.md) guesses them: `void (int rdi)` for a function taking one `int` and returning nothing. With **Guessed signature** ticked, the log shows only those arguments, typed (`int rdi=23`, `float xmm0=1.5`), a `float` or `double` result read from `xmm0`, and no result for a `void` function; unticked, it shows the raw argument registers and `rax`. The [example with the test game](../examples/dummy-game-code-explorer.md#4-what-add_gold-takes) explains how the guess is made and how to check it.

<figure markdown="span">
  ![Native monitor](../assets/screenshots/native-monitor-dark.webp#only-dark)
  ![Native monitor](../assets/screenshots/native-monitor-light.webp#only-light)
</figure>

Open it from a candidate of the native trace, from a favorite, or with a location you type in: an address (`0x55aa…`), `module+0x…` (stays the same across restarts) or a symbol name. Before **Add**:

- **Arguments:** how many arguments are read per call. More than the function has does no harm; the rest show random register contents.
- **Compare (bytes):** how many bytes behind each pointer argument are compared before and after the call; `0` turns the comparison off. More finds more fields but costs time for functions called often.
- The kind: **Function start** logs arguments, result, caller and the memory comparison; **Single instruction** only the registers at every pass, for a location inside a function, e.g. from the [write watch](write-watch.md). **Automatic** takes the function start where a symbol begins, else the instruction. Never monitor an address inside a function as a function start: that overwrites a local variable of the game. For such a location **Monitor enclosing function** monitors the function it lies in.

Both options apply to functions added afterwards. **Only calls with changes** shows only calls after which something changed behind a pointer argument. Beyond 200 calls a second, calls are only counted, so that the game stays playable.
