# Permissions on Linux

Scans and writes go through `/proc/<pid>/mem`; the write watch, hooks and the engine tools attach to the game through [Frida](https://frida.re) (or ptrace). Both are subject to Linux's protections, in particular Yama's `kernel.yama.ptrace_scope`:

| Value | Meaning |
| --- | --- |
| `0` | Processes of the same user may be inspected. |
| `1` | Only one's own child processes. The default of many distributions. Hermes then cannot access a game started separately. |

Do not run Hermes as root just in case, and do not loosen `ptrace_scope` permanently system-wide. For a session a temporary change is enough; it lapses at the next reboot. Note down the current value first, so you can restore it later:

```bash
sysctl kernel.yama.ptrace_scope        # show the current value, e.g. 1
sudo sysctl kernel.yama.ptrace_scope=0
```

!!! warning "This is a system-wide setting"
    `ptrace_scope` is not a setting of Hermes Hook. It changes the ptrace policy for the whole system: as long as it is `0`, every process of your user may inspect and modify every other process of that user, not just Hermes and the game. Restore the previous value once you are done.

To undo the change without rebooting, set the value you noted before (on most distributions `1`):

```bash
sudo sysctl kernel.yama.ptrace_scope=1
```

A reboot also restores the distribution's default, unless the value was set persistently in a file under `/etc/sysctl.d/`. In that case remove or change the entry there and reload it with `sudo sysctl --system`.

The process list only shows processes whose information the current user may access.

!!! tip "Windows games under Wine or Proton"
    Frida cannot attach to Wine processes the usual way. Hermes loads Frida's Windows gadget into the game instead, which needs a one-time download. See [Wine and Proton](../wine-proton.md).

On Windows no extra permissions are needed for games running as the same user.
