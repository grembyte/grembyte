"""Short system sounds when a hotkey ran its script.

In a fullscreen game nothing shows that a hotkey did anything; a sound does.
It plays in Grembyte's own process, never in the game's, so nothing changes
under Wine. On Linux ``pw-play`` or ``paplay`` play the freedesktop sound
theme's file from /usr/share/sounds/freedesktop/stereo/. Not through
libcanberra first: it plays as a system notification, and with Plasma's
notification sounds muted (as many keep them on a desktop) nothing was heard;
Grembyte's own setting (and ``grembyte-cli --no-sound``) turns these sounds
off. ``canberra-gtk-play -i <name>`` is left for a system without either
player or file, and without it nothing sounds. Windows has its message beeps.

``feedback(result, error)`` picks the sound of a script run: an error, a
script that switched something on (it returned ``true``) or off (``false``),
or any other success.
"""

import shutil
import subprocess
import sys
import threading
from pathlib import Path

SUCCESS, ERROR, ON, OFF = "success", "error", "on", "off"
# The freedesktop sound theme's names
THEME_NAMES = {SUCCESS: "bell", ERROR: "dialog-warning", ON: "device-added",
               OFF: "device-removed"}
THEME_FOLDER = Path("/usr/share/sounds/freedesktop/stereo")
# How long a player may take before it is left alone
PLAY_TIMEOUT_S = 10


def feedback(result, error=None):
    """The sound of a script run: ERROR, ON (it returned true), OFF (false) or SUCCESS."""
    if error:
        return ERROR
    text = str(result or "").strip().lower()
    if text == "true":
        return ON
    if text == "false":
        return OFF
    return SUCCESS


def command(sound, which=shutil.which, folder=THEME_FOLDER):
    """The command that plays ``sound`` on Linux, or None when nothing can."""
    name = THEME_NAMES[sound]
    file = folder / f"{name}.oga"
    if file.is_file():
        for player in ("pw-play", "paplay"):
            if which(player):
                return [player, str(file)]
    if which("canberra-gtk-play"):
        return ["canberra-gtk-play", "-i", name, "-d", "Grembyte"]
    return None


def _windows_beep(sound):
    import winsound  # pylint: disable=import-outside-toplevel,import-error
    kinds = {SUCCESS: winsound.MB_OK, ERROR: winsound.MB_ICONHAND,
             ON: winsound.MB_ICONASTERISK, OFF: winsound.MB_ICONEXCLAMATION}
    winsound.MessageBeep(kinds[sound])


def _run(args):
    try:
        subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=PLAY_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        pass  # no sound rather than an error


def play(sound):
    """Play ``sound`` (SUCCESS, ERROR, ON, OFF) without waiting for it; silent
    when the system has no way to."""
    if sys.platform == "win32":
        try:
            _windows_beep(sound)
        except (ImportError, RuntimeError):
            pass
        return
    args = command(sound)
    if args is not None:
        # A thread waits for the player, so that it leaves no zombie behind
        threading.Thread(target=_run, args=(args,), daemon=True).start()
