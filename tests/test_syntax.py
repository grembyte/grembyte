"""Syntax check of hook scripts in Frida's engine (see grembyte.hooks.syntax)."""

import pytest

from grembyte.hooks.syntax import (
    BODY,
    FUNCTION,
    SyntaxChecker,
    SyntaxIssue,
    check_body,
    check_function,
    checker,
    looks_like_function,
)

pytestmark = pytest.mark.skipif(not checker().available,
                                reason="needs Frida's system session")


def test_errors_name_the_line_of_the_checked_code():
    source = "function (ctx) {\n  const a = 1;\n  return ctx.result * ;\n}"

    assert check_function(source) == SyntaxIssue("unexpected token in expression: ';'", 3)
    assert check_body("const x = 1;\nif (x > ) return 2;").line == 2
    # A missing end is reported on the last line, not on what the check appends.
    assert check_function("function (ctx) {\n  return 1;\n").line == 3


def test_modern_javascript_passes():
    # Qt's engine rejects all of these; Frida's (in the game) runs them.
    for source in ("x => ({...x})", "x => x?.a ?? 1", "async x => await x", "x => 1n",
                   "function (ctx) { class A { b = 1 } return new A().b; }"):
        assert check_function(source) is None, source
    assert check_body("const { a, ...rest } = ctx.args; return rest;") is None


def test_the_code_is_compiled_not_run():
    assert check_body("throw new Error('ran');") is None
    assert check_body("while (true) {}") is None


def test_a_checker_without_frida_checks_nothing(monkeypatch):
    def refuse(_pid):
        raise OSError("no frida")

    monkeypatch.setattr("grembyte.hooks.syntax.frida.attach", refuse)
    without = SyntaxChecker()

    assert without.check(BODY, "if (") is None
    assert not without.available
    assert without.check(FUNCTION, "(") is None


def test_function_hint():
    assert looks_like_function("function (ctx) { return 1; }")
    assert looks_like_function("// doubles\nctx => ctx.new * 2")
    assert looks_like_function("/* a */ (ctx) => 1")
    assert looks_like_function("async function f(ctx) {}")
    assert not looks_like_function("ctx.result * 2")
