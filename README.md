# Hermes Hook

Hermes Hook is a desktop application for inspecting and manipulating the memory of running games. It runs on Linux, including Windows games under Wine/Proton, and on Windows. The project is under development.

**Documentation: <https://winterhafen.github.io/hermes-hook/>** – installation, tutorials, the user guide and examples with screenshots. Its sources are in [`docs/`](docs/index.md); `mkdocs serve` shows them locally (see [Development](docs/development/index.md#documentation)).

## Disclaimer

This tool is intended solely for educational, research, and single-player game modding purposes. It is not designed or intended to bypass anti-cheat mechanisms or be used in online multiplayer games. The developer assumes no responsibility or liability for any account bans, suspensions, or damages resulting from the use or misuse of this software.

Use of the feature for restoring memory modifications may be detected by anti-tamper systems (such as Arxan) and, when used online, carries a considerable risk of account bans or soft bans. Use it at your own risk.

## Features

- **Memory scan** for integers and floats, with follow-up scans, tolerance and rounded floats
- **Tracked addresses** with live values, writing and freezing
- **Pointer scan** that finds the value again after the game restarts
- **Write and read watch**: which code writes or reads an address
- **Hooks** on write instructions: multiply, fix, clamp or compute values in JavaScript, reinstalled at every start
- **Hook templates**, testable without a game, and a debug window for hooks
- **Scripts** that call game code on a schedule or by hand
- **Native trace and function monitor** for any engine
- **Unity (Mono and IL2CPP) and Unreal Engine**: catalog of classes and methods, trace, method monitor, object inspector, object browser (Mono and Unreal), method hooks and calls; GAS attributes for Unreal
- **Wine and Proton** support through Frida's Windows gadget
- Memory viewer, sessions, activity log, export and import per game, color themes, German and English

## Quick start

Python 3.11 or newer is needed. In the project directory:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
./.venv/bin/hermes
```

On Linux, Hermes needs `kernel.yama.ptrace_scope=0` to access a game started separately. The [installation guide](https://winterhafen.github.io/hermes-hook/getting-started/installation/), the [permissions](https://winterhafen.github.io/hermes-hook/getting-started/permissions/) and a [tutorial](https://winterhafen.github.io/hermes-hook/getting-started/first-hook/) with the test game in `test_app/` have the details.

## License

Copyright (C) 2026 Frank Hoogen

Hermes Hook is distributed under the MIT License. It is provided without any warranty; see [LICENSE](LICENSE).

The third-party components and their licenses are listed in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES); the full license texts are in [LICENSES](LICENSES/).
