# Object browser

The [object inspector](../engines/index.md#object-inspector) shows one object, but you first need that object: from a monitored call, a script result or a field of another object. The object browser starts from the other end. It lists the objects that live in the game right now, grouped by class, so you can open the player, the game state or a party member's inventory without knowing where they are kept.

It is available for [Unreal Engine](#unreal-engine) and [Unity Mono](#unity-mono) games. Open it with **View → Object browser …** while Grembyte is attached to the game.

<figure markdown="span">
  ![Object browser](../assets/screenshots/object-browser-dark.webp#only-dark)
  ![Object browser](../assets/screenshots/object-browser-light.webp#only-light)
  <figcaption>The game's own classes in Pillars of Eternity, with the objects of <code>Health</code> opened.</figcaption>
</figure>

## The tree

The first level lists the classes that have live objects, with how many objects of exactly that class live (objects of subclasses count for their own class). Open a class to list its objects:

- Unreal objects and Unity objects show with their name, as `"Companion_Eder(Clone)_0"@0x2a41c80a1e0`.
- Strings show with their text.
- Other C# objects show as `Class@address`.

Games keep hundreds of thousands of objects, so a class's objects are only read when you open it, and a class lists at most 5,000 of them. The filter narrows them.

Double-click an object, or select it and click **Open in inspector**, to show its fields in the [object inspector](../engines/index.md#object-inspector). The context menu copies an object's address or name, or a class's name or address. **Show references** opens the object's [reference graph](reference-graph.md): which objects hold it, back to a static field or a module's global data.

Objects live only for a while: a level, a fight, a session. To keep an interesting one for later, use **Save as HTML …** in its inspector. The page records its fields and getters as they were then.

## Narrowing the list

- **Package:** all packages, **Game only** (without the engine's and the framework's), or one package. A package is an assembly in Unity games (`Assembly-CSharp`) and a package as the catalog groups them in Unreal games (`/Script/<Module>`, `/Game/<Folder>`). The dropdown shows how many objects each one has.
- **Filter:** a class whose name contains all the words you type shows with all its objects. A class with objects named so shows with only those, e.g. `eder` finds `Health` with its object `Companion_Eder(Clone)_0`. The column *Objects* then shows those named so out of all.
- **Singletons only:** classes with exactly one object. The *Singleton* column says why: the engine class that has only one object (Unreal), the static field that holds it (Unity, e.g. `GameState.s_Instance`), or *one object* for a game class that just happens to have one.

The tree is a snapshot: objects created later are missing, and an object may be gone by the time you open it. **Refresh** reads the objects again.

Like the other tools that stay in use while you play, the object browser can be [docked](tools.md#docking-windows) beside the scanner. It closes when the game ends.

## Unreal Engine

Grembyte reads every object from the engine's object table (`GUObjectArray`), the same table [scripts](scripts.md) search with `ctx.find`. Reading it does not stop the game.

- The objects' names are `FName`s, which repeat: `PointsAvailableTracker` may exist once per widget. The tooltip of an object names its outer, the object it belongs to (`Sidekick in WidgetTree_7`).
- *Singletons* are objects of the engine's single-instance classes and their subclasses: game instance, subsystems, game mode, game state, player controller, world settings, game user settings.
- **Templates and reflection** also lists class default objects (`Default__…`), archetypes and the reflection objects themselves: packages, classes, functions, structs, enums. They are left out otherwise. The inspector shows a reflection object's properties, not its contents (an enum's values, a class's fields).

## Unity Mono

Grembyte reads the objects with Unity's own liveness calculation, the one Unity uses to unload unused assets. It finds the objects reachable from static fields and from Unity's objects.

- **The game stands still** while the calculation runs, since it marks every object it visits. The status line says for how long.
- **Objects only a thread's stack holds** are not found, e.g. a local variable of a method that is running right now.
- **Singletons** are found through static fields that hold a class's only object, also through generic bases such as `Singleton<T>`.
- Unity objects show with their name, which Grembyte asks Unity for on the game's main thread.
- **At most 2 million objects** are kept per read; the status line says when there were more. Some games keep far more alive (Vampire Survivors about 2.5 million, mostly JSON data), and the classes found last are then missing. [Settings](settings.md) → *Object browser: objects read* raises the limit to up to 10 million (16 bytes per object in the game).
- **Only one Grembyte per game** may be attached (see [permissions](../getting-started/permissions.md#one-grembyte-per-game)); two of them can crash it here.

It works from Unity 4 to Unity 6, in 64-bit and [32-bit games](../engines/unity-mono.md#32-bit-games), and also in games that [sleep out of focus](../engines/unity-mono.md#games-that-sleep-out-of-focus).

Tested in Pillars of Eternity (64-bit), Might & Magic X – Legacy (Unity 4.2, 32-bit, Proton) and Vampire Survivors (Unity 6, native Linux), where the newer API runs.

## Not yet available

- **Unity IL2CPP:** IL2CPP has its own liveness calculation (`il2cpp_unity_liveness_*`); the browser does not use it yet.
- **Native games:** without an engine's reflection there is no list of classes to group objects by.
