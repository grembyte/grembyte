# Test game: a pointer chain from the code

The gold of the [test game](../getting-started/first-hook.md) lives on the heap: its address changes with every start and every **New game**. What stays the same is the way there – a *pointer chain*. The [pointer scan](../guide/pointer-scan.md) finds it by searching the game's memory. This example finds the same chain another way: by reading the code that changes the gold. The game's own code knows the way, because it has to walk it every time.

It starts at `add_gold`, the function the [code explorer example](dummy-game-code-explorer.md) found behind the button **Find treasure**, and ends with the gold on the address list, found again after every **New game** and every restart. The second half explains, without any knowledge of assembly, how to tell a step of a chain from a plain offset – the one thing that is easy to get wrong.

Build and start the game and open **View → Code explorer …** as in the [code explorer example](dummy-game-code-explorer.md#build-and-start-the-game).

!!! note "Your addresses differ"
    The addresses on this page come from GCC on Fedora, which links the test game at a fixed address (image base `0x400000`). Another compiler or distribution gives other numbers; the steps are the same. Take the numbers from your own code explorer.

## 1. The code that changes the gold

In the **Functions** tab, type `gold` into the filter and double-click `add_gold`. Then switch from **Assembler** to **Pseudocode (guess)**:

```c
void add_gold(int32_t a1 /* rdi */)
{
    *(int32_t *)(*(uint64_t *)(g_game + 0x28)) += a1; // 0x4006db  g_game → 0x28 → 0x0
}
```

One line: "add `a1`, the amount, to the number at …". The comment after `//` holds two things:

- `0x4006db` is the address of the instruction that does it – in the code, not where the gold is.
- `g_game → 0x28 → 0x0` is the way to the gold: the pointer chain we are after. The code explorer writes it down for every line that reaches memory from a global variable.

The disassembly says the same in three instructions:

```text
0x4006d0  mov rax, qword ptr [rip + 0x2a31]    g_game
0x4006d7  mov rax, qword ptr [rax + 0x28]
0x4006db  add dword ptr [rax], edi
```

## 2. Reading the line from the inside out

A line like this is read from the innermost parentheses outwards, the order in which the processor does it:

| Piece | What happens | Step of the chain |
| --- | --- | --- |
| `g_game` | Read the global variable `g_game`. It holds an address: where the `Game` object lies on the heap. | Start: `g_game` |
| `g_game + 0x28` | Go `0x28` bytes further into the `Game` object. Nothing is read yet. | the offset `0x28` |
| `*(uint64_t *)( … )` | Read the 8 bytes there. They are again an address: where the player's `Character` lies. | `→` |
| `*(int32_t *)( … )` | The 4-byte number at that address, `0x0` bytes into the `Character`: the gold. | `→ 0x0` |
| `+= a1` | Add the amount. | – |

The source of the test game confirms it (`test_app/dummy_game.c`): `Game` has a 32-byte name and a 4-byte round counter, then 4 bytes of padding and the pointer `player` at `0x28`; `Character` starts with `gold`.

<figure markdown="span">
  ![add_gold as pseudocode, the references to g_game above it](../assets/screenshots/example-chain-add-gold-dark.webp#only-dark)
  ![add_gold as pseudocode, the references to g_game above it](../assets/screenshots/example-chain-add-gold-light.webp#only-light)
  <figcaption><code>add_gold</code> as pseudocode with its chain <code>g_game → 0x28 → 0x0</code>. Above it, after <strong>Go to</strong> <code>g_game</code>: the twelve instructions that use the global, and in the header where it lies, <code>dummy_game.exe+0x3108</code>.</figcaption>
</figure>

## 3. Where the chain starts

A chain has to start somewhere that does not move: a global variable inside the program's file. `g_game` is one. Type `g_game` into the field at the top and click **Go to**. The header of the references reads:

```text
References to 0x403108 (g_game, dummy_game.exe+0x3108): 12.
```

`dummy_game.exe+0x3108` is the start of the chain: `0x3108` bytes into the module `dummy_game.exe`. Grembyte writes chains with the module, not with `0x403108`, because a program built position-independent (most Linux distributions, all Windows games with ASLR) is loaded at a different address with every start – the offset inside the module stays.

So the chain of the gold is:

```text
dummy_game.exe+0x3108 → 0x28 → 0x0
```

!!! tip "Every user of the gold, in one list"
    The references to `g_game` lead to every function that works with the game state: `refresh_ui` shows the whole player – `g_game → 0x28 → 0x4` the hit points, `→ 0x14` the mana, `→ 0x18` the experience. Same way, different last offset.

## 4. On the address list

In the main window, below **Tracked addresses**, click **Pointer chain …** and type the chain. Arrows are typed as `->`, `=>` or `:`, whatever is easiest:

```text
dummy_game.exe+0x3108 -> 0x28 -> 0x0
```

Choose the type `int32` (the line read `int32_t`) and a description. Below the input the dialog says where the chain leads right now. **Add**.

<figure markdown="span">
  ![The chain of the gold in the dialog Pointer chain …](../assets/screenshots/example-chain-dialog-dark.webp#only-dark)
  ![The chain of the gold in the dialog Pointer chain …](../assets/screenshots/example-chain-dialog-light.webp#only-light)
  <figcaption>The chain read from <code>add_gold</code>, entered by hand: it leads to the player's gold on the heap.</figcaption>
</figure>

Now check it in the game: **Gold +100** changes the value on the list. **New game** puts the player somewhere else on the heap – the address column changes, the value is still the gold. After a restart of the game the entry finds it again, too. It is the same chain the [pointer scan](../guide/pointer-scan.md) finds, only read from the code instead of searched in memory.

## Chain or offset?

A chain has two kinds of numbers, and the whole difference is whether something is **read**.

### The picture

Think of the game's memory as one very long street. Every byte is a house with a number.

- An **offset** (written `+`) means: from here, walk that many houses further. Nothing is read, you only count. `0x403108 + 0x28` is simply house number `0x403130`.
- An **arrow** (written `→`) means: go into the house, read the note on the table – it holds a house number – and go *there*. Where you end up depends on what the note says right now. That is why a chain still works when the game moves the player: the game updates the note.

Read like that, `dummy_game.exe+0x3108 → 0x28 → 0x0` says: go to house `g_game` and read the note (where the game is). Walk `0x28` houses further and read the note there (where the player is). Walk `0x0` houses further: here lies the gold.

`dummy_game.exe+0x3108 + 0x28` would be something else entirely: the house `0x28` behind `g_game` – just other memory of the program, not the player.

### How the code tells you

**A step of the chain is a read whose result is then used as an address.** In the code explorer that looks like this:

| In the disassembly | In the pseudocode | In the chain |
| --- | --- | --- |
| `mov rax, qword ptr [rip + …]`, comment `g_game` – reads the global | `rax = g_game;` | the start, read with an arrow after it |
| `mov esi, 0x403108` or `lea rax, [rip + …]` – only the address, nothing read | `&g_game` | an address: offsets after it are added with `+` |
| `mov rax, qword ptr [rax + 0x28]` – 8 bytes read, then used inside `[ ]` | `rax = *(uint64_t *)(rax + 0x28);` | `→ 0x28` |
| `lea rbx, [rax + 0x14]`, `add rax, 0x14` – counted, not read | `rbx = rax + 0x14;`, `rax += 0x14;` | `+ 0x14`: the same object, the last offset changes |
| `add dword ptr [rax], edi` – the value itself, 4 bytes | `*(int32_t *)rax += a1;` | the end: the last offset, here `0x0` |

Three rules of thumb:

1. **Square brackets read** (in the pseudocode: a `*(…)` around something). The exception is `lea`: it only computes what is in its brackets.
2. **8 bytes read and then used as an address** (`qword`, `uint64_t`; in a 32-bit game 4 bytes, `dword`) is a pointer: an arrow. A 4-byte `int`, a `float` or a `double` that is only used as a number is the value itself: the chain ends there.
3. **Count the pointer reads.** Every read of an address – the global at the start counts too – is one arrow. `*(int32_t *)(*(uint64_t *)(g_game + 0x28))` reads `g_game` and the player pointer: two arrows, `→ 0x28 → 0x0`. The last read, of the gold itself, is no arrow; it only says where the chain ends.

The test game has both kinds in one place: `main` prints where the gold is, with `printf("g_game @ %p (pointer chain: g_game -> +%#zx player -> +%#zx gold)", &g_game, 40, 0)`. In the references to `g_game` that is `mov esi, 0x403108` – the address of `g_game`, not its content, so the `&`. `add_gold` instead reads the content: `mov rax, qword ptr [rip + 0x2a31]`.

### When a register already holds the object

Often a function does not read the global itself but gets the object handed over – as an argument `a1`, in `rcx` under Windows (`this` in C++). The code of that function alone does not tell where the object lies. The [native function monitor](../guide/native-tools.md#native-function-monitor) does: select the instruction, click **Monitor**, let the game run it, and look at the register.

- **The value lies in a module** – the column **Meaning** shows e.g. `game.exe+0x43e600`: the object itself is a global. Its field is at that address **plus** the offset, `game.exe+0x43e600+0xb0`. No arrow: nothing was read to get there.
- **The value lies on the heap** – no module in **Meaning**: someone read it from somewhere. **Callers** shows who calls the function; follow them until a line reads a global (`rax = g_game;`). Each read on the way is one arrow. If that gets long, let the [pointer scan](../guide/pointer-scan.md) do the searching.

### If you mixed them up

- **A `+` where an arrow belongs** lands in the memory right behind the global. The value on the list is some other number of the program and does not follow the game.
- **An arrow where a `+` belongs** takes a number for an address. The dialog **Pointer chain …** then says the chain leads nowhere right now, or the list shows nonsense.

Both are harmless – only reading is involved. Fix the entry and try again: the preview in the dialog shows at once whether the chain leads somewhere.

## What carries over to real games

- **Chains in real games are longer**, and every function shows only its part of the way. **Callers** leads to where the object comes from; the pointer scan is the check from the other end.
- **Stripped games have no names.** Globals show up as `*(uint64_t *)0x14043e600`, and the comment writes the chain with the module right away: `game.exe+0x43e600 → 0x28 → 0x0`.
- **32-bit games** have 4-byte pointers: a step of the chain reads a `dword`, not a `qword`.
