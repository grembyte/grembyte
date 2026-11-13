# Reference graph

The [object inspector](../engines/index.md#object-inspector) shows what an object holds. The reference graph shows the other direction: which objects hold this one, which objects hold those, and so on back to a *root* that keeps the whole chain alive. This answers questions such as "where does the game keep its `GameManager`?" or "which list is this enemy in?", and it leads to a static field or a module's global data, a good starting point for a [pointer chain](pointer-scan.md).

It is available for [Unity Mono](#unity-mono) and [Unreal Engine](#unreal-engine) games. Open it from an object:

- **References** in the object inspector's toolbar, or
- **Show references** in the context menu of an object in the [object browser](object-browser.md).

## Reading the graph

The object you started from is on the right. Each column to the left is one step further away: the objects holding it, the objects holding those, and so on. An arrow points from the holder to the object it holds and is labelled with the field it is held in:

- a field: `_gameManager`, a struct's member as `stats.owner`;
- an array element: `[3]`, an element of an array of structs as `[1422].value`;
- in Unreal games also `OuterPrivate`, the object an object belongs to.

An object can also hold one in its own column, e.g. an array that holds both the start object and another of its holders. Such an arrow loops out to the left of the column; one pointing back to a column further right runs from the holder's left side to the other's right side. Labels that would cover each other move apart.

A node shows the class and the object's name (Unity and Unreal objects, strings) or its address. The boxes stand for different kinds of holders:

| Box | What holds the object |
|---|---|
| Object (plain border) | A field of another object. A **+** on its left means its holders have not been looked up yet; **+n** in its corner that more locations hold it than are shown. |
| **Static field** (green) | A static field of a class, e.g. `GameManager.Instance`: a root that keeps the object alive while the game runs. |
| **Global data** (green) | A module's global data, as `module+0x…`: a root, and a base for a pointer chain that survives a restart. |
| **Stack** (green) | A local variable of a function that is running right now: a root only for as long as the function runs. |
| **Other memory** (dashed) | Memory no object of the engine covers: the engine's native memory (Unity's own objects, which hold their C# components), the element blocks of Unreal's arrays, maps and sets, or a number that only looks like the address. |

Stack and other-memory locations of one object share a node, which counts them; their tooltip lists the first addresses. Other memory is hidden by default and only counts in **+n** (the tooltip says how many); [Settings](settings.md) → *Reference graph: show other memory* shows it.

## Working with it

- **Double-click** an object to look up what holds it. Double-click an object whose holders are already shown, or select it and click **Open in inspector**, to show its fields in the object inspector.
- **Find way to a root** unfolds the objects nearest to the start one after another until one of them is held by a root, and marks the way there in orange. It stops after 200 objects; then unfold by hand where it looks promising.
- **Fit** zooms so that the whole graph fits the window. **Ctrl + mouse wheel** zooms, dragging the background moves the graph.
- The context menu of a node also copies its address, or the name of a static field or the `module+0x…` of global data.
- Lists, events and caches can hold an object hundreds of times, and much of it is often native memory. Up to 5,000 locations are looked at per object and at most 100 holders shown: static fields and global data first, then objects; the stack and other memory are one node each (other memory only if the settings show it). **+n** in a node's corner says how many locations were left out.

Like the other tools that stay in use while you play, the window can be [docked](tools.md#docking-windows) beside the scanner.

## How it works

When the window opens, Grembyte reads all writable memory of the game once and keeps every pointer it finds, the same way the [pointer scan](pointer-scan.md) does. The locations holding an object's address are then found at once, however often you unfold. The engine names each location: the object it lies in and the field there, or the class whose static data it is. Locations in a module's data or on a thread's stack are recognized without asking it.

The graph is therefore a **snapshot** of the moment the window opened. Objects created later are missing, and an object may be gone by the time you open it in the inspector. **Read again** reads the memory anew and starts the graph again from the object.

A reference graph shows where an object is *kept*, not who *created* it. The code that called `new`, `Instantiate` or `NewObject` leaves no trace in memory once it has returned. A singleton is often held only by its own static field, so its graph ends after one step.

## Unity Mono

- The objects are the ones the [object browser](object-browser.md#unity-mono) reads with Unity's liveness calculation, so the same applies: **the game stands still briefly** while they are read (at most once a minute), objects only a stack holds are not found, and [Settings](settings.md) → *Object browser: objects read* limits how many are kept.
- **Static fields** are named for the classes with live objects, their base classes (such as `Singleton<T>`) and every class of the game's own assemblies, so a static class without objects is found too. Reading a class's static data does not run its static constructor.
- Many Unity objects are held mainly by Unity's native side, which shows as **Other memory**. Their C# references (events, lists, other components) still show.

Tested in Vampire Survivors (Unity 6, native Linux): from the `GameManager` it finds the static field `VampireSurvivors.Framework.GM.Core`, and from the player's `CharacterController` the way to `PhysicsManager._sInstance`.

## Unreal Engine

- The objects come from the engine's object table (`GUObjectArray`), as in the [object browser](object-browser.md#unreal-engine). Reading it does not stop the game.
- Fields are named from the UProperties of the object's class, structs included. `ClassPrivate` and `OuterPrivate`, which every object has, are named too.
- The elements of `TArray`, `TMap` and `TSet` lie in memory blocks of their own, outside the object. A location there shows as **Other memory** for now.

## Not yet available

- **Unity IL2CPP:** it needs the list of live objects that IL2CPP's object browser does not read yet.
- **Native games:** without an engine's reflection there are no objects or fields to name the locations by; the [pointer scan](pointer-scan.md) finds chains to an address there.
