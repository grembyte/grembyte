"""Open a Frida session on a target process, native or Wine/Proton.

``frida.attach`` injects Frida's Linux agent, which crashes Wine processes:
it looks up the main program through the program headers the kernel recorded
for ``wine-preloader``, where nothing is mapped (Frida 17.23/17.24, Proton
11). For those, the session goes through the Windows build of
Frida's gadget instead (wine_gadget.py), which hermes loads into the game
once and every worker then shares.
"""

import frida

from hermes.core import wine_gadget
from hermes.platform import is_wine_process


def attach_session(pid):
    """Return a Frida session on ``pid``; raises on failure.

    Under Wine the agent sees the process as Windows does: PE modules such as
    ``mono-2.0-bdwgc.dll``, ``Process.platform === 'windows'`` and the Win64
    calling convention.
    """
    if is_wine_process(pid):
        return wine_gadget.connect(pid)
    return frida.attach(pid)
