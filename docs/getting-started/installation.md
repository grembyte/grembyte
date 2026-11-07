# Installation

## Requirements

- GNU/Linux or Windows on x86-64
- Python 3.11 or newer, with `venv` and `pip`
- On Linux a graphical session with X11 or Wayland

On Debian or Ubuntu, Python and the tools come from the distribution if needed:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
```

The Python dependencies, PySide6 among them, are installed with `pip` from `pyproject.toml`. If Qt reports a missing platform plugin at start, add the distribution's Qt/XCB runtime packages.

## Install

In the project directory:

=== "Linux"

    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install --upgrade pip
    python -m pip install -e .
    ```

=== "Windows"

    ```powershell
    py -m venv .venv
    .venv\Scripts\activate
    python -m pip install --upgrade pip
    python -m pip install -e .
    ```

The editable install also sets up the console command `hermes`.

## Start

With the virtual environment activated:

```bash
hermes
```

Or straight from the project directory, without activating anything:

```bash
./.venv/bin/python -m hermes
```

### Menu entry and shortcut

=== "Linux"

    `scripts/hermes-hook.sh` activates the virtual environment (`.venv` in the project, or `$HERMES_VENV`) and starts Hermes from any working directory. A menu entry for the current user (KDE, GNOME, …) appears under Development and Games:

    ```bash
    ./scripts/install-desktop-entry.sh              # install or update the entry
    ./scripts/install-desktop-entry.sh --uninstall  # remove it
    ```

    The entry points to the project directory, so run the script again after moving the repository.

=== "Windows"

    `scripts\hermes-hook.cmd` starts Hermes the same way. For the Start menu and the taskbar, install a shortcut for the current user, then right-click "Hermes Hook" in the Start menu and choose "Pin to taskbar":

    ```powershell
    powershell -ExecutionPolicy Bypass -File scripts\install-windows-shortcut.ps1             # install or update
    powershell -ExecutionPolicy Bypass -File scripts\install-windows-shortcut.ps1 -Uninstall  # remove it
    ```

## Language

The interface is German or English, depending on the system locale (English for any other locale). Choose it under **Settings → General …**, or for one start with an environment variable, which applies while the setting is "as the system":

```bash
HERMES_LANGUAGE=en hermes
HERMES_LANGUAGE=de hermes
```

## First start

At the first start Hermes creates its SQLite database, where it keeps addresses, hooks, templates, scripts and settings per game. [Files and environment](../reference/files.md) lists where it lives and how to move it.

On Linux, continue with the [permissions](permissions.md) Hermes needs to read another process's memory.
