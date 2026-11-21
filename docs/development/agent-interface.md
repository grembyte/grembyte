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
| [`assemblies`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L2) | `assemblies` | [`features/call_graph.py:427`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/call_graph.py#L427), [`features/catalog.py:253`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.py#L253) | – |
| [`classes`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L5) | `classes` | [`features/call_graph.py:438`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/call_graph.py#L438), [`features/catalog.py:294`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.py#L294) | – |
| [`fields`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L8) | `fields` | [`features/catalog.py:28`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.py#L28) (+1) | – |
| [`methodBodies`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L12) | `method_bodies` | [`features/call_graph.py:469`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/call_graph.py#L469) | – |
| [`nativeSymbols`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L15) | `native_symbols` | [`features/catalog.py:326`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.py#L326) | – |
| [`staticAreas`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L19) | `static_areas` | [`features/managed_statics.py:59`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/managed_statics.py#L59) | – |
| [`resolveTokens`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L23) | `resolve_tokens` | [`features/call_graph.py:470`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/call_graph.py#L470) | – |
| [`functionCalls`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/catalog.js#L26) | `function_calls` | [`features/call_graph.py:465`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/call_graph.py#L465) | – |

## `features/monitor.js`

Agent: [`features/monitor.js`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js). Loaded by: [`features/method_hooks.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py), [`features/monitor.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py), [`features/scripts.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| [`addMonitor`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3039) | `add_monitor` | [`features/monitor.py:322`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L322) | [`ui/widgets/method_monitor.py:382`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_monitor.py#L382) |
| [`removeMonitor`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3040) | `remove_monitor` | [`features/monitor.py:329`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L329) | [`ui/widgets/method_monitor.py:394`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_monitor.py#L394) (+1) |
| [`inspect`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3041) | `inspect` | [`features/method_hooks.py:209`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L209) (+2), [`features/monitor.py:201`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L201) (+2), [`features/scripts.py:442`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L442) (+1) | – |
| [`staticFields`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3042) | `static_fields` | [`features/method_hooks.py:248`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L248) (+2), [`features/scripts.py:543`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L543) (+1) | [`ui/widgets/catalog.py:953`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/catalog.py#L953), [`ui/widgets/code_completion.py:530`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/code_completion.py#L530) (+2), [`ui/widgets/member_lookup.py:76`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/member_lookup.py#L76) |
| [`fullText`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3043) | `full_text` | [`features/method_hooks.py:398`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L398), [`features/monitor.py:372`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L372), [`features/scripts.py:673`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L673) | [`ui/widgets/inspection.py:171`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/inspection.py#L171) |
| [`evaluate`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3044) | `evaluate` | [`features/method_hooks.py:215`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L215) (+2), [`features/monitor.py:211`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L211) (+2), [`features/scripts.py:448`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L448) (+1) | – |
| [`describeCall`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3045) | `describe_call` | [`features/monitor.py:342`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L342) | [`ui/widgets/method_call.py:204`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L204) (+1) |
| [`callStatic`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3046) | `call_static` | [`features/monitor.py:347`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L347) | – |
| [`callFound`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3047) | `call_found` | [`features/monitor.py:238`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L238) (+2) | [`engines/base.py:133`](https://github.com/grembyte/grembyte/blob/main/grembyte/engines/base.py#L133), [`ui/widgets/method_call.py:214`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L214) |
| [`listMethods`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3048) | `list_methods` | [`features/method_hooks.py:359`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L359), [`features/monitor.py:357`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L357), [`features/scripts.py:667`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L667) | [`ui/widgets/inspection.py:124`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/inspection.py#L124) |
| [`classMembers`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3049) | `class_members` | [`features/method_hooks.py:383`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L383), [`features/scripts.py:668`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L668) | [`ui/widgets/member_lookup.py:121`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/member_lookup.py#L121) |
| [`classInfo`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3050) | `class_info` | [`features/method_hooks.py:241`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L241) (+2), [`features/scripts.py:479`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L479) (+1) | [`ui/widgets/code_completion.py:584`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/code_completion.py#L584), [`ui/widgets/member_lookup.py:65`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/member_lookup.py#L65) |
| [`classNames`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3051) | `class_names` | [`features/method_hooks.py:235`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L235) (+2), [`features/scripts.py:471`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L471) (+1) | [`ui/widgets/code_completion.py:525`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/code_completion.py#L525) (+1) |
| [`staticFieldSources`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3052) | `static_field_sources` | [`features/monitor.py:114`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L114) | – |
| [`assemblyFields`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3054) | `assembly_fields` | [`features/monitor.py:125`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L125) | – |
| [`describeMethod`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3055) | `describe_method` | [`features/method_hooks.py:261`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L261) (+2), [`features/monitor.py:252`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L252) (+2), [`features/scripts.py:493`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L493) (+1) | [`ui/widgets/method_call.py:229`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L229) |
| [`callOn`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3056) | `call_on` | [`features/method_hooks.py:267`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L267) (+2), [`features/monitor.py:259`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L259) (+2), [`features/scripts.py:499`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L499) (+1) | [`ui/widgets/method_call.py:230`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L230) |
| [`addMethodHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3057) | `add_method_hook` | [`features/method_hooks.py:325`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L325) | [`ui/widgets/hooks.py:947`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L947) (+2) |
| [`removeMethodHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3058) | `remove_method_hook` | [`features/method_hooks.py:333`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L333) | – |
| [`setMethodHookEnabled`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3059) | `set_method_hook_enabled` | [`features/method_hooks.py:339`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L339) | – |
| [`setMethodHookTransform`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3063) | `set_method_hook_transform` | [`features/method_hooks.py:336`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L336) | – |
| [`setMethodHookPhase`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3069) | `set_method_hook_phase` | [`features/method_hooks.py:345`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L345) | – |
| [`setMethodHookDebug`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3078) | `set_method_hook_debug` | [`features/method_hooks.py:342`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L342) | – |
| [`gasAttributes`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3082) | `gas_attributes` | [`features/scripts.py:509`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L509) (+1) | [`ui/widgets/gas_attributes.py:255`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/gas_attributes.py#L255) |
| [`gasSetBase`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3083) | `gas_set_base` | [`features/scripts.py:516`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L516) (+1) | [`ui/widgets/gas_attributes.py:272`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/gas_attributes.py#L272) |
| [`browseClasses`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3084) | `browse_classes` | [`features/scripts.py:530`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L530) (+1) | [`ui/widgets/object_browser.py:251`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/object_browser.py#L251), [`ui/widgets/reference_graph.py:624`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/reference_graph.py#L624) |
| [`browseInstances`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3085) | `browse_instances` | [`features/scripts.py:554`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L554) (+1) | [`ui/widgets/object_browser.py:273`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/object_browser.py#L273) |
| [`resolveReferences`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3086) | `resolve_references` | [`features/scripts.py:570`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L570) (+1) | [`ui/widgets/reference_graph.py:523`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/reference_graph.py#L523) |
| [`addScript`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3087) | `add_script` | [`features/scripts.py:636`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L636) | [`cli.py:674`](https://github.com/grembyte/grembyte/blob/main/grembyte/cli.py#L674), [`ui/widgets/scripts.py:536`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/scripts.py#L536) (+5) |
| [`updateScript`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3088) | `update_script` | [`features/scripts.py:639`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L639) | [`cli.py:725`](https://github.com/grembyte/grembyte/blob/main/grembyte/cli.py#L725) (+2), [`ui/widgets/scripts.py:722`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/scripts.py#L722) |
| [`removeScript`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3089) | `remove_script` | [`features/scripts.py:642`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L642) | [`ui/widgets/gas_attributes.py:455`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/gas_attributes.py#L455), [`ui/widgets/scripts.py:759`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/scripts.py#L759) (+1) |
| [`setScriptTimings`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3090) | `set_script_timings` | [`features/scripts.py:595`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L595) (+1) | – |
| [`prewarmScripts`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3091) | `prewarm_scripts` | [`features/scripts.py:251`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L251) | – |
| [`reflectionBuild`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3093) | `reflection_build` | [`features/scripts.py:239`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L239) | – |
| [`useStoredOffsets`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3095) | `use_stored_offsets` | [`features/scripts.py:241`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L241) | – |
| [`runScriptNow`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3096) | `run_script_now` | [`features/scripts.py:645`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L645) | – |
| [`scriptOptions`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3097) | `script_options` | [`features/scripts.py:676`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L676) | [`ui/widgets/script_params.py:283`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/script_params.py#L283) |
| [`dispose`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3098) | `dispose` | [`features/method_hooks.py:309`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L309), [`features/monitor.py:306`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L306), [`features/scripts.py:620`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L620) | – |

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
| [`addHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3109) | `add_hook` | [`hooks/engine.py:442`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L442) | [`cli.py:674`](https://github.com/grembyte/grembyte/blob/main/grembyte/cli.py#L674), [`ui/main_window.py:2606`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/main_window.py#L2606), [`ui/widgets/hooks.py:831`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L831) (+4) |
| [`removeHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3110) | `remove_hook` | [`hooks/engine.py:450`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L450) | [`ui/widgets/hooks.py:1807`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L1807) (+1) |
| [`setTrainingPaused`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3111) | `set_training_paused` | [`hooks/engine.py:461`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L461) | – |
| [`setTransform`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3112) | `set_transform` | [`hooks/engine.py:453`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L453) | [`ui/widgets/hooks.py:1061`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L1061) (+1) |
| [`setRepatch`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3126) | `set_repatch` | [`hooks/engine.py:392`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L392) (+1) | – |
| [`setProfiling`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3130) | `set_profiling` | [`hooks/engine.py:393`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L393) (+1) | – |
| [`setDebug`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3136) | `set_debug` | [`hooks/engine.py:465`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L465) | [`ui/widgets/hooks.py:1149`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L1149) (+2) |
| [`setEnabled`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3147) | `set_enabled` | [`hooks/engine.py:456`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L456) | [`ui/widgets/hooks.py:1850`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L1850) |
| [`dispose`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L3160) | `dispose` | [`hooks/engine.py:418`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L418) | – |

## `hooks/syntax.py`

Agent: the agent is a string in this module. Loaded by: [`hooks/syntax.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/syntax.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| `check` | `check` | [`hooks/syntax.py:104`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/syntax.py#L104) (+3) | – |
