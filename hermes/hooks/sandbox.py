"""Run a transform function outside the target process, e.g. to test a template.

The function gets the same ``ctx`` as inside the hook agent and its result is
normalized the same way. It runs in Qt's JavaScript engine instead of Frida's,
so Frida APIs such as ``ptr()`` or ``Memory`` are not available. ``ctx.log()``
and ``console.log()`` output is collected. A QCoreApplication must exist.
"""

import math
from dataclasses import dataclass, field

from PyQt6.QtQml import QJSEngine  # pylint: disable=import-error,no-name-in-module

from hermes.core.value_types import VALUE_TYPES


# Same ranges as VALUE_IO in the hook agent: 64-bit integers are passed as
# JavaScript numbers and limited to the exactly representable range.
VALUE_LIMITS = {
    name: None if kind.limits is None else (
        max(kind.limits[0], -(2**53 - 1)), min(kind.limits[1], 2**53 - 1)
    )
    for name, kind in VALUE_TYPES.items()
}

_RUNNER = r"""
(function (fn, data, state) {
  // QJSEngine has no globalThis.
  const global = Function('return this')();
  const logs = [];
  const format = args => args.map(arg => {
    if (typeof arg === 'string')
      return arg;
    try {
      return JSON.stringify(arg);
    } catch (_error) {
      return String(arg);
    }
  }).join(' ');
  const log = (...args) => { logs.push(format(args)); };
  const previousConsole = global.console;
  global.console = { log, info: log, warn: log, error: log, debug: log };
  // Game code is out of reach here (method hooks).
  const call = () => { throw new Error('ctx.call() only works in the game'); };
  const ctx = Object.assign({ call }, data, { state, log });
  let result;
  let error = null;
  try {
    result = fn(ctx);
  } catch (e) {
    error = (e && e.stack) ? `${e}\n${e.stack}` : String(e);
  } finally {
    global.console = previousConsole;
  }
  let stateText;
  try {
    stateText = JSON.stringify(state);
  } catch (_error) {
    stateText = String(state);
  }
  return {
    error,
    logs,
    state: stateText,
    kept: error === null && (result === undefined || result === null),
    type: typeof result,
    value: (typeof result === 'number' || typeof result === 'boolean') ? result : null,
    text: result === undefined ? 'undefined' : String(result)
  };
})
"""


@dataclass
class SandboxResult:
    """Outcome of one test run.

    ``value`` is the value the hook would store; with ``kept`` the game's value
    stays. ``error`` is set when the function threw or returned no number.
    """

    value: float | int | None = None
    kept: bool = False
    error: str | None = None
    logs: list = field(default_factory=list)
    state: str = "{}"


def normalize_value(value, value_type):
    """Mirror the agent: finite numbers only; integers truncated and clamped."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise ValueError(f"Transform returned {value}, expected a finite number")
    limits = VALUE_LIMITS[value_type]
    if limits is None:
        return float(value)
    return min(limits[1], max(limits[0], math.trunc(value)))


class TransformSandbox:
    """Call transform functions with a persistent ``ctx.state``."""

    def __init__(self):
        self._engine = QJSEngine()
        self._runner = self._engine.evaluate(_RUNNER)
        self._state = self._engine.newObject()

    def reset_state(self):
        """Start again with an empty ``ctx.state``."""
        self._state = self._engine.newObject()

    def run_method(self, source, params, result):
        """Call a method hook's ``source`` like the agent would for ``result``.

        There are no arguments, objects or game calls here: ``ctx.args`` and
        ``ctx.refs`` are empty, ``ctx.this`` is null and ``ctx.call`` throws.
        """
        fn = self._engine.evaluate(f"({source}\n)")
        if fn.isError():
            return SandboxResult(error=fn.toString())
        if not fn.isCallable():
            return SandboxResult(error="The source does not evaluate to a function")
        data = {"phase": "after", "result": result, "args": {}, "refs": {}, "this": None,
                "params": dict(params), "method": "Test", "hookId": 0, "threadId": 0}
        outcome = self._runner.call([fn, self._engine.toScriptValue(data), self._state])
        if outcome.isError():
            return SandboxResult(error=outcome.toString())
        outcome = outcome.toVariant()
        sandbox_result = SandboxResult(
            logs=list(outcome.get("logs") or []),
            state=outcome.get("state") or "{}",
            kept=bool(outcome.get("kept")),
            error=outcome.get("error"),
        )
        if sandbox_result.error is None and not sandbox_result.kept:
            value = outcome.get("value")
            sandbox_result.value = outcome.get("text") if value is None else value
        return sandbox_result

    def run(self, source, params, old, new, value_type="int32"):
        # pylint: disable=too-many-arguments
        """Call ``source`` like the hook agent would after a write ``old`` -> ``new``."""
        fn = self._engine.evaluate(f"({source}\n)")
        if fn.isError():
            return SandboxResult(error=fn.toString())
        if not fn.isCallable():
            return SandboxResult(error="The source does not evaluate to a function")
        data = {
            "old": old,
            "new": new,
            "delta": new - old,
            "params": dict(params),
            "valueType": value_type,
            "address": "0x0",
            "writePc": "0x0",
            "threadId": 0,
            "hookId": 0,
        }
        outcome = self._runner.call([fn, self._engine.toScriptValue(data), self._state])
        if outcome.isError():
            return SandboxResult(error=outcome.toString())
        outcome = outcome.toVariant()
        result = SandboxResult(
            logs=list(outcome.get("logs") or []),
            state=outcome.get("state") or "{}",
            kept=bool(outcome.get("kept")),
            error=outcome.get("error"),
        )
        if result.error is None and not result.kept:
            value = outcome.get("value") if outcome.get("type") == "number" else outcome.get("text")
            try:
                result.value = normalize_value(value, value_type)
            except ValueError as error:
                result.error = str(error)
        return result
