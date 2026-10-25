"""Syntax check of hook scripts in Frida's JavaScript engine, without a game.

Hook code runs in Frida's engine inside the game. Qt's engine (see
hermes.hooks.sandbox) knows less of modern JavaScript (object spread, class
fields, BigInt, async) and would reject code that runs fine there, so the
check uses Frida's: a script in Frida's system session, which needs no
target process, compiles the code without running it.

Two kinds of code are checked the way the hooks compile them:

``FUNCTION``
    A template's source, a function expression (``compileTransform`` in
    hermes/hooks/engine.js: ``eval('(' + source + '\\n)')``).
``BODY``
    A ``code`` parameter, run as ``new Function('ctx', code)`` (see the
    "method_script" example in hermes.hooks.transforms).

Only syntax is checked; whether the code works shows when it runs. Without
Frida's system session nothing is checked (``check`` returns None).
"""

import atexit
import re
import threading
from dataclasses import dataclass

import frida

from hermes.core import logs
from hermes.core.write_watch import CLEANUP_ERRORS, FRIDA_ERRORS

FUNCTION, BODY = "function", "body"

# Compiles only: new Function never runs the code it is given.
_AGENT_SOURCE = r"""
rpc.exports = {
  check(kind, code) {
    try {
      if (kind === 'function')
        new Function('return (' + code + '\n)');
      else
        new Function('ctx', code);
      return null;
    } catch (e) {
      // e.lineNumber is where new Function was called; the stack's first
      // frame is the place in the compiled code.
      const at = /at <input>:(\d+)/.exec(e.stack || '');
      return [e instanceof SyntaxError, e.message, at === null ? null : Number(at[1])];
    }
  }
};
"""

# The start of a function expression: function, async function, (a) =>, a =>
_FUNCTION_START = re.compile(r"(async\s+)?(function\b|\(|[A-Za-z_$][\w$]*\s*=>)")
# Comments before it
_LEADING_COMMENTS = re.compile(r"\s*(//[^\n]*\n|/\*.*?\*/)*\s*", re.DOTALL)


@dataclass(frozen=True)
class SyntaxIssue:
    """A syntax error: the engine's message and the line in the checked code, if known."""

    message: str
    line: int = None


def looks_like_function(source):
    """Whether a template's source starts like a function expression.

    Only a hint: whether it really is one shows when the hook compiles it.
    """
    return _FUNCTION_START.match(source, _LEADING_COMMENTS.match(source).end()) is not None


class SyntaxChecker:
    """Check hook code in Frida's system session; see the module documentation.

    The session starts with the first check. Safe to use from several threads.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._session = None
        self._script = None
        self._failed = False
        # Lines the engine counts before the checked code, per kind
        self._offsets = {}

    @property
    def available(self):
        """Whether checks can run (the session started, or was not tried yet)."""
        return not self._failed

    def check(self, kind, code):
        """The SyntaxIssue of ``code`` (``FUNCTION`` or ``BODY``), else None.

        None too when the check cannot run.
        """
        with self._lock:
            if not self._start():
                return None
            try:
                answer = self._script.exports_sync.check(kind, code)
            except FRIDA_ERRORS + (frida.RPCException,) as error:
                logs.log.warning("syntax check failed: %s", error)
                return None
        if answer is None:
            return None
        _syntax, message, line = answer
        if line is not None:
            # A missing end shows on the line the check appends: the last one.
            line = min(max(1, line - self._offsets.get(kind, 0)), code.count("\n") + 1)
        return SyntaxIssue(message, line)

    def _start(self):
        if self._script is not None:
            return True
        if self._failed:
            return False
        try:
            session = frida.attach(0)
            script = session.create_script(_AGENT_SOURCE)
            script.load()
        except FRIDA_ERRORS + (frida.RPCException, OSError) as error:
            logs.log.warning("syntax check unavailable: %s", error)
            self._failed = True
            return False
        self._session, self._script = session, script
        # A syntax error on the first line tells how many lines come before.
        for kind in (FUNCTION, BODY):
            answer = script.exports_sync.check(kind, "@")
            line = answer[2] if answer is not None else None
            self._offsets[kind] = line - 1 if line is not None else 0
        return True

    def close(self):
        with self._lock:
            script, session = self._script, self._session
            self._script = self._session = None
        for close in (script and script.unload, session and session.detach):
            if close:
                try:
                    close()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)


_checker = None
_checker_lock = threading.Lock()


def checker():
    """The SyntaxChecker the program shares."""
    global _checker  # pylint: disable=global-statement
    with _checker_lock:
        if _checker is None:
            _checker = SyntaxChecker()
            atexit.register(_checker.close)
        return _checker


def check_function(source):
    """The SyntaxIssue of a template's source (a function expression), else None."""
    return checker().check(FUNCTION, source)


def check_body(code):
    """The SyntaxIssue of a ``code`` parameter (a function body), else None."""
    return checker().check(BODY, code)
