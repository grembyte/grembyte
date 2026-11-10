# Unity (Mono)

Unity games built with Mono ship their code as .NET assemblies (`Assembly-CSharp.dll` and others) and compile it at run time. Hermes talks to the Mono runtime in the game – `libmono`/`mono-2.0-bdwgc.dll` – through its exported API, natively on Linux and under Wine/Proton on Windows games.

Everything in the [engine overview](index.md) is available:

- The **catalog** lists the classes and methods of the game's assemblies, also offline for games read before.
- **Assemblies …** limits the catalog and the trace to chosen assemblies.
- The **trace** finds the methods a scenario calls, with preselection filters. Mono compiles every traced method first, which takes a moment for large selections; from a configurable number of methods Hermes asks before it starts.
- The **method monitor** logs calls with arguments and results; **method hooks** change arguments, fields or results with templates of their own.
- The **call graph** shows which methods a method calls and which call it, from the IL code.
- The **object inspector** shows the fields of C# objects, and **Call method** runs a static method on the game's main thread.
  An enum argument is chosen from the enum's names, e.g. `StrongholdAdventure.Type`: `Minor (1)`, `Average (2)` …; a number can be typed in, too. Nested classes read as C# writes them (`StrongholdAdventure.Type`), not as Mono does (`StrongholdAdventure/Type`).

!!! tip "Try it on HermesQuest"
    HermesQuest is a small Unity Mono role-playing game made for trying out Hermes – the running example of the [Examples](../examples/index.md) section. Download it for [Linux](https://github.com/winterhafen/hermes-hook/releases/download/v0.5.0-alpha/HermesQuest-RPG-Linux) or [Windows](https://github.com/winterhafen/hermes-hook/releases/download/v0.5.0-alpha/HermesQuest-RPG-Windows.zip) (also for Wine/Proton); both are zip archives. The examples walk through scans, pointer scans, value and method hooks and scripts on it.

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

Pillars of Eternity, Pillars of Eternity II, Kathy Rain 2 (Proton) and Shadowrun Returns (32-bit, Proton).

<small>The screenshots of Pillars of Eternity on this page only illustrate how Hermes Hook works. All game names, trademarks and game content belong to their respective owners. Hermes Hook is not affiliated with, endorsed or sponsored by them.</small>
