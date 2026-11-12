# Unity (Mono)

Unity games built with Mono ship their code as .NET assemblies (`Assembly-CSharp.dll` and others) and compile it at run time. Hermes talks to the Mono runtime in the game – `libmono`/`mono-2.0-bdwgc.dll` – through its exported API, natively on Linux and under Wine/Proton on Windows games.

Everything in the [engine overview](index.md) is available:

- The **catalog** lists the classes and methods of the game's assemblies, also offline for games read before.
- **Assemblies …** limits the catalog and the trace to chosen assemblies.
- The **trace** finds the methods a scenario calls, with preselection filters. Mono compiles every traced method first, which takes a moment for large selections; from a configurable number of methods Hermes asks before it starts.
- The **method monitor** logs calls with arguments and results; **method hooks** change arguments, fields or results with templates of their own.
- The **call graph** shows which methods a method calls and which call it, from the IL code.
- The **object inspector** shows the fields of C# objects, and **Call method** runs a static method on the game's main thread.
- The **object browser** lists the live objects by class (see [below](#object-browser)).
  An enum argument is chosen from the enum's names, e.g. `StrongholdAdventure.Type`: `Minor (1)`, `Average (2)` …; a number can be typed in, too. Nested classes read as C# writes them (`StrongholdAdventure.Type`), not as Mono does (`StrongholdAdventure/Type`).

!!! tip "Try it on HermesQuest"
    HermesQuest is a small Unity Mono role-playing game made for trying out Hermes – the running example of the [Examples](../examples/index.md) section. Download it for [Linux](https://github.com/winterhafen/hermes-hook/releases/download/v0.5.0-alpha/HermesQuest-RPG-Linux) or [Windows](https://github.com/winterhafen/hermes-hook/releases/download/v0.5.0-alpha/HermesQuest-RPG-Windows.zip) (also for Wine/Proton); both are zip archives. The examples walk through scans, pointer scans, value and method hooks and scripts on it.

## Object browser

**View → Object browser …** shows the game's live objects by class, as for [Unreal](unreal.md#object-browser): a package (here: assembly) to focus on, a filter on class and object names, and singletons only. Unity objects show with their name, strings with their text, other C# objects as `Class@address`; a double-click opens one in the [object inspector](index.md#object-inspector). The browser can be [docked](../guide/tools.md#docking-windows) beside the scanner.

Unity's Mono has no heap walk: `mono_gc_walk_heap` belongs to the SGen collector and does nothing in Unity's Boehm GC. Hermes uses Unity's own liveness calculation instead (`mono_unity_liveness_*`, which `Resources.UnloadUnusedAssets` uses). It finds the objects reachable from the static fields and from Unity's objects. Objects only a thread's stack holds are not among them.

- The calculation marks the objects it visits, so the game stands still while it runs. The status line says for how long; **Refresh** reads again.
- *Singleton* names the static field that holds a class's only object, also through a generic base such as `Singleton<T>`. Game classes with exactly one object count as singletons too.

Both generations of the API work: `calculation_begin`/`end` (Unity 4 to 2020, also Unity 4's old `mono.dll`) and `allocate_struct` (Unity 2021 and later), in 64-bit and 32-bit games. Tested in Pillars of Eternity (64-bit) and Might & Magic X – Legacy (Unity 4.2, 32-bit, Proton); the newer API so far against a stand-in runtime only.

## 32-bit games

32-bit Unity games under Wine/Proton work with all Mono tools: catalog, trace, method monitor, object inspector, **Call method** and the object browser. Mono features always go through the Frida gadget; value hooks and the write watch use ptrace in 32-bit games (see [backends](../guide/write-watch.md#backends-frida-or-ptrace)).

## Games that sleep out of focus

Some Unity games stop running frames when their window loses focus (Unity's *Run In Background* switched off), e.g. Might & Magic X – Legacy in window mode. Getters, **Call method**, Unity object names and the object browser run on the game's main thread, which then waits for the next window message instead of running game code. Hermes wakes it with an empty window message and does its work there, so these tools also work while you are in Hermes; the game itself stays paused.

## Mono and value hooks

The [pointer scan](../guide/pointer-scan.md#mono-games-static-fields-as-chain-bases) can start chains in the static fields of the classes in the marked assemblies, which Mono keeps on the heap.

Code compiled by Mono lies in no module and moves with every start. Hermes therefore names a hit of the [write watch](../guide/write-watch.md) in JIT code by its C# method, and a [hook](../guide/hooks.md) with a pointer chain remembers that method: after a restart Mono compiles it and the hook is installed in it right away.

## Example: Pillars of Eternity

Pillars of Eternity (native Linux build) shows what the engine tools do in a real game.

### A cheat function from the catalog

Its assembly keeps a static class `Scripts` with the developers' console commands. Typing `scripts player` into the catalog's filter finds them, among them `GivePlayerMoney (int)`:

<figure markdown="span">
  ![Catalog in Pillars of Eternity](../assets/photos/mono-pillars-catalog-dark.webp#only-dark)
  ![Catalog in Pillars of Eternity](../assets/photos/mono-pillars-catalog-light.webp#only-light)
  <figcaption>The catalog filtered for <code>scripts player</code>: 8 of 30480 methods.</figcaption>
</figure>

A [script](../guide/scripts.md) with a number parameter `amount` calls it. **Copy signature** in the catalog gives the name to paste:

```js
const value = Math.trunc(ctx.args.amount);   // the method takes an int
ctx.call("Scripts:GivePlayerMoney (int)", value);
return "Gold increased by " + value;
```

`ctx.call` ignores the parameter list `(int)`; it picks the method with as many parameters as arguments given. `Math.trunc` cuts off decimals that someone may type into the number field.

<figure markdown="span">
  ![Script giving money in Pillars of Eternity](../assets/photos/mono-pillars-script-money-dark.webp#only-dark)
  ![Script giving money in Pillars of Eternity](../assets/photos/mono-pillars-script-money-light.webp#only-light)
  <figcaption>The script's result in the log, and the game's own message: the money went through the game's code.</figcaption>
</figure>

### A method hook on the damage

Every hit in Pillars of Eternity goes through `Health:DoDamage(DamageInfo damage, GameObject attacker)`. The [method monitor](index.md#method-monitor) shows a call with its arguments: the object hit (`this`), the `DamageInfo` and the attacking GameObject. **Object details** opens each in the object inspector, where the field `m_damageBase` of `DamageInfo` turns up.

A method hook **before the call** changes that field before the game uses it. The attacker is a GameObject; `component("CharacterStats")` gives its component with the character's stats, whose `IsPartyMember` tells the party from its enemies:

```js
const stats = ctx.args.attacker && ctx.args.attacker.component("CharacterStats");
const base_old = ctx.args.damage.m_damageBase;

if (stats && !stats.IsPartyMember)
  ctx.args.damage.m_damageBase *= 0.3;   // enemies deal 30 %

if (stats && stats.IsPartyMember)
  ctx.args.damage.m_damageBase *= 20;    // the party deals 20 times the damage

ctx.log("Damagebase old/new:", base_old, "/", ctx.args.damage.m_damageBase);
```

`ctx.args.attacker && …` guards against hits without an attacker (traps, falling), where the argument is `null`.

=== "1. Monitor"

    <figure markdown="span">
      ![Method monitor on Health:DoDamage](../assets/photos/mono-pillars-dodamage-monitor-dark.webp#only-dark)
      ![Method monitor on Health:DoDamage](../assets/photos/mono-pillars-dodamage-monitor-light.webp#only-light)
      <figcaption>One call of <code>Health:DoDamage</code>: a companion hits a forest troll for 4.78.</figcaption>
    </figure>

=== "2. Hook"

    <figure markdown="span">
      ![Method hook on Health:DoDamage](../assets/photos/mono-pillars-dodamage-hook-definition-dark.webp#only-dark)
      ![Method hook on Health:DoDamage](../assets/photos/mono-pillars-dodamage-hook-definition-light.webp#only-light)
      <figcaption>The hook: template <b>Method script</b>, before the call.</figcaption>
    </figure>

=== "3. In the game"

    <figure markdown="span">
      ![The hook in the game](../assets/photos/mono-pillars-dodamage-hook-running-dark.webp#only-dark)
      ![The hook in the game](../assets/photos/mono-pillars-dodamage-hook-running-light.webp#only-light)
      <figcaption>The hook log shows the base damage raised from 8.07 to 161.46; the game reports a critical hit for 469.</figcaption>
    </figure>

The [HermesQuest examples](../examples/hermesquest-hooks.md#method-hooks) explain method hooks step by step.

## Tested games

Pillars of Eternity, Pillars of Eternity II, Kathy Rain 2 (Proton), Shadowrun Returns (32-bit, Proton) and Might & Magic X – Legacy (Unity 4.2, 32-bit, Proton).

<small>The screenshots of Pillars of Eternity on this page only illustrate how Hermes Hook works. All game names, trademarks and game content belong to their respective owners. Hermes Hook is not affiliated with, endorsed or sponsored by them.</small>
