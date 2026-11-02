# Hermes Test RPG (Unity)

A small top-down ASCII role-playing game as a test target for the Mono adapter
and as the running example of the documentation. It has a character sheet, enemies
that spawn at the press of a key, and one damage pipeline where hooks and scripts can
multiply or reduce damage.

The game needs no assets: it starts from code (`Bootstrap`), draws an 80 x 30
character grid with IMGUI and reads the keyboard from IMGUI events.

## Setup

1. Install Unity 6 LTS (or 2022.3 LTS) with the modules *Linux Build Support (Mono)*
   and *Windows Build Support (Mono)*.
2. Open `test_app/unity` in Unity Hub (*Add project from disk*). On the first start,
   Unity creates `ProjectSettings/` and the `.meta` files; commit them.
3. Press Play in the editor, or build:
   - menu *Hermes Test → Build Linux / Build Windows / Build All*, or
   - `Unity -batchmode -quit -projectPath test_app/unity -executeMethod HermesTestRpg.Editor.BuildScript.BuildAll`

The builds land in `test_app/unity/Build/Linux` and `Build/Windows`, as release builds
with the Mono backend. The Windows build is for testing under Proton.

`-seed <n>` on the command line fixes the map and all rolls, including after "New game".

## Keys

| Key | Action |
|---|---|
| Arrows, WASD, numpad | move; walk into an enemy to attack |
| Space / R | wait / rest (recover 2 health) |
| F | Fire Bolt at the nearest enemy, costs 7.5 mana |
| E, 1-5 | spawn a random enemy / rat, goblin, skeleton, orc, ogre |
| Delete | remove all enemies |
| C, F1, H | character sheet, debug view with exact values, help |
| N | new game |

## What to test

**Values (memory scan, pointer scan).** All values live in heap objects:
`GameState.Current → Player → Stats → {Health, Mana, Experience, Gold, Strength, ...}`.
"New game" builds new objects, so the addresses change. Health and Mana are `float`,
Experience is a `double`; Mana regenerates by 0.13 per second in real time, even in the
background. The F1 view shows the exact values and the process ID.

**Damage pipeline (hooks, scripts).** Melee and Fire Bolt go through the same methods:

```
Combat.MeleeAttack / Combat.CastFireBolt
  → Combat.ApplyDamage(game, attacker, target, baseDamage, source)
      → Combat.CalculateDamage(attacker, target, baseDamage)   returns the damage
      → Character.TakeDamage(amount)                           writes Stats.Health
```

- `Character.DamageOutgoingMultiplier` / `DamageIncomingMultiplier` (float, 1.0) are
  fields a script can set per character.
- A hook on `CalculateDamage` can change the return value; the log and the F1 view show
  base, multipliers and the damage actually dealt, and mark a changed result.
- `TakeDamage` hits player and enemies alike; a hook tells them apart by `IsPlayer`.
- `Heal`, `SpendMana`, `RegenerateMana`, `AddGold` and `AddExperience` each write one
  value in their own method.

All these methods are marked `NoInlining`, so the Mono JIT keeps them as separate
functions.
