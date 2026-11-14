# Engines

Memory scans, the write watch, hooks and the native tools work with any game. For games built on **Unity** (Mono or IL2CPP) and **Unreal Engine**, Grembyte also reads the engine's own metadata: the classes and methods of the game, the fields of its objects. That opens a second way in: instead of searching for a value and the instruction that writes it, you find the method that does the work and change its arguments or result.

Grembyte recognises the engine when it attaches; the status bar names it.

## Grembyte tools per engine

Which of Grembyte's engine tools are available for games built on each engine. The table is about Grembyte, not about the features of the engines themselves.

| Grembyte tool | Unity (Mono) | Unity (IL2CPP) | Unreal Engine |
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
| [Object browser](../guide/object-browser.md) | :material-check: | – | :material-check: |
| [Engine names](../guide/code-explorer.md#names-from-the-engine) in the code explorer | – | :material-check: | :material-check: |
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

**View → Trace …** finds the methods that a scenario in the game calls – *which method runs when I buy something?* It hooks the methods of the chosen assemblies and works like the [native trace](../guide/native-tools.md): a **baseline** while the game idles, then the **scenario**; the methods that are new or called more often in the scenario are the candidates. The trace can be [docked](../guide/tools.md#docking-windows) at the bottom of the main window.

1. Choose the methods. The search text keeps those whose class, method or assembly contains every word typed, e.g. `inventory gold`; empty means all methods of the game's assemblies (without `mscorlib`, `System`, `UnityEngine` and the like). The **preselection** narrows this further: filters such as *Money*, *Inventory*, *Character stats*, *Combat* or *Name sounds like a change* pick the methods whose names sound like game values. Ticked filters combine – a method has to match one of them. The window shows how many methods are chosen.
2. **Set hooks**. Mono compiles every chosen method first; from a configurable number Grembyte asks before it starts (see [settings](../guide/settings.md)). Under Wine/Proton a trace is limited, by default to 5000 methods.
3. **Record baseline** for a few seconds while nothing special happens in the game, then **Stop baseline**.
4. **Record scenario** while you trigger the action, e.g. spend gold, then **Stop scenario**.
5. The table lists the methods with their calls, calls per second, the rate in the baseline and a verdict: *new*, *2.5× as often* or *like baseline*. **Notable methods only** keeps those that did not run in the baseline or ran clearly more often. Below, the call sequence shows the scenario's calls in order, with thread and caller, without the methods of the baseline.
6. **Monitor** (or a double click) hands the selected method to the [method monitor](#method-monitor). **Remove hooks** ends the trace.

The filters above the table narrow the results: by name, and by number of calls – `3` exactly three, `2-10` a range, `>5`, `%3` multiples of three, several separated by commas. Trigger the action three times in the scenario, and `%3` finds the methods that ran once for each.

**Preselection filters** are kept in Grembyte and can be changed: **New …**, **Edit …** (or a double click) and **Delete**, **Tick all** and **Untick all**. A filter has

- **Words**: a method matches when one of them matches a part of its class or method name. Names are split at capitals, `_`, `.` and digits (`AddGold` is *add* + *gold*); a short word also matches its plural (`item`: `Items`), a longer one also the start of a part (`inventory`: `InventorySlot`), so `gold` finds `AddGold` but not `Marigold`.
- **Except**: words that must not match.
- **Number as parameter or result**, and **Name sounds like a change** (property setters `set_…` and names starting with *Add*, *Set*, *Remove*, *Spend*, *Gain*, *Take* …).

The dialog previews the methods a filter matches. **Include generated code** adds the methods the compiler made (lambdas, closures, iterators), which are rarely wanted but many.

What the trace cannot see:

- **Mono** often builds small methods right into their callers (inlining). Such calls never reach a hook, neither in the trace nor in the method monitor.
- **Unreal**: the trace sees calls from Blueprints and through `ProcessEvent` (events, delegates). C++ calling C++ directly is only seen by the [native trace](../guide/native-tools.md); a Blueprint function calling another Blueprint function directly is not seen.

<figure markdown="span">
  ![Trace](../assets/screenshots/trace-dark.webp#only-dark)
  ![Trace](../assets/screenshots/trace-light.webp#only-light)
</figure>

## Method monitor

The method monitor logs the calls of chosen methods with their arguments, the result and the object they run on. It can be [docked](../guide/tools.md#docking-windows) at the bottom of the main window.

It shares the limits of the trace: calls Mono has inlined are missing. In Unreal games, when a Blueprint calls a native function, the arguments come from the Blueprint's bytecode: variables and constants are shown, other arguments as `?`.

<figure markdown="span">
  ![Method monitor](../assets/screenshots/method-monitor-dark.webp#only-dark)
  ![Method monitor](../assets/screenshots/method-monitor-light.webp#only-light)
</figure>

## Favorites

Methods and functions found in the catalog, a trace or the [native trace](../guide/native-tools.md) can be marked with ★ in the first column. **View → Favorites …** lists them per program with a note, and hands them to the [method monitor](#method-monitor) or the [native function monitor](../guide/native-tools.md#native-function-monitor). Favorites are saved at once, so they survive a frozen or crashed game.

<figure markdown="span">
  ![Favorites](../assets/screenshots/favorites-dark.webp#only-dark)
  ![Favorites](../assets/screenshots/favorites-light.webp#only-light)
</figure>

## Object inspector

The object inspector shows the fields of an object – for Unity also static fields and properties, for Unreal the UProperties of objects and structs – evaluated on the game's main thread. Open it from an object in the monitor, a script result or the [object browser](../guide/object-browser.md). The filter above the fields keeps those whose name or type contains every word typed, e.g. `health` among the hundreds of fields of an Unreal player character.

- **Fields**: their current values; **open** shows an object field in an inspector of its own. **Live** reads them again twice a second. **Add to address list** in a number field's context menu puts it on the [tracked addresses](../guide/addresses.md), to watch, change or freeze it there.
- **Getter methods – called in the game**: the object's properties (C# properties such as `IsPartyMember`) and, for GameObjects, its components. Their value is not necessarily stored in a field, so they are called in the game: **evaluate** one, or **Evaluate all**. A property with a setter can be changed with **set …**, which runs the setter on the game's main thread.
- **Methods – called in the game**: the object's instance methods (Unreal: its UFunctions). **call …** asks for the arguments and runs the method on this object. **Base classes** adds the inherited methods, often hundreds for Unity components. In Unity Mono this is how instance methods are called; [Call method](#call-method) runs static ones.
- The arrow in a section's header folds it; inspectors remember which sections are folded.
- **References** (Unity Mono and Unreal) opens the object's [reference graph](../guide/reference-graph.md): which objects hold it, and which hold those, back to a static field or a module's global data – or, switched over, which objects it points to.

Inspectors can be [docked](../guide/tools.md#docking-windows) beside the scanner; an object opened from a docked inspector opens as a tab beside it.

**Save …** (as HTML) keeps an object for later, also after the game has freed it. It first calls all of the object's getters in the game, then writes a self-contained HTML page with its class, name and address, its fields (name, type, value, offset, address), the getters' values and the instance methods with those of its base classes, plus the program and the time. A getter that throws shows its error. The values are those of that moment; the page is a record, not a way back to the object. For an inspector of a recorded call the fields are those of the call. The file dialog starts in the folder of the last export (see [Saving, export and import](../guide/data.md#where-exports-go)).

<figure markdown="span">
  ![Object inspector](../assets/screenshots/object-inspector-dark.webp#only-dark)
  ![Object inspector](../assets/screenshots/object-inspector-light.webp#only-light)
</figure>

## Method hooks

A method hook changes a method's arguments or the fields of its object before it runs, or its result after it. Like value hooks, method hooks use [templates](../guide/templates.md) – their script sees the method's arguments and `ctx.this` – and they are stored per program and reinstalled at the next start.

Method hook scripts can reach other game code: `ctx.call` calls a static method, `ctx.static` reads static fields, `ctx.find("ClassName")` gives a live object of a class (in Unity games only Unity objects: GameObjects and components).

## Call method

**Call method** runs a method of the game on its main thread with arguments you enter, and shows the result. See the engine pages for what each engine supports.

<figure markdown="span">
  ![Call method](../assets/screenshots/method-call-dark.webp#only-dark){ width="640" }
  ![Call method](../assets/screenshots/method-call-light.webp#only-light){ width="640" }
</figure>

## Call graph

For Mono games the call graph shows which methods a method calls and which call it, read from the IL code, drawn like the [reference graph](../guide/reference-graph.md). Open it with **Calls …** on a method of the catalog. The method stands in the middle, the methods it calls to its right, in the order of the code, and the methods calling it to its left.

- **What it calls** comes from the method's IL code. For a virtual method or an interface it names the method the code was compiled against, not the override that runs; async methods and iterators are followed into the code the compiler made for them (labelled `async`). Edges of calls other than plain calls say how: `newobj` (a constructor), `ldftn` (a delegate or lambda) …
- **Who calls it**: **Search callers** reads the code of the game's assemblies (or the [assembly selection](#assemblies)) the first time, later from the cache; from then on every method put in the middle shows its callers at once. A method without callers there may still be called through delegates, reflection, virtually or by the engine.
- **Double-click** a method to unfold it the way it stands: a method on the right shows what it calls, one on the left its callers, the method in the middle its callers. Double-click it again to fold that in. A **+** on a side means there is more to unfold that way.
- **Framework methods** (`mscorlib`, `UnityEngine` …) are left out unless ticked; they count in a method's **+n**, as do the methods beyond [Settings](../guide/settings.md) → *Graphs* → *Nodes shown per unfolding at most* (100 by default).
- The **context menu of a method**: show or fold in its callers and the methods it calls, **Start from this method** (it moves to the middle; **Back** and **Forward** return), **Monitor**, **Copy signature**, **Highlight the way to the method in the middle**, **Hide** and **Hide all *Class***. The background's: **Fit**, **Clear the highlight**, **Show hidden (n)** and **Export as HTML**.
- **Ctrl + mouse wheel** zooms, dragging the background moves the graph. **Copy signature** and **Monitor** below act on the method selected, else on the one in the middle.

<figure markdown="span">
  ![Call graph](../assets/screenshots/call-graph-dark.webp#only-dark)
  ![Call graph](../assets/screenshots/call-graph-light.webp#only-light)
</figure>
