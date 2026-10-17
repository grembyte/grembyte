"""Tests for running transforms outside the target process."""

import os

import pytest

from hermes.hooks.sandbox import normalize_value
from hermes.hooks.transforms import ClampTransform, MultiplyTransform


@pytest.fixture(name="sandbox", scope="module")
def fixture_sandbox():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # pylint: disable=import-error,no-name-in-module,import-outside-toplevel
    from PyQt6.QtWidgets import QApplication
    from hermes.hooks.sandbox import TransformSandbox

    app = QApplication.instance() or QApplication([])
    yield TransformSandbox()
    del app


def test_runs_builtin_transforms(sandbox):
    params = {"factor": 10, "direction": "increase"}

    assert sandbox.run(MultiplyTransform.source, params, 100, 110).value == 200
    kept = sandbox.run(MultiplyTransform.source, params, 110, 100)
    assert kept.kept and kept.value is None and kept.error is None
    clamp = sandbox.run(ClampTransform.source, {"minimum": None, "maximum": 50}, 1, 99)
    assert clamp.value == 50


def test_collects_logs_and_keeps_state(sandbox):
    sandbox.reset_state()
    source = """ctx => {
      ctx.state.calls = (ctx.state.calls || 0) + 1;
      ctx.log('call', ctx.state.calls, { delta: ctx.delta });
      console.log('console');
      return ctx.new;
    }"""

    sandbox.run(source, {}, 1, 2)
    result = sandbox.run(source, {}, 1, 2)

    assert result.logs == ['call 2 {"delta":1}', "console"]
    assert result.state == '{"calls":2}'
    sandbox.reset_state()
    assert sandbox.run(source, {}, 1, 2).state == '{"calls":1}'


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("ctx => { throw new Error('boom'); }", "boom"),
        ("ctx => 'gold'", "expected a finite number"),
        ("function (", "SyntaxError"),
        ("42", "does not evaluate to a function"),
    ],
)
def test_reports_errors(sandbox, source, message):
    result = sandbox.run(source, {}, 1, 2)

    assert result.value is None
    assert message in result.error


@pytest.mark.parametrize(
    ("value", "value_type", "expected"),
    [
        (1e20, "int32", 2147483647),
        (-2.7, "int32", -2),
        (2**60, "int64", 2**53 - 1),
        (1.5, "float", 1.5),
    ],
)
def test_normalizes_like_the_agent(sandbox, value, value_type, expected):
    assert sandbox.run(f"ctx => {value!r}", {}, 0, 0, value_type).value == expected
    assert normalize_value(value, value_type) == expected
