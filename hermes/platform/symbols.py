"""Local PE symbols through Windows DbgHelp, without attaching to a process."""

import ctypes
import os
import sys
import threading
from ctypes import wintypes


_LOCK = threading.Lock()
_SYMOPT_UNDNAME = 0x00000002
_SYMOPT_DEFERRED_LOADS = 0x00000004
_SYMOPT_FAIL_CRITICAL_ERRORS = 0x00000200
_SYMOPT_EXACT_SYMBOLS = 0x00000400
_SYMOPT_IGNORE_NT_SYMPATH = 0x00001000
_SYMOPT_NO_PROMPTS = 0x00080000


class _SymbolInfo(ctypes.Structure):
    _fields_ = [
        ("SizeOfStruct", ctypes.c_uint32),
        ("TypeIndex", ctypes.c_uint32),
        ("Reserved", ctypes.c_uint64 * 2),
        ("Index", ctypes.c_uint32),
        ("Size", ctypes.c_uint32),
        ("ModBase", ctypes.c_uint64),
        ("Flags", ctypes.c_uint32),
        ("Value", ctypes.c_uint64),
        ("Address", ctypes.c_uint64),
        ("Register", ctypes.c_uint32),
        ("Scope", ctypes.c_uint32),
        ("Tag", ctypes.c_uint32),
        ("NameLen", ctypes.c_uint32),
        ("MaxNameLen", ctypes.c_uint32),
        ("Name", ctypes.c_wchar * 1),
    ]


def load_pe_symbols(path, base, size, symbol_path=None):
    """Return address -> name from matching local symbols; optional on Windows.

    A private DbgHelp session loads the file at its preferred base, not into
    the game. Only the module's directory and an optional local symbol folder
    are searched, never symbol servers or the environment's symbol path.
    DbgHelp checks PDB identity and age.
    """
    if sys.platform != "win32":
        return {}
    try:
        with _LOCK:
            path = os.path.abspath(path)
            folders = [os.path.dirname(path)]
            if symbol_path is not None and os.path.isdir(symbol_path):
                folders.insert(0, os.path.abspath(symbol_path))
            return _load_symbols(path, base, size, ";".join(folders))
    except (OSError, AttributeError):
        return {}


def _load_symbols(path, base, size, search_path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, ctypes.POINTER(_SymbolInfo), wintypes.ULONG, ctypes.c_void_p)

    def function(library, name, result, *arguments):
        found = getattr(library, name)
        found.restype = result
        found.argtypes = arguments
        return found

    create_event = function(kernel, "CreateEventW", wintypes.HANDLE,
                            ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
    close_handle = function(kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    get_options = function(dbghelp, "SymGetOptions", wintypes.DWORD)
    set_options = function(dbghelp, "SymSetOptions", wintypes.DWORD, wintypes.DWORD)
    initialize = function(dbghelp, "SymInitializeW", wintypes.BOOL,
                          wintypes.HANDLE, wintypes.LPCWSTR, wintypes.BOOL)
    cleanup = function(dbghelp, "SymCleanup", wintypes.BOOL, wintypes.HANDLE)
    load = function(dbghelp, "SymLoadModuleExW", ctypes.c_uint64,
                    wintypes.HANDLE, wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
                    ctypes.c_uint64, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
    enumerate_symbols = function(dbghelp, "SymEnumSymbolsW", wintypes.BOOL,
                                 wintypes.HANDLE, ctypes.c_uint64, wintypes.LPCWSTR,
                                 callback_type, ctypes.c_void_p)
    handle = create_event(None, False, False, None)
    if not handle:
        return {}
    previous = get_options()
    initialized = False
    try:
        set_options(_SYMOPT_UNDNAME | _SYMOPT_DEFERRED_LOADS | _SYMOPT_EXACT_SYMBOLS
                    | _SYMOPT_FAIL_CRITICAL_ERRORS | _SYMOPT_IGNORE_NT_SYMPATH
                    | _SYMOPT_NO_PROMPTS)
        initialized = initialize(handle, search_path, False)
        if not initialized:
            return {}
        loaded = load(handle, None, path, None, base, size, None, 0)
        if not loaded:
            return {}
        names = {}

        @callback_type
        def collect(pointer, _size, _context):
            info = pointer.contents
            if (info.Tag in (5, 7, 10) and base <= info.Address < base + size
                    and 0 < info.NameLen <= 65536):
                name = ctypes.wstring_at(
                    ctypes.addressof(info) + _SymbolInfo.Name.offset, info.NameLen)
                names.setdefault(info.Address, name)
            return True

        if not enumerate_symbols(handle, loaded, None, collect, None):
            return {}
        return names
    finally:
        if initialized:
            cleanup(handle)
        set_options(previous)
        close_handle(handle)