"""Windows: processes through the Win32 API (ReadProcessMemory, VirtualQueryEx, …).

Modules are the PE images the process' loader lists, under the names the
loader gives them (``KERNEL32.DLL``); Frida's Windows backend uses the same
names, so ``module+offset`` from here and from an agent agree.

``proc_root`` parameters exist only to match the Linux signatures and are
ignored.
"""

import ctypes
import ntpath
import string
from contextlib import contextmanager
from ctypes import wintypes

from grembyte.core.messages import LocalizedOSError
from grembyte.platform.mapping import Mapping

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
THREAD_QUERY_LIMITED_INFORMATION = 0x0800

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x1000000

PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE = 0x10
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100
PAGE_NOCACHE = 0x200
PAGE_WRITECOMBINE = 0x400

# Base protection (the low byte) as "rwx".
_PERMISSIONS = {
    PAGE_NOACCESS: "---",
    PAGE_READONLY: "r--",
    PAGE_READWRITE: "rw-",
    PAGE_WRITECOPY: "rw-",
    PAGE_EXECUTE: "--x",
    PAGE_EXECUTE_READ: "r-x",
    PAGE_EXECUTE_READWRITE: "rwx",
    PAGE_EXECUTE_WRITECOPY: "rwx",
}
_SHARED_WRITABLE = {PAGE_READWRITE, PAGE_EXECUTE_READWRITE}
_EXECUTABLE = {PAGE_EXECUTE, PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY}

LIST_MODULES_ALL = 0x03
STILL_RUNNING = 0x102  # WAIT_TIMEOUT
IMAGE_FILE_MACHINE_UNKNOWN = 0
_32BIT_MACHINES = {0x014C, 0x01C4}  # i386, ARMNT

_ADDRESS_LIMIT = 1 << 64


class _MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _function(name, restype, *argtypes):
    function = getattr(_kernel32, name)
    function.restype = restype
    function.argtypes = argtypes
    return function


_OpenProcess = _function("OpenProcess", wintypes.HANDLE,
                         wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_CloseHandle = _function("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_ReadProcessMemory = _function(
    "ReadProcessMemory", wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
_WriteProcessMemory = _function(
    "WriteProcessMemory", wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_char_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
_VirtualQueryEx = _function(
    "VirtualQueryEx", ctypes.c_size_t, wintypes.HANDLE, ctypes.c_void_p,
    ctypes.POINTER(_MemoryBasicInformation), ctypes.c_size_t)
_VirtualProtectEx = _function(
    "VirtualProtectEx", wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
    wintypes.DWORD, wintypes.PDWORD)
_FlushInstructionCache = _function(
    "FlushInstructionCache", wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t)
_QueryFullProcessImageNameW = _function(
    "QueryFullProcessImageNameW", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
    wintypes.LPWSTR, wintypes.PDWORD)
_GetProcessTimes = _function(
    "GetProcessTimes", wintypes.BOOL, wintypes.HANDLE, *[ctypes.POINTER(ctypes.c_uint64)] * 4)
_WaitForSingleObject = _function(
    "WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
_IsWow64Process = _function(
    "IsWow64Process", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
try:
    # Windows 10 1511 and later; tells ARM64 and x86 emulation apart.
    _IsWow64Process2 = _function(
        "IsWow64Process2", wintypes.BOOL, wintypes.HANDLE,
        ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT))
except AttributeError:
    _IsWow64Process2 = None
_EnumProcessModulesEx = _function(
    "K32EnumProcessModulesEx", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p),
    wintypes.DWORD, wintypes.LPDWORD, wintypes.DWORD)
_GetModuleBaseNameW = _function(
    "K32GetModuleBaseNameW", wintypes.DWORD, wintypes.HANDLE, ctypes.c_void_p,
    wintypes.LPWSTR, wintypes.DWORD)
_GetModuleFileNameExW = _function(
    "K32GetModuleFileNameExW", wintypes.DWORD, wintypes.HANDLE, ctypes.c_void_p,
    wintypes.LPWSTR, wintypes.DWORD)
_GetMappedFileNameW = _function(
    "K32GetMappedFileNameW", wintypes.DWORD, wintypes.HANDLE, ctypes.c_void_p,
    wintypes.LPWSTR, wintypes.DWORD)
_QueryDosDeviceW = _function(
    "QueryDosDeviceW", wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
_OpenThread = _function("OpenThread", wintypes.HANDLE,
                        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_GetThreadTimes = _function(
    "GetThreadTimes", wintypes.BOOL, wintypes.HANDLE, *[ctypes.POINTER(ctypes.c_uint64)] * 4)
_QueryThreadCycleTime = _function(
    "QueryThreadCycleTime", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint64))
_LocalFree = _function("LocalFree", ctypes.c_void_p, ctypes.c_void_p)
try:
    # Windows 10 1607 and later.
    _GetThreadDescription = _function(
        "GetThreadDescription", ctypes.c_long, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p))
except AttributeError:
    _GetThreadDescription = None


def _open(pid, access):
    """Return a handle to ``pid``; raises OSError (PermissionError if denied)."""
    handle = _OpenProcess(access, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


@contextmanager
def _process(pid, access):
    handle = _open(pid, access)
    try:
        yield handle
    finally:
        _CloseHandle(handle)


def _regions(handle, address=0):
    """Yield the MEMORY_BASIC_INFORMATION of each region from ``address`` on."""
    while address < _ADDRESS_LIMIT:
        info = _MemoryBasicInformation()
        if not _VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(info),
                               ctypes.sizeof(info)):
            return
        yield info
        address = (info.BaseAddress or 0) + info.RegionSize


def _readable(info):
    return (info.State == MEM_COMMIT and not info.Protect & PAGE_GUARD
            and info.Protect & 0xFF not in (PAGE_NOACCESS, PAGE_EXECUTE))


def _modules(handle):
    """{load address: (loader name, path)} of the loaded PE images."""
    needed = wintypes.DWORD()
    count = 1024
    while True:
        bases = (ctypes.c_void_p * count)()
        if not _EnumProcessModulesEx(handle, bases, ctypes.sizeof(bases), ctypes.byref(needed),
                                     LIST_MODULES_ALL):
            return {}
        if needed.value <= ctypes.sizeof(bases):
            break
        count = needed.value // ctypes.sizeof(ctypes.c_void_p) + 64
    modules = {}
    buffer = ctypes.create_unicode_buffer(32768)
    for base in bases[:needed.value // ctypes.sizeof(ctypes.c_void_p)]:
        if not base:
            continue
        if not _GetModuleBaseNameW(handle, base, buffer, len(buffer)):
            continue
        name = buffer.value
        path = buffer.value if _GetModuleFileNameExW(handle, base, buffer, len(buffer)) else name
        modules[base] = (name, path)
    return modules


_dos_devices = None


def _dos_path(device_path):
    r"""``\Device\HarddiskVolume3\x`` as ``C:\x``; unchanged if no drive matches."""
    global _dos_devices  # pylint: disable=global-statement
    if _dos_devices is None:
        _dos_devices = {}
        buffer = ctypes.create_unicode_buffer(1024)
        for letter in string.ascii_uppercase:
            if _QueryDosDeviceW(f"{letter}:", buffer, len(buffer)):
                _dos_devices[buffer.value + "\\"] = f"{letter}:\\"
    for device, drive in _dos_devices.items():
        if device_path.startswith(device):
            return drive + device_path[len(device):]
    return device_path


def _mapped_file(handle, address):
    buffer = ctypes.create_unicode_buffer(32768)
    if not _GetMappedFileNameW(handle, ctypes.c_void_p(address), buffer, len(buffer)):
        return ""
    return _dos_path(buffer.value)


def read_mappings(pid):
    """Return the Mappings of process ``pid``: its committed memory regions."""
    with _process(pid, PROCESS_QUERY_INFORMATION | PROCESS_VM_READ) as handle:
        modules = _modules(handle)
        files = {}
        mappings = []
        for info in _regions(handle):
            if info.State != MEM_COMMIT:
                continue
            start = info.BaseAddress or 0
            allocation = info.AllocationBase or 0
            protection = info.Protect & 0xFF
            shared = info.Type == MEM_MAPPED and protection in _SHARED_WRITABLE
            permissions = _PERMISSIONS.get(protection, "---") + ("s" if shared else "p")
            if info.Protect & PAGE_GUARD:
                permissions = "---p"
            writable = (
                permissions.startswith("rw") and not shared
                and not info.Protect & (PAGE_GUARD | PAGE_NOCACHE | PAGE_WRITECOMBINE)
            )
            module = None
            path = ""
            if info.Type == MEM_IMAGE:
                if allocation in modules:
                    module, path = modules[allocation]
                else:
                    # Not (yet) in the loader's list: name it after its file.
                    if allocation not in files:
                        files[allocation] = _mapped_file(handle, allocation)
                    path = files[allocation]
                    module = ntpath.basename(path) or None
            elif info.Type == MEM_MAPPED:
                if allocation not in files:
                    files[allocation] = _mapped_file(handle, allocation)
                path = files[allocation]
            mappings.append(Mapping(start, start + info.RegionSize, permissions, path, module,
                                    writable))
        return mappings


def _image_path(pid):
    with _process(pid, PROCESS_QUERY_LIMITED_INFORMATION) as handle:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not _QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value


def main_module(pid):
    """Return the module name of the program itself, or None if unknown."""
    try:
        return ntpath.basename(_image_path(pid))
    except OSError:
        return None


class ProcessMemory:
    """Read, and if ``writable`` also write, a process' memory via ReadProcessMemory."""

    def __init__(self, pid, writable=False):
        self.pid = pid
        access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
        if writable:
            access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION
        self._handle = _open(pid, access)

    def close(self):
        """Close the process handle."""
        if self._handle:
            _CloseHandle(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _read(self, address, size):
        buffer = ctypes.create_string_buffer(size)
        done = ctypes.c_size_t()
        if not _ReadProcessMemory(self._handle, ctypes.c_void_p(address), buffer, size,
                                  ctypes.byref(done)) or done.value != size:
            return None
        return buffer.raw

    def _readable_size(self, address, size):
        """How many of ``size`` bytes at ``address`` lie in readable memory."""
        end = address
        for info in _regions(self._handle, address):
            if not _readable(info):
                break
            end = (info.BaseAddress or 0) + info.RegionSize
            if end >= address + size:
                return size
        return max(0, end - address)

    def read_available(self, address, size):
        """Return up to ``size`` bytes at ``address``.

        Fewer if readable memory ends earlier, ``b""`` if ``address`` itself
        cannot be read.
        """
        if address < 0 or size <= 0 or address + size > _ADDRESS_LIMIT:
            return b""
        data = self._read(address, size)
        if data is not None:
            return data
        # ReadProcessMemory reads all or nothing: retry up to the readable end.
        readable = self._readable_size(address, size)
        if readable <= 0 or readable >= size:
            return b""
        return self._read(address, readable) or b""

    def read(self, address, size):
        """Return ``size`` bytes or None if the memory cannot be read."""
        data = self.read_available(address, size)
        return data if len(data) == size else None

    def read_pointer(self, address, size=8):
        """Return the pointer at ``address`` or None."""
        data = self.read(address, size)
        return None if data is None else int.from_bytes(data, "little")

    def _write(self, address, data):
        done = ctypes.c_size_t()
        return bool(_WriteProcessMemory(self._handle, ctypes.c_void_p(address), data, len(data),
                                        ctypes.byref(done))) and done.value == len(data)

    def write(self, address, data):
        """Write ``data`` to ``address``; raises OSError unless all of it was written.

        Read-only pages are written too, as on Linux: their protection is
        lifted for the write and restored afterwards.
        """
        data = bytes(data)
        if self._write(address, data):
            return
        end = address + len(data)
        restore = []
        try:
            for info in _regions(self._handle, address):
                start = info.BaseAddress or 0
                if start >= end:
                    break
                protection = info.Protect & 0xFF
                writable = (PAGE_EXECUTE_READWRITE if protection in _EXECUTABLE
                            else PAGE_READWRITE)
                old = wintypes.DWORD()
                low, high = max(start, address), min(start + info.RegionSize, end)
                if not _VirtualProtectEx(self._handle, ctypes.c_void_p(low), high - low, writable,
                                         ctypes.byref(old)):
                    raise ctypes.WinError(ctypes.get_last_error())
                restore.append((low, high - low, old.value))
            written = self._write(address, data)
        finally:
            for low, size, protection in restore:
                _VirtualProtectEx(self._handle, ctypes.c_void_p(low), size, protection,
                                  ctypes.byref(wintypes.DWORD()))
        _FlushInstructionCache(self._handle, ctypes.c_void_p(address), len(data))
        if not written:
            raise LocalizedOSError("error.platform.incomplete_write_windows", size=len(data),
                                   address=f"{address:#x}")


def thread_stack_pointers(pid, proc_root=None):  # pylint: disable=unused-argument
    """None: Windows marks thread stacks with guard pages instead (see
    grembyte.core.memory_regions)."""
    return None


def process_start_time(pid, proc_root=None):  # pylint: disable=unused-argument
    """Return when ``pid`` was started (FILETIME), or None once it has exited.

    Comparing start times tells a restarted program that got the same PID
    apart from the original one.
    """
    try:
        with _process(pid, PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE) as handle:
            if _WaitForSingleObject(handle, 0) != STILL_RUNNING:
                return None
            times = [ctypes.c_uint64() for _ in range(4)]
            if not _GetProcessTimes(handle, *(ctypes.byref(time) for time in times)):
                return None
            return times[0].value
    except OSError:
        return None


def program_bits(pid, proc_root=None):  # pylint: disable=unused-argument
    """Return 32 or 64 for the program ``pid`` runs, or None if unknown."""
    try:
        with _process(pid, PROCESS_QUERY_LIMITED_INFORMATION) as handle:
            if _IsWow64Process2 is not None:
                process_machine, native_machine = wintypes.USHORT(), wintypes.USHORT()
                if not _IsWow64Process2(handle, ctypes.byref(process_machine),
                                        ctypes.byref(native_machine)):
                    return None
                machine = process_machine.value
                if machine == IMAGE_FILE_MACHINE_UNKNOWN:
                    machine = native_machine.value
                return 32 if machine in _32BIT_MACHINES else 64
            wow64 = wintypes.BOOL()
            if not _IsWow64Process(handle, ctypes.byref(wow64)):
                return None
            return 32 if wow64.value else 64
    except OSError:
        return None


def is_wine_process(pid, proc_root=None):  # pylint: disable=unused-argument
    """Always False: on Windows a program runs natively."""
    return False


@contextmanager
def _thread(tid):
    handle = _OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, tid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield handle
    finally:
        _CloseHandle(handle)


def thread_times(tid):
    """(CPU seconds, CPU cycles) thread ``tid`` has used, or None once it is gone.

    The cycles tell a thread that woke up only briefly from one that slept,
    below the resolution of the CPU time.
    """
    try:
        with _thread(tid) as handle:
            times = [ctypes.c_uint64() for _ in range(4)]
            cycles = ctypes.c_uint64()
            if not _GetThreadTimes(handle, *(ctypes.byref(time) for time in times)) \
                    or not _QueryThreadCycleTime(handle, ctypes.byref(cycles)):
                return None
    except OSError:
        return None
    # Kernel and user time, in units of 100 ns.
    return (times[2].value + times[3].value) / 10_000_000, cycles.value


def thread_name(tid):
    """The name a program gave thread ``tid`` (SetThreadDescription), or None."""
    if _GetThreadDescription is None:
        return None
    try:
        with _thread(tid) as handle:
            text = ctypes.c_void_p()
            if _GetThreadDescription(handle, ctypes.byref(text)) < 0 or not text.value:
                return None
            try:
                return ctypes.wstring_at(text.value) or None
            finally:
                _LocalFree(text)
    except OSError:
        return None
