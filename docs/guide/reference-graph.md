# Reference graph

The reference graph shows which objects hold an object, which objects hold those, and so on back to a *root* that keeps the whole chain alive. This answers questions such as "where does the game keep its `GameManager`?" or "which list is this enemy in?", and it leads to a static field or a module's global data, a good starting point for a [pointer chain](pointer-scan.md).

Switched over, it shows the other way: which objects the object points to, which objects those point to, and so on – the [object inspector](../engines/index.md#object-inspector)'s fields as a graph you can follow several steps at once.

It is available for [Unity Mono](#unity-mono) and [Unreal Engine](#unreal-engine) games. Open it from an object:

- **References** in the object inspector's toolbar, or
- **Show references** in the context menu of an object in the [object browser](object-browser.md).

<figure markdown="span">
  ![Reference graph: what holds an object](../assets/screenshots/reference-graph-dark.webp#only-dark)
  ![Reference graph: what holds an object](../assets/screenshots/reference-graph-light.webp#only-light)
  <figcaption>What holds Eder's <code>Health</code> in Pillars of Eternity: the way to the static field <code>PartyMemberAI.PartyMembers</code> found by <b>Find way to a root</b> (orange), and the way from the party bar highlighted from the context menu (lime).</figcaption>
</figure>

## The tool bar

| Control | What it does |
|---|---|
| **Export as HTML** | Saves the graph as it is drawn now as an HTML page (see [Exporting](#exporting)). |
| **What holds it** / **What it points to** | Which way the graph grows from the object (see [The two ways](#the-two-ways)). The active way is highlighted. |
| **Find way to a root** | Only for *What holds it*: unfolds the objects nearest to the start one after another until one of them is held by a root, and marks the way there in orange. It stops after 200 objects; then unfold by hand where it looks promising. |
| **Only singletons** | Shows only objects whose class has exactly one object (see [Only singletons](#only-singletons)). |
| **Read again** | Reads the game's memory anew and starts both graphs again from the object. What you hid stays hidden. |

Everything else is in the context menu (see [Working with it](#working-with-it)).

## Reading the graph

With *What holds it*, the object you started from is on the right. Each column to the left is one step further away: the objects holding it, the objects holding those, and so on. An arrow points from the holder to the object it holds and is labelled with the field it is held in:

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

## The two ways

**What holds it** grows the graph to the left, to the holders, as described above. **What it points to** grows it to the right: the object's fields that point to the start of another object, labelled with the field. The pointers lie in the object's own memory, its size as the engine knows it (in Unreal the class's `PropertiesSize`). A pointer to anything else – a vtable, the element block of an array – is not shown and only counts in **+n**. In Unity Mono games the static fields of the object's class (and of its bases from the game's assemblies) are followed too: each shows as its green **Static field** node between the object and what it points to. In Pillars of Eternity, for example, `GameState` reaches the player and the stronghold only that way, through `GameState.s_playerCharacter` and `GameState.m_stronghold`.

<figure markdown="span">
  ![Reference graph: what an object points to](../assets/screenshots/reference-graph-targets-dark.webp#only-dark)
  ![Reference graph: what an object points to](../assets/screenshots/reference-graph-targets-light.webp#only-light)
  <figcaption>What Eder's <code>PartyMemberAI</code> points to, its <code>CharacterStats</code> unfolded and the way to its <code>Equipment</code> highlighted.</figcaption>
</figure>

Each way has a graph of its own, kept while you switch, and a double click unfolds a node in the way shown. In Fatekeeper (Unreal), for example, the player's character points to its controller, player state, mesh, movement and its `AttributeComponent`, which holds the values the game shows – one double click away.

## Working with it

- **Double-click** an object to unfold it: what holds it, or what it points to. Double-click it again to fold it in: what unfolding it showed goes again, with everything only reached through it. Its fields open in the object inspector from the context menu.
- **Ctrl + mouse wheel** zooms, dragging the background moves the graph.
- The **context menu of a node**:
    - **Find holders** (the other way: **Find what it points to**) unfolds it, **Fold in** folds it in again, **Open in inspector** shows its fields;
    - **Copy address**, and for a static field or global data **Copy name** (the field, or `module+0x…`);
    - **Highlight the way to the object** (the other way: *from* the object) marks the shortest way between the node and the start object in lime, beside the orange way to a root;
    - **Hide** leaves the node out, **Hide all *Class*** every object of its class – handy for the dozens of `UNiagaraComponent`s that hold an Unreal actor. What only hidden nodes led to goes with them, and the way to a root is looked for around them.
- The **context menu of the background**: **Fit** zooms so that the whole graph fits the window, **Clear the highlight**, **Show hidden (n)** shows everything hidden again, and **Export as HTML**.
- Lists, events and caches can hold an object hundreds of times, and much of it is often native memory. Up to 5,000 locations are looked at per object and at most 100 shown: static fields and global data first, then objects; the stack and other memory are one node each (other memory only if the settings show it). **+n** in a node's corner says how many locations were left out; its tooltip says how many of them are other memory, hidden by you or not singletons.

### Only singletons

With **Only singletons** ticked, the graph shows only objects whose class has exactly one object, as the [object browser](object-browser.md) defines singletons: a class of the game with one object, or an engine class with a role such as `PlayerController` or `GameMode`. Roots and the start object always stay. The classes are listed once when you tick it, and again after **Read again**.

The way to a root is then looked for through singletons only, and among many holders the singletons are kept first. This finds the managers that keep an object – the game mode, the player controller, a subsystem – without the components and effects around it. Nodes unfolded before ticking it keep the holders chosen then; tick it before unfolding, or read again.

### Exporting

**Export as HTML** saves the graph as it is drawn now – with what is hidden, the highlighted ways and the theme's colors – as a self-contained page: the object's address, the program and the time above the graph, the nodes' tooltips shown when the pointer rests on them. The page needs nothing besides itself, and the browser's zoom scales it. The file dialog starts in the folder of the last export (see [Saving, export and import](data.md#where-exports-go)).

Like the other tools that stay in use while you play, the window can be [docked](tools.md#docking-windows) beside the scanner.

## How it works

When the window opens, Grembyte reads all writable memory of the game once and keeps every pointer it finds, the same way the [pointer scan](pointer-scan.md) does. The locations holding an object's address are then found at once, however often you unfold. The engine names each location: the object it lies in and the field there, or the class whose static data it is. Locations in a module's data or on a thread's stack are recognized without asking it.

The graph is therefore a **snapshot** of the moment the window opened. Objects created later are missing, and an object may be gone by the time you open it in the inspector. **Read again** reads the memory anew and starts the graph again from the object. *What it points to* uses the same snapshot: the pointers lying inside an object, which the engine then names.

A reference graph shows where an object is *kept*, not who *created* it. The code that called `new`, `Instantiate` or `NewObject` leaves no trace in memory once it has returned. A singleton is often held only by its own static field, so its graph ends after one step.

## Unity Mono

- The objects are the ones the [object browser](object-browser.md#unity-mono) reads with Unity's liveness calculation, so the same applies: **the game stands still briefly** while they are read (at most once a minute), objects only a stack holds are not found, and [Settings](settings.md) → *Object browser: objects read* limits how many are kept.
- **Static fields** are named for the classes with live objects, their base classes (such as `Singleton<T>`) and every class of the game's own assemblies, so a static class without objects is found too. Reading a class's static data does not run its static constructor.
- Many Unity objects are held mainly by Unity's native side, which shows as **Other memory**. Their C# references (events, lists, other components) still show.

Tested in Vampire Survivors (Unity 6, native Linux): from the `GameManager` it finds the static field `VampireSurvivors.Framework.GM.Core`, and from the player's `CharacterController` the way to `PhysicsManager._sInstance`.

## Unreal Engine

- The objects come from the engine's object table (`GUObjectArray`), as in the [object browser](object-browser.md#unreal-engine). Reading it does not stop the game.
- Fields are named from the UProperties of the object's class, structs included. `ClassPrivate` and `OuterPrivate`, which every object has, are named too.
- The elements of `TArray`, `TMap` and `TSet` lie in memory blocks of their own, outside the object. A location there shows as **Other memory** for now, and *What it points to* does not follow them yet.

## Not yet available

- **Unity IL2CPP:** it needs the list of live objects that IL2CPP's object browser does not read yet.
- **Native games:** without an engine's reflection there are no objects or fields to name the locations by; the [pointer scan](pointer-scan.md) finds chains to an address there.
