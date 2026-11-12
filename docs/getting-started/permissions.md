# Permissions on Linux

Grembyte reads and changes the memory of a game that runs as your own user. Linux allows that only to a process that may *trace* the game: the same check that applies to debuggers such as gdb. Yama's `kernel.yama.ptrace_scope` decides who may:

| Value | Meaning |
| --- | --- |
| `0` | A process may trace any other process of the same user. |
| `1` | Only its own child processes. The default of many distributions. Grembyte then cannot access a game started separately. |

## What needs which access

Grembyte uses three ways into a game, and each feature needs only one of them. All of them act on processes of your own user only; Grembyte needs no root rights and asks for none.

| Access | Used by | Needs `ptrace_scope = 0` |
| --- | --- | --- |
| **None**: reading `/proc` and files | the process list, the [code explorer](../guide/code-explorer.md) (it reads the game's file on disk, not its memory), the offline catalog, settings and the database | no |
| **`/proc/<pid>/mem`**: reading and writing memory | [value scans](../guide/scanning.md), changing and freezing [addresses](../guide/addresses.md), the [pointer scan](../guide/pointer-scan.md), the [memory viewer](../guide/tools.md#memory-viewer) | yes |
| **[Frida](https://frida.re)**: an agent in the game | the [write watch](../guide/write-watch.md) and [hooks](../guide/hooks.md) with the Frida backend, the [native trace and monitor](../guide/native-tools.md), [scripts](../guide/scripts.md), and all [engine tools](../engines/index.md): catalog, trace, method monitor, object inspector, method hooks, calling methods, call graph, GAS attributes | yes, for native Linux games (Frida loads its agent through ptrace) |
| **ptrace** directly, like gdb | the write watch and hooks with the [ptrace backend](../guide/write-watch.md#backends-frida-or-ptrace) (CPU debug registers, nothing is injected); for Wine/Proton games a fallback to load the Frida gadget | yes |

Scans and the address list therefore need `ptrace_scope = 0` as much as the Frida tools do; only the process list and the code explorer work without it. Grembyte starts nothing in the game on its own: Frida and ptrace are only used when you open one of the tools above, and the game is released when you close it or detach.

For **Wine and Proton games** Frida's Linux agent cannot be used. Grembyte loads Frida's Windows gadget into the game instead, through a small Windows helper that runs in the game's Wine prefix and uses the Windows API (`CreateRemoteThread` on `LoadLibraryW`); ptrace serves only as a fallback. The gadget listens on `127.0.0.1` only, so it is not reachable from other computers. It is downloaded once from Frida's GitHub releases, and Grembyte checks its SHA-256 checksum against the one GitHub publishes for that release (Frida releases from before 2025 list none; Grembyte then logs a warning). See [Wine and Proton](../wine-proton.md).

Grembyte makes no other network connections, apart from links you open yourself (documentation, bug reports).

## Allowing access for a session

Do not run Grembyte as root just in case, and do not loosen `ptrace_scope` permanently system-wide. For a session a temporary change is enough; it lapses at the next reboot. Note down the current value first, so you can restore it later:

```bash
sysctl kernel.yama.ptrace_scope        # show the current value, e.g. 1
sudo sysctl kernel.yama.ptrace_scope=0
```

!!! warning "This is a system-wide setting"
    `ptrace_scope` is not a setting of Grembyte. It changes the ptrace policy for the whole system: as long as it is `0`, every process of your user may inspect and modify every other process of that user, not just Grembyte and the game. Restore the previous value once you are done.

To undo the change without rebooting, set the value you noted before (on most distributions `1`):

```bash
sudo sysctl kernel.yama.ptrace_scope=1
```

A reboot also restores the distribution's default, unless the value was set persistently in a file under `/etc/sysctl.d/`. In that case remove or change the entry there and reload it with `sudo sysctl --system`.

The process list only shows processes whose information the current user may access.

## Windows

On Windows no extra permissions are needed for games running as the same user.
