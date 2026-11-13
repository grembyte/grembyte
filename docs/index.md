# Grembyte

Grembyte is a desktop application for inspecting and changing the memory of running games. It finds values such as gold or health, shows which code writes them, and hooks that code so every write is changed the way you want: a multiplier, a fixed value, bounds, or a JavaScript function of your own. Hooks, addresses and scripts are stored per game and come back the next time it starts.

It runs on **Linux**, including Windows games under **Wine and Proton**, and on **Windows**. For Unity (Mono and IL2CPP) and Unreal Engine games it also understands the game's own classes and methods.

!!! info "Alpha stage"
    Grembyte is still in an early alpha stage. Features, the user interface and the format of saved game data can still change between releases, and some games or setups may not work yet. Bug reports and game reports are welcome on [GitHub](https://github.com/grembyte/grembyte/issues).

<figure
markdown="span">
  ![Grembyte main window with scan results and tracked addresses](assets/screenshots/main-window-dark.webp#only-dark)
  ![Grembyte main window with scan results and tracked addresses](assets/screenshots/main-window-light.webp#only-light)
  <figcaption>The main window after a few scans for the gold of the test game: results on the left, the value search on the right, tracked addresses with their pointer chains below.</figcaption>
</figure>

!!! warning "Single-player only"
    This tool is intended solely for educational, research and single-player game modding purposes. It is not designed or intended to bypass anti-cheat mechanisms or to be used in online multiplayer games. The developer assumes no responsibility or liability for any account bans, suspensions or damages resulting from the use or misuse of this software.

    Restoring memory modifications that a game removed can be detected by anti-tamper systems (such as Arxan) and, when used online, carries a considerable risk of account bans or soft bans. Use it at your own risk.

## What it does

<div class="grid cards" markdown>

-   :material-magnify:{ .lg .middle } **Find values**

    ---

    Scan for integers and floats: exact, ranges, unknown initial value, changed or increased by an amount, with tolerance and rounded floats.

    [:octicons-arrow-right-24: Scanning](guide/scanning.md)

-   :material-link-variant:{ .lg .middle } **Keep them across restarts**

    ---

    The pointer scan finds chains like `game+0x4040 → 0x28 → 0x0` that lead to the value again after the game restarts.

    [:octicons-arrow-right-24: Pointer scan](guide/pointer-scan.md)

-   :material-eye-outline:{ .lg .middle } **See who writes them**

    ---

    A hardware watchpoint shows the instructions that write, or also read, an address.

    [:octicons-arrow-right-24: Write and read watch](guide/write-watch.md)

-   :material-hook:{ .lg .middle } **Hook the write**

    ---

    Change every write at that instruction: multiply, fix, clamp or compute the value in JavaScript. Hooks are reinstalled automatically.

    [:octicons-arrow-right-24: Hooks](guide/hooks.md)

-   :material-language-csharp:{ .lg .middle } **Work with game code**

    ---

    For Unity and Unreal games: browse classes and methods, trace what a scenario calls, monitor and hook methods, call them yourself.

    [:octicons-arrow-right-24: Engines](engines/index.md)

-   :material-script-text-outline:{ .lg .middle } **Script the game**

    ---

    JavaScript that runs on the game's main thread on a schedule or by hand, with access to its objects and methods.

    [:octicons-arrow-right-24: Scripts](guide/scripts.md)

-   :material-file-search-outline:{ .lg .middle } **Explore native code**

    ---

    From a text the game shows to the code using it: strings, references, functions and callers of any executable, and byte patterns that still find the code after an update.

    [:octicons-arrow-right-24: Code explorer](guide/code-explorer.md)

-   :material-note-edit-outline:{ .lg .middle } **Keep notes**

    ---

    Note per game in Markdown which classes hold the values and which methods give gold, experience or items, docked beside the main window.

    [:octicons-arrow-right-24: Notes](guide/notes.md)

</div>

## Where to start

1. [Download and install](getting-started/installation.md) Grembyte (no GitHub experience needed) and check the [permissions](getting-started/permissions.md) it needs on Linux.
2. Follow the [tutorial](getting-started/first-hook.md): with the small test game in the repository you find a value, watch its writes and hook them, in about ten minutes.
3. Read the [user guide](guide/scanning.md) for the details of each tool, and the [engine pages](engines/index.md) for Unity and Unreal games.
4. Try everything on [HermesQuest](examples/index.md), a small Unity role-playing game made for it, with worked examples of pointer scans, hooks and scripts – and an introduction to [JavaScript in Grembyte](reference/javascript.md).

!!! info "Under development"
    Grembyte is under active development. The [roadmap](https://github.com/grembyte/grembyte/blob/main/ROADMAP.md) lists what is planned and what is still open.
