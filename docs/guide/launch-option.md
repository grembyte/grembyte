# Starting games with grembyte-cli

`grembyte-cli` starts a game and installs the hooks, method hooks and scripts Grembyte saved for it, without the window. You set a game up once in Grembyte, then play it through the launch option.

## Setting it up

1. Attach to the game in Grembyte and set up its [hooks](hooks.md), method hooks and [scripts](scripts.md). They are saved for the program, as always.
2. In Grembyte, choose **File → Copy Steam launch option**. It puts the launch option on the clipboard: `grembyte-cli` in front of `%command%`, with the full path of your installation:

    ```text
    /path/to/grembyte/.venv/bin/grembyte-cli %command%
    ```

3. In Steam, open the game's **Properties → General → Launch options** and paste it.
4. Start the game from Steam.

### Without the path

Steam looks for the command of a launch option only in `/usr/local/bin`, `/usr/bin` and `/bin`, not in the folders of your shell's `PATH` such as `~/.local/bin`. To write just `grembyte-cli %command%`, link it into `/usr/local/bin` once:

```bash
sudo ln -s /path/to/grembyte/.venv/bin/grembyte-cli /usr/local/bin/grembyte-cli
```

A link, not a copy: it keeps using Grembyte's virtual environment, and updates of Grembyte apply. **Copy Steam launch option** then copies the short form. It does so only if the link leads to the installation it runs from; for another one it keeps the full path.

`grembyte-cli` starts the command first, so the game runs whatever goes wrong in Grembyte. Among the processes the command starts it looks for the one with something saved for it. It waits a few seconds so the engine can load, then attaches. It ends when the command ends and exits with the command's exit code.

| Option | Meaning |
| --- | --- |
| `--delay SECONDS` | How long to wait after the game was found before attaching (default 5). Raise it for games that load their engine slowly. |
| `--no-sound` | No [sound](scripts.md#hotkeys) when a hotkey ran its script, for this launch; the setting stays as it is. |
| `-h`, `--help` | Show the options. |

Options go before `%command%`, for example `grembyte-cli --delay 15 %command%` (with the full path, unless you linked it as above).

## What it installs

- **Hooks** and **method hooks**, as the hook panel restores them at attach. A hook that learns its callers or its C# method keeps them, as in the window.
- **Scripts** that run once or repeatedly, and manual scripts with a [hotkey](scripts.md#hotkeys): pressed in the game, it runs the script with the defaults of its parameters. The hotkey of a repeating script pauses and resumes it, and the change is saved, as in the window. Manual scripts without a hotkey are left out: only the window can run them.
- If a session ends while the game still runs, `grembyte-cli` starts it again up to three times.

Its messages go to the terminal (start Steam from a terminal to see them) and to `grembyte.log` (see [Files and environment](../reference/files.md)).

`grembyte-cli` never attaches to a game that ships EasyAntiCheat or BattlEye. The game then runs without Grembyte.

`grembyte-cli` runs on Linux only: it finds the game through `/proc`.

## Proton games

`grembyte-cli` loads the [Frida gadget](../wine-proton.md#the-frida-gadget) as the window does, but it never asks: if the matching gadget is missing and automatic downloads are off (the default), it reports that it could not attach, and the game runs without Grembyte. Attach to the game once in the window, which offers the download, or switch automatic downloads on in the [settings](settings.md). For 32-bit games it picks the ptrace backend for hooks by itself, as the window does (see [32-bit games](../wine-proton.md#32-bit-games)).

## Permissions

Since the game is a child of `grembyte-cli`, attaching also works with `kernel.yama.ptrace_scope = 1`, as long as the game stays a descendant. If a launcher starts the game and then exits, the game is no longer a descendant. It then needs `ptrace_scope = 0`, as with the window (see [Permissions](../getting-started/permissions.md)).

## Taking the game over in the window

While `grembyte-cli` runs a game, the window can take the game over, for example to change a hook:

1. Open the process list. A game that runs through `grembyte-cli` is marked with a link icon. **Only grembyte-cli** next to the filter shows only these games.
2. Attach to it. Grembyte asks whether to take the game over from `grembyte-cli`.
3. Confirm. `grembyte-cli` removes its hooks and stops its sessions, then the window attaches and restores the saved hooks and scripts as usual.

If you decline, the window does not attach. Two sets of the same hooks in one game can crash it.

Once the window detaches, attaches to another process or is closed (also if it crashes), `grembyte-cli` attaches again and installs what is saved then, including your changes. Taking the game over needs `ptrace_scope = 0`: the game is no child of the window.
