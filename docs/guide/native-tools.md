# Native trace and monitor

When a game offers no engine metadata – or the logic you look for is plain C++ – the native tools work on machine code directly, for any engine.

## Native trace

**View → Native trace …** answers *which functions run when I do this?* It uses Frida's Stalker to count the functions the chosen threads call:

1. Record a **baseline** while the game idles.
2. Record a **scenario** while doing the thing you are interested in, e.g. paying gold.
3. The functions that are new in the scenario, or called more often there, are the candidates.

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

<figure markdown="span">
  ![Native monitor](../assets/screenshots/native-monitor-dark.webp#only-dark)
  ![Native monitor](../assets/screenshots/native-monitor-light.webp#only-light)
</figure>

Open it from a candidate of the native trace, from a favorite, or with an address you type in.
