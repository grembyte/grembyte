# Memory, sessions and logs

## Memory viewer

**Memory** in the toolbar, or **Show in memory viewer** in the context menu of an address, shows the memory around an address as bytes and as text.

<figure markdown="span">
  ![Memory viewer](../assets/screenshots/memory-viewer-dark.webp#only-dark)
  ![Memory viewer](../assets/screenshots/memory-viewer-light.webp#only-light)
  <figcaption>The memory around the gold of the test game: 1350 is <code>46 05 00 00</code> in little-endian.</figcaption>
</figure>

## Sessions

**Sessions** in the toolbar lists Hermes' connections to processes – Frida sessions and ptrace attachments – with what uses them. A session that no longer responds can be cut there:

- **Stop** lets the session remove its hooks and detach cleanly.
- **Disconnect** cuts the Frida session if it does not react to Stop. The part of Hermes it belongs to (e.g. the hooks) then has to be started again.

<figure markdown="span">
  ![Sessions](../assets/screenshots/sessions-dark.webp#only-dark){ width="720" }
  ![Sessions](../assets/screenshots/sessions-light.webp#only-light){ width="720" }
</figure>

## Favorites

Methods and functions found in the catalog, a trace or the native trace can be marked with ★ in the first column. **View → Favorites …** lists them per program with a note, and hands them to the method or native function monitor. Favorites are saved at once, so they survive a frozen or crashed game.

<figure markdown="span">
  ![Favorites](../assets/screenshots/favorites-dark.webp#only-dark)
  ![Favorites](../assets/screenshots/favorites-light.webp#only-light)
</figure>

## Log and activity

**Log** in the status bar opens the log window. It turns red when errors were logged.

The **Activity** tab shows the activity log: the course of every run, written to `activity.jsonl` in the log folder. It records

- the attached process with its details: 32 or 64 bit, native or Wine/Proton with the Proton version, engine, how the gadget was loaded;
- sessions and agents in the order they started and ended;
- every call into the game with its duration and outcome (frequent ones summed up);
- clicks in Hermes, and why a Frida session ended, with Frida's crash report.

Values from the game's memory are not in it. The activity log can be switched off in the [settings](settings.md).

<figure markdown="span">
  ![Activity log](../assets/screenshots/log-activity-dark.webp#only-dark)
  ![Activity log](../assets/screenshots/log-activity-light.webp#only-light)
</figure>

**Export bug report …** packs the activity, the log and `crash.log` into a ZIP file – the best thing to attach to a bug report.
