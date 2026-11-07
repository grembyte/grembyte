# Installation

## Get the source code

Hermes Hook is not offered as a ready-made installer. You download its source code from GitHub and install it with Python, as described further down. GitHub is a website where software projects keep their files; you need no account to download them.

The project page is **<https://github.com/winterhafen/hermes-hook>**. There are two ways to get the files from it:

- **Download a ZIP file**: no extra programs, a few clicks in the browser. Good for trying Hermes out. For a newer version you download the ZIP again.
- **Clone with Git**: needs the program Git once, then a single command brings your copy up to date. Recommended if you want to follow the development.

Either way you end up with a folder with the project's files, the **project directory**. All later commands on this page run in it.

!!! tip "Pick a simple place"
    Put the project directory somewhere you will find it again and whose path has no spaces or special characters, for example `~/Programs/hermes-hook` on Linux or `C:\Programs\hermes-hook` on Windows. The menu entry and shortcut installed below point to this folder, so it should stay there.

### Option 1: Download as a ZIP file

1. Open <https://github.com/winterhafen/hermes-hook> in your browser.
2. Above the list of files, click the green **Code** button.
3. In the menu that opens, click **Download ZIP** at the bottom. The browser saves a file named `hermes-hook-main.zip`, usually in your *Downloads* folder.
4. Extract the ZIP file:
    - **Linux**: right-click the file in your file manager and choose *Extract here* (the wording depends on the desktop), or in a terminal: `unzip hermes-hook-main.zip`
    - **Windows**: right-click the file in Explorer, choose **Extract All…**, and confirm.
5. This creates a folder `hermes-hook-main`. Move it to the place you picked and rename it to `hermes-hook` if you like. This folder is your project directory.

!!! note "Do not start from inside the ZIP"
    Windows lets you open a ZIP file like a folder. Always extract it first; programs cannot run properly from inside the ZIP.

**Updating:** download the ZIP again, extract it and replace the old project directory with the new one. Then repeat the [install](#install) step, because the virtual environment `.venv` lives in the project directory. Your addresses, hooks, scripts and settings are not affected: Hermes keeps them outside the project directory (see [Files and environment](../reference/files.md)).

### Option 2: Clone with Git

Git is a program that downloads a project and later fetches only the changes. *Cloning* means making your own local copy of the project.

**Install Git** (once):

=== "Linux"

    Git usually comes from the distribution's package manager. Check first whether it is already there:

    ```bash
    git --version
    ```

    If the command is not found, install it:

    ```bash
    sudo apt install git     # Debian, Ubuntu, Linux Mint
    sudo dnf install git     # Fedora
    sudo pacman -S git       # Arch, Manjaro, SteamOS desktop mode
    ```

=== "Windows"

    Download Git for Windows from <https://git-scm.com/downloads/win> and run the installer. The default settings on every page are fine; just click **Next** through to **Install**.

    Alternatively, in a terminal with `winget`:

    ```powershell
    winget install --id Git.Git -e
    ```

    Afterwards open a **new** terminal (PowerShell or *Terminal* from the Start menu) and check that it worked with `git --version`.

**Clone the project:**

Open a terminal (Linux: e.g. *Konsole* or *Terminal*; Windows: *PowerShell* or *Terminal*), change to the folder that should contain the project directory, and clone:

=== "Linux"

    ```bash
    mkdir -p ~/Programs
    cd ~/Programs
    git clone https://github.com/winterhafen/hermes-hook.git
    cd hermes-hook
    ```

=== "Windows"

    ```powershell
    mkdir C:\Programs -Force
    cd C:\Programs
    git clone https://github.com/winterhafen/hermes-hook.git
    cd hermes-hook
    ```

`git clone` creates the folder `hermes-hook` with all files; that is your project directory. The last command already takes you into it, so you can continue straight with the [install](#install).

**Updating:** in the project directory run

```bash
git pull
```

This fetches the latest changes. If they changed the dependencies, running `python -m pip install -e .` again with the virtual environment activated brings them up to date; it does no harm if nothing changed.

!!! note "Changed files"
    `git pull` refuses to overwrite files in the project directory you edited yourself. If you did not mean to change them, `git status` lists them and `git restore <file>` puts a file back. Your Hermes data is not in the project directory and is never touched by Git.

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
