"""The keyboard state of the system, for hotkeys (see grembyte.core.hotkeys).

``open_reader()`` gives an object with

``pressed(names)``
    The names (modifiers ``ctrl``, ``shift``, ``alt``, ``meta`` and keys as
    grembyte.core.hotkeys.KEYS names them) of those held down now.
``focused_pid()``
    The pid of the process whose window has the keyboard focus; 0 if no
    window has it, None if it cannot be told.
``close()``

or None where the keyboard cannot be read. Both read the state without
taking keys away from the game.

Linux (X11)
    Through Xlib: XQueryKeymap, and the ``_NET_WM_PID`` of the window with
    the input focus (or of the first of its parents that has one). Games
    under Wayland run through XWayland, which knows the keys only while one
    of its windows has the focus: for a game that is exactly when they
    count. Games that are Wayland clients themselves are not seen.
Windows
    GetAsyncKeyState and the process of the foreground window.
"""

import ctypes
import ctypes.util
import string
import sys

# The keysym names (X11) of the keys a hotkey can have
_X11_KEYSYMS = {
    **{letter: [letter.lower()] for letter in string.ascii_uppercase},
    **{digit: [digit] for digit in string.digits},
    **{f"F{number}": [f"F{number}"] for number in range(1, 13)},
    "Ins": ["Insert"], "Del": ["Delete"], "Home": ["Home"], "End": ["End"],
    "PgUp": ["Prior"], "PgDown": ["Next"], "Pause": ["Pause"], "Space": ["space"],
    "ctrl": ["Control_L", "Control_R"], "shift": ["Shift_L", "Shift_R"],
    "alt": ["Alt_L", "Alt_R", "Meta_L", "Meta_R"], "meta": ["Super_L", "Super_R"],
}
# The virtual key codes (Windows) of the same keys
_WINDOWS_KEYS = {
    **{letter: [ord(letter)] for letter in string.ascii_uppercase},
    **{digit: [ord(digit)] for digit in string.digits},
    **{f"F{number}": [0x6F + number] for number in range(1, 13)},
    "Ins": [0x2D], "Del": [0x2E], "Home": [0x24], "End": [0x23], "PgUp": [0x21],
    "PgDown": [0x22], "Pause": [0x13], "Space": [0x20],
    "ctrl": [0x11], "shift": [0x10], "alt": [0x12], "meta": [0x5B, 0x5C],
}
# How many parents of the focused window are asked for their process
_MAX_PARENTS = 32
_POINTER_ROOT = 1


class X11Keyboard:
    """The keyboard of an X display (``DISPLAY`` if ``display`` is None)."""

    # pylint: disable=too-many-instance-attributes

    _ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def __init__(self, display=None):
        name = ctypes.util.find_library("X11")
        if name is None:
            raise OSError("libX11 not found")
        xlib = ctypes.CDLL(name)
        self._declare(xlib)
        self._xlib = xlib
        self._display = xlib.XOpenDisplay(display.encode() if display else None)
        if not self._display:
            raise OSError(f"cannot open the X display {display or '$DISPLAY'}")
        # An X error (a window that went away meanwhile) must not end Grembyte
        self._ignore_errors = self._ERROR_HANDLER(lambda _display, _event: 0)
        self._pid_atom = xlib.XInternAtom(self._display, b"_NET_WM_PID", True)
        self._keycodes = {}
        for key, keysyms in _X11_KEYSYMS.items():
            codes = []
            for keysym_name in keysyms:
                keysym = xlib.XStringToKeysym(keysym_name.encode())
                code = xlib.XKeysymToKeycode(self._display, keysym) if keysym else 0
                if code:
                    codes.append(code)
            self._keycodes[key] = codes

    @staticmethod
    def _declare(xlib):
        display, window, ulong = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong
        xlib.XOpenDisplay.restype = display
        xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        xlib.XCloseDisplay.argtypes = [display]
        xlib.XStringToKeysym.restype = ulong
        xlib.XStringToKeysym.argtypes = [ctypes.c_char_p]
        xlib.XKeysymToKeycode.restype = ctypes.c_ubyte
        xlib.XKeysymToKeycode.argtypes = [display, ulong]
        xlib.XQueryKeymap.argtypes = [display, ctypes.c_char * 32]
        xlib.XGetInputFocus.argtypes = [display, ctypes.POINTER(window),
                                        ctypes.POINTER(ctypes.c_int)]
        xlib.XInternAtom.restype = ulong
        xlib.XInternAtom.argtypes = [display, ctypes.c_char_p, ctypes.c_int]
        xlib.XGetWindowProperty.argtypes = [
            display, window, ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int, ulong,
            ctypes.POINTER(ulong), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ulong),
            ctypes.POINTER(ulong), ctypes.POINTER(ctypes.c_void_p)]
        xlib.XQueryTree.argtypes = [
            display, window, ctypes.POINTER(window), ctypes.POINTER(window),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint)]
        xlib.XFree.argtypes = [ctypes.c_void_p]
        xlib.XSetErrorHandler.restype = ctypes.c_void_p
        xlib.XSetErrorHandler.argtypes = [ctypes.c_void_p]

    def pressed(self, names):
        keymap = (ctypes.c_char * 32)()
        self._xlib.XQueryKeymap(self._display, keymap)
        state = bytes(keymap)
        return {name for name in names
                if any(state[code // 8] & (1 << (code % 8))
                       for code in self._keycodes.get(name, ()))}

    def focused_pid(self):
        previous = self._xlib.XSetErrorHandler(
            ctypes.cast(self._ignore_errors, ctypes.c_void_p))
        try:
            return self._focused_pid()
        finally:
            self._xlib.XSetErrorHandler(previous)

    def _focused_pid(self):
        window = ctypes.c_ulong()
        revert = ctypes.c_int()
        self._xlib.XGetInputFocus(self._display, ctypes.byref(window), ctypes.byref(revert))
        if window.value in (0, _POINTER_ROOT):
            return 0
        if not self._pid_atom:
            return None
        current = window.value
        for _level in range(_MAX_PARENTS):
            pid = self._window_pid(current)
            if pid is not None:
                return pid
            current = self._parent(current)
            if not current:
                return None
        return None

    def _window_pid(self, window):
        actual_type, actual_format = ctypes.c_ulong(), ctypes.c_int()
        items, after = ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.c_void_p()
        status = self._xlib.XGetWindowProperty(
            self._display, window, self._pid_atom, 0, 1, False, 6,  # XA_CARDINAL
            ctypes.byref(actual_type), ctypes.byref(actual_format), ctypes.byref(items),
            ctypes.byref(after), ctypes.byref(data))
        try:
            if status != 0 or not data.value or items.value < 1 or actual_format.value != 32:
                return None
            # Format 32 comes as C longs
            return ctypes.cast(data, ctypes.POINTER(ctypes.c_long))[0]
        finally:
            if data.value:
                self._xlib.XFree(data)

    def _parent(self, window):
        root, parent = ctypes.c_ulong(), ctypes.c_ulong()
        children, count = ctypes.c_void_p(), ctypes.c_uint()
        if not self._xlib.XQueryTree(self._display, window, ctypes.byref(root),
                                     ctypes.byref(parent), ctypes.byref(children),
                                     ctypes.byref(count)):
            return 0
        if children.value:
            self._xlib.XFree(children)
        return 0 if parent.value in (0, root.value) else parent.value

    def close(self):
        if self._display:
            self._xlib.XCloseDisplay(self._display)
            self._display = None


class WindowsKeyboard:
    """The keyboard through the Win32 API."""

    def __init__(self):
        self._user32 = ctypes.WinDLL("user32")  # pylint: disable=no-member
        self._user32.GetAsyncKeyState.restype = ctypes.c_short
        self._user32.GetForegroundWindow.restype = ctypes.c_void_p
        self._user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

    def pressed(self, names):
        return {name for name in names
                if any(self._user32.GetAsyncKeyState(code) & 0x8000
                       for code in _WINDOWS_KEYS.get(name, ()))}

    def focused_pid(self):
        window = self._user32.GetForegroundWindow()
        if not window:
            return 0
        pid = ctypes.c_ulong()
        self._user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        return pid.value or None

    def close(self):
        pass


def open_reader():
    """A reader of the keyboard state; raises OSError where there is none."""
    if sys.platform == "win32":
        return WindowsKeyboard()
    return X11Keyboard()
