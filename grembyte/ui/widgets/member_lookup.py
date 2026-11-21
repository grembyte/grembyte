"""The members of the game's classes for the completion of code fields.

Asks the agent of the attached game (a ScriptWorker or MethodHookWorker,
``class_members``) once per class for its fields, properties and methods,
inherited ones too, and keeps the answers while the process stays attached.
The completion (grembyte.ui.widgets.code_completion) calls the lookup with a
type name: it gets the members, ``None`` (no such class, or no game), or
``PENDING`` while the agent is asked; ``changed`` then tells it to look again.
"""

import time

from PySide6.QtCore import QObject, Signal  # pylint: disable=import-error,no-name-in-module

from grembyte.ui.widgets.code_completion import MISSING, PENDING, ClassMembers


# How long tooltips trust what was read: a class's live objects, a static value
CLASS_INFO_SECONDS = 5
STATIC_VALUE_SECONDS = 2


class MemberLookup(QObject):
    """The members of classes by name, read from the game on demand.

    ``worker`` is a callable returning the worker to ask (started if need
    be), or None without an attached game.
    """

    changed = Signal()

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self._worker_of = worker
        self._worker = None
        # type name -> ClassMembers, None (no such class) or PENDING
        self._known = {}
        # request id -> type name
        self._requests = {}
        # Lists read once ("classes": the class names ctx.find finds,
        # "statics": [name, type] of the fields ctx.static reads, "methods":
        # [name, parameters, return type] of the methods ctx.call calls):
        # kind -> list or PENDING, and request id -> kind
        self._lists = {}
        self._list_requests = {}
        # What tooltips show, read again after a while (values and objects
        # change): key -> (time read, answer or PENDING), request id -> key
        self._timed = {}
        self._timed_requests = {}

    def clear(self):
        """Forget what was read: another process, or the game was updated."""
        self._known.clear()
        self._requests.clear()
        self._lists.clear()
        self._list_requests.clear()
        self._timed.clear()
        self._timed_requests.clear()
        self._disconnect()

    def class_info(self, class_name):
        """``{name, parents, more, objects}`` of the class ctx.find looks up
        by ``class_name`` (see ScriptWorker.class_info), MISSING for no such
        class, None without a game, or PENDING. Kept for a few seconds."""
        return self._timed_answer(("class", class_name), CLASS_INFO_SECONDS,
                                  lambda worker: worker.class_info(class_name))

    def static_value(self, qualified_name):
        """The static field ``Namespace.Class.Field`` (or ``Class:Field``) as
        ScriptWorker.static_fields gives it (``type``, ``value`` …), MISSING
        if the class declares no such field, None without a game, or PENDING.
        Read again after a moment: it is the value now."""
        owner, _dot, name = qualified_name.replace(":", ".").rpartition(".")
        if not owner or not name:
            return None
        fields = self._timed_answer(("statics", owner), STATIC_VALUE_SECONDS,
                                    lambda worker: worker.static_fields(owner))
        if fields is PENDING or fields is None or fields is MISSING:
            return fields
        return next((field for field in fields if field["name"].lower() == name.lower()),
                    MISSING)

    def _timed_answer(self, key, seconds, ask):
        known = self._timed.get(key)
        if known is not None and (known[1] is PENDING or time.monotonic() - known[0] < seconds):
            return known[1]
        worker = self._connected_worker()
        if worker is None:
            return None
        self._timed[key] = (time.monotonic(), PENDING)
        self._timed_requests[ask(worker)] = key
        return PENDING

    def class_names(self):
        """The names of the classes ctx.find and ctx.forEach can find (a
        list), PENDING while the game is asked, or None without one."""
        return self._list("classes")

    def static_fields(self):
        """``[name, type]`` of the static fields ctx.static can read
        (``Namespace.Class.Field``), PENDING or None (see class_names)."""
        return self._list("statics")

    def static_methods(self):
        """``[name, parameters, return type]`` of the static methods ctx.call
        can call (``Namespace.Class:Method``), PENDING or None (see
        class_names)."""
        return self._list("methods")

    def _list(self, kind):
        if kind in self._lists:
            return self._lists[kind]
        worker = self._connected_worker()
        if worker is None:
            return None
        self._lists[kind] = PENDING
        ask = getattr(worker, {"classes": "class_names", "statics": "static_field_names",
                               "methods": "static_method_names"}[kind])
        self._list_requests[ask()] = kind
        return PENDING

    def __call__(self, type_name):
        if type_name in self._known:
            return self._known[type_name]
        worker = self._connected_worker()
        if worker is None:
            return None
        self._known[type_name] = PENDING
        self._requests[worker.class_members(type_name)] = type_name
        return PENDING

    def _connected_worker(self):
        worker = self._worker_of()
        if worker is not self._worker:
            self._disconnect()
            # Requests to a worker that ended are not answered: ask anew
            self._known = {name: members for name, members in self._known.items()
                           if members is not PENDING}
            self._requests.clear()
            self._lists = {kind: found for kind, found in self._lists.items()
                           if found is not PENDING}
            self._list_requests.clear()
            self._timed.clear()
            self._timed_requests.clear()
            if worker is not None:
                worker.members_read.connect(self._on_read)
                worker.members_failed.connect(self._on_failed)
                worker.names_read.connect(self._on_names)
                worker.names_failed.connect(self._on_names_failed)
                for answered, failed in ((worker.info_read, worker.info_failed),
                                         (worker.statics_read, worker.statics_failed)):
                    answered.connect(self._on_timed)
                    failed.connect(self._on_timed_failed)
            self._worker = worker
        return worker

    def _disconnect(self):
        if self._worker is not None:
            try:
                self._worker.members_read.disconnect(self._on_read)
                self._worker.members_failed.disconnect(self._on_failed)
                self._worker.names_read.disconnect(self._on_names)
                self._worker.names_failed.disconnect(self._on_names_failed)
                for signal in (self._worker.info_read, self._worker.statics_read):
                    signal.disconnect(self._on_timed)
                for signal in (self._worker.info_failed, self._worker.statics_failed):
                    signal.disconnect(self._on_timed_failed)
            except (RuntimeError, TypeError):
                pass  # the worker is gone already
            self._worker = None

    def _on_read(self, request_id, members):
        name = self._requests.pop(request_id, None)
        if name is not None:
            self._known[name] = None if members is None else ClassMembers.from_agent(members)
            self.changed.emit()

    def _on_names(self, request_id, names):
        kind = self._list_requests.pop(request_id, None)
        if kind is not None:
            self._lists[kind] = list(names or ())
            self.changed.emit()

    def _on_names_failed(self, request_id, _message):
        kind = self._list_requests.pop(request_id, None)
        if kind is not None:
            self._lists[kind] = []
            self.changed.emit()

    def _on_timed(self, request_id, answer):
        key = self._timed_requests.pop(request_id, None)
        if key is not None:
            # The agent answers None for a class it does not have
            self._timed[key] = (time.monotonic(), MISSING if answer is None else answer)
            self.changed.emit()

    def _on_timed_failed(self, request_id, _message):
        key = self._timed_requests.pop(request_id, None)
        if key is not None:
            # static_fields fails for a class the game does not have
            self._timed[key] = (time.monotonic(), MISSING if key[0] == "statics" else None)
            self.changed.emit()

    def _on_failed(self, request_id, _message):
        name = self._requests.pop(request_id, None)
        if name is not None:
            self._known[name] = None
            self.changed.emit()
