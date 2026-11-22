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
| [`addMonitor`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3073) | `add_monitor` | [`features/monitor.py:367`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L367) | [`ui/widgets/method_monitor.py:382`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_monitor.py#L382) |
| [`removeMonitor`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3074) | `remove_monitor` | [`features/monitor.py:374`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L374) | [`ui/widgets/method_monitor.py:394`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_monitor.py#L394) (+1) |
| [`inspect`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3075) | `inspect` | [`features/method_hooks.py:210`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L210) (+2), [`features/monitor.py:246`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L246) (+2), [`features/scripts.py:449`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L449) (+1) | – |
| [`staticFields`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3076) | `static_fields` | [`features/method_hooks.py:249`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L249) (+2), [`features/scripts.py:567`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L567) (+1) | [`ui/widgets/catalog.py:953`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/catalog.py#L953), [`ui/widgets/code_completion.py:537`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/code_completion.py#L537) (+2), [`ui/widgets/member_lookup.py:77`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/member_lookup.py#L77) |
| [`fullText`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3077) | `full_text` | [`features/method_hooks.py:412`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L412), [`features/monitor.py:417`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L417), [`features/scripts.py:717`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L717) | [`ui/widgets/inspection.py:171`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/inspection.py#L171) |
| [`evaluate`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3078) | `evaluate` | [`features/method_hooks.py:216`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L216) (+2), [`features/monitor.py:256`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L256) (+2), [`features/scripts.py:455`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L455) (+1) | – |
| [`describeCall`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3079) | `describe_call` | [`features/monitor.py:387`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L387) | [`ui/widgets/method_call.py:204`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L204) (+1) |
| [`callStatic`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3080) | `call_static` | [`features/monitor.py:392`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L392) | – |
| [`callFound`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3081) | `call_found` | [`features/monitor.py:283`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L283) (+2) | [`engines/base.py:138`](https://github.com/grembyte/grembyte/blob/main/grembyte/engines/base.py#L138), [`ui/widgets/method_call.py:214`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L214) |
| [`listMethods`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3082) | `list_methods` | [`features/method_hooks.py:368`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L368), [`features/monitor.py:402`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L402), [`features/scripts.py:711`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L711) | [`ui/widgets/inspection.py:124`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/inspection.py#L124) |
| [`classMembers`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3083) | `class_members` | [`features/method_hooks.py:397`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L397), [`features/scripts.py:712`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L712) | [`ui/widgets/member_lookup.py:129`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/member_lookup.py#L129) |
| [`classInfo`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3084) | `class_info` | [`features/method_hooks.py:242`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L242) (+2), [`features/scripts.py:486`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L486) (+1) | [`ui/widgets/code_completion.py:598`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/code_completion.py#L598), [`ui/widgets/member_lookup.py:66`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/member_lookup.py#L66) |
| [`classNames`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3085) | `class_names` | [`features/method_hooks.py:236`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L236) (+2), [`features/scripts.py:478`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L478) (+1) | [`ui/widgets/code_completion.py:532`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/code_completion.py#L532) (+1), [`ui/widgets/member_lookup.py:117`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/member_lookup.py#L117) |
| [`staticFieldSources`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3086) | `static_field_sources` | [`features/monitor.py:114`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L114) | – |
| [`assemblyFields`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3088) | `assembly_fields` | [`features/monitor.py:125`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L125) | – |
| [`staticMethodSources`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3089) | `static_method_sources` | [`features/monitor.py:152`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L152) | – |
| [`assemblyClasses`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3091) | `assembly_classes` | [`features/monitor.py:163`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L163) | – |
| [`describeMethod`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3092) | `describe_method` | [`features/method_hooks.py:270`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L270) (+2), [`features/monitor.py:297`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L297) (+2), [`features/scripts.py:517`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L517) (+1) | [`ui/widgets/method_call.py:229`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L229) |
| [`callOn`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3093) | `call_on` | [`features/method_hooks.py:276`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L276) (+2), [`features/monitor.py:304`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L304) (+2), [`features/scripts.py:523`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L523) (+1) | [`ui/widgets/method_call.py:230`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/method_call.py#L230) |
| [`addMethodHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3094) | `add_method_hook` | [`features/method_hooks.py:334`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L334) | [`ui/widgets/hooks.py:958`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L958) (+2) |
| [`removeMethodHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3095) | `remove_method_hook` | [`features/method_hooks.py:342`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L342) | – |
| [`setMethodHookEnabled`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3096) | `set_method_hook_enabled` | [`features/method_hooks.py:348`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L348) | – |
| [`setMethodHookTransform`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3100) | `set_method_hook_transform` | [`features/method_hooks.py:345`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L345) | – |
| [`setMethodHookPhase`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3106) | `set_method_hook_phase` | [`features/method_hooks.py:354`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L354) | – |
| [`setMethodHookDebug`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3115) | `set_method_hook_debug` | [`features/method_hooks.py:351`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L351) | – |
| [`gasAttributes`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3119) | `gas_attributes` | [`features/scripts.py:533`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L533) (+1) | [`ui/widgets/gas_attributes.py:255`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/gas_attributes.py#L255) |
| [`gasSetBase`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3120) | `gas_set_base` | [`features/scripts.py:540`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L540) (+1) | [`ui/widgets/gas_attributes.py:272`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/gas_attributes.py#L272) |
| [`browseClasses`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3121) | `browse_classes` | [`features/scripts.py:554`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L554) (+1) | [`ui/widgets/object_browser.py:251`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/object_browser.py#L251), [`ui/widgets/reference_graph.py:624`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/reference_graph.py#L624) |
| [`browseInstances`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3122) | `browse_instances` | [`features/scripts.py:578`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L578) (+1) | [`ui/widgets/object_browser.py:273`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/object_browser.py#L273) |
| [`resolveReferences`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3123) | `resolve_references` | [`features/scripts.py:594`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L594) (+1) | [`ui/main_window.py:3769`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/main_window.py#L3769), [`ui/widgets/owning_objects.py:191`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/owning_objects.py#L191), [`ui/widgets/reference_graph.py:523`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/reference_graph.py#L523) |
| [`locateFields`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3124) | `locate_fields` | [`features/scripts.py:608`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L608) (+1) | [`ui/main_window.py:3445`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/main_window.py#L3445) |
| [`addScript`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3125) | `add_script` | [`features/scripts.py:673`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L673) | [`cli.py:674`](https://github.com/grembyte/grembyte/blob/main/grembyte/cli.py#L674), [`ui/widgets/scripts.py:556`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/scripts.py#L556) (+5) |
| [`updateScript`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3126) | `update_script` | [`features/scripts.py:676`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L676) | [`cli.py:725`](https://github.com/grembyte/grembyte/blob/main/grembyte/cli.py#L725) (+2), [`ui/widgets/scripts.py:782`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/scripts.py#L782) |
| [`removeScript`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3127) | `remove_script` | [`features/scripts.py:679`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L679) | [`ui/widgets/gas_attributes.py:455`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/gas_attributes.py#L455), [`ui/widgets/scripts.py:819`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/scripts.py#L819) (+1) |
| [`setScriptTimings`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3128) | `set_script_timings` | [`features/scripts.py:632`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L632) (+1) | – |
| [`prewarmScripts`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3129) | `prewarm_scripts` | [`features/scripts.py:252`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L252) | – |
| [`reflectionBuild`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3131) | `reflection_build` | [`features/scripts.py:240`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L240) | – |
| [`useStoredOffsets`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3133) | `use_stored_offsets` | [`features/scripts.py:242`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L242) | – |
| [`inspectExpression`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3134) | `inspect_expression` | [`features/scripts.py:495`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L495) (+1) | [`ui/widgets/scripts.py:678`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/scripts.py#L678) |
| [`runScriptNow`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3135) | `run_script_now` | [`features/scripts.py:682`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L682) | – |
| [`scriptOptions`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3136) | `script_options` | [`features/scripts.py:720`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L720) | [`ui/widgets/script_params.py:303`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/script_params.py#L303) |
| [`dispose`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.js#L3137) | `dispose` | [`features/method_hooks.py:318`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/method_hooks.py#L318), [`features/monitor.py:351`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/monitor.py#L351), [`features/scripts.py:657`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/scripts.py#L657) | – |

## `features/native_monitor.js`

Agent: [`features/native_monitor.js`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.js). Loaded by: [`features/native_monitor.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| [`add`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.js#L378) | `add` | [`features/native_monitor.py:182`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.py#L182) (+2) | – |
| [`remove`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.js#L379) | `remove` | [`features/native_monitor.py:185`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.py#L185) (+2) | – |
| [`dispose`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/native_monitor.js#L380) | `dispose` | [`features/trace.py:399`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/trace.py#L399) | – |

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
| [`stop`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/write_watch.js#L333) | `stop` | [`features/write_watch.py:110`](https://github.com/grembyte/grembyte/blob/main/grembyte/features/write_watch.py#L110) | – |

## `hooks/engine.js`

Agent: [`hooks/engine.js`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js). Loaded by: [`hooks/engine.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| [`addHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L2866) | `add_hook` | [`hooks/engine.py:442`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L442) | [`cli.py:674`](https://github.com/grembyte/grembyte/blob/main/grembyte/cli.py#L674), [`ui/main_window.py:2688`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/main_window.py#L2688), [`ui/widgets/hooks.py:838`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L838) (+4) |
| [`removeHook`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L2867) | `remove_hook` | [`hooks/engine.py:450`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L450) | [`ui/widgets/hooks.py:1820`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L1820) (+1) |
| [`setTrainingPaused`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L2868) | `set_training_paused` | [`hooks/engine.py:461`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L461) | – |
| [`setTransform`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L2869) | `set_transform` | [`hooks/engine.py:453`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L453) | [`ui/widgets/hooks.py:1072`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L1072) (+1) |
| [`setRepatch`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L2883) | `set_repatch` | [`hooks/engine.py:392`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L392) (+1) | – |
| [`setProfiling`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L2887) | `set_profiling` | [`hooks/engine.py:393`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L393) (+1) | – |
| [`setDebug`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L2893) | `set_debug` | [`hooks/engine.py:465`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L465) | [`ui/widgets/hooks.py:1160`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L1160) (+2) |
| [`setEnabled`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L2904) | `set_enabled` | [`hooks/engine.py:456`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L456) | [`ui/widgets/hooks.py:1863`](https://github.com/grembyte/grembyte/blob/main/grembyte/ui/widgets/hooks.py#L1863) |
| [`dispose`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.js#L2917) | `dispose` | [`hooks/engine.py:418`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/engine.py#L418) | – |

## `hooks/syntax.py`

Agent: the agent is a string in this module. Loaded by: [`hooks/syntax.py`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/syntax.py).

| Export | Python | Called in | Same name elsewhere |
| --- | --- | --- | --- |
| `check` | `check` | [`hooks/syntax.py:104`](https://github.com/grembyte/grembyte/blob/main/grembyte/hooks/syntax.py#L104) (+3) | – |
