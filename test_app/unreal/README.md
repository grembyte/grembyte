# HermesQuest (Unreal) — planned

The Unreal counterpart of [the Unity game](../unity/README.md): same map, keys and
enemies, so the documentation can show the same steps for both engines.

Plan:

- UE5 C++ project with the GameplayAbilities plugin.
- `UHermesAttributeSet` with `FGameplayAttributeData` for Health, MaxHealth, Mana,
  MaxMana, Strength, Dexterity, Intelligence, DamageOutgoingMult and
  DamageIncomingMult, as in shipped GAS games.
- Damage as a `UGameplayEffect` with an `UGameplayEffectExecutionCalculation`, so
  the multipliers apply in the place real games use.
- Shipping builds only: the Unreal adapter has to work without exports and symbols.
  Linux builds from Linux; the Windows build (for Proton) needs a Windows machine,
  since Unreal does not cross-compile from Linux to Windows.
