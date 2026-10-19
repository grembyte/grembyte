"""Information about Linux processes from /proc."""

from pathlib import Path


def process_start_time(pid, proc_root="/proc"):
    """Return when ``pid`` was started (clock ticks after boot), or None.

    None means the process has exited, including zombies that wait for their
    parent. Comparing start times tells a restarted program that got the same
    PID apart from the original one.
    """
    try:
        stat = (Path(proc_root) / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The command name may contain spaces and parentheses; fields follow the
    # last ')'. They start with field 3 (state); starttime is field 22.
    fields = stat[stat.rindex(")") + 2:].split()
    if fields[0] in {"Z", "X", "x"}:
        return None
    return int(fields[19])


def is_wine_process(pid, proc_root="/proc"):
    """Return True if ``pid`` runs under Wine or Proton.

    Wine starts Windows programs through its static ``wine-preloader``
    (``wine64-preloader``), or through the ``wine`` loader itself.
    """
    try:
        name = (Path(proc_root) / str(pid) / "exe").readlink().name
    except OSError:
        return False
    return name in {"wine", "wine64"} or (
        name.startswith("wine") and name.endswith("-preloader"))

