# Permissions on Linux

Scans and writes go through `/proc/<pid>/mem`; the write watch, hooks and the engine tools attach to the game through [Frida](https://frida.re) (or ptrace). Both are subject to Linux's protections, in particular Yama's `kernel.yama.ptrace_scope`:

| Value | Meaning |
| --- | --- |
| `0` | Processes of the same user may be inspected. |
| `1` | Only one's own child processes. The default of many distributions. Hermes then cannot access a game started separately. |

Do not run Hermes as root just in case, and do not loosen `ptrace_scope` permanently system-wide. For a session a temporary change is enough; it lapses at the next reboot:

```bash
sudo sysctl kernel.yama.ptrace_scope=0
```

The process list only shows processes whose information the current user may access.

!!! tip "Windows games under Wine or Proton"
    Frida cannot attach to Wine processes the usual way. Hermes loads Frida's Windows gadget into the game instead, which needs a one-time download. See [Wine and Proton](../wine-proton.md).

On Windows no extra permissions are needed for games running as the same user.
