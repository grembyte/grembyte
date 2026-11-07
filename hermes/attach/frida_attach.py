"""Open a Frida session on a target process, native or Wine/Proton.

``frida.attach`` injects Frida's Linux agent, which crashes Wine processes:
it looks up the main program through the program headers the kernel recorded
for ``wine-preloader``, where nothing is mapped (Frida 17.23/17.24, Proton
11). For those, the session goes through the Windows build of
Frida's gadget instead (wine_gadget.py), which hermes loads into the game
once and every worker then shares.

The session handed out records itself in the activity log (activity.py):
the agents loaded and unloaded, every RPC, script errors, and why the
session ended. It stands in for Frida's session and script otherwise.

The gadget under Wine closes the connection on any message from hermes
larger than 128 KiB (Frida 17.24.3, Proton Experimental; agent to hermes is
not limited). Larger agents go there through a small loader that receives
the source in parts and evaluates it.
"""

import threading
import time

import frida

from hermes.attach import wine_gadget
from hermes.core import activity, messages
from hermes.platform import is_wine_process

# What Frida raises when attaching, loading or talking to an agent fails.
FRIDA_ERRORS = (
  frida.AddressInUseError,
  frida.ExecutableNotFoundError,
  frida.ExecutableNotSupportedError,
  frida.InvalidArgumentError,
  frida.InvalidOperationError,
  frida.NotSupportedError,
  frida.OperationCancelledError,
  frida.PermissionDeniedError,
  frida.ProcessNotFoundError,
  frida.ProcessNotRespondingError,
  frida.ProtocolError,
  frida.ServerNotRunningError,
  frida.TimedOutError,
  frida.TransportError,
)

# Raised by exports_sync calls when the agent-side RPC handler throws.
CLEANUP_ERRORS = FRIDA_ERRORS + (frida.RPCException, RuntimeError)

# Agents above this size are sent to the gadget in parts.
GADGET_SOURCE_LIMIT = 100_000
# Characters per part; even escaped as \uXXXX in JSON it stays below the limit.
GADGET_SOURCE_PART = 16_000
GADGET_LOAD_TIMEOUT = 30.0
LOADER_MESSAGE = "hermes:source"
LOADED_KEY = "hermes:loaded"
# Indirect eval runs the agent as global code, as Frida would.
LOADER_SOURCE = f"""\
const parts = [];
function onPart(message) {{
  if (message.part !== undefined) {{
    parts.push(message.part);
    recv('{LOADER_MESSAGE}', onPart);
    return;
  }}
  const source = parts.join('');
  parts.length = 0;
  try {{
    (0, eval)(source + '\\n//# sourceURL=/agent.js');
  }} finally {{
    send({{ '{LOADED_KEY}': true }});
  }}
}}
recv('{LOADER_MESSAGE}', onPart);
"""


def attach_session(pid):
    """Return a Frida session on ``pid``; raises on failure.

    Under Wine the agent sees the process as Windows does: PE modules such as
    ``mono-2.0-bdwgc.dll``, ``Process.platform === 'windows'`` and the Win64
    calling convention.
    """
    wine = is_wine_process(pid)
    session_number, kind = activity.current_session()
    began = time.monotonic()
    try:
        session = wine_gadget.connect(pid) if wine else frida.attach(pid)
    except Exception as error:
        activity.record("attach_failed", pid=pid, session=session_number, kind=kind,
                        via="gadget" if wine else "frida.attach",
                        error=messages.describe(error))
        raise
    activity.record("attached", pid=pid, session=session_number, kind=kind,
                    via="gadget" if wine else "frida.attach",
                    seconds=round(time.monotonic() - began, 3))
    return RecordedSession(session, pid, session_number, kind, gadget=wine)


FRIDA_THREAD_NAME = "frida-events"


def _name_frida_thread():
    # Frida calls back from a thread of its own, which Python calls "Dummy-<n>".
    thread = threading.current_thread()
    if thread.name.startswith("Dummy-"):
        thread.name = FRIDA_THREAD_NAME


class RecordedSession:
    """A Frida session that notes its scripts and its end in the activity log."""

    def __init__(self, session, pid, number, kind, gadget=False):
        self._session = session
        self.pid = pid
        self.number = number
        self.kind = kind
        self.gadget = gadget
        session.on("detached", self._on_detached)

    def __getattr__(self, name):
        return getattr(self._session, name)

    def _on_detached(self, reason, crash=None):
        _name_frida_thread()
        fields = {}
        if crash is not None:
            fields["crash"] = {"summary": getattr(crash, "summary", None),
                               "report": (getattr(crash, "report", "") or "")[:4000]}
        activity.flush()
        activity.record("detached", pid=self.pid, session=self.number, kind=self.kind,
                        reason=str(reason), in_flight=activity.in_flight(), **fields)

    def create_script(self, source, *args, **kwargs):
        if self.gadget and len(source.encode()) > GADGET_SOURCE_LIMIT:
            script = self._session.create_script(LOADER_SOURCE, *args, **kwargs)
            return RecordedScript(script, self, len(source), deferred=source)
        script = self._session.create_script(source, *args, **kwargs)
        return RecordedScript(script, self, len(source))

    def detach(self):
        activity.record("detach", pid=self.pid, session=self.number, kind=self.kind)
        return self._session.detach()


def _is_loaded_message(message):
    payload = message.get("payload")
    return (message.get("type") == "send" and isinstance(payload, dict)
            and payload.get(LOADED_KEY) is True)


class RecordedScript:
    """A Frida script that notes its loading, unloading, RPCs and errors."""

    def __init__(self, script, session, size, deferred=None):
        self._script = script
        self._session = session
        self._size = size
        # The agent's source when the script is the gadget loader
        self._deferred = deferred
        self.agent = f"{session.kind or 'agent'}#{activity.new_agent()}"
        self._loaded = None
        self._exports = None

    def __getattr__(self, name):
        return getattr(self._script, name)

    def _fields(self):
        return {"agent": self.agent, "pid": self._session.pid, "session": self._session.number}

    def on(self, signal, callback):
        if signal == "message":
            original = callback

            def callback(message, data):  # pylint: disable=function-redefined
                _name_frida_thread()
                if _is_loaded_message(message):
                    return None
                if message.get("type") == "error":
                    activity.record("agent_error", **self._fields(),
                                    description=message.get("description"),
                                    where=f"{message.get('fileName', '')}:"
                                          f"{message.get('lineNumber', '')}")
                return original(message, data)
        return self._script.on(signal, callback)

    def load(self):
        began = time.monotonic()
        try:
            result = self._script.load()
            if self._deferred is not None:
                self._load_deferred()
        except Exception as error:
            activity.record("agent_load_failed", **self._fields(), error=messages.describe(error))
            raise
        self._loaded = time.monotonic()
        activity.record("agent_loaded", **self._fields(), size=self._size,
                        seconds=round(self._loaded - began, 3))
        return result

    def _load_deferred(self):
        # Like Frida's load: returns once the agent's global code has run;
        # its errors arrive as "error" messages.
        loaded = threading.Event()

        def on_message(message, _data):
            if _is_loaded_message(message):
                loaded.set()

        source, self._deferred = self._deferred, None
        self._script.on("message", on_message)
        try:
            for start in range(0, len(source), GADGET_SOURCE_PART):
                self._script.post({"type": LOADER_MESSAGE,
                                   "part": source[start:start + GADGET_SOURCE_PART]})
            self._script.post({"type": LOADER_MESSAGE})
            if not loaded.wait(GADGET_LOAD_TIMEOUT):
                raise frida.TimedOutError("Timed out while loading the agent through the gadget")
        finally:
            self._script.off("message", on_message)

    def unload(self):
        activity.flush(self.agent)
        began = time.monotonic()
        try:
            return self._script.unload()
        finally:
            activity.record("agent_unloaded", **self._fields(),
                            lived=None if self._loaded is None
                            else round(began - self._loaded, 3),
                            seconds=round(time.monotonic() - began, 3))

    @property
    def exports_sync(self):
        if self._exports is None:
            self._exports = RecordedExports(self._script.exports_sync, self.agent)
        return self._exports


class RecordedExports:
    """An agent's RPC exports, timed into the activity log."""

    def __init__(self, exports, agent):
        self._exports = exports
        self._agent = agent

    def __getattr__(self, name):
        function = getattr(self._exports, name)
        agent = self._agent

        def call(*args, **kwargs):
            handle = activity.rpc_started(agent, name)
            try:
                result = function(*args, **kwargs)
            except Exception as error:
                activity.rpc_ended(handle, messages.describe(error).split("\n", 1)[0])
                raise
            activity.rpc_ended(handle)
            return result
        call.__name__ = name
        return call
