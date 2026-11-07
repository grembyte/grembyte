# Unity (IL2CPP)

Unity games built with IL2CPP translate their C# code to C++ ahead of time; there is no Mono runtime and no IL in the game. Hermes reads such games through the `il2cpp_*` API that `GameAssembly.dll` (or `GameAssembly.so`) exports.

- The **catalog**, **Assemblies …** and the **trace** work as for Mono, with the same signatures (`Ns.Class:Method (int,single)`).
- Code addresses in `GameAssembly` are named as the C# method they belong to: in the trace's callers, in the [write watch](../guide/write-watch.md) and at hook sites. IL2CPP keeps no table from code to methods, so Hermes builds one when it attaches; function bounds come from the module's unwind data.
- The **method monitor** logs calls with arguments, results and the object they run on (with its Unity name, or the struct for a struct's methods); the **object inspector** shows fields, static fields and properties, evaluated on the game's main thread.
- **Method hooks** work as for Mono: scripts change arguments, fields and results and use `ctx.call`, `ctx.static` and `ctx.find`.

## Shared code

Many methods share one piece of code: the linker folds identical functions, and generic code is shared. Their names end in `[shared with N]`, and the trace counts them under the first. A hook on such code affects every method sharing it.

## Not yet available

- The **Call method** dialog and the **call graph** (IL2CPP keeps no IL).
- Games built on Unity's ECS run much of their logic as Burst code, which method hooks do not see. In V Rising most gameplay systems are Burst-compiled; their data lies in ECS chunks rather than objects.

## Tested games

V Rising (Unity 2022.3, metadata v31, Proton).
