# Agent interface

<!-- Written by scripts/agent_interface.py; do not edit by hand. -->

The agents that run in the game answer Grembyte through Frida's RPC: each defines
`rpc.exports`, and Python calls an export through `script.exports_sync` in snake case
(`exports_sync.add_monitor(...)` runs `addMonitor`). This page lists, per agent, its
exports and where Python calls them, so that a change on one side finds the other.

The callers are found by name: a call `x.name(...)` or the string `"name"` (dispatch
through `getattr`), so a same-named method of another object can show up.

- *Loaded by*: the modules that create the agent.
- *Called in*: calls in those modules, and in the base classes their classes inherit
  from another module (such as `AgentWorker` in `features/trace.py`).
- *Same name elsewhere*: the same name in other modules. These are mostly the window
  calling the worker's Python method of that name, which then calls the export. Common
  names without an underscore (`stop`, `inspect`) are left out there.

Regenerate the page after changing an agent's exports:

```bash
./.venv/bin/python scripts/agent_interface.py
```


## `features/catalog.js`

Agent: [`features/catalog.js`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js). Loaded by: [`features/call_graph.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/call_graph.py), [`features/catalog.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.py), [`features/managed_statics.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/managed_statics.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| [`assemblies`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L2) | `assemblies` | [`features/call_graph.py:427`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/call_graph.py#L427), [`features/catalog.py:212`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.py#L212) | – |
| [`classes`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L5) | `classes` | [`features/call_graph.py:438`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/call_graph.py#L438), [`features/catalog.py:247`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.py#L247) | – |
| [`methodBodies`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L8) | `method_bodies` | [`features/call_graph.py:469`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/call_graph.py#L469) | – |
| [`nativeSymbols`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L11) | `native_symbols` | [`features/catalog.py:279`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.py#L279) | – |
| [`staticAreas`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L15) | `static_areas` | [`features/managed_statics.py:59`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/managed_statics.py#L59) | – |
| [`resolveTokens`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L19) | `resolve_tokens` | [`features/call_graph.py:470`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/call_graph.py#L470) | – |
| [`functionCalls`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L22) | `function_calls` | [`features/call_graph.py:465`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/call_graph.py#L465) | – |

## `features/monitor.js`

Agent: [`features/monitor.js`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js). Loaded by: [`features/method_hooks.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py), [`features/monitor.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py), [`features/scripts.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| [`addMonitor`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2672) | `add_monitor` | [`features/monitor.py:286`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L286) | [`ui/widgets/method_monitor.py:382`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_monitor.py#L382) |
| [`removeMonitor`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2673) | `remove_monitor` | [`features/monitor.py:293`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L293) | [`ui/widgets/method_monitor.py:394`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_monitor.py#L394) (+1) |
| [`inspect`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2674) | `inspect` | [`features/method_hooks.py:195`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L195) (+2), [`features/monitor.py:165`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L165) (+2), [`features/scripts.py:361`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L361) (+1) | – |
| [`fullText`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2675) | `full_text` | [`features/method_hooks.py:326`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L326), [`features/monitor.py:336`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L336), [`features/scripts.py:525`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L525) | [`ui/widgets/inspection.py:171`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/inspection.py#L171) |
| [`evaluate`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2676) | `evaluate` | [`features/method_hooks.py:201`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L201) (+2), [`features/monitor.py:175`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L175) (+2), [`features/scripts.py:367`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L367) (+1) | – |
| [`describeCall`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2677) | `describe_call` | [`features/monitor.py:306`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L306) | [`ui/widgets/method_call.py:204`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L204) (+1) |
| [`callStatic`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2678) | `call_static` | [`features/monitor.py:311`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L311) | – |
| [`callFound`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2679) | `call_found` | [`features/monitor.py:202`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L202) (+2) | [`engines/base.py:129`](https://github.com/grembyte/grembyte/blob/main/grembyte/engines/base.py#L129), [`ui/widgets/method_call.py:214`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L214) |
| [`listMethods`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2680) | `list_methods` | [`features/method_hooks.py:311`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L311), [`features/monitor.py:321`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L321), [`features/scripts.py:522`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L522) | [`ui/widgets/inspection.py:124`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/inspection.py#L124) |
| [`describeMethod`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2681) | `describe_method` | [`features/method_hooks.py:213`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L213) (+2), [`features/monitor.py:216`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L216) (+2), [`features/scripts.py:379`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L379) (+1) | [`ui/widgets/method_call.py:229`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L229) |
| [`callOn`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2682) | `call_on` | [`features/method_hooks.py:219`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L219) (+2), [`features/monitor.py:223`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L223) (+2), [`features/scripts.py:385`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L385) (+1) | [`ui/widgets/method_call.py:230`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L230) |
| [`addMethodHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2683) | `add_method_hook` | [`features/method_hooks.py:277`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L277) | [`ui/widgets/hooks.py:808`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L808) (+2) |
| [`removeMethodHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2684) | `remove_method_hook` | [`features/method_hooks.py:285`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L285) | – |
| [`setMethodHookEnabled`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2685) | `set_method_hook_enabled` | [`features/method_hooks.py:291`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L291) | – |
| [`setMethodHookTransform`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2689) | `set_method_hook_transform` | [`features/method_hooks.py:288`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L288) | – |
| [`setMethodHookPhase`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2695) | `set_method_hook_phase` | [`features/method_hooks.py:297`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L297) | – |
| [`setMethodHookDebug`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2704) | `set_method_hook_debug` | [`features/method_hooks.py:294`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L294) | – |
| [`gasAttributes`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2708) | `gas_attributes` | [`features/scripts.py:395`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L395) (+1) | [`ui/widgets/gas_attributes.py:255`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/gas_attributes.py#L255) |
| [`gasSetBase`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2709) | `gas_set_base` | [`features/scripts.py:402`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L402) (+1) | [`ui/widgets/gas_attributes.py:272`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/gas_attributes.py#L272) |
| [`browseClasses`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2710) | `browse_classes` | [`features/scripts.py:416`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L416) (+1) | [`ui/widgets/object_browser.py:247`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/object_browser.py#L247), [`ui/widgets/reference_graph.py:624`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/reference_graph.py#L624) |
| [`browseInstances`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2711) | `browse_instances` | [`features/scripts.py:427`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L427) (+1) | [`ui/widgets/object_browser.py:269`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/object_browser.py#L269) |
| [`resolveReferences`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2712) | `resolve_references` | [`features/scripts.py:443`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L443) (+1) | [`ui/widgets/reference_graph.py:523`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/reference_graph.py#L523) |
| [`addScript`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2713) | `add_script` | [`features/scripts.py:502`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L502) | [`cli.py:648`](https://github.com/grembyte/grembyte/blob/main/grembyte/cli.py#L648), [`ui/widgets/scripts.py:493`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/scripts.py#L493) (+5) |
| [`updateScript`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2714) | `update_script` | [`features/scripts.py:505`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L505) | [`cli.py:699`](https://github.com/grembyte/grembyte/blob/main/grembyte/cli.py#L699) (+1), [`ui/widgets/scripts.py:591`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/scripts.py#L591) |
| [`removeScript`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2715) | `remove_script` | [`features/scripts.py:508`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L508) | [`ui/widgets/gas_attributes.py:454`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/gas_attributes.py#L454), [`ui/widgets/scripts.py:628`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/scripts.py#L628) (+1) |
| [`runScriptNow`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2716) | `run_script_now` | [`features/scripts.py:511`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L511) | – |
| [`scriptOptions`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2717) | `script_options` | [`features/scripts.py:528`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L528) | [`ui/widgets/script_params.py:280`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/script_params.py#L280) |
| [`dispose`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L2718) | `dispose` | [`features/method_hooks.py:261`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L261), [`features/monitor.py:270`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L270), [`features/scripts.py:486`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L486) | – |

## `features/native_monitor.js`

Agent: [`features/native_monitor.js`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.js). Loaded by: [`features/native_monitor.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| [`add`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.js#L366) | `add` | [`features/native_monitor.py:182`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.py#L182) (+2) | – |
| [`remove`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.js#L367) | `remove` | [`features/native_monitor.py:185`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.py#L185) (+2) | – |
| [`dispose`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.js#L368) | `dispose` | [`features/trace.py:399`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L399) | – |

## `features/native_trace.js`

Agent: [`features/native_trace.js`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_trace.js). Loaded by: [`features/native_trace.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_trace.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| [`inventory`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_trace.js#L950) | `inventory` | [`features/native_trace.py:251`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_trace.py#L251) (+3) | – |
| [`start`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_trace.js#L951) | `start` | [`features/native_trace.py:255`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_trace.py#L255) (+4) | – |
| [`begin`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_trace.js#L952) | `begin` | [`features/trace.py:325`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L325) (+2) | – |
| [`finish`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_trace.js#L953) | `finish` | [`features/trace.py:328`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L328) (+2) | – |
| [`stop`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_trace.js#L954) | `stop` | [`features/native_trace.py:311`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_trace.py#L311) (+1), [`features/trace.py:332`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L332) (+2) | – |
| [`dispose`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_trace.js#L960) | `dispose` | [`features/trace.py:399`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L399) | – |

## `features/trace.js`

Agent: [`features/trace.js`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.js). Loaded by: [`features/trace.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| [`assemblies`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.js#L394) | `assemblies` | [`features/trace.py:518`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L518) (+1) | – |
| [`classes`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.js#L397) | `classes` | [`features/trace.py:526`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L526) | – |
| [`choose`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.js#L400) | `choose` | [`features/trace.py:545`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L545) | – |
| [`start`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.js#L401) | `start` | [`features/trace.py:493`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L493) (+3) | – |
| [`begin`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.js#L402) | `begin` | [`features/trace.py:325`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L325) (+2) | – |
| [`finish`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.js#L403) | `finish` | [`features/trace.py:328`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L328) (+2) | – |
| [`stop`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.js#L404) | `stop` | [`features/trace.py:332`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L332) (+2) | – |
| [`dispose`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.js#L405) | `dispose` | [`features/trace.py:399`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L399) | – |

## `features/write_watch.js`

Agent: [`features/write_watch.js`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/write_watch.js). Loaded by: [`features/write_watch.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/write_watch.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| [`stop`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/write_watch.js#L189) | `stop` | [`features/write_watch.py:103`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/write_watch.py#L103) | – |

## `hooks/engine.js`

Agent: [`hooks/engine.js`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js). Loaded by: [`hooks/engine.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| [`addHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3109) | `add_hook` | [`hooks/engine.py:442`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L442) | [`cli.py:648`](https://github.com/grembyte/grembyte/blob/main/grembyte/cli.py#L648), [`ui/main_window.py:2571`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/main_window.py#L2571), [`ui/widgets/hooks.py:773`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L773) (+4) |
| [`removeHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3110) | `remove_hook` | [`hooks/engine.py:450`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L450) | [`ui/widgets/hooks.py:1657`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L1657) (+1) |
| [`setTrainingPaused`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3111) | `set_training_paused` | [`hooks/engine.py:461`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L461) | – |
| [`setTransform`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3112) | `set_transform` | [`hooks/engine.py:453`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L453) | [`ui/widgets/hooks.py:911`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L911) (+1) |
| [`setRepatch`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3126) | `set_repatch` | [`hooks/engine.py:392`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L392) (+1) | – |
| [`setProfiling`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3130) | `set_profiling` | [`hooks/engine.py:393`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L393) (+1) | – |
| [`setDebug`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3136) | `set_debug` | [`hooks/engine.py:465`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L465) | [`ui/widgets/hooks.py:999`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L999) (+2) |
| [`setEnabled`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3147) | `set_enabled` | [`hooks/engine.py:456`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L456) | [`ui/widgets/hooks.py:1700`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L1700) |
| [`dispose`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3160) | `dispose` | [`hooks/engine.py:418`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L418) | – |

## `hooks/syntax.py`

Agent: the agent is a string in this module. Loaded by: [`hooks/syntax.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/syntax.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| `check` | `check` | [`hooks/syntax.py:104`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/syntax.py#L104) (+3) | – |
