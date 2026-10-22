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
"""

import threading
import time

import frida

from hermes.core import activity, wine_gadget
from hermes.platform import is_wine_process


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
                        via="gadget" if wine else "frida.attach", error=str(error))
        raise
    activity.record("attached", pid=pid, session=session_number, kind=kind,
                    via="gadget" if wine else "frida.attach",
                    seconds=round(time.monotonic() - began, 3))
    return RecordedSession(session, pid, session_number, kind)


FRIDA_THREAD_NAME = "frida-events"


def _name_frida_thread():
    # Frida calls back from a thread of its own, which Python calls "Dummy-<n>".
    thread = threading.current_thread()
    if thread.name.startswith("Dummy-"):
        thread.name = FRIDA_THREAD_NAME


class RecordedSession:
    """A Frida session that notes its scripts and its end in the activity log."""

    def __init__(self, session, pid, number, kind):
        self._session = session
        self.pid = pid
        self.number = number
        self.kind = kind
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
        script = self._session.create_script(source, *args, **kwargs)
        return RecordedScript(script, self, len(source))

    def detach(self):
        activity.record("detach", pid=self.pid, session=self.number, kind=self.kind)
        return self._session.detach()


class RecordedScript:
    """A Frida script that notes its loading, unloading, RPCs and errors."""

    def __init__(self, script, session, size):
        self._script = script
        self._session = session
        self._size = size
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
        except Exception as error:
            activity.record("agent_load_failed", **self._fields(), error=str(error))
            raise
        self._loaded = time.monotonic()
        activity.record("agent_loaded", **self._fields(), size=self._size,
                        seconds=round(self._loaded - began, 3))
        return result

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
                activity.rpc_ended(handle, str(error).split("\n", 1)[0])
                raise
            activity.rpc_ended(handle)
            return result
        call.__name__ = name
        return call
