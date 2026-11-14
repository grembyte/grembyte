# Settings and themes

## Settings

**Settings → General …** holds the settings, stored in Grembyte's database. The list on the left chooses a page: General, Scan, Pointer scan, Trace, Hooks, Object browser, Graphs and Wine/Proton.

<figure markdown="span">
  ![Settings](../assets/screenshots/settings-dark.webp#only-dark){ width="640" }
  ![Settings](../assets/screenshots/settings-light.webp#only-light){ width="640" }
</figure>

| Setting | Meaning |
| --- | --- |
| Language | Deutsch, English, or as the system (the default). Takes effect at the next start. |
| Sound when a hotkey ran its script | On by default: a short system sound once a script started by its [hotkey](scripts.md#hotkeys) has run, for success, an error, or a script that switched something on or off; and when a hotkey paused or resumed a repeating script. In the window at once, in `grembyte-cli` at its next start. |
| Interface size (%) | Enlarges text, icons and spacing of the whole interface, from 100 % (the default) to 150 % in steps of 10 %. Takes effect at the next start and adds to the scaling of the desktop. Only the code fields can also be [zoomed on their own](../reference/javascript.md#where-javascript-is-used). |
| Refresh remembered addresses every (ms) | How often tracked addresses read their values (default 1000, 100 to 10 000). |
| Record the activity log | Write the [activity log](tools.md#log-and-activity) (off by default). Takes effect at once. |
| Aligned addresses only | The first [value scan](scanning.md#data-types) checks only addresses that are a multiple of the type's size (on by default). Faster and uses far less memory. |
| Pointer scan: Max. results | How many chains a [pointer scan](pointer-scan.md#search-limits) keeps (default 10 000, 100 to 1 million). More results take more memory, also in the database. |
| Pointer scan: Search thread stacks | Let chains pass through the thread stacks (off by default); see [memory regions](pointer-scan.md#memory-regions). |
| Pointer scan: Pointers lie at / Pointers point to | What counts as a pointer; both default to the pointer size of the program. See [alignment](pointer-scan.md#alignment). |
| Trace under Wine/Proton: at most methods | Most methods a trace hooks in a Wine/Proton game (default 5000, or no limit). Traces of tens of thousands of methods froze or crashed games there. |
| Trace: ask first from methods | From this many methods the trace asks before it starts; Mono has to compile them all (default 5000). |
| Trace: entries in the sequence | How many calls the trace and the native trace keep in order during the scenario (default 100 000, up to 500 000); raise it when the window reports dropped ones. |
| Automatically download the Wine/Proton Frida gadget | Off by default; see [Wine and Proton](../wine-proton.md). |
| Hooks: short jump in tight spots (experimental) | Off by default; see the [hook limitations](hooks.md#limitations). |
| Hooks: patch hooks the game removed again | Off by default. For games that check and restore their code, e.g. Dark Souls III. Grembyte checks every second and patches again. Can be detected by anti-tamper systems. |
| Hooks: measure their cost | Off by default. Measures every write at a hooked site: calls per second, skipped writes, waiting time and run time. Shown in the tooltip of the "Applied" column and in `grembyte.log`. |
| Object browser: objects read (Unity Mono) | How many live objects the [object browser](object-browser.md#unity-mono) and the [reference graph](reference-graph.md#unity-mono) keep per read (default 2 million, up to 10 million). Each takes 16 bytes in the game while Grembyte is attached. Raise it when the browser reports that not all objects could be kept. Takes effect at the next refresh. |
| Nodes shown per unfolding at most (page Graphs) | How many holders, targets, callers or called methods the [reference graph](reference-graph.md) and the [call graph](../engines/index.md#call-graph) show when a node is unfolded (default 100, 10 to 1,000). The others only count in the node's **+n**. Takes effect at the next unfolding. |
| Reference graph: show other memory | Off by default: locations in memory no object of the engine covers only count in a node's **+n** in the [reference graph](reference-graph.md#reading-the-graph). On: they show as a node of their own. Takes effect at once, also in open graphs. |

**Defaults** fills in the default values on every page; **Save** stores them.

**Settings → Backend (watch & hooks)** chooses between Frida and ptrace; see [backends](write-watch.md#backends-frida-or-ptrace).

## Color themes

**Settings → Color theme …** chooses a theme or edits its colors. Changes preview immediately; **Save** stores the palette and activates it, **Cancel** discards the preview. Enter a new name in the theme selector to create a theme of your own.

<figure markdown="span">
  ![Theme editor](../assets/screenshots/theme-editor-dark.webp#only-dark)
  ![Theme editor](../assets/screenshots/theme-editor-light.webp#only-light)
</figure>

The bundled themes are **Github-Dark** (the default), **Github-Light**, **Solarized-Dark** and **Solarized-Light**. Themes of your own are saved as `grembyte_themes_<name>.json`; [Files and environment](../reference/files.md) says where.

## Window layout

Every window keeps its size, its splitters and the column widths you set; windows that are not modal also keep their position.
