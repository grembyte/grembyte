# Regular expressions

A regular expression is a pattern for text: "starts with `BP_`", "ends in a number", "contains *potion*, in any case". In Grembyte they are useful in the JavaScript of [scripts](../guide/scripts.md), [hook templates](../guide/templates.md), method hooks and the options of choice parameters, wherever names or texts of the game are picked, cleaned up or taken apart. They are JavaScript's regular expressions; this page covers the part needed for that. If JavaScript itself is new to you, start with [JavaScript in Grembyte](javascript.md).

The filters in Grembyte's own windows (catalog, code explorer, trace, monitor) are no regular expressions: they keep the rows containing every word typed.

## Writing one

```js
const potion = /potion/i;              // a literal between slashes, flags after it
const set = new RegExp("^" + name);    // built from text at run time
```

| Method | Gives | Example |
| --- | --- | --- |
| `re.test(text)` | `true` if the pattern occurs | `/^BP_/.test(obj.className)` |
| `text.match(re)` | the first match and its groups, or `null` | `"Gold: 600".match(/\d+/)[0]` → `"600"` |
| `text.matchAll(re)` | every match (needs the `g` flag) | `[..."a1 b22".matchAll(/\d+/g)].map(m => m[0])` → `["1", "22"]` |
| `text.replace(re, by)` | the text with the match replaced (every match with `g`) | `"BP_Hero_C_2".replace(/_\d+$/, "")` → `"BP_Hero_C"` |
| `text.split(re)` | the parts between matches | `"a, b;c".split(/[,;]\s*/)` → `["a", "b", "c"]` |
| `text.search(re)` | the position of the first match, or -1 | `"x=5".search(/\d/)` → `2` |

## Building blocks

| Pattern | Matches |
| --- | --- |
| `abc` | the characters `abc` |
| `.` | any character except a line break |
| `\d` / `\D` | a digit / anything else |
| `\w` / `\W` | a letter, digit or `_` / anything else |
| `\s` / `\S` | white space (space, tab, line break) / anything else |
| `[abc]` | one of `a`, `b`, `c` |
| `[a-z0-9_]` | one character from these ranges |
| `[^abc]` | one character that is none of them |
| `^` / `$` | the start / the end of the text |
| `\b` | a word boundary: `\bgold\b` finds `gold`, not `golden` |
| `a|b` | `a` or `b` |
| `(…)` | a group: what it matched is kept (`m[1]`, `m[2]`, …) |
| `(?<name>…)` | a named group (`m.groups.name`) |
| `(?:…)` | a group that keeps nothing, just for `|` or a quantifier |
| `(?=…)` / `(?!…)` | followed by / not followed by, without consuming it |

### How often

| Quantifier | Means |
| --- | --- |
| `a?` | `a` or nothing |
| `a*` | any number of `a`, also none |
| `a+` | at least one `a` |
| `a{3}` / `a{2,5}` / `a{2,}` | exactly 3 / 2 to 5 / at least 2 |
| `a+?`, `a*?` | as few as possible instead of as many (*lazy*) |

### Flags

| Flag | Effect |
| --- | --- |
| `i` | ignore case: `/potion/i` finds `Potion` |
| `g` | every match (`matchAll`, `replace` of all) |
| `m` | `^` and `$` at every line, not only at the start and end of the text |
| `s` | `.` matches line breaks too |
| `u` | full Unicode (`\u{1F600}`, letters of every script with `\p{L}`) |

### Characters with a meaning

`. * + ? ^ $ ( ) [ ] { } | \ /` mean something in a pattern. To match one of them literally, put a backslash before it: `AttributeSetCore\.Might`, `\(Clone\)`, `\$`. When a pattern is built from text with `new RegExp`, the backslash itself is written twice in the string: `new RegExp("\\d+")`.

## In Grembyte

**Unreal names with a running number.** Unreal often numbers objects anew on every load: `AttributeSet_Health_2147445233`, `BP_Hero_C_1`. Cut the number off, or look for the start:

```js
const set = ctx.gas.findSet(/^AttributeSet_Health/);      // its name, or null
const base = obj.name.replace(/_\d+$/, "");               // "BP_Hero_C_1" -> "BP_Hero_C"
```

**Picking objects by name.** Count the enemies whose class starts with `BP_Enemy_` or `BP_Boss_`:

```js
let count = 0;
ctx.forEach("Actor", actor => { if (/^BP_(Enemy|Boss)_/.test(actor.className)) count++; });
return count;
```

**Options of a choice parameter.** Only the potions of the game's item list, sorted by name (Gothic 1 Remake, see the [Unreal page](../engines/unreal.md#example-a-script-with-a-choice-parameter)):

```js
return ctx.call("CheatModeHelper:GetAllItems", "")
  .filter(item => /^ItFo_Potion/.test(item.className))
  .map(item => ({ label: String(item.m_Name), value: item.className }))
  .sort((a, b) => a.label.localeCompare(b.label));
```

**Numbers from a text.** A label or log line of the game holds a value:

```js
const text = "Mana: 12.5 / 50";
const m = text.match(/Mana:\s*(?<now>[\d.]+)\s*\/\s*(?<max>\d+)/);
if (m) ctx.log(`mana ${Number(m.groups.now)} of ${Number(m.groups.max)}`);
```

**Checking a text parameter.** A script asks for a name: refuse anything but letters, digits and `_`:

```js
if (!/^\w{1,32}$/.test(ctx.args.name)) return "Only letters, digits and _ (at most 32)";
```

**Byte patterns are something else.** `ctx.native.scan("48 8B 05 ?? ?? ?? ??")` takes hex bytes with `??` for any byte, not a regular expression (see [ctx.native](script-context.md#native-code-ctxnative)).

## Pitfalls

- **`g` and `test`:** a regular expression with the `g` flag remembers where its last match ended (`lastIndex`), so calling `test` again on another text may start in the middle. Use `g` only with `matchAll` and `replace`.
- **Greedy matches:** `".+"` in `"a" and "b"` matches all of `"a" and "b"`; `".+?"` matches `"a"`.
- **Anchors:** without `^` and `$` a pattern may match anywhere: `/\d+/.test("Level 3 hero")` is true. `/^\d+$/` wants nothing but digits.
- **Hooks run often:** a hook template runs on every write of a value. A literal `/…/` costs nothing there; building patterns with `new RegExp` on every call does.
- **Not for everything:** nested structures (JSON, brackets inside brackets) are beyond regular expressions; `JSON.parse` reads JSON.

## Trying patterns

[regex101](https://regex101.com/) explains a pattern piece by piece and shows what it matches; choose the flavor *ECMAScript (JavaScript)*. In Grembyte, a script with `ctx.log(text.match(/…/))` shows the match on real game text.
