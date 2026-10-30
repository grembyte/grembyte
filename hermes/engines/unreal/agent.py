"""Unreal Engine games: their reflection data (UClass, UFunction, FProperty).

An Unreal game is native code; what Mono's embedding API is for Mono games
is Unreal's reflection here, which the engine keeps for Blueprints,
serialization and networking. Nothing is exported for it in a shipping
build, so the agent snippet in agent.js finds two globals in the data of the
game's executable by their shape:

``FNamePool``
    Every FName's text, in blocks; the first block starts with ``None`` and
    ``ByteProperty``. Unreal Engine 4.23 and later.
``GUObjectArray``
    Every UObject (classes, functions, packages, actors ...), in chunks of
    65536 items, each item starting with the object's address.

Where UStruct, FField, FProperty and UFunction keep their fields differs
between engine versions and build settings; the agent measures it on
objects every game has (CoreUObject's ``Class``, ``Object``, ``Vector``,
``Transform`` and ``Object:ExecuteUbergraph``).

``UNREAL_AGENT_SOURCE`` is the Unreal engine adapter of the agent: it
defines ``unreal`` and registers it (see hermes/engines/base.py for the
contract). The catalog's assemblies are packages: ``/Script/<Module>`` for
the classes of a C++ module, and for Blueprints, which have a package each,
their first folder (``/Game/Blueprints``). The methods are UFunctions; the
token naming one is a hash of package, class and function name, since
object indexes change between runs.

The trace hooks a native UFunction's exec thunk (its ``Func``), and gives a
Blueprint function a stub of its own as ``Func`` that jumps on to the script
interpreter (``releaseCode`` restores them). Either way it sees calls through
``ProcessEvent`` and from Blueprint code, not C++ calling C++ directly (the
native trace sees those), nor Blueprint code calling a Blueprint function of
its own class directly (the interpreter does not pass ``Func`` for that).

For the method monitor, ``prepare`` answers ``frame: true`` and
``callFrame`` finds a call's arguments in its ``FFrame`` (Node, Object, Code,
Locals): through ProcessEvent the frame is the function's own and Locals its
parameters; called from Blueprint code, a native function gets the caller's
frame with Code at the argument expressions, of which the simple ones
(``EX_LocalVariable``, ``EX_InstanceVariable``, constants, ``EX_Self``) are
decoded. ``layout`` and ``readValue`` read objects and structs through their
FProperties (FString, FName, TArray, bitfield bools, weak pointers).
"""

from hermes.core.agent_source import load_agent_source


UNREAL_AGENT_SOURCE = load_agent_source(__file__)
