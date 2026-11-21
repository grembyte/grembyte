"""What the code fields offer while typing: the fixed members of ``ctx``, of
object wrappers and of Frida's API, and the JavaScript keywords.

A heuristic, not a JavaScript parser: it looks at the expression before the
dot (``ctx.``, ``ctx.find("…").``, ``p.add(0x10).``, a variable assigned from
one of them) and offers what that is known to have. Where it cannot tell –
a field of a game object, the result of a game method – it offers nothing
rather than something wrong. The game's own classes and fields are not
offered yet (see ROADMAP).

The members of ``ctx`` are those of the editor's context (scripts, write or
method hooks),
limited to the attached game's engine: ``ctx.isPlaying()`` only in Unreal
games, ``ctx.static`` only in Unity ones. ``engine`` is an adapter id
("mono", "il2cpp", "unreal"), ``NATIVE`` for a game without an adapter, or
None while the engine is not known (then everything is offered).
"""

import dataclasses
import re

SCRIPT = "script"
HOOK = "hook"  # a write or a method hook
VALUE_HOOK = "value_hook"
METHOD_HOOK = "method_hook"
NATIVE = "native"

_UNITY = ("mono", "il2cpp")
_ENGINES = ("mono", "il2cpp", "unreal")
_UNREAL = ("unreal",)
_ENUMS = ("unreal", "mono")


@dataclasses.dataclass(frozen=True)
class Completion:
    """One entry of the popup.

    ``params`` is None for a value (inserted as the name), else the
    parameter list shown after the name (a method: ``name()`` is inserted).
    ``description`` is a translation key, formatted with ``values``.
    ``engines`` limits the entry to these engine adapters (None: every game).
    """

    name: str
    description: str
    params: str | None = None
    engines: tuple | None = None
    values: tuple = ()
    # Matched anywhere in the name, not only at its start (class names)
    anywhere: bool = False

    @property
    def label(self):
        """The name as the popup shows it, with the parameters of a method."""
        return self.name if self.params is None else f"{self.name}({self.params})"

    def available(self, engine):
        """Whether the entry works in a game with ``engine`` (see the module)."""
        return engine is None or self.engines is None or engine in self.engines


def _value(name, engines=None):
    return Completion(name, f"completion.{{}}.{name}", None, engines)


def _method(name, params="", engines=None, key=None, values=()):
    return Completion(name, f"completion.{{}}.{key or name}", params, engines, tuple(values))


def _group(group, entries):
    return tuple(dataclasses.replace(entry, description=entry.description.format(group))
                 for entry in entries)


# ctx in the Scripts tab (scriptContext in grembyte/features/monitor.js)
SCRIPT_CONTEXT = _group("ctx", (
    _method("find", "className[, objectName]", _ENGINES),
    _method("findClass", "className", _UNREAL),
    _method("forEach", "className, callback", _ENGINES),
    _method("object", "address", _ENGINES),
    _method("isPlaying", "", _UNREAL),
    _method("outerName", "object", _UNREAL),
    _method("enum", "enumName", _ENUMS),
    _method("enumName", "enumName, value", _ENUMS),
    _method("call", "method, ...args", _ENGINES),
    _method("static", "field", _UNITY),
    _value("gas", _UNREAL),
    _value("native"),
    _value("args"),
    _value("lang"),
    _method("log", "...items"),
    _value("state"),
    _value("runs"),
    _value("scriptId"),
))

# ctx of write hooks (grembyte/hooks/engine.js)
VALUE_HOOK_CONTEXT = _group("hook", (
    _value("old"),
    _value("new"),
    _value("delta"),
    _value("params"),
    _value("state"),
    _value("valueType"),
    _value("address"),
    _value("writePc"),
    _value("threadId"),
    _value("hookId"),
    _method("log", "...items"),
))

# ctx of method hooks (hookBefore/hookLeave in grembyte/features/monitor.js)
METHOD_HOOK_CONTEXT = tuple(entry for entry in VALUE_HOOK_CONTEXT if entry.name in {
    "params", "state", "threadId", "hookId", "log"}) + _group("hook", (
    _value("phase"),
    _value("args"),
    _value("result"),
    _value("refs"),
    _value("this"),
    _value("caller"),
    _value("method"),
)) + tuple(entry for entry in SCRIPT_CONTEXT if entry.name in {
    "call", "static", "find", "forEach", "object", "outerName", "isPlaying", "enum",
    "enumName"})

# Both, for code whose kind of hook is not known
HOOK_CONTEXT = VALUE_HOOK_CONTEXT + tuple(
    entry for entry in METHOD_HOOK_CONTEXT if entry not in VALUE_HOOK_CONTEXT)

# What every object wrapper has besides the game's fields and methods
WRAPPER = _group("wrapper", (
    _value("name"),
    _value("className"),
    _value("address"),
    _method("field", "name"),
    _method("get", "name"),
    _method("call", "name, ...args"),
    _method("component", "className", _UNITY),
))

NATIVE_CONTEXT = _group("native", (
    _method("module", "[name]"),
    _method("scan", "pattern[, module]"),
    _method("scanAll", "pattern[, module]"),
    _method("rip", "address, dispOffset, length"),
    _method("fn", "address, returnType, argTypes[, abi]"),
))

GAS = _group("gas", (
    _method("attributes"),
    _method("get", "name"),
    _method("setBase", "name, value"),
    _method("findSet", "regex"),
))

_INTEGERS = (("S8", 8), ("U8", 8), ("S16", 16), ("U16", 16), ("S32", 32), ("U32", 32),
             ("S64", 64), ("U64", 64))
# Frida's NativePointer
POINTER = _group("pointer", (
    _method("add", "offset"),
    _method("sub", "offset"),
    _method("readPointer"),
    _method("writePointer", "value"),
    *(_method(f"read{suffix}", "", key="read_signed" if suffix[0] == "S" else "read_unsigned",
              values=(("bits", bits),)) for suffix, bits in _INTEGERS),
    *(_method(f"write{suffix}", "value",
              key="write_signed" if suffix[0] == "S" else "write_unsigned",
              values=(("bits", bits),)) for suffix, bits in _INTEGERS),
    _method("readFloat"),
    _method("readDouble"),
    _method("writeFloat", "value"),
    _method("writeDouble", "value"),
    _method("readUtf8String", "[length]"),
    _method("readUtf16String", "[length]"),
    _method("writeUtf8String", "text"),
    _method("writeUtf16String", "text"),
    _method("readByteArray", "length"),
    _method("writeByteArray", "bytes"),
    _method("isNull"),
    _method("equals", "other"),
    _method("compare", "other"),
    _method("and", "mask"),
    _method("toInt32"),
    _method("toString", "[radix]"),
))

# A Frida Module (ctx.native.module(), Process.mainModule …)
MODULE = _group("module", (
    _value("name"),
    _value("base"),
    _value("size"),
    _value("path"),
    _method("findExportByName", "name"),
    _method("getExportByName", "name"),
    _method("findSymbolByName", "name"),
    _method("enumerateExports"),
    _method("enumerateImports"),
))

MEMORY = _group("memory", (
    _method("alloc", "size"),
    _method("allocUtf8String", "text"),
    _method("allocUtf16String", "text"),
    _method("copy", "destination, source, length"),
    _method("protect", "address, size, protection"),
    _method("scanSync", "address, size, pattern"),
))

PROCESS = _group("process", (
    _value("id"),
    _value("arch"),
    _value("platform"),
    _value("pointerSize"),
    _value("mainModule"),
    _method("getModuleByName", "name"),
    _method("findModuleByName", "name"),
    _method("enumerateModules"),
    _method("findRangeByAddress", "address"),
))

MODULE_API = _group("module_api", (
    _method("load", "path"),
    _method("findGlobalExportByName", "name"),
    _method("getGlobalExportByName", "name"),
))

# Names that stand on their own: Frida's globals and ctx
GLOBALS = _group("global", (
    _value("ctx"),
    _method("ptr", "value"),
    _value("NULL"),
    _value("NativePointer"),
    _value("NativeFunction"),
    _value("Memory"),
    _value("Process"),
    _value("Module"),
    _method("hexdump", "target[, options]"),
    _value("Int64"),
    _value("UInt64"),
    _value("console"),
))

# The JavaScript keywords and literals a script uses (the highlighter's list
# without the words of TypeScript and modules, which a script cannot use)
KEYWORDS = tuple(Completion(word, "completion.keyword") for word in sorted({
    "async", "await", "break", "case", "catch", "class", "const", "continue", "debugger",
    "default", "delete", "do", "else", "extends", "false", "finally", "for", "function",
    "if", "in", "instanceof", "let", "new", "null", "of", "return", "super", "switch",
    "this", "throw", "true", "try", "typeof", "undefined", "var", "void", "while", "yield",
}))

# What an expression is, and what follows its dot
_POINTER, _WRAPPER, _MODULE, _CLASS = "pointer", "wrapper", "module", "class"
_MEMBERS = {
    _POINTER: POINTER, _WRAPPER: WRAPPER, _MODULE: MODULE, "native": NATIVE_CONTEXT,
    "gas": GAS, "memory": MEMORY, "process": PROCESS, "module_api": MODULE_API,
}
# NativePointer methods that give a NativePointer again
_POINTER_METHODS = frozenset({"add", "sub", "and", "or", "xor", "readPointer"})
# Names that are known on their own: (name, called) -> kind
_ROOTS = {
    ("ctx", False): "ctx", ("ptr", True): _POINTER, ("NULL", False): _POINTER,
    ("Memory", False): "memory", ("Process", False): "process", ("Module", False): "module_api",
}
# What a step gives: (kind of the object, member, called) -> kind
_STEPS = {
    ("ctx", "find", True): _WRAPPER,
    ("ctx", "object", True): _WRAPPER,
    ("ctx", "findClass", True): _WRAPPER,
    ("ctx", "this", False): _WRAPPER,
    ("ctx", "native", False): "native",
    ("ctx", "gas", False): "gas",
    ("native", "scan", True): _POINTER,
    ("native", "rip", True): _POINTER,
    ("native", "module", True): _MODULE,
    (_MODULE, "base", False): _POINTER,
    ("process", "mainModule", False): _MODULE,
    ("process", "getModuleByName", True): _MODULE,
    ("process", "findModuleByName", True): _MODULE,
    ("module_api", "load", True): _MODULE,
    ("module_api", "findGlobalExportByName", True): _POINTER,
    ("module_api", "getGlobalExportByName", True): _POINTER,
    (_WRAPPER, "component", True): _WRAPPER,
    # a wrapper's address; nothing else offered here has one
    (_WRAPPER, "address", False): _POINTER,
    (None, "address", False): _POINTER,
    **{(_POINTER, name, True): _POINTER for name in _POINTER_METHODS},
}
_PREFIX = re.compile(r"[\w$]*$")
_MAX_DEPTH = 6


def _skip_back(text, end, opening, closing):
    """The index of the bracket ``opening`` matching the ``closing`` one at
    ``end - 1``, or -1. Brackets inside strings are not told apart."""
    depth = 0
    for position in range(end - 1, -1, -1):
        if text[position] == closing:
            depth += 1
        elif text[position] == opening:
            depth -= 1
            if depth == 0:
                return position
    return -1


def receiver(text):
    """The member chain ``text`` ends with: ``ctx.find("A").address`` for
    ``x = ctx.find("A").address``; "" if it does not end with one."""
    end = len(text.rstrip())
    position = end
    while position > 0:
        character = text[position - 1]
        if character in ")]":
            start = _skip_back(text, position, "([" [")]".index(character)], character)
            if start < 0:
                return ""
            position = start
            continue
        match = re.search(r"[\w$]+$", text[:position])
        if match is None:
            break
        position = match.start()
        before = text[:position].rstrip()
        if before.endswith("?."):
            position = len(before) - 2
        elif before.endswith("."):
            position = len(before) - 1
        else:
            # ``new NativePointer(…)`` is one expression
            if before.endswith("new") and not re.search(r"[\w$]new$", before):
                position = len(before) - 3
            break
    return text[position:end].strip()


def _split_last(chain):
    """``(object, member, called, arguments)`` of the chain's last step:
    ``("p", "add", True, "4")`` for ``p.add(4)``; object "" for a chain of
    one name, arguments None for a step without a call."""
    chain = chain.strip()
    arguments = None
    if chain.endswith(")"):
        start = _skip_back(chain, len(chain), "(", ")")
        if start < 0:
            return None
        arguments = chain[start + 1:-1]
        chain = chain[:start].rstrip()
    elif chain.endswith("]"):
        return None  # an element of an array or a computed member
    match = re.search(r"(\?\.|\.)?\s*([\w$]+)$", chain)
    if match is None:
        return None
    rest = chain[:match.start()].rstrip()
    called = arguments is not None
    if match.group(1):
        return (rest, match.group(2), called, arguments) if rest else None
    return None if rest else ("", match.group(2), called, arguments)


# The members of a game class (see ClassMembers.from_agent) while the agent is
# asked for them
PENDING = object()
# What the game answered for a tooltip when it has no such class or field
MISSING = object()
# Types whose values are no wrappers: numbers, texts, enums of Unreal
_PLAIN_TYPES = frozenset({
    "bool", "boolean", "byte", "sbyte", "char", "short", "ushort", "int", "uint", "long",
    "ulong", "single", "float", "double", "decimal", "string", "object", "void",
    "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64",
    "intptr", "uintptr", "fstring", "fname", "ftext", "?",
})
_STRING_ARGUMENT = re.compile(r"""\s*(["'`])([^"'`$]+)\1\s*(?:,|$)""")


def class_type(type_name):
    """The class a value of ``type_name`` is a wrapper of, as the agent looks
    it up, or None: numbers, texts, enums of Unreal, arrays and generic types
    are not followed."""
    name = (type_name or "").strip().rstrip("&*").strip()
    if not name or "<" in name or "[" in name or "`" in name:
        return None
    short = name.rsplit(".", 1)[-1] if name.startswith("System.") else name
    if short.lower() in _PLAIN_TYPES or name.lower() in _PLAIN_TYPES:
        return None
    if name.startswith("E") and name[1:2].isupper() and "." not in name:
        return None  # an Unreal enum (EPrimaryAttribute)
    return name


@dataclasses.dataclass(frozen=True)
class ClassMembers:
    """What a game class has: fields and properties (name -> type) and
    methods (name -> [(parameters, return type)], one per overload)."""

    name: str
    fields: dict
    properties: dict
    methods: dict
    statics: frozenset = frozenset()

    @classmethod
    def from_agent(cls, members):
        """From the agent's ``classMembers`` answer (grembyte/features/monitor.js)."""
        fields, properties, methods, statics = {}, {}, {}, set()
        for field in members.get("fields", ()):
            if field["name"].startswith("<"):
                continue  # compiler-made, e.g. a property's backing field
            fields.setdefault(field["name"], field["type"])
            if field.get("static"):
                statics.add(field["name"])
        for prop in members.get("properties", ()):
            properties.setdefault(prop["name"], prop["type"])
        for method in members.get("methods", ()):
            name = method["name"]
            if name.startswith(("<", ".", "op_")):
                continue
            params = ", ".join(f"{kind} {param}".strip() for kind, param in method["params"])
            overloads = methods.setdefault(name, [])
            if (params, method["returns"]) not in overloads:
                overloads.append((params, method["returns"]))
        return cls(members.get("name") or "", fields, properties, methods, frozenset(statics))

    def member_type(self, name, called):
        """The type of field or property ``name`` (``called`` False) or the
        return type of method ``name``; None if the class has no such member,
        or overloads return different types."""
        if not called:
            return self.fields.get(name, self.properties.get(name))
        returns = {result for _params, result in self.methods.get(name, ())}
        return returns.pop() if len(returns) == 1 else None

    def entries(self):
        """The members as Completions: fields and properties, then methods."""
        values = [
            Completion(name, "completion.member.static" if name in self.statics
                       else "completion.member.field", values=(("type", kind),))
            for name, kind in self.fields.items()]
        values += [Completion(name, "completion.member.property", values=(("type", kind),))
                   for name, kind in self.properties.items() if name not in self.fields]
        values.sort(key=lambda entry: entry.name.lower())
        methods = [Completion(name, "completion.member.method", params, values=(("type", result),))
                   for name, overloads in self.methods.items()
                   for params, result in overloads]
        methods.sort(key=lambda entry: entry.name.lower())
        return tuple(values + methods)


@dataclasses.dataclass
class Scope:
    """What the completion knows beyond the fixed names.

    ``lookup(type_name)`` gives ClassMembers, None or PENDING (see
    grembyte.ui.widgets.member_lookup); ``args`` are a method hook's
    parameters (name -> type) and ``this`` its class. ``pending`` is set when
    an answer of the lookup is still missing.
    """

    lookup: object = None
    args: dict = dataclasses.field(default_factory=dict)
    this: str | None = None
    pending: bool = False

    def members(self, type_name):
        """The ClassMembers of ``type_name``, or None."""
        name = class_type(type_name)
        if name is None or self.lookup is None:
            return None
        found = self.lookup(name)
        if found is PENDING:
            self.pending = True
            return None
        return found

    def class_names(self):
        """The names of the classes ctx.find finds, () while not known."""
        return self._list("class_names")

    def static_fields(self):
        """``[name, type]`` of the static fields ctx.static reads, () while
        not known."""
        return self._list("static_fields")

    def answer(self, kind, name):
        """What the lookup's ``kind`` (class_info, static_value) gives for
        ``name``: a dict, MISSING, PENDING, or None without a game."""
        ask = getattr(self.lookup, kind, None)
        return None if ask is None else ask(name)

    def _list(self, kind):
        ask = getattr(self.lookup, kind, None)
        found = None if ask is None else ask()
        if found is PENDING:
            self.pending = True
            return ()
        return found or ()


# The name a ctx.find("…", ctx.forEach("…" or ctx.static("…" is typed with:
# the function, the quote and the text typed so far in the string
_STRING_ARGUMENT_AT_END = re.compile(
    r"""\bctx\s*\.\s*(find|forEach|static)\s*\(\s*(["'`])([^"'`\\$]*)$""")
MAX_NAMES = 200


def string_argument(line):
    """``(typed, quote, function)`` if ``line`` ends inside the first
    argument of ctx.find, ctx.forEach or ctx.static (a class or a static
    field's name), else None."""
    match = _STRING_ARGUMENT_AT_END.search(line)
    return None if match is None else (match.group(3), match.group(2), match.group(1))


_STRING_ARGUMENT_IN_LINE = re.compile(
    r"""\bctx\s*\.\s*(find|forEach|static)\s*\(\s*(["'`])([^"'`\\$]*)\2""")


def string_at(line, column):
    """``(function, text)`` of the first argument of ctx.find, ctx.forEach or
    ctx.static on ``line`` whose string (quotes included) holds ``column``,
    else None."""
    for match in _STRING_ARGUMENT_IN_LINE.finditer(line):
        if match.start(3) - 1 <= column <= match.end(3):
            return match.group(1), match.group(3)
    return None


def tooltip(function, text, scope, translations, engine=None):
    """The tooltip of the string ``text`` given to ctx.find, ctx.forEach
    (the class: its base classes, in Unreal games its live objects) or
    ctx.static (the field's type and value now); None for none. While the
    game is asked it says so, and ``scope.pending`` is set."""
    allowed = _UNITY if function == "static" else _ENGINES
    if not text.strip() or (engine is not None and engine not in allowed):
        return None
    kind = "static_value" if function == "static" else "class_info"
    found = scope.answer(kind, text)
    if found is PENDING:
        scope.pending = True
        return translations["completion.tooltip_reading"]
    if function == "static":
        return _static_tooltip(text, found, scope, translations)
    if found is MISSING:
        return translations["completion.tooltip_no_class"].format(name=text)
    if found is None:
        return None
    lines = [translations["completion.tooltip_class"].format(name=found["name"])]
    if found.get("parents"):
        parents = " → ".join(found["parents"]) + (" → …" if found.get("more") else "")
        lines.append(translations["completion.tooltip_parents"].format(parents=parents))
    if found.get("objects") is not None:
        lines.append(translations["completion.tooltip_objects"].format(count=found["objects"]))
    return "\n".join(lines)


def _static_tooltip(text, found, scope, translations):
    if found is MISSING:
        return translations["completion.tooltip_no_static"].format(name=text)
    if found is None:
        # No game: the type, if the list of static fields is known
        known = dict(scope.static_fields())
        return (translations["completion.tooltip_static_type"].format(name=text, type=known[text])
                if text in known else None)
    value = (found.get("value") or {}).get("text") or "—"
    return translations["completion.tooltip_static"].format(
        name=text, type=found.get("type", "?"), value=value)


def matches(entry, typed):
    """Whether ``entry`` fits what was typed of its name: its start, or for
    class names any part of it (``Player`` finds ``BP_PlayerCharacter_C``)."""
    name, wanted = entry.name.lower(), typed.lower()
    return name.startswith(wanted) or (entry.anywhere and wanted in name)


def _name_entries(typed, function, scope):
    """The class names (ctx.find, ctx.forEach) or static fields (ctx.static)
    containing ``typed``, those starting with it first, at most MAX_NAMES."""
    if function == "static":
        named = [(name, type_name) for name, type_name in scope.static_fields()]
    else:
        named = [(name, None) for name in scope.class_names()]
    wanted = typed.lower()
    found = [entry for entry in named if wanted in entry[0].lower()]
    found.sort(key=lambda entry: (not entry[0].lower().startswith(wanted), entry[0].lower()))
    return tuple(
        Completion(name, "completion.class", anywhere=True) if type_name is None
        else Completion(name, "completion.member.static", values=(("type", type_name),),
                        anywhere=True)
        for name, type_name in found[:MAX_NAMES])


def method_parameters(signature):
    """``(class, {name: type})`` of a method signature with parameter names,
    as the hook dialog shows it: ``Game.Combat:RollDamage (CharacterStats
    roller,single scale)`` or with full types (``System.Int32 min``). A
    parameter without a name is left out."""
    head, separator, rest = (signature or "").partition(" (")
    owner = head.partition(":")[0].strip() or None
    if not separator:
        return owner, {}
    depth, part, parts = 0, "", []
    for character in rest[:rest.rfind(")")] if ")" in rest else rest:
        depth += {"<": 1, ">": -1}.get(character, 0)
        if character == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += character
    parts.append(part)
    params = {}
    for entry in parts:
        kind, _space, name = entry.strip().rpartition(" ")
        if kind and re.fullmatch(r"[A-Za-z_$][\w$]*", name):
            params[name] = kind.strip()
    return owner, params


def _class_kind(type_name):
    name = class_type(type_name)
    return None if name is None else (_CLASS, name)


def classify(chain, source="", depth=0, scope=None):
    """What the expression ``chain`` gives, as far as known: "ctx",
    "pointer", "wrapper", "module", "native", "gas", "memory", "process",
    "module_api" (Frida's ``Module``), "args" (a method hook's ctx.args),
    ``("class", name)`` for a wrapper of a known class, or None. ``source``
    is the code before it, where a variable's assignment is looked up."""
    if depth > _MAX_DEPTH:
        return None
    scope = scope or Scope()
    chain = chain.strip()
    if chain.startswith("new "):
        return _POINTER if re.fullmatch(r"new\s+NativePointer\s*\(.*\)", chain, re.S) else None
    parts = _split_last(chain)
    if parts is None:
        return None
    owner, member, called, arguments = parts
    if owner == "":
        if (member, called) in _ROOTS:
            return _ROOTS[member, called]
        return None if called else _variable(member, source, depth, scope)
    kind = classify(owner, source, depth + 1, scope)
    if kind == "ctx":
        return _ctx_step(member, called, arguments, scope)
    if kind == "args":
        return None if called else _class_kind(scope.args.get(member))
    if isinstance(kind, tuple):
        return _class_step(kind[1], member, called, arguments, scope)
    return _STEPS.get((kind, member, called))


def _ctx_step(member, called, arguments, scope):
    if member in ("find", "findClass") and called:
        # ctx.find("BP_PlayerCharacter_C"): a wrapper of that class
        named = _STRING_ARGUMENT.match(arguments or "")
        return _class_kind(named.group(2)) if named else _WRAPPER
    if member == "this" and not called:
        return _class_kind(scope.this) if scope.this else _WRAPPER
    if member == "static" and called:
        # ctx.static("Ns.Class.Field"): an object of the field's type
        named = _STRING_ARGUMENT.match(arguments or "")
        if named is None:
            return None
        owner, _dot, field = named.group(2).replace(":", ".").rpartition(".")
        members = scope.members(owner) if owner else None
        return None if members is None else _class_kind(members.member_type(field, False))
    if member == "args" and not called and scope.args:
        return "args"
    return _STEPS.get(("ctx", member, called))


def _class_step(class_name, member, called, arguments, scope):
    if member == "address" and not called:
        return _POINTER
    if member == "component" and called:
        named = _STRING_ARGUMENT.match(arguments or "")
        return _class_kind(named.group(2)) if named else _WRAPPER
    if member in ("field", "get", "call") and called:
        named = _STRING_ARGUMENT.match(arguments or "")
        if named is None:
            return None
        member, called = named.group(2), member == "call"
    members = scope.members(class_name)
    return None if members is None else _class_kind(members.member_type(member, called))


def _variable(name, source, depth, scope):
    """What the variable ``name`` holds, from its last ``const``/``let``/``var``
    before the cursor; None if it is not assigned there or not known."""
    found = None
    for match in re.finditer(
            rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*([^;\n]+)", source):
        found = match
    if found is None:
        return None
    value = re.sub(r"//.*$", "", found.group(1)).strip()
    return classify(value, source[:found.start()], depth + 1, scope)


def _in_code(line, state):
    """Whether the end of ``line`` is code, not a string or a comment; ``state``
    is the line's starting state as the JavaScript highlighter keeps it."""
    # pylint: disable=import-outside-toplevel
    from grembyte.ui.widgets.code_editor import highlight_javascript_tokens

    tokens = highlight_javascript_tokens(line + " ", state)
    return tokens[len(line)] not in ("string", "comment")


_CONTEXTS = {SCRIPT: SCRIPT_CONTEXT, HOOK: HOOK_CONTEXT, VALUE_HOOK: VALUE_HOOK_CONTEXT,
             METHOD_HOOK: METHOD_HOOK_CONTEXT}


def hook_context(kind):
    """The completion context of a hook of ``kind`` ("value", "method"; else
    either)."""
    return {"value": VALUE_HOOK, "method": METHOD_HOOK}.get(kind, HOOK)


def completions(source, line, state=0, context=SCRIPT, engine=None, scope=None):
    """``(prefix, entries)``: what to offer at the end of ``source`` (all code
    before the cursor), whose last line ``line`` starts in highlighter state
    ``state``. ``prefix`` is the part of the name typed already; entries is
    empty where nothing is known. ``scope`` (a Scope) adds the game's
    classes and a method hook's arguments; its ``pending`` tells whether
    members are still being read."""
    scope = scope or Scope()
    scope.pending = False
    argument = string_argument(line)
    if argument is not None and not _in_code(line[:len(line) - len(argument[0]) - 1], state):
        argument = None  # the ctx.find is in a comment or a string itself
    if argument is not None:
        typed, _quote, function = argument
        allowed = (_UNITY if function == "static" else _ENGINES)
        if engine is not None and engine not in allowed:
            return typed, ()
        return typed, _name_entries(typed, function, scope)
    prefix = _PREFIX.search(line)[0]  # always matches, maybe empty
    if not _in_code(line, state):
        return prefix, ()
    before = source[:len(source) - len(prefix)]
    stripped = before.rstrip()
    if stripped.endswith("."):
        if stripped.endswith("..."):
            return prefix, ()
        owner = stripped[:-2] if stripped.endswith("?.") else stripped[:-1]
        if owner and owner[-1].isdigit() and re.search(r"(?<![\w$])\d+$", owner):
            return prefix, ()  # a number: 1.5
        chain = receiver(owner.rstrip())
        if not chain:
            return prefix, ()
        kind = classify(chain, before, scope=scope)
        if kind == "ctx":
            members = _CONTEXTS[context]
        elif kind == "args":
            members = tuple(Completion(name, "completion.member.argument",
                                       values=(("type", type_name),))
                            for name, type_name in scope.args.items())
        elif isinstance(kind, tuple):
            found = scope.members(kind[1])
            members = (found.entries() if found is not None else ()) + WRAPPER
        else:
            members = _MEMBERS.get(kind, ())
        return prefix, tuple(entry for entry in members if entry.available(engine))
    if not expression_starts(line[:len(line) - len(prefix)]):
        return prefix, ()
    return prefix, GLOBALS + KEYWORDS


# Before a name that starts an expression or a statement: an operator or an
# opening bracket, or a keyword that takes one (return ctx…, typeof ptr…)
_EXPRESSION_START_CHARACTERS = frozenset("=(,;{}[!?:&|+-*/<>%~^")
_EXPRESSION_START_WORD = re.compile(
    r"(?<![\w$.])(?:return|typeof|new|await|case|else|in|of|do|throw|yield|void|delete|"
    r"instanceof)$")


def expression_starts(text):
    """Whether a new expression or statement can start after ``text`` (the
    line before a name): at the start of the line, after an operator, an
    opening bracket or a keyword like ``return``; not after a closed
    bracket, a string, a number or another name, where only an operator or a
    dot can follow."""
    text = text.rstrip()
    if not text:
        return True
    return text[-1] in _EXPRESSION_START_CHARACTERS or bool(_EXPRESSION_START_WORD.search(text))


def description(entry, translations):
    """The entry's translated description."""
    return translations[entry.description].format(**dict(entry.values))
