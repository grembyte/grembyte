"""The object inspectors and text viewers a window opens through its MethodMonitorWorker."""

import json

import shiboken6  # pylint: disable=import-error,no-name-in-module
from PySide6.QtCore import QObject, QPoint, Signal  # pylint: disable=import-error,no-name-in-module

from grembyte.ui.widgets.docking import RIGHT, find_dock_manager
from grembyte.ui.widgets.object_inspector import ObjectInspectorDialog
from grembyte.ui.widgets.reference_graph import find_reference_opener
from grembyte.ui.widgets.text_viewer import TextViewerDialog

# The kind of dialog docked (see DockManager)
INSPECTOR = "inspector"


class Inspections(QObject):
    """Open object inspectors and string viewers for ``window``.

    Their requests go to ``worker`` (a MethodMonitorWorker or ScriptWorker:
    ``inspect``, ``evaluate``, ``list_methods``, ``full_text``), the answers
    back to the dialog that asked. An inspector calls an object's methods
    through a MethodCallDialog on the same worker. ``time()`` names when
    shown values were read, for the viewers' titles.
    """

    # a field of an inspected object to put on the address list (see ObjectInspectorDialog)
    address_requested = Signal(object)

    def __init__(self, translations, worker, window, time=lambda: ""):
        super().__init__(window)
        self.translations = translations
        self._worker = worker
        self._window = window
        self._time = time
        # open object inspectors; inspection request id -> waiting inspector
        self.inspectors = []
        self._inspections = {}
        # request id -> TextViewerDialog waiting for a long string's whole text
        self._text_requests = {}
        worker.inspected.connect(self._on_inspected)
        worker.inspect_failed.connect(self._on_inspect_failed)
        worker.evaluated.connect(self._on_evaluated)
        worker.evaluate_failed.connect(self._on_evaluate_failed)
        worker.methods_listed.connect(self._on_methods_listed)
        worker.methods_failed.connect(self._on_methods_failed)
        worker.text_fetched.connect(self._on_text_fetched)
        worker.text_failed.connect(self._on_text_failed)

    # --- Object inspector ------------------------------------------------------
    def inspect(self, target, origin=None, name="", instance_name=None):
        """Show the current fields of ``target`` in a dialog of its own.

        ``origin`` is the dialog it was opened from (one level less deep);
        ``instance_name`` the object's name if known (see ObjectInspectorDialog).
        """
        self.show_inspector(ObjectInspectorDialog(
            self.translations, self.inspect, target=target, request=self._request_inspection,
            evaluate=self._request_evaluation, name=name,
            depth=1 if origin is None else origin.depth + 1, parent=self._window,
            show_text=self.show_text, list_methods=self._request_methods,
            call_method=self.call_method, instance_name=instance_name,
            pid=self._worker.pid, show_references=find_reference_opener(self._window)),
            origin)

    def inspect_snapshot(self, target, snapshot, captured, name=""):
        """Show the fields of ``target`` as recorded at a call (``snapshot``)."""
        self.show_inspector(ObjectInspectorDialog(
            self.translations, self.inspect, target=target, contents=snapshot,
            captured=captured, evaluate=self._request_evaluation, name=name,
            parent=self._window, show_text=self.show_text,
            list_methods=self._request_methods, call_method=self.call_method,
            pid=self._worker.pid))

    def show_inspector(self, inspector, origin=None):
        """Show ``inspector``: docked in the main window as a tab if the last
        one was or ``origin`` is (see grembyte.ui.widgets.docking), else as a
        window of its own."""
        self.inspectors.append(inspector)
        inspector.address_requested.connect(self.address_requested.emit)
        inspector.destroyed.connect(self._forget_inspectors)
        floating_origin = origin is not None and origin.isWindow()
        if floating_origin:
            # Opened from another window: a little offset, so both stay visible.
            inspector.move(origin.pos() + QPoint(32, 32))
        manager = find_dock_manager(self._window)
        if manager is None:
            inspector.show()
            inspector.activateWindow()
            return
        # One opened from a docked inspector goes next to it
        docked_origin = origin is not None and not floating_origin
        manager.add(inspector, INSPECTOR, RIGHT, dock=True if docked_origin else None)

    # Slots for ``destroyed`` are bound methods, not lambdas: PySide does not
    # call a bound method of a dialog the garbage collector is taking apart,
    # while a lambda's ``self`` may already be cleared then (a fatal error in
    # the slot). The destroyed object is not the wrapper we keep, but that
    # one is marked as deleted already.
    def _forget_inspectors(self, _object=None):
        self.inspectors = [inspector for inspector in self.inspectors
                           if getattr(shiboken6, "isValid")(inspector)]
        for request_id, waiting in list(self._inspections.items()):
            if not getattr(shiboken6, "isValid")(waiting):
                del self._inspections[request_id]

    def _request_inspection(self, target, inspector):
        self._inspections[self._worker.inspect(target)] = inspector

    def _request_evaluation(self, target, names, inspector):
        self._inspections[self._worker.evaluate(target, names)] = inspector

    def _on_evaluated(self, request_id, results):
        inspector = self._inspections.pop(request_id, None)
        if inspector is not None:
            inspector.show_properties(results)

    def _on_evaluate_failed(self, request_id, message):
        inspector = self._inspections.pop(request_id, None)
        if inspector is not None:
            inspector.show_property_error(message)

    def _request_methods(self, target, inspector):
        self._inspections[self._worker.list_methods(target)] = inspector

    def _on_methods_listed(self, request_id, methods):
        inspector = self._inspections.pop(request_id, None)
        if inspector is not None:
            inspector.show_methods(methods)

    def _on_methods_failed(self, request_id, message):
        inspector = self._inspections.pop(request_id, None)
        if inspector is not None:
            inspector.show_methods_error(message)

    def call_method(self, target, entry, inspector, object_text):
        """Offer to call the instance method ``entry`` (see ``list_methods``)
        on ``target`` in the method call dialog of ``inspector``."""
        # pylint: disable=import-outside-toplevel
        # method_call opens object inspectors through this module itself.
        from grembyte.ui.widgets.method_call import MethodCallDialog
        dialog = inspector.findChild(MethodCallDialog)
        if dialog is None:
            dialog = MethodCallDialog(self.translations, parent=inspector, worker=self._worker)
            dialog.address_requested.connect(self.address_requested.emit)
            dialog.call_done.connect(inspector.on_call_done)
        dialog.set_instance_method(target, entry, object_text)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def _on_inspected(self, request_id, contents):
        inspector = self._inspections.pop(request_id, None)
        if inspector is not None:
            inspector.show_contents(contents)

    def _on_inspect_failed(self, request_id, message):
        inspector = self._inspections.pop(request_id, None)
        if inspector is not None:
            inspector.show_error(message)

    # --- Long texts ------------------------------------------------------------
    def show_text(self, value, name, time=None):
        """Open a string value whole in a window of its own; a long one comes
        from the agent. ``time``: when the value was read (else ``time()``)."""
        title = self.translations["monitor.full_text_title"].format(
            name=name, time=time or self._time())
        if value.get("fullText"):
            viewer = TextViewerDialog(self.translations, title, parent=self._window)
            self._text_requests[self._worker.full_text(value["fullText"])] = (viewer, value)
            viewer.destroyed.connect(self._forget_texts)
        else:
            # Short: the text is all there, as a JSON string literal.
            viewer = TextViewerDialog(self.translations, title, json.loads(value["text"]),
                                      parent=self._window)
        viewer.show()
        viewer.activateWindow()

    def _forget_texts(self, _object=None):
        for request_id, (waiting, _value) in list(self._text_requests.items()):
            if not getattr(shiboken6, "isValid")(waiting):
                del self._text_requests[request_id]

    def _on_text_fetched(self, request_id, text):
        viewer, value = self._text_requests.pop(request_id, (None, None))
        if viewer is None:
            return
        if text is None:
            viewer.show_missing()
        else:
            viewer.set_text(text, value.get("length"))

    def _on_text_failed(self, request_id, message):
        viewer, _value = self._text_requests.pop(request_id, (None, None))
        if viewer is not None:
            viewer.show_error(message)
