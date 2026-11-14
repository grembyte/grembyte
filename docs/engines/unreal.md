# Unreal Engine

Grembyte supports Unreal Engine 4.23 and later, 64-bit, natively and under Proton. It finds the engine's reflection data – `FNamePool` and `GUObjectArray` – in memory by its shape, without signatures, SDK dumps or a mod loader, and measures the layout of objects, properties and functions on the game itself.

- The **catalog** lists the classes and UFunctions with C++ parameter types, grouped by package: `/Script/<Module>` for C++ classes, the first folder for Blueprints (`/Game/Blueprints`).
- **Assemblies …** (here: packages) and the **trace** work as for Mono. The trace hooks the exec thunks of native UFunctions and gives every Blueprint function a hookable stub of its own. It sees calls from Blueprints and through `ProcessEvent` (events, delegates), not C++ calling C++ directly – the [native trace](../guide/native-tools.md) sees those.
- The **method monitor** logs UFunction calls with the object they run on (with its UObject name), arguments and result; the **object inspector** shows the UProperties of objects and structs. Arguments come from the parameters `ProcessEvent` passes, or, when a Blueprint calls a native function, from the bytecode of the call (variables and constants; other arguments show as `?`).
- **Method hooks** change arguments (from `ProcessEvent`'s parameters, or ref arguments from Blueprint code) and fields before a call, or the result after it. Their scripts can call UFunctions: `obj.call("Function", ...args)` runs it through `UObject::ProcessEvent` on the game thread, and `ctx.find("ClassName")` gives a live object of a class, e.g. the game's cheat manager.

## Call method

**Call method** runs a UFunction through `ProcessEvent` on the game thread: a static one on its class default object, an instance one (from the catalog or favorites) on a live object of its class – the first one found, or the one whose name or address you type in.

<figure markdown="span">
  ![Call method](../assets/screenshots/method-call-dark.webp#only-dark){ width="640" }
  ![Call method](../assets/screenshots/method-call-light.webp#only-light){ width="640" }
  <figcaption><code>AlabamaCheatManager:GiveHealth</code> in Avowed.</figcaption>
</figure>

| Parameter type | Entered as |
| --- | --- |
| `FString`, `FName` | text |
| `bool` | checkbox |
| enums | chosen from their names, or as a number |
| structs of numbers (`FVector`, …) | `1 2 3` or `X=1; Z=3` |
| outputs | nothing; shown after the call |

Not possible as arguments: `TArray`, `TMap`, `TSet`, `FText` and delegates.

Many shipping builds keep their cheat manager: in Avowed `BP_CheatManager_C` (an `AlabamaCheatManager`) has 147 functions with code, such as `GiveHealth`, `AddCurrency` and `ToggleUnlimitedStamina`.

## Object browser

**View → Object browser …** lists every live object of the game by class, read from `GUObjectArray`, and opens them in the object inspector. Packages, a filter on class and object names, singletons and templates are explained in the [object browser guide](../guide/object-browser.md#unreal-engine).

From an object, the [reference graph](../guide/reference-graph.md#unreal-engine) shows which objects hold it, or which objects it points to.

## GAS attributes

Games built on Unreal's Gameplay Ability System keep values such as health, strength or damage multipliers as *attributes*. **View → GAS attributes …** shows the player's attribute sets with base and current value, live. The window can be [docked](../guide/tools.md#docking-windows) beside the scanner.

!!! note "Games without GAS"
    Not every Unreal game keeps its values with GAS, even when the plugin is loaded: Queen's Domain, for one, keeps health, level and potions in plain fields of the player character. The table then stays empty and the window says:

    > The player has no attribute sets: the game does not keep its values with the Gameplay Ability System. They may be fields of the player: "Inspect player" shows them.

    The line below the table names the player and its class. **Inspect player** opens the player's object in the [object inspector](index.md#object-inspector); its filter finds a field such as `Health` among hundreds, which can then be read, changed or put on the address list.

<figure markdown="span">
  ![GAS attributes](../assets/screenshots/gas-attributes-dark.webp#only-dark)
  ![GAS attributes](../assets/screenshots/gas-attributes-light.webp#only-light)
</figure>

GAS computes the current value anew from the base value and the active effects, so writing it into memory does not last. A base value set in the window goes through the game's own setter instead (found by its parameters; in Avowed `AlabamaAbilitySystemBlueprintLibrary:SetFloatAttributeBaseFromAbilitySystemComponent`), and the active effects keep counting: in Avowed `DamageOutgoingMult` 3 with a +40 % bonus deals 4.2 times the damage.

The search finds sets and attributes by name. **Only changed** shows only attributes whose current value differs from the base (effects act on them) or that are held; the current value is orange where effects change it. **Live** reads the values every second, **Refresh** once.

**Hold** keeps the selected attributes at their base value against the game resetting it (loading, levelling up) with a script in the [Scripts](../guide/scripts.md) tab, also the next time Grembyte attaches; the column **Held** marks them. **Release** deletes those scripts; the values stay until the game changes them. Scripts can do the same with `ctx.gas.get("Might")` and `ctx.gas.setBase("AttributeSetCore.Might", 14)`.

Some games give a set's object a new number on every load, as Gothic 1 Remake does with `AttributeSet_Health_2147445233`. Scripts may leave the number out (`ctx.gas.get("AttributeSet_Health.Health")`), and hold scripts from the window do so themselves. `ctx.gas.findSet(/^AttributeSet_Health/)` gives the set's current full name.

In Avowed, filter the window for `might` and double-click the base value: the character sheet follows at once.

=== "Before"

    <figure markdown="span">
      ![Might 1 in Avowed](../assets/photos/unreal-avowed-gas-before-dark.webp#only-dark)
      ![Might 1 in Avowed](../assets/photos/unreal-avowed-gas-before-light.webp#only-light)
      <figcaption>Might at its base value 1.</figcaption>
    </figure>

=== "After"

    <figure markdown="span">
      ![Might 30 in Avowed](../assets/photos/unreal-avowed-gas-after-dark.webp#only-dark)
      ![Might 30 in Avowed](../assets/photos/unreal-avowed-gas-after-light.webp#only-light)
      <figcaption>Base value set to 30: the game shows Might 30 with +45 % damage. It counts the points as spent, so the points available drop below zero.</figcaption>
    </figure>

## Example: a script with a choice parameter

In Gothic 1 Remake, a manual [script](../guide/scripts.md) adds any item to the inventory. Its parameter *Item Name* is a choice: its options code reads the game's item list, so the run dialog offers every item by name, filtered as you type; *Count* is a number.

=== "Script"

    <figure markdown="span">
      ![The script in Gothic 1 Remake](../assets/photos/unreal-gothic-script-definition-dark.webp#only-dark)
      ![The script in Gothic 1 Remake](../assets/photos/unreal-gothic-script-definition-light.webp#only-light)
      <figcaption>The script in the editor: it finds the player, looks the chosen item up in the game's item list and hands the count to <code>CheatModeHelper:AddItemsToInventory</code>.</figcaption>
    </figure>

=== "Run dialog"

    <figure markdown="span">
      ![Run dialog of a script in Gothic 1 Remake](../assets/photos/unreal-gothic-script-before-dark.webp#only-dark)
      ![Run dialog of a script in Gothic 1 Remake](../assets/photos/unreal-gothic-script-before-light.webp#only-light)
      <figcaption>The run dialog asks for the item and the count; <b>Reload options</b> runs the options code again.</figcaption>
    </figure>

=== "Result"

    <figure markdown="span">
      ![The item in Gothic 1 Remake's inventory](../assets/photos/unreal-gothic-script-after-dark.webp#only-dark)
      ![The item in Gothic 1 Remake's inventory](../assets/photos/unreal-gothic-script-after-light.webp#only-light)
      <figcaption>The ore nuggets in the inventory and the script's result in the log.</figcaption>
    </figure>

## Call graph

The [call graph](index.md#call-graph) reads the calls from **Blueprint bytecode**, the code of the functions made in the editor. A Blueprint function shows the functions it calls, Blueprint and native alike, in the order of its code; **Search callers** finds the Blueprint functions calling a function. Calls the engine makes by name show the label **virtual**: on the Blueprint itself they go to the function of its class or the nearest base class declaring it, on another object to the only function of that name (with several of that name they are left out).

What it cannot see:

- **Native (C++) functions have no bytecode.** What they call is not shown, and C++ code calling a Blueprint function does not show as a caller. In games written mostly in C++ (Avowed) the graph shows the Blueprint layer around it.
- **Events of the event graph** all jump into one function, `ExecuteUbergraph_<Blueprint>`: an event shows that function as what it calls, and the calls of every event of the graph are that function's.
- Functions whose Blueprint is not loaded at the moment (another level, a menu not opened) are not in the catalog and cannot be found.

## Not yet available

- Unreal Engine before 4.23 and 32-bit games.

## Tested games

Avowed (UE5, Proton), Gothic 1 Remake (UE5, Proton; its GAS setter follows AngelscriptGAS).

<small>The screenshots of Avowed and Gothic 1 Remake on this page only illustrate how Grembyte works. All game names, trademarks and game content belong to their respective owners. Grembyte is not affiliated with, endorsed or sponsored by them.</small>
