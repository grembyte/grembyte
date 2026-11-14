"""The Steam launch option that starts a game through grembyte-cli.

Steam runs a launch option with its own PATH (``/usr/local/bin:/usr/bin:/bin``
on a desktop session), not the one of the user's shell: a grembyte-cli in
``~/.local/bin`` is not found there. So the option names grembyte-cli by its
full path, unless one of Steam's folders holds this very installation's
grembyte-cli (a link in /usr/local/bin, see docs/guide/launch-option.md).
"""

import os
import shlex
import shutil
import sys
from pathlib import Path

CLI_NAME = "grembyte-cli"
# The folders Steam looks in for a launch option's command
STEAM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")


def own_cli(executable=None):
    """The grembyte-cli of this installation (next to its Python, as a venv
    has it), else the one on the PATH, else None."""
    beside = Path(executable or sys.executable).parent / CLI_NAME
    if beside.is_file():
        return beside
    found = shutil.which(CLI_NAME)
    return Path(found) if found else None


def launch_option(executable=None, steam_path=STEAM_PATH):
    """The launch option for Steam (``... %command%``), or None without a grembyte-cli."""
    own = own_cli(executable)
    if own is None:
        return None
    on_path = shutil.which(CLI_NAME, path=os.pathsep.join(steam_path))
    if on_path is not None and os.path.realpath(on_path) == os.path.realpath(own):
        command = CLI_NAME
    else:
        command = shlex.quote(str(own))
    return f"{command} %command%"
