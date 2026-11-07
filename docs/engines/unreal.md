# Unreal Engine

Hermes supports Unreal Engine 4.23 and later, 64-bit, natively and under Proton. It finds the engine's reflection data – `FNamePool` and `GUObjectArray` – in memory by its shape, without signatures, SDK dumps or a mod loader, and measures the layout of objects, properties and functions on the game itself.

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
| enums | by name |
| structs of numbers (`FVector`, …) | `1 2 3` or `X=1; Z=3` |
| outputs | nothing; shown after the call |

Not possible as arguments: `TArray`, `TMap`, `TSet`, `FText` and delegates.

Many shipping builds keep their cheat manager: in Avowed `BP_CheatManager_C` (an `AlabamaCheatManager`) has 147 functions with code, such as `GiveHealth`, `AddCurrency` and `ToggleUnlimitedStamina`.

## GAS attributes

Games built on Unreal's Gameplay Ability System keep values such as health, strength or damage multipliers as *attributes*. **View → GAS attributes …** shows the player's attribute sets with base and current value, live.

<figure markdown="span">
  ![GAS attributes](../assets/screenshots/gas-attributes-dark.webp#only-dark)
  ![GAS attributes](../assets/screenshots/gas-attributes-light.webp#only-light)
</figure>

GAS computes the current value anew from the base value and the active effects, so writing it into memory does not last. A base value set in the window goes through the game's own setter instead (found by its parameters; in Avowed `AlabamaAbilitySystemBlueprintLibrary:SetFloatAttributeBaseFromAbilitySystemComponent`), and the active effects keep counting: in Avowed `DamageOutgoingMult` 3 with a +40 % bonus deals 4.2 times the damage.

**Hold** keeps a value against the game resetting it (loading, levelling up) with a script in the [Scripts](../guide/scripts.md) tab. Scripts can do the same with `ctx.gas.get("Might")` and `ctx.gas.setBase("AttributeSetCore.Might", 14)`.

!!! screenshot "Screenshot to add: the effect in the game"
    Avowed's character sheet before and after setting an attribute in the GAS window.
    File: `docs/assets/photos/avowed-gas.png`

## Not yet available

- The call graph (from Blueprint bytecode).
- Unreal Engine before 4.23 and 32-bit games.

## Tested games

Avowed (UE5, Proton), Gothic 1 Remake (UE5, Proton; its GAS setter follows AngelscriptGAS).
