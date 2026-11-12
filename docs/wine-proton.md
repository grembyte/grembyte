# Wine and Proton

Grembyte works with Windows games running under Wine or Steam's Proton on Linux. Scans, writes and the pointer scan read the game's memory as with any process. For write watches and value hooks, Frida is the default, but the Linux `ptrace` backend can do both without injecting into the game; Grembyte selects it automatically for 32-bit Wine/Proton games. Engine tools, native trace and monitor, and scripts use Frida inside the game and need its Windows gadget.

## The Frida gadget

`frida.attach` crashes Wine processes. Instead, Grembyte loads Frida's Windows build of its *gadget* into the game as an ordinary DLL. Inside the game it sees PE modules, Windows threads and the Win64 calling convention the way the game does, and Grembyte connects to it over a local TCP port.

The gadget has to match the installed Frida version and the game's architecture: `frida-gadget-<version>-windows-<x86|x86_64>.dll`. Grembyte caches it under `~/.local/share/grembyte/gadget/`.

**Automatic downloads are off by default.** When the matching gadget is missing as you select a Wine/Proton game, Grembyte asks whether to download it from GitHub:

- Approving downloads it and turns automatic downloads on for future missing gadgets.
- Declining leaves them off.

You can also enable automatic downloads in advance under **Settings → General → Automatically download the Wine/Proton Frida gadget**.

**File → Check Frida Gadget Version …** checks locally for the gadget matching the installed Frida version – for the selected game's architecture, or for both x86 and x86_64 when none is selected – and offers a one-time download. It does not query GitHub for a newer Frida release. While Frida uses a Wine gadget, the status bar adds **(Update available)** when the matching gadget file is missing.

The gadget stays loaded until the game exits.

## Loading the gadget

A small Windows program, `grembyte-inject-x86_64.exe` or `grembyte-inject-x86.exe`, loads the gadget: Grembyte starts it with the game's Wine and prefix, and it loads the DLL into the game with `CreateRemoteThread` on `LoadLibraryW`. If that fails, Grembyte falls back to ptrace for 64-bit games: it parks one of the game's threads at a Windows function the game calls often and lets that thread load the DLL.

The helper executables are not in the repository. Grembyte builds them the first time a Wine game needs them, with MinGW (`mingw64-gcc` and `mingw32-gcc`) or zig (`pip install ziglang`). To build them by hand:

```bash
sh grembyte/attach/inject/build.sh
```

They land in `build/inject/`; `GREMBYTE_INJECT_DIR` points Grembyte to executables elsewhere.

## 32-bit games

The helper also loads the x86 gadget into 32-bit games, and the engine tools work there. Value hooks, however, need 64-bit code: for 32-bit Wine/Proton games Grembyte picks the ptrace backend for the watch and hooks by itself.

## Known limits

- **Large traces** of tens of thousands of methods froze or crashed games under Wine. Grembyte limits the trace there (5000 methods by default, see [settings](guide/settings.md)).
- **Steam Linux Runtime:** the helper runs outside Steam's container and needs the shared `/tmp` and `/dev/shm`.
- The gadget drops messages from Grembyte larger than 128 KiB; Grembyte sends larger agents in parts.
