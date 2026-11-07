# Unity (Mono)

Unity games built with Mono ship their code as .NET assemblies (`Assembly-CSharp.dll` and others) and compile it at run time. Hermes talks to the Mono runtime in the game – `libmono`/`mono-2.0-bdwgc.dll` – through its exported API, natively on Linux and under Wine/Proton on Windows games.

Everything in the [engine overview](index.md) is available:

- The **catalog** lists the classes and methods of the game's assemblies, also offline for games read before.
- **Assemblies …** limits the catalog and the trace to chosen assemblies.
- The **trace** finds the methods a scenario calls, with preselection filters. Mono compiles every traced method first, which takes a moment for large selections; from a configurable number of methods Hermes asks before it starts.
- The **method monitor** logs calls with arguments and results; **method hooks** change arguments, fields or results with templates of their own.
- The **call graph** shows which methods a method calls and which call it, from the IL code.
- The **object inspector** shows the fields of C# objects, and **Call method** runs a static method on the game's main thread.

## Mono and value hooks

Code compiled by Mono lies in no module and moves with every start. Hermes therefore names a hit of the [write watch](../guide/write-watch.md) in JIT code by its C# method, and a [hook](../guide/hooks.md) with a pointer chain remembers that method: after a restart Mono compiles it and the hook is installed in it right away.

!!! screenshot "Screenshot to add: a real Mono game"
    The catalog or the trace on a real Unity Mono game, e.g. Pillars of Eternity under Proton, with a method like `PlayerInventory:AddCurrency` found.
    File: `docs/assets/photos/mono-trace-pillars.png`

## Tested games

Pillars of Eternity, Pillars of Eternity II, Kathy Rain 2 (Proton) and Shadowrun Returns (32-bit, Proton).

