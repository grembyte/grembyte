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
