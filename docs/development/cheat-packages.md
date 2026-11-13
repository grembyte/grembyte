# Cheat packages (draft)

!!! note "Draft"
    This page sketches a planned format; nothing of it is implemented yet. It is the first
    step towards letting other tools use parts of Grembyte, for example a trainer with its
    own interface. The format may change until a first tool outside Grembyte uses it.

A cheat package (`grembyte-pack`) holds cheats for one game, ready to be switched on by a
player: what they are called, how they are controlled, and how Grembyte's runtime carries
them out. Grembyte writes packages from what is saved for a game; `grembyte-cli` and other
tools read them.

## Package and export

The [export](../guide/data.md#export-and-import) and the package are separate formats:

| | Export (`grembyte-export`) | Package (`grembyte-pack`) |
|---|---|---|
| Purpose | Back up or move *your work* | Hand on *selected* cheats |
| Content | Everything: addresses, pointer scans, favorites, notes, hooks as they are | Only what a player should switch on |
| Addressing | Also absolute addresses and program paths | Only what survives a restart of the game |
| Presentation | None (only a manual script's hotkey) | Controls, texts, categories, hotkeys |
| Read by | Grembyte | Grembyte, `grembyte-cli`, other tools |

A package is made in Grembyte's window ("Publish as package …" is the working title): the
user picks saved entries and adds what the package needs beyond them. The export stays
Grembyte's own format; the package is the one other tools rely on.

## Decisions

- **One package per game.** Differences between builds of a game are handled when the
  package is loaded: each cheat is checked on its own (see [Loading](#loading)), so an update
  of the game disables the cheats it breaks rather than the whole package.
- **One JSON file**, UTF-8. A container (ZIP with images) is only worth it once packages
  carry images such as a cover or category icons.
- **No dependencies between cheats** ("needs X", "excludes Y") until a package needs them.
- **Engine identities name their engine.** Values and methods found through an engine's
  reflection carry an `engine` field (`mono`, `il2cpp`, `unreal`), so that one format covers
  every engine adapter.

## Layout

A package has three layers, so that a foreign interface only has to understand the first
two:

1. **Head** – the package, the game and what the runtime needs.
2. **`cheats`** – what the player sees: each cheat has a control (`toggle`, `number`,
   `button`, `choice`) and a list of effects.
3. **Effects** – what only Grembyte's runtime understands: `values`, `write_hooks`,
   `method_hooks`, `scripts`, and the JavaScript `functions` they share.

An effect refers to an entry of the third layer by `ref`. A binding passes the control's
value on: `"factor": {"from": "value"}` sets the effect's parameter `factor` to the value of
the cheat's control.

## Example

Pillars of Eternity (Unity, Mono): gold through a chain based on a static field, and the
damage enemies roll through a method hook. The comments only explain the example; a package
is plain JSON.

```jsonc
{
  "format": "grembyte-pack",
  "version": 1,                              // version of the format, not of the package
  "id": "community.pillars-of-eternity",
  "pack_version": "1.0.0",
  "title":       { "en": "Pillars of Eternity", "de": "Pillars of Eternity" },
  "description": { "en": "Gold and easier fights.", "de": "Gold und leichtere Kämpfe." },
  "authors": ["winterhafen"],
  "license": "CC0-1.0",
  "created_with": "grembyte 0.9.0",

  "game": {
    "title": "Pillars of Eternity",
    "stores": { "steam": 291650 },
    "engine": "mono",
    "executables": [
      { "platform": "linux",   "name": "PillarsOfEternity" },
      { "platform": "windows", "name": "PillarsOfEternity.exe" }
    ],
    // Optional: the builds the package was made and checked with
    "tested_builds": [
      { "label": "3.08", "assemblies": { "Assembly-CSharp": { "mvid": "…" } } }
    ]
  },

  "requires": {
    "runtime": 1,
    "features": ["managed_values", "method_hooks", "freeze"]
  },

  "cheats": [
    {
      "id": "gold",
      "label": { "en": "Gold", "de": "Gold" },
      "category": "inventory",
      "control": { "type": "number", "min": 0, "max": 999999, "step": 100,
                   "lockable": true },             // "lock" freezes the value
      "effects": [ { "use": "value", "ref": "player_gold" } ]
    },
    {
      "id": "enemy_damage",
      "label": { "en": "Enemy damage", "de": "Gegnerschaden" },
      "description": { "en": "Damage rolled by enemies, as a factor.",
                       "de": "Schadenswurf der Gegner als Faktor." },
      "category": "combat",
      "control": { "type": "number", "min": 0, "max": 1, "step": 0.1,
                   "default": 0.2, "toggle": true, "format": "percent" },
      "hotkey": "Ctrl+F2",
      "effects": [
        { "use": "method_hook", "ref": "roll_damage",
          "params": { "factor": { "from": "value" } } }
      ]
    }
  ],

  "values": {
    "player_gold": {
      "value_type": "float",
      "pointer": {
        "base": { "managed": { "engine": "mono", "assembly": "Assembly-CSharp",
                               "class": "GameState", "field": "s_playerCharacter" } },
        "offsets": ["0x78", "0x68", "0x10"]
      }
    }
  },

  "method_hooks": {
    "roll_damage": {
      "method": { "engine": "mono", "assembly": "Assembly-CSharp",
                  "signature": "DamagePacket:RollDamage (CharacterStats,single)",
                  "returns": "System.Single" },
      "phase": "after",
      "function": "scale_enemy_result",
      "params": { "factor": 0.2 }
    }
  },

  "functions": {
    "scale_enemy_result": {
      "kind": "method",
      "source": "function (ctx) {\n  if (ctx.args.roller.IsPartyMember) return undefined;\n  return ctx.result * ctx.params.factor;\n}",
      "parameters": [ { "name": "factor", "kind": "number", "default": 1 } ]
    }
  }
}
```

A write hook (native or JIT code) and a script:

```jsonc
"write_hooks": {
  "strength": {
    "site": { "module": "BaldursGate", "offset": "0x1a2b3c",
              "instruction_bytes": "89 90 90 07 00 00",   // checked before patching
              "signature": null, "managed": null },
    "value_type": "int32",
    "callers": [ { "module": "BaldursGate", "offset": "0x0f00ba" } ],  // shared write site
    "function": "set_to",
    "params": { "value": 25 }
  }
},
"scripts": {
  "give_xp": {
    "source": "…",
    "mode": "manual",
    "args": [ { "name": "amount", "kind": "number" } ]
  }
}
```

A button runs the script: `{ "use": "script", "ref": "give_xp", "args": { "amount": { "from": "value" } } }`.

## Fields

### Head

`format`, `version`
:   `"grembyte-pack"` and the version of the format. A runtime refuses versions it does not
    know.

`id`, `pack_version`
:   A stable name of the package (lower case, dots) and its own version (semantic
    versioning), so that a tool can tell an update from another package.

`title`, `description`, `label`
:   Texts by language (`{"en": …, "de": …}`). `en` is required and used when the player's
    language is missing.

`game.executables`
:   How the game is recognized: the process name per platform, as Grembyte's process list
    names it.

`game.engine`, `requires`
:   The engine adapter the package needs and the runtime features it uses, so that an older
    runtime can say what is missing.

### Cheats

`category`
:   One of a fixed list (`inventory`, `combat`, `character`, `world`, `misc`), so that
    interfaces can group cheats and give them icons.

`control`
:   `toggle` (on/off), `number` (`min`, `max`, `step`, `default`, `format`; `lockable`
    freezes the value, `toggle` adds an on/off switch), `button` (runs once), `choice`
    (`choices` with labels).

`hotkey`
:   A suggested key; the interface decides whether and how it is used.

### Effects

`values`
:   A value type and a pointer chain whose base is a module (`{"module": "GameAssembly.dll",
    "offset": "0x…"}`) or a static field found through the engine (`{"managed": …}`).
    Effects of numbers write the value; `lockable` freezes it.

`write_hooks`
:   Like a saved hook: the write site (module and offset, or the code `signature` and
    `managed` method of JIT code), the bytes expected there, the callers that tell shared
    write sites apart, and the function with its parameters.

`method_hooks`
:   A method found through the engine, `before` or `after` the call, and the function with its
    parameters.

`scripts`
:   Code that calls game code, once (`manual`) or by time, with its arguments.

`functions`
:   The JavaScript functions of hooks, as templates are today: `kind` (`value` or `method`),
    `source` and `parameters` (as `TransformParameter.to_json()`).

### Engine identities

Values and methods found through an engine carry `engine` and what that engine needs to find
them again:

| `engine` | Static field (base of a chain) | Method |
|---|---|---|
| `mono` | `assembly`, `class`, `field` | `assembly`, `signature`, `returns` |
| `il2cpp` | `assembly`, `class`, `field` | `assembly`, `signature`, `returns` |
| `unreal` | to be decided with a prototype (class path and property) | to be decided (class path and function) |

Unreal is left open on purpose: Avowed, whose attributes are set through the game's setter
rather than in memory, is a good test of what the identity needs.

## Differences from the export

- **No absolute addresses, no metadata tokens.** Values are only reached through chains
  whose base is a module or a named static field; methods through their signature.
  Grembyte already finds methods by their signature after a restart; in a package it is the
  only way. Today a managed chain stores `mono:Assembly-CSharp:GameState` and a
  `base_offset`; the field name lasts longer across builds and can be read.
- **Offsets as hex strings** (`"0x78"`), because packages are read and maintained by hand
  too. Numbers are accepted as well.
- **No state.** `enabled`, `frozen` and `frozen_value` are left out; `control.default` says
  what switching a cheat on does.
- **Functions belong to the package.** A hook refers to `functions` instead of a local
  template id; this is today's `transform_source` and `transform_parameters` without the
  copies.

## Loading

1. **Find the game** by `game.executables`. A game that ships EasyAntiCheat or BattlEye is
   refused, whatever interface asks; the check belongs to the runtime, so that every tool
   inherits it.
2. **Check every cheat on its own** before it is offered: a value's chain resolves, the
   `instruction_bytes` of a write hook match, the method of a method hook exists. Each cheat
   gets a state: `ready`, `unavailable` (with the reason) or `waiting` (for example while no
   save game is loaded and the chain is still empty). An interface greys out single cheats
   instead of rejecting the package.
3. **Check `requires`**, so that an older runtime names what the package needs.

## Security

A package is executable code. `functions` and `scripts` run with Frida inside the game's
process, with every right of the player's account: through `NativeFunction` they can call
any function of the C library, read files or start programs. The QJSEngine sandbox
(`grembyte/hooks/sandbox.py`) only runs a template while it is tested, not in the game.

- Before a package with code is loaded the first time, the runtime says so and shows the
  code.
- A package may carry a `signature` (Ed25519 over the canonical JSON). Signed packages of
  authors the player trusts load without asking.
- A package of only `values` (writing and freezing) contains no code and can be marked as
  such.

## Next steps

The steps towards packages and a runtime other tools can use are planned in the
[roadmap](https://github.com/grembyte/grembyte/blob/main/ROADMAP.md) ("Use by other tools").
