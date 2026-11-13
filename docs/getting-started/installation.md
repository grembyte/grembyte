# Installation

## Get the source code

Grembyte is not offered as a ready-made installer. You download its source code from GitHub and install it with Python, as described further down. GitHub is a website where software projects keep their files; you need no account to download them.

The project page is **<https://github.com/grembyte/grembyte>**. There are two ways to get the files from it:

- **Download a ZIP file**: no extra programs, a few clicks in the browser. Good for trying Grembyte out. For a newer version you download the ZIP again.
- **Clone with Git**: needs the program Git once, then a single command brings your copy up to date. Recommended if you want to follow the development.

Either way you end up with a folder with the project's files, the **project directory**. All later commands on this page run in it.

!!! tip "Pick a simple place"
    Put the project directory somewhere you will find it again and whose path has no spaces or special characters, for example `~/Programs/grembyte` on Linux or `C:\Programs\grembyte` on Windows. The menu entry and shortcut installed below point to this folder, so it should stay there.

### Option 1: Download as a ZIP file

1. Open <https://github.com/grembyte/grembyte> in your browser.
2. Above the list of files, click the green **Code** button.
3. In the menu that opens, click **Download ZIP** at the bottom. The browser saves a file named `grembyte-main.zip`, usually in your *Downloads* folder.
4. Extract the ZIP file:
    - **Linux**: right-click the file in your file manager and choose *Extract here* (the wording depends on the desktop), or in a terminal: `unzip grembyte-main.zip`
    - **Windows**: right-click the file in Explorer, choose **Extract All…**, and confirm.
5. This creates a folder `grembyte-main`. Move it to the place you picked and rename it to `grembyte` if you like. This folder is your project directory.

!!! note "Do not start from inside the ZIP"
    Windows lets you open a ZIP file like a folder. Always extract it first; programs cannot run properly from inside the ZIP.

**Updating:** download the ZIP again, extract it and replace the old project directory with the new one. Then repeat the [install](#install) step, because the virtual environment `.venv` lives in the project directory. Your addresses, hooks, scripts and settings are not affected: Grembyte keeps them outside the project directory (see [Files and environment](../reference/files.md)).

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
    git clone https://github.com/grembyte/grembyte.git
    cd grembyte
    ```

=== "Windows"

    ```powershell
    mkdir C:\Programs -Force
    cd C:\Programs
    git clone https://github.com/grembyte/grembyte.git
    cd grembyte
    ```

`git clone` creates the folder `grembyte` with all files; that is your project directory. The last command already takes you into it, so you can continue straight with the [install](#install).

**Updating:** in the project directory run

```bash
git pull
```

This fetches the latest changes. If they changed the dependencies, running `python -m pip install -e .` again with the virtual environment activated brings them up to date; it does no harm if nothing changed.

!!! note "Changed files"
    `git pull` refuses to overwrite files in the project directory you edited yourself. If you did not mean to change them, `git status` lists them and `git restore <file>` puts a file back. Your Grembyte data is not in the project directory and is never touched by Git.

## Requirements

- GNU/Linux or Windows on x86-64
- Python 3.11 or newer, with `venv` and `pip`
- On Linux a graphical session with X11 or Wayland

The Python dependencies, PySide6 (Qt) among them, are installed with `pip` from `pyproject.toml` into the virtual environment; Qt itself comes with PySide6. What has to come from the system is Python and, on Linux, a few X11/XCB libraries that Qt's `xcb` platform plugin loads. Desktop installations usually have most of them, but `libxcb-cursor` is often missing, and Qt 6 needs it for the `xcb` plugin.

### System packages on Linux

=== "Debian, Ubuntu, Linux Mint"

    ```bash
    sudo apt update
    sudo apt install python3 python3-venv python3-pip
    sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 \
        libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xkb1 \
        libegl1 libgl1 libfontconfig1 libdbus-1-3
    ```

=== "Fedora"

    ```bash
    sudo dnf install python3 python3-pip
    sudo dnf install xcb-util-cursor xcb-util-wm xcb-util-image xcb-util-keysyms \
        xcb-util-renderutil libxkbcommon-x11 mesa-libEGL mesa-libGL fontconfig dbus-libs
    ```

=== "Arch, Manjaro"

    ```bash
    sudo pacman -S --needed python python-pip
    sudo pacman -S --needed xcb-util-cursor xcb-util-wm xcb-util-image xcb-util-keysyms \
        xcb-util-renderutil libxkbcommon-x11 libglvnd fontconfig dbus
    ```

=== "openSUSE"

    ```bash
    sudo zypper install python3 python3-pip
    sudo zypper install libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 \
        libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xkb1 \
        libEGL1 libGL1 fontconfig libdbus-1-3
    ```

    On Leap the default `python3` can be older than 3.11; install a newer one, e.g. `python311`, and create the virtual environment with `python3.11 -m venv .venv`.

On a Wayland session Qt uses its `wayland` plugin, whose libraries every Wayland desktop already has. The XCB packages are still worth installing: Qt falls back to `xcb` (through XWayland) when the Wayland plugin cannot start. If Grembyte still does not start, see [Troubleshooting](#troubleshooting).

### For Windows games under Wine or Proton

Grembyte builds a small Windows helper the first time a Wine/Proton game needs it (see [Wine and Proton](../wine-proton.md#loading-the-gadget)). It needs MinGW, the compilers `x86_64-w64-mingw32-gcc` and `i686-w64-mingw32-gcc`:

| System | Packages |
| --- | --- |
| Debian, Ubuntu, Linux Mint | `gcc-mingw-w64-x86-64 gcc-mingw-w64-i686` |
| Fedora | `mingw64-gcc mingw32-gcc` |
| Arch, Manjaro | `mingw-w64-gcc` |
| openSUSE | `mingw64-cross-gcc mingw32-cross-gcc` |

Without MinGW it uses zig instead: `./.venv/bin/pip install ziglang` installs it into Grembyte's virtual environment, where the build finds it.

On Windows nothing besides Python is needed: install it from <https://www.python.org/downloads/windows/> (tick *Add python.exe to PATH*) or with `winget install --id Python.Python.3.13 -e`.

### Supported platforms

**Tested**: Grembyte is developed on these systems, and every release is tried on them:

| System | Version | Notes |
| --- | --- | --- |
| Fedora Linux | 44 | KDE Plasma on Wayland, Python 3.14; native games and Windows games under Wine/Proton |
| Windows | 11 26H2 | |

**Expected to work, not tested**: these meet the requirements above, but nobody checks them regularly. Problems specific to them may go unnoticed until someone reports them:

| System | Version | Notes |
| --- | --- | --- |
| Debian | 12 and newer | Python 3.11 from the distribution |
| Ubuntu, Linux Mint | Ubuntu 24.04 / Mint 22 and newer | Ubuntu 22.04 ships Python 3.10, which is too old |
| Arch Linux, Manjaro | current | |
| openSUSE | Tumbleweed; Leap with Python 3.11 or newer | |
| Other Linux distributions | | with Python 3.11+ and the libraries above |
| Windows | 10 22H2 | |

**Not supported**: macOS, 32-bit systems and ARM computers (Grembyte reads x86 and x86-64 code).

If Grembyte runs on a system that is not tested, or does not, a [bug report](https://github.com/grembyte/grembyte/issues) or a short note helps.

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

The editable install also sets up the console command `grembyte`.

## Start

With the virtual environment activated:

```bash
grembyte
```

Or straight from the project directory, without activating anything:

```bash
./.venv/bin/python -m grembyte
```

### Menu entry and shortcut

=== "Linux"

    `scripts/grembyte.sh` activates the virtual environment (`.venv` in the project, or `$GREMBYTE_VENV`) and starts Grembyte from any working directory. A menu entry for the current user (KDE, GNOME, …) appears under Development and Games:

    ```bash
    ./scripts/install-desktop-entry.sh              # install or update the entry
    ./scripts/install-desktop-entry.sh --uninstall  # remove it
    ```

    The entry points to the project directory, so run the script again after moving the repository.

=== "Windows"

    `scripts\grembyte.cmd` starts Grembyte the same way. For the Start menu and the taskbar, install a shortcut for the current user, then right-click "Grembyte" in the Start menu and choose "Pin to taskbar":

    ```powershell
    powershell -ExecutionPolicy Bypass -File scripts\install-windows-shortcut.ps1             # install or update
    powershell -ExecutionPolicy Bypass -File scripts\install-windows-shortcut.ps1 -Uninstall  # remove it
    ```

## Language

The interface is German or English, depending on the system locale (English for any other locale). Choose it under [Settings → General …](../guide/settings.md), or for one start with `GREMBYTE_LANGUAGE=en` or `de` (see [environment variables](../reference/files.md)).

## First start

At the first start Grembyte creates its SQLite database, where it keeps addresses, hooks, templates, scripts and settings per game. [Files and environment](../reference/files.md) lists where it lives and how to move it.

On Linux, continue with the [permissions](permissions.md) Grembyte needs to read another process's memory.

## Troubleshooting

### A Python module is missing

```text
ModuleNotFoundError: No module named 'PySide6'
```

(or `frida`, `capstone`, `sqlalchemy`, `grembyte`, …) almost always means that Python runs outside the virtual environment, or that the install step did not finish.

1. Check which Python runs. With the environment activated, the path must end in the project's `.venv`:

    ```bash
    python -c "import sys; print(sys.prefix)"
    ```

    If it does not, activate it (`source .venv/bin/activate`, on Windows `.venv\Scripts\activate`) or start Grembyte with `./.venv/bin/python -m grembyte`. The menu entry and the shortcut use `.venv` by themselves.

2. Install again and read the end of the output; an error there names the package that failed:

    ```bash
    python -m pip install -e .
    ```

3. Check the dependencies:

    ```bash
    python -m pip check
    python -c "import PySide6, frida, capstone, psutil, numpy, sqlalchemy; print('ok')"
    ```

A failed `pip install` usually comes from a Python that is too old (`python --version`, at least 3.11) or a version so new that a package has no prebuilt wheel for it yet. Then create the environment with an older Python, e.g. `python3.13 -m venv .venv`, after deleting the old `.venv`. After moving the project directory, also delete `.venv` and install again: a virtual environment does not survive being moved.

### Qt cannot load a platform plugin

```text
qt.qpa.plugin: From 6.5.0, xcb-cursor0 or libxcb-cursor0 is needed to load the Qt xcb platform plugin.
qt.qpa.plugin: Could not load the Qt platform plugin "xcb" in "" even though it was found.
This application failed to start because no Qt platform plugin could be initialized.
```

The plugin is there, but a system library it needs is missing. Install the [system packages](#system-packages-on-linux) for your distribution first. If that does not help:

1. Let Qt report what fails:

    ```bash
    QT_DEBUG_PLUGINS=1 grembyte
    ```

    The output names the plugin and the library that could not be loaded (`Cannot load library …: libxcb-cursor.so.0: cannot open shared object file`).

2. List every library of the `xcb` plugin that is not found:

    ```bash
    ldd .venv/lib/python3*/site-packages/PySide6/Qt/plugins/platforms/libqxcb.so | grep "not found"
    ```

    Search your package manager for the missing file name, e.g. `dnf provides '*/libxcb-cursor.so.0'`, `apt-file search libxcb-cursor.so.0` or `pacman -F libxcb-cursor.so.0`.

3. Try the other platform plugin. On Wayland, `wayland` and `xcb` are both possible:

    ```bash
    QT_QPA_PLATFORM=wayland grembyte
    QT_QPA_PLATFORM=xcb grembyte
    ```

    Remove a `QT_QPA_PLATFORM` set in your shell profile or environment that names a plugin your session does not have.

A message `Could not find the Qt platform plugin "…" in ""` (*find*, not *load*) means that PySide6 itself is incomplete: install it again with `python -m pip install --force-reinstall PySide6-Essentials`.

If neither helps, open a [bug report](https://github.com/grembyte/grembyte/issues) with the output of `QT_DEBUG_PLUGINS=1 grembyte`, your distribution and its version.
