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
    ctx.valueType  "int32" | "int64" | "float" | "double"
    ctx.address    target address as hex string
    ctx.writePc    address of the hooked write instruction as hex string
    ctx.threadId   id of the writing thread
    ctx.hookId     id of the hook

Integer results are truncated and clamped to the value type's range; int64
values are passed as JavaScript numbers and are only exact up to 2**53.

New transforms subclass ``ValueTransform``, set ``kind`` and ``source`` and
register themselves with ``@register_transform``. Declaring ``parameters``
lets the UI build an input form for the transform automatically; labels are
looked up as ``transform.<kind>`` and ``transform.<kind>.<parameter>`` in the
translation catalogs and fall back to ``title`` / the parameter name.
``ScriptTransform`` accepts an arbitrary function source for one-off
experiments.
"""

import math
from dataclasses import dataclass


_TRANSFORMS = {}


@dataclass(frozen=True)
class TransformParameter:
    """Describes one constructor argument of a transform for UI forms.

    ``kind`` is ``"number"``, ``"choice"`` (one of ``choices``) or ``"code"``
    (multi-line text). ``optional`` parameters may be left empty (``None``).
    """

    name: str
    kind: str = "number"
    default: object = None
    choices: tuple = ()
    optional: bool = False


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
