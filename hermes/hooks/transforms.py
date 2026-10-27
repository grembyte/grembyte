"""Value transforms applied by write hooks inside the target process.

A transform is a JavaScript function expression that runs in the Frida agent
right after the hooked instruction has written the target value. It receives
a single ``ctx`` object and returns the value to store, or ``undefined`` /
``null`` to keep the value the game wrote:

    ctx.old        value before the write
    ctx.new        value the game just wrote
    ctx.delta      ctx.new - ctx.old
    ctx.params     the transform's JSON parameters (see ``ValueTransform.params``)
    ctx.state      per-hook object that persists between calls
    ctx.valueType  e.g. "int32", "uint8" or "float" (see hermes.core.value_types)
    ctx.address    target address as hex string
    ctx.writePc    address of the hooked write instruction as hex string
    ctx.threadId   id of the writing thread
    ctx.hookId     id of the hook
    ctx.log(...)   output shown in the hook debug window (no-op otherwise)

Integer results are truncated and clamped to the value type's range; int64
and uint64 values are passed as JavaScript numbers and are only exact up to
2**53.

New transforms subclass ``ValueTransform``, set ``kind`` and ``source`` and
register themselves with ``@register_transform``. Declaring ``parameters``
lets the UI build an input form for the transform automatically; labels
fall back to ``title`` / the parameter name.
``ScriptTransform`` accepts an arbitrary function source for one-off
experiments.

Hook templates maintained in the database are ``TransformDefinition``
objects (name, function source, parameters); ``TemplateTransform`` applies
one with concrete parameter values. Their ``kind`` is ``"value"`` for the
write hooks described above or ``"method"`` for method hooks, whose function
gets another ``ctx`` (see ``hermes.features.method_hooks``). A hook keeps its own copy of the
definition, so later template changes only reach it when they are adopted.
"""

import math
import re
from dataclasses import dataclass, field


_TRANSFORMS = {}


@dataclass(frozen=True)
class TransformParameter:
    """Describes one constructor argument of a transform for UI forms.

    ``kind`` is ``"number"``, ``"choice"`` (one of ``choices``) or ``"code"``
    (multi-line text). ``optional`` parameters may be left empty (``None``).
    ``label`` and ``choice_labels`` (parallel to ``choices``) are display
    texts used by templates.
    """

    name: str
    kind: str = "number"
    default: object = None
    choices: tuple = ()
    optional: bool = False
    label: str = ""
    choice_labels: tuple = ()

    def choice_label(self, index):
        """Return the display text of ``choices[index]``."""
        if index < len(self.choice_labels) and self.choice_labels[index]:
            return self.choice_labels[index]
        return str(self.choices[index])

    def to_json(self):
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "optional": self.optional,
            "choices": [
                [choice, self.choice_label(index)]
                for index, choice in enumerate(self.choices)
            ],
        }

    @classmethod
    def from_json(cls, data):
        choices = data.get("choices") or []
        return cls(
            name=data["name"],
            kind=data.get("kind", "number"),
            default=data.get("default"),
            choices=tuple(choice for choice, _label in choices),
            optional=bool(data.get("optional", False)),
            label=data.get("label", ""),
            choice_labels=tuple(label for _choice, label in choices),
        )


PARAMETER_NAME = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


@dataclass(frozen=True)
class TransformDefinition:
    """A manipulation as maintained in a hook template or copied into a hook.

    Two definitions are equal when name, source and parameters match;
    ``description`` and ``template_id`` (the template it stems from) do not
    count.
    """

    name: str
    source: str
    parameters: tuple = ()
    description: str = field(default="", compare=False)
    template_id: int | None = field(default=None, compare=False)
    kind: str = "value"  # or "method"

    def parameters_json(self):
        return [parameter.to_json() for parameter in self.parameters]

    @classmethod
    def from_json(cls, name, source, parameters, description="", template_id=None,
                  kind="value"):
        # pylint: disable=too-many-arguments
        return cls(
            name=name,
            source=source,
            parameters=tuple(TransformParameter.from_json(p) for p in parameters or []),
            description=description or "",
            template_id=template_id,
            kind=kind or "value",
        )


def register_transform(cls):
    """Class decorator that makes a transform available via ``create_transform``."""
    if not cls.kind:
        raise ValueError(f"{cls.__name__} must define a non-empty kind")
    if cls.kind in _TRANSFORMS:
        raise ValueError(f"Transform kind already registered: {cls.kind}")
    _TRANSFORMS[cls.kind] = cls
    return cls


def available_transforms():
    """Return the registered transform classes keyed by kind."""
    return dict(_TRANSFORMS)


def create_transform(kind, **params):
    """Instantiate a registered transform by its kind."""
    try:
        cls = _TRANSFORMS[kind]
    except KeyError as error:
        raise ValueError(f"Unknown transform kind: {kind}") from error
    return cls(**params)


def _require_number(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return value


class ValueTransform:
    """Base class for a value manipulation executed by the hook agent."""

    kind = ""
    title = ""
    source = ""
    parameters = ()

    def __init__(self, **params):
        self.params = self.validate(params)

    def form_values(self):
        """Return the constructor arguments, e.g. to pre-fill an edit form."""
        return dict(self.params)

    def validate(self, params):
        """Check and normalize the parameters; return the dict sent to the agent."""
        return params

    def to_agent(self):
        return {
            "kind": self.kind,
            "source": self.source,
            "params": self.params,
            "native": native_spec(self.source, self.params),
        }

    def describe(self):
        details = ", ".join(
            f"{key}={value}" for key, value in self.params.items() if value is not None
        )
        return f"{self.kind}({details})"

    def __repr__(self):
        return f"<{type(self).__name__} {self.describe()}>"


@register_transform
class MultiplyTransform(ValueTransform):
    """Multiply the change of each write, e.g. gold gains x10."""

    kind = "multiply"
    title = "Multiply"
    source = r"""
function (ctx) {
  const { factor, direction } = ctx.params;
  if (direction === 'increase' && ctx.delta <= 0)
    return undefined;
  if (direction === 'decrease' && ctx.delta >= 0)
    return undefined;
  return ctx.old + ctx.delta * factor;
}
"""

    DIRECTIONS = ("increase", "decrease", "both")
    parameters = (
        TransformParameter("factor", default=2),
        TransformParameter("direction", "choice", "increase", DIRECTIONS),
    )

    def __init__(self, factor, direction="increase"):
        super().__init__(factor=factor, direction=direction)

    def validate(self, params):
        _require_number("factor", params["factor"])
        if params["direction"] not in self.DIRECTIONS:
            raise ValueError(
                f"direction must be one of {', '.join(self.DIRECTIONS)}"
            )
        return params


@register_transform
class FreezeTransform(ValueTransform):
    """Overwrite every write with a fixed value."""

    kind = "freeze"
    title = "Freeze"
    source = r"""
function (ctx) {
  return ctx.params.value;
}
"""
    parameters = (TransformParameter("value", default=0),)

    def __init__(self, value):
        super().__init__(value=value)

    def validate(self, params):
        _require_number("value", params["value"])
        return params


@register_transform
class ClampTransform(ValueTransform):
    """Keep written values within optional lower and upper bounds."""

    kind = "clamp"
    title = "Clamp"
    parameters = (
        TransformParameter("minimum", optional=True),
        TransformParameter("maximum", optional=True),
    )
    source = r"""
function (ctx) {
  const { minimum, maximum } = ctx.params;
  let value = ctx.new;
  if (minimum !== null && value < minimum)
    value = minimum;
  if (maximum !== null && value > maximum)
    value = maximum;
  return value;
}
"""

    def __init__(self, minimum=None, maximum=None):
        super().__init__(minimum=minimum, maximum=maximum)

    def validate(self, params):
        minimum = params["minimum"]
        maximum = params["maximum"]
        if minimum is None and maximum is None:
            raise ValueError("clamp needs a minimum, a maximum or both")
        if minimum is not None:
            _require_number("minimum", minimum)
        if maximum is not None:
            _require_number("maximum", maximum)
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("minimum must not be greater than maximum")
        return params


@register_transform
class ScriptTransform(ValueTransform):
    """User-supplied JavaScript function, e.g. ``ctx => ctx.old + ctx.delta * 2``."""

    kind = "script"
    title = "Script (JavaScript)"
    parameters = (
        TransformParameter(
            "source",
            "code",
            "ctx => ctx.delta > 0 ? ctx.old + ctx.delta * 2 : undefined",
        ),
    )

    def __init__(self, source, **params):
        if not isinstance(source, str) or not source.strip():
            raise ValueError("script transform needs a function source")
        self.source = source
        super().__init__(**params)

    def form_values(self):
        return {"source": self.source, **self.params}

    def describe(self):
        first_line = self.source.strip().splitlines()[0]
        return f"script({first_line})"


class TemplateTransform(ValueTransform):
    """Apply a ``TransformDefinition`` with concrete parameter values."""

    kind = "template"

    def __init__(self, definition, **params):
        self.definition = definition
        self.source = definition.source
        self.parameters = definition.parameters
        super().__init__(**params)

    def validate(self, params):
        values = {}
        for parameter in self.parameters:
            value = params.get(parameter.name, parameter.default)
            name = parameter.label or parameter.name
            if value is None:
                if not parameter.optional and parameter.kind != "code":
                    raise ValueError(f"{name} needs a value")
            elif parameter.kind == "number":
                _require_number(name, value)
            elif parameter.kind == "choice" and value not in parameter.choices:
                raise ValueError(f"{name} must be one of {', '.join(map(str, parameter.choices))}")
            values[parameter.name] = value
        return values

    def to_agent(self):
        return {"kind": self.definition.name, "source": self.source, "params": self.params,
                "native": native_spec(self.source, self.params)}

    def describe(self):
        details = ", ".join(
            f"{key}={value}" for key, value in self.params.items() if value is not None
        )
        return f"{self.definition.name}({details})"


def _code(source):
    return " ".join(source.split())


def native_spec(source, params):
    """The built-in manipulation ``source`` is a copy of, for the agent to run in C.

    ``{"op": "multiply", "factor", "direction"}``, ``{"op": "freeze", "value"}``
    or ``{"op": "clamp", "minimum", "maximum"}``; None for other code (it runs
    in JavaScript). Copies of the examples in templates and imports count by
    their code, whatever their name.
    """
    code = _code(source)
    try:
        if code == _code(MultiplyTransform.source):
            return {"op": "multiply", "factor": float(params["factor"]),
                    "direction": params.get("direction", "increase")}
        if code == _code(FreezeTransform.source):
            return {"op": "freeze", "value": float(params["value"])}
        if code == _code(ClampTransform.source):
            minimum, maximum = params.get("minimum"), params.get("maximum")
            return {"op": "clamp",
                    "minimum": None if minimum is None else float(minimum),
                    "maximum": None if maximum is None else float(maximum)}
    except (KeyError, TypeError, ValueError):
        return None
    return None


def as_template_transform(transform):
    """Return ``transform`` as a TemplateTransform, e.g. to store its definition."""
    if isinstance(transform, TemplateTransform):
        return transform
    parameters = tuple(p for p in type(transform).parameters if p.name in transform.params)
    definition = TransformDefinition(transform.kind, transform.source.strip(), parameters)
    return TemplateTransform(definition, **transform.params)


# Built-in transforms offered as initial hook templates, keyed by seed key.
EXAMPLE_TEMPLATE_KINDS = ("multiply", "freeze", "clamp")


# Example templates for method hooks: seed key -> (source, parameters).
METHOD_EXAMPLES = {
    "method_multiply": (
        """function (ctx) {
  // The method's result times the factor, e.g. the damage of Health:DoDamage.
  return ctx.result * ctx.params.factor;
}""",
        (TransformParameter("factor", default=2),),
    ),
    "method_script": (
        """function (ctx) {
  // Runs the script typed into the hook's "script" field.
  if (ctx.state.code !== ctx.params.code) {
    ctx.state.code = ctx.params.code;
    ctx.state.fn = new Function('ctx', ctx.params.code);
  }
  return ctx.state.fn(ctx);
}""",
        (TransformParameter(
            "code", kind="code",
            default="""// ctx.phase ("before"/"after"), ctx.args.<name>, ctx.this, ctx.result, ctx.log(...)
// Objects: .name, .className, fields and properties directly (e.g. .IsPartyMember),
// .field("x"), .get("property"), .call("Method", ...), .component("Type")
// Static methods: ctx.call("Namespace.Class.Method", ...)
// After the call: return a value to replace the result, nothing to keep it.
// Before the call: ctx.args.x = …, obj.Field = … change what the method works with.
const attacker = ctx.args.attacker;
if (attacker && !/^(Player|Companion)_/.test(attacker.name))
  return ctx.result * 2;
"""),),
    ),
}


# Texts of the example templates. They are English whatever the language of
# the interface: templates are data, saved once and shared by exports.
EXAMPLE_TEXTS = {
    "multiply": "Multiplier",
    "multiply.factor": "Factor",
    "multiply.direction": "Apply to",
    "freeze": "Fixed value",
    "freeze.value": "Value",
    "clamp": "Clamp",
    "clamp.minimum": "Minimum",
    "clamp.maximum": "Maximum",
    "choice.increase": "Increases only",
    "choice.decrease": "Decreases only",
    "choice.both": "All changes",
    "method_multiply": "Result × factor",
    "method_multiply.factor": "Factor",
    "method_multiply.description":
        "Multiplies the method's result, e.g. the damage of Health:DoDamage.",
    "method_script": "Method script",
    "method_script.code": "Script",
    "method_script.description":
        "Your own JavaScript with ctx.args, ctx.this, ctx.result and ctx.call(…). After the "
        "call a returned value replaces the result, before the call assignments change "
        "arguments and fields (choose When above).",
}

# German texts earlier versions saved the examples with (keys as in
# EXAMPLE_TEXTS, ``<seed>.<parameter>_default`` for code). Only these are
# translated in saved templates and hooks; texts the user changed stay.
GERMAN_EXAMPLE_TEXTS = {
    "multiply": ("Multiplikator",),
    "multiply.factor": ("Faktor",),
    "multiply.direction": ("Anwenden auf",),
    "freeze": ("Fester Wert",),
    "freeze.value": ("Wert",),
    "clamp": ("Begrenzen",),
    "choice.increase": ("Nur Erhöhungen",),
    "choice.decrease": ("Nur Verringerungen",),
    "choice.both": ("Alle Änderungen",),
    "method_multiply": ("Ergebnis × Faktor",),
    "method_multiply.factor": ("Faktor",),
    "method_multiply.description": (
        "Multipliziert das Ergebnis der Methode, z. B. den Schaden von Health:DoDamage.",),
    "method_script": ("Methoden-Skript",),
    "method_script.code": ("Skript",),
    "method_script.description": (
        "Eigenes JavaScript mit ctx.args, ctx.this, ctx.result und ctx.call(…). Nach dem "
        "Aufruf ersetzt ein zurückgegebener Wert das Ergebnis, vor dem Aufruf ändern "
        "Zuweisungen Argumente und Felder (Zeitpunkt oben wählen).",
        "Eigenes JavaScript mit ctx.args, ctx.this, ctx.result und ctx.call(…); Objekte "
        "haben .name, .field(), .get(), .call() und .component(). Ein zurückgegebener Wert "
        "ersetzt das Ergebnis.",
    ),
    "method_script.code_default": (
        """// ctx.phase ("before"/"after"), ctx.args.<Name>, ctx.this, ctx.result, ctx.log(...)
// Objekte: .name, .className, Felder und Eigenschaften direkt (z. B. .IsPartyMember),
// .field("x"), .get("Eigenschaft"), .call("Methode", ...), .component("Typ")
// Statische Methoden: ctx.call("Namespace.Klasse.Methode", ...)
// Nach dem Aufruf: return ersetzt das Ergebnis, ohne return bleibt es.
// Vor dem Aufruf: ctx.args.x = …, obj.Feld = … ändern, womit die Methode arbeitet.
const attacker = ctx.args.attacker;
if (attacker && !/^(Player|Companion)_/.test(attacker.name))
  return ctx.result * 2;
""",
        """// ctx.args.<Name>, ctx.this, ctx.result, ctx.params, ctx.log(...)
// Objekte: .name, .className, .field("x"), .get("Eigenschaft"),
// .call("Methode", ...), .component("Typ")
// Statische Methoden: ctx.call("Namespace.Klasse.Methode", ...)
// Ein zurückgegebener Wert ersetzt das Ergebnis, ohne return bleibt es.
const attacker = ctx.args.attacker;
if (attacker && !/^(Player|Companion)_/.test(attacker.name))
  return ctx.result * 2;
""",
    ),
}


def example_templates():
    """Return the example template definitions ``{seed_key: definition}``."""
    def text(key, fallback):
        return EXAMPLE_TEXTS.get(key, fallback)

    examples = {}
    for kind in EXAMPLE_TEMPLATE_KINDS:
        cls = _TRANSFORMS[kind]
        parameters = tuple(
            TransformParameter(
                name=parameter.name,
                kind=parameter.kind,
                default=parameter.default,
                choices=parameter.choices,
                optional=parameter.optional,
                label=text(f"{kind}.{parameter.name}", parameter.name),
                choice_labels=tuple(
                    text(f"choice.{choice}", str(choice)) for choice in parameter.choices
                ),
            )
            for parameter in cls.parameters
        )
        examples[kind] = TransformDefinition(
            name=text(kind, cls.title or kind),
            source=cls.source.strip(),
            parameters=parameters,
            description=text(f"{kind}.description", ""),
        )
    for seed, (source, parameters) in METHOD_EXAMPLES.items():
        examples[seed] = TransformDefinition(
            name=text(seed, seed),
            source=source,
            parameters=tuple(
                TransformParameter(
                    name=parameter.name, kind=parameter.kind, default=parameter.default,
                    label=text(f"{seed}.{parameter.name}", parameter.name),
                )
                for parameter in parameters
            ),
            description=text(f"{seed}.description", ""),
            kind="method",
        )
    return examples


def example_seed(name):
    """The seed key of the example a German ``name`` was saved for, or None."""
    for seed in (*EXAMPLE_TEMPLATE_KINDS, *METHOD_EXAMPLES):
        if name in GERMAN_EXAMPLE_TEXTS.get(seed, ()):
            return seed
    return None


def english_example(seed, name, description, parameters):
    """``name``, ``description`` and ``parameters`` (JSON) of a saved example in English.

    Only the German texts of earlier versions are replaced: a name, label or
    script the user changed stays as it is.
    """
    english = example_templates()[seed]

    def swap(key, value, new):
        return new if value in GERMAN_EXAMPLE_TEXTS.get(key, ()) else value

    name = swap(seed, name, english.name)
    if not description or description in GERMAN_EXAMPLE_TEXTS.get(f"{seed}.description", ()):
        description = english.description or description
    by_name = {parameter.name: parameter for parameter in english.parameters}
    translated = []
    for parameter in parameters or []:
        parameter = dict(parameter)
        new = by_name.get(parameter.get("name"))
        if new is not None:
            key = f"{seed}.{new.name}"
            parameter["label"] = swap(key, parameter.get("label"), new.label)
            if new.kind == "code":
                parameter["default"] = swap(f"{key}_default", parameter.get("default"),
                                            new.default)
            parameter["choices"] = [
                [choice, swap(f"choice.{choice}", label, new.choice_label(new.choices.index(choice))
                              if choice in new.choices else label)]
                for choice, label in parameter.get("choices") or []
            ]
        translated.append(parameter)
    return name, description, translated
