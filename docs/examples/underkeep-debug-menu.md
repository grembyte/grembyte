# Underkeep: a hidden debug menu

Underkeep runs on an engine of its own under Proton, so there is no engine adapter: this example uses [scripts in native games](../guide/scripts.md#scripts-in-native-games), which find code by its bytes. It needs the [code explorer](../guide/code-explorer.md) to follow along, and no other preparation.

Underkeep (own engine, under Proton) has a hidden debug menu. Two native scripts make it usable: the first opens the menu, the second switches on the commands behind its buttons.

```js title="Open the hidden debug menu (Underkeep, own engine, under Proton)"
const n = ctx.native;
// OpenPage(int page, bool replace): push rbp / sub rsp, 0x20 / cmp [menu], 0 / mov ebp, ecx / je
const openPageAt = n.scan("40 55 48 83 EC 20 83 3D ?? ?? ?? ?? 00 8B E9 0F 84");
// The cmp at +6 reads the menu object: +0 state (0: closed), +4 page
const menu = n.rip(openPageAt.add(6), 2, 7);
if (menu.readS32() === 0) return "Open the in-game menu first (Esc)";
n.fn(openPageAt, "void", ["int", "uint8"])(11, 0);
return "Debug menu opened";
```

The menu opens, but its buttons do nothing yet. Each button sends a console command (`heal_party`, `kill_nearby_enemies`, …), and each command first asks a check that in the release build only logs *"This command is available only in debug mode."* and returns false. The [code explorer](../guide/code-explorer.md#from-a-string-to-the-code) leads there from that message. A second script patches the check so that it returns true (`mov al, 1; ret`); run again, it restores the original bytes:

```js title="Switch the debug commands on or off (Underkeep, own engine, under Proton)"
const n = ctx.native;
// The check: push rbx / sub rsp, 0x50 / mov ebx, -1 / lea rdx, [message] / … /
// xor al, al / add rsp, 0x50 / pop rbx / ret. The first three bytes are left
// open: they are what this script changes.
const check = n.scan(
  "?? ?? ?? 83 EC 50 BB FF FF FF FF 48 8D 15 ?? ?? ?? ?? 44 8B C3 48 8D 4C 24 30 " +
  "E8 ?? ?? ?? ?? 45 33 C9 48 C7 44 24 20 00 00 00 00 45 33 C0 48 8D 54 24 30 " +
  "8D 4B 02 E8 ?? ?? ?? ?? 48 8B 44 24 30 48 85 C0 74 13 F0 0F C1 18 83 FB 01 " +
  "75 0A 48 8B 4C 24 30 E8 ?? ?? ?? ?? 32 C0 48 83 C4 50 5B C3");
const ORIGINAL = [0x40, 0x53, 0x48];  // push rbx / (sub rsp …)
const ALLOW = [0xB0, 0x01, 0xC3];     // mov al, 1 / ret
const now = Array.from(new Uint8Array(check.readByteArray(3)));
const is = bytes => bytes.every((value, i) => value === now[i]);
if (!is(ORIGINAL) && !is(ALLOW))
  throw new Error(`unexpected bytes at ${check}: ${now.map(b => b.toString(16)).join(" ")}`);
const allow = is(ORIGINAL);
Memory.patchCode(check, 3, code => code.writeByteArray(allow ? ALLOW : ORIGINAL));
return allow ? "Debug commands enabled" : "Debug commands disabled again";
```

To use the debug menu:

1. Attach Grembyte to the game and add both scripts in the **Scripts** tab, each set to *Only when run by hand*.
2. Run **Switch the debug commands on or off** once. The result says *Debug commands enabled*.
3. In the game, open the in-game menu with ++esc++, then run **Open the hidden debug menu**. Page 11 of the menu appears: kill nearby or all enemies, level up the party, unlock skills, 100 skill points, heal the party, reveal the map, unlock all recipes and fast travel, weather, and more.
4. The buttons now work. To switch the commands off again, run the second script once more.

The patch lives only in the game's memory: restarting the game undoes it, and the scripts must be run again. Both were found in version 1.0.4; their byte patterns keep them working as long as an update leaves the two functions alone.

!!! warning "Achievements and statistics"
    The debug commands include `reset_steam_stats_and_achievements`, which resets your Steam statistics and achievements. Achievements may also still unlock while the commands are enabled.

<small>Underkeep is named on this page only to illustrate how Grembyte works. All game names, trademarks and game content belong to their respective owners. Grembyte is not affiliated with, endorsed or sponsored by them.</small>
