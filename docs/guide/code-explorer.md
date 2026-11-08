# Code explorer

**View → Code explorer …** finds the code behind something the game shows or logs. It reads a module's file – the game's executable or one of its libraries, a PE image (also under Wine/Proton) or an ELF file – and lists its strings, the instructions that refer to them, its functions and their callers. Because it reads the file and not the game's memory, it cannot disturb the game, and it also works without a game attached (**Open file …**). x86 and x86-64 code is supported.

<figure markdown="span">
  ![Code explorer](../assets/screenshots/code-explorer-dark.webp#only-dark)
  ![Code explorer](../assets/screenshots/code-explorer-light.webp#only-light)
  <figcaption>The test game: the button text "Find treasure" among the strings, the instruction in <code>on_activate</code> that hands its handler <code>on_treasure</code> to GTK, and <code>on_treasure</code> calling <code>add_gold</code>.</figcaption>
</figure>

!!! example "Step by step with the test game"
    The [code explorer example](../examples/dummy-game-code-explorer.md) goes from the button **Find treasure** of the tutorial's test game to the function that pays out the gold, and ends with a native script that gives 500 gold through it.

It is the static counterpart of the [native trace and monitor](native-tools.md): the trace shows which functions run, the explorer shows what a function does and who calls it. A function found here can be [called from a script](scripts.md#scripts-in-native-games).

## From a string to the code

Most ways into unknown code start with a text: a menu entry, a message, a log line, a console command.

1. Choose the **module**. The game's executable comes first, then the modules in its folder. Indexing a large executable takes a few seconds; the window keeps the result while Hermes runs.
2. Type words of the text into the filter of the **Strings** tab and select the string. The references list shows every instruction that loads its address, with the function it is in.
3. Double-click a reference to open its **function**. Above the disassembly, the explorer shows the function's **signature guessed from its code** (x86-64): `void add_gold(int rdi)`, from the argument registers the function reads before writing them and from what its callers do with the result – see [how it is guessed](../examples/dummy-game-code-explorer.md#4-what-add_gold-takes). The disassembly names what instructions refer to: strings, functions, imported functions of other modules (`SDL_PollEvent`, `kernel32.dll!CreateFileW`), and pointers to strings or functions.
4. **Callers** lists who calls the function. Double-click one to go there; **Back** returns.

A string is not always loaded by the code directly. Tables of names (commands, settings) hold pointers to their strings; the explorer lists such a pointer as a reference of the kind *pointer*. Double-clicking it shows who reads that table entry.

**Go to** takes an address of the file (e.g. `0x140257270`), an address in the game's memory, `module+0x…`, or the name of an export or symbol. Code opens its function; data shows who refers to it. Double-clicking an instruction follows the address it refers to.

## Using what you found

| Button | Does |
| --- | --- |
| **Copy pattern** | Copies the shortest unique byte pattern starting at the selected instruction (or the function's start). Addresses, branch targets and RIP-relative displacements become `??`, so the pattern still matches after an update that moved the code around. The status line says whether it is unique. |
| **Copy script** | Copies a [native script](scripts.md#scripts-in-native-games) that finds the selected instruction or function by that pattern. For a function it prepares a call with `ctx.native.fn`, with the arguments and return type guessed from the code; for an instruction loading an address it resolves that address (`ctx.native.rip`, or the absolute address in a 32-bit game). Paste it into a new script and adjust it. |
| **Monitor** | Logs the calls of the function, or the runs of the selected instruction, in the [native function monitor](native-tools.md#native-function-monitor). Needs the game attached. |
| **Memory** | Shows what the selected instruction refers to in the [memory viewer](tools.md). Needs the game attached. |

## Example: why a debug menu does nothing

Underkeep has a hidden debug menu whose buttons have no effect. In the explorer:

1. The string `heal party` (the button's label) has one reference, in the function building the menu. Next to the label that function loads a second string, `heal_party`.
2. `heal_party` has a second reference: a function registering console commands, each name with a handler.
3. The handler's first instruction calls a function that only loads `"This command is available only in debug mode."` and returns false: the release build switches the commands off there.

**Copy script** on that function gives the pattern a script needs to find it in every version of the game that leaves it alone; the script then makes it return true.

## Limits

- Functions come from the executable's exception directory (64-bit PE), its symbols (ELF), its exports and the targets of calls; names from its symbols, exports and imports. Code reached only through tables of addresses may show no function; **Go to** still finds its references.
- The disassembly is a linear sweep: data inside the code section (jump tables) shows as odd instructions.
- Packed or encrypted executables (some copy protections) hold no readable code in their file.
