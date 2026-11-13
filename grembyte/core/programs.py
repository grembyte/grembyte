"""Which program a process runs, as Grembyte names it.

The process list and grembyte-cli both name a process by ``(name, path)``;
the saved hooks and scripts of a program are kept under that pair (see
``grembyte.db.models.Process``), so both must work it out alike.
"""

import ntpath
import os
import re


_PYTHON_NAME = re.compile(r"^python[\d.]*w?$")
# The Linux programs that run Windows programs under Wine/Proton
_WINE_LOADER = re.compile(r"^wine(64)?(-preloader)?$")
# Python options that consume the following argument.
_PYTHON_OPTS_WITH_ARG = {"-c", "-m", "-W", "-X", "--check-hash-based-pycs"}


def resolve_display(name, exe, cmdline, cwd):
    """Return (name, path) to display: the script of a Python process, the
    Windows program of a Wine process.

    For interpreters, the kernel only knows the interpreter binary; the
    script is merely an argument in the command line.
    """
    if cmdline and _WINE_LOADER.match(os.path.basename(exe)):
        # The kernel's name of a Wine process is that of its main thread,
        # which games rename (Unreal: "GameThread"); its program is the
        # Windows path in the command line.
        program = cmdline[0].strip()
        if program.lower().endswith(".exe"):
            return ntpath.basename(program), program
    if not cmdline or not _PYTHON_NAME.match(os.path.basename(cmdline[0]) or name):
        return name, exe
    args = iter(cmdline[1:])
    for arg in args:
        if arg == "-m":
            module = next(args, "")
            return (f"{name} -m {module}" if module else name), exe
        if arg in _PYTHON_OPTS_WITH_ARG:
            next(args, None)
            continue
        if arg.startswith("-"):
            continue
        script = arg if os.path.isabs(arg) or not cwd else os.path.join(cwd, arg)
        return os.path.basename(arg), os.path.normpath(script)
    return name, exe


def program_of(pid):
    """``(name, path)`` of the program ``pid`` runs, as the process list names it;
    None if the process is gone or not readable."""
    import psutil  # pylint: disable=import-outside-toplevel
    try:
        info = psutil.Process(pid).as_dict(["name", "exe", "cmdline", "cwd"])
    except psutil.Error:
        return None
    return resolve_display(info["name"] or "", info["exe"] or "", info["cmdline"], info["cwd"])
