# Engines

Memory scans, the write watch, hooks and the native tools work with any game. For games built on **Unity** (Mono or IL2CPP) and **Unreal Engine**, Hermes also reads the engine's own metadata: the classes and methods of the game, the fields of its objects. That opens a second way in: instead of searching for a value and the instruction that writes it, you find the method that does the work and change its arguments or result.

Hermes recognises the engine when it attaches; the status bar names it.

## What each engine offers

| Tool | Unity (Mono) | Unity (IL2CPP) | Unreal Engine |
| --- | :---: | :---: | :---: |
| [Catalog](#catalog) of classes and methods | :material-check: | :material-check: | :material-check: |
| [Assemblies / packages](#assemblies) | :material-check: | :material-check: | :material-check: |
| [Trace](#trace) | :material-check: | :material-check: | :material-check: |
| [Method monitor](#method-monitor) | :material-check: | :material-check: | :material-check: |
| [Object inspector](#object-inspector) | :material-check: | :material-check: | :material-check: |
| [Method hooks](#method-hooks) | :material-check: | :material-check: | :material-check: |
| [Call method](#call-method) | :material-check: | – | :material-check: |
| [Call graph](#call-graph) | :material-check: | – | – |
| [Scripts](../guide/scripts.md) | :material-check: | :material-check: | :material-check: |
| [GAS attributes](unreal.md#gas-attributes) | – | – | :material-check: |

The engine pages describe the details: [Unity (Mono)](unity-mono.md), [Unity (IL2CPP)](unity-il2cpp.md), [Unreal Engine](unreal.md).

## Catalog

**View → Catalog …** lists the game's classes and methods, grouped by assembly (Unity) or package (Unreal), with a filter. Methods carry their signature, e.g. `Ns.Class:Method (int,single)`. From a method the context menu leads to the monitor, a method hook, the call dialog, the call graph or a favorite (★).

<figure markdown="span">
  ![Catalog](../assets/screenshots/catalog-dark.webp#only-dark)
  ![Catalog](../assets/screenshots/catalog-light.webp#only-light)
</figure>

A catalog read once is cached; **View → Catalog (offline) …** opens it without the game running.

## Assemblies

**View → Assemblies …** limits the catalog and the trace to chosen assemblies (Unreal: packages). The choice is saved per program. Without a choice both use all of the game's assemblies, and leave out those of the engine and the framework.

<figure markdown="span">
  ![Assemblies](../assets/screenshots/assemblies-dark.webp#only-dark){ width="560" }
  ![Assemblies](../assets/screenshots/assemblies-light.webp#only-light){ width="560" }
</figure>

## Trace

**View → Trace …** finds the methods that a scenario in the game calls – *which method runs when I buy something?* It hooks the methods of the chosen assemblies and works like the [native trace](../guide/native-tools.md): a **baseline** while the game idles, then the **scenario**; the methods that are new or called more often in the scenario are the candidates. Preselection filters keep large games manageable.

What the trace cannot see:

- **Mono** often builds small methods right into their callers (inlining). Such calls never reach a hook, neither in the trace nor in the method monitor.
- **Unreal**: the trace sees calls from Blueprints and through `ProcessEvent` (events, delegates). C++ calling C++ directly is only seen by the [native trace](../guide/native-tools.md); a Blueprint function calling another Blueprint function directly is not seen.

<figure markdown="span">
  ![Trace](../assets/screenshots/trace-dark.webp#only-dark)
  ![Trace](../assets/screenshots/trace-light.webp#only-light)
</figure>

## Method monitor

The method monitor logs the calls of chosen methods with their arguments, the result and the object they run on.

It shares the limits of the trace: calls Mono has inlined are missing. In Unreal games, when a Blueprint calls a native function, the arguments come from the Blueprint's bytecode: variables and constants are shown, other arguments as `?`.

<figure markdown="span">
  ![Method monitor](../assets/screenshots/method-monitor-dark.webp#only-dark)
  ![Method monitor](../assets/screenshots/method-monitor-light.webp#only-light)
</figure>

## Object inspector

The object inspector shows the fields of an object – for Unity also static fields and properties, for Unreal the UProperties of objects and structs – evaluated on the game's main thread. Open it from an object in the monitor or a script result; fields that hold a value can be handed to the tracked addresses.

<figure markdown="span">
  ![Object inspector](../assets/screenshots/object-inspector-dark.webp#only-dark)
  ![Object inspector](../assets/screenshots/object-inspector-light.webp#only-light)
</figure>

## Method hooks

A method hook changes a method's arguments or the fields of its object before it runs, or its result after it. Like value hooks, method hooks use [templates](../guide/templates.md) – their script sees the method's arguments and `ctx.this` – and they are stored per program and reinstalled at the next start.

Method hook scripts can reach other game code: `ctx.call` calls a static method, `ctx.static` reads static fields, `ctx.find("ClassName")` gives a live object of a class.

## Call method

**Call method** runs a method of the game on its main thread with arguments you enter, and shows the result. See the engine pages for what each engine supports.

<figure markdown="span">
  ![Call method](../assets/screenshots/method-call-dark.webp#only-dark){ width="640" }
  ![Call method](../assets/screenshots/method-call-light.webp#only-light){ width="640" }
</figure>

## Call graph

For Mono games the call graph shows which methods a method calls and which call it, read from the IL code.

<figure markdown="span">
  ![Call graph](../assets/screenshots/call-graph-dark.webp#only-dark)
  ![Call graph](../assets/screenshots/call-graph-light.webp#only-light)
</figure>
