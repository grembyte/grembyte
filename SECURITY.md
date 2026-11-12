# Security Policy

## Supported versions

Grembyte is in early alpha. Only the latest release and the current `main` branch receive fixes.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Report them privately through GitHub instead:

1. Open the [Security tab](https://github.com/grembyte/grembyte/security) of the repository.
2. Click **Report a vulnerability** and describe the problem, the affected version and, if possible, the steps to reproduce it.

Grembyte is maintained in spare time, so answers are on a best-effort basis. You can usually expect a first reply within two weeks. Once a fix is released, the report is published as a security advisory, with credit if you want it.

## Scope

Grembyte attaches to processes, injects code into them and runs JavaScript from hooks, templates and scripts. Some of this is intended behaviour, not a vulnerability. Reports are welcome for, for example:

- code from Grembyte running somewhere it should not, e.g. an imported file that executes code without the user running it on purpose;
- Grembyte weakening the security of the system beyond what attaching to a process needs, e.g. files or sockets other users can access;
- leaks of personal data, e.g. into exported files or bug reports.

Out of scope:

- Hook templates, scripts and exports from other people run code in the game by design. Only import files from sources you trust.
- Anti-cheat or anti-tamper systems detecting Grembyte, and bans that result from it. Grembyte is meant for single-player games only (see the [README](README.md#disclaimer)).
- Crashes or changed behaviour of the game you attached Grembyte to. Please report those as a normal [bug](https://github.com/grembyte/grembyte/issues/new/choose).
