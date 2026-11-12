/*
 * grembyte-inject: load a DLL into another process of the same Wine prefix.
 *
 * grembyte runs this with the wine and WINEPREFIX of a running game, so it
 * talks to the game's own wineserver. It uses the standard Windows route
 * (VirtualAllocEx, WriteProcessMemory, CreateRemoteThread on LoadLibraryW)
 * and exits; no ptrace or breakpoint is involved.
 *
 *   grembyte-inject list
 *       One line per process:
 *       "<pid>\t<creation, Unix ms>\t<exe name>\t<32 or 64, 0 if unknown>".
 *   grembyte-inject inject <pid> <dll path> [LoadLibraryW address, hex]
 *       Prints "ok <module low 32 bits>" and exits 0, or "error <text>" and
 *       a non-zero code. Without an address the helper's own LoadLibraryW is
 *       used, which Wine maps at the same address in every process of a
 *       prefix.
 *
 * Build with MinGW or zig, once per architecture (see build.sh).
 */
#include <windows.h>
#include <tlhelp32.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#define INJECT_TIMEOUT_MS 20000

static int fail(const char *what)
{
    printf("error %s (Windows error %lu)\n", what, GetLastError());
    fflush(stdout);
    return 2;
}

/* FILETIME (100 ns since 1601) to milliseconds since the Unix epoch. */
static unsigned long long unix_ms(FILETIME time)
{
    ULARGE_INTEGER value;
    value.LowPart = time.dwLowDateTime;
    value.HighPart = time.dwHighDateTime;
    return (value.QuadPart - 116444736000000000ULL) / 10000ULL;
}

/* 32 or 64 for another process, 0 if unknown. A 32-bit process on a 64-bit
   system runs under WoW64; on a purely 32-bit Wine everything is 32-bit. */
static int process_bits(HANDLE process)
{
    BOOL target_wow = FALSE, self_wow = FALSE;

    if (!IsWow64Process(process, &target_wow) ||
        !IsWow64Process(GetCurrentProcess(), &self_wow))
        return 0;
    if (target_wow)
        return 32;
    return sizeof(void *) == 4 && !self_wow ? 32 : 64;
}

static int list_processes(void)
{
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    PROCESSENTRY32W entry;

    if (snapshot == INVALID_HANDLE_VALUE)
        return fail("CreateToolhelp32Snapshot");
    entry.dwSize = sizeof(entry);
    if (Process32FirstW(snapshot, &entry)) {
        do {
            unsigned long long created = 0;
            int bits = 0;
            HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE,
                                         entry.th32ProcessID);
            if (process) {
                FILETIME creation, exit_time, kernel, user;
                if (GetProcessTimes(process, &creation, &exit_time, &kernel, &user))
                    created = unix_ms(creation);
                bits = process_bits(process);
                CloseHandle(process);
            }
            /* UTF-8, so non-ASCII exe names survive the pipe to grembyte. */
            char name[MAX_PATH * 4];
            WideCharToMultiByte(CP_UTF8, 0, entry.szExeFile, -1, name, sizeof(name),
                                NULL, NULL);
            printf("%lu\t%llu\t%s\t%d\n", entry.th32ProcessID, created, name, bits);
        } while (Process32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    fflush(stdout);
    return 0;
}

static int inject(DWORD pid, const wchar_t *dll, ULONG_PTR load_library)
{
    SIZE_T size = (wcslen(dll) + 1) * sizeof(wchar_t);
    HANDLE process, thread;
    LPVOID remote;
    DWORD code = 0, waited;

    if (!load_library)
        load_library = (ULONG_PTR)GetProcAddress(GetModuleHandleW(L"kernel32.dll"),
                                                 "LoadLibraryW");
    if (!load_library)
        return fail("LoadLibraryW not found");

    process = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
                          PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ,
                          FALSE, pid);
    if (!process)
        return fail("OpenProcess");
    remote = VirtualAllocEx(process, NULL, size, MEM_COMMIT | MEM_RESERVE,
                            PAGE_READWRITE);
    if (!remote) {
        CloseHandle(process);
        return fail("VirtualAllocEx");
    }
    if (!WriteProcessMemory(process, remote, dll, size, NULL)) {
        VirtualFreeEx(process, remote, 0, MEM_RELEASE);
        CloseHandle(process);
        return fail("WriteProcessMemory");
    }
    thread = CreateRemoteThread(process, NULL, 0, (LPTHREAD_START_ROUTINE)load_library,
                                remote, 0, NULL);
    if (!thread) {
        VirtualFreeEx(process, remote, 0, MEM_RELEASE);
        CloseHandle(process);
        return fail("CreateRemoteThread");
    }
    waited = WaitForSingleObject(thread, INJECT_TIMEOUT_MS);
    if (waited != WAIT_OBJECT_0) {
        /* The thread may still read the path: leave the buffer allocated. */
        CloseHandle(thread);
        CloseHandle(process);
        printf("error LoadLibraryW did not return within %d ms\n", INJECT_TIMEOUT_MS);
        fflush(stdout);
        return 3;
    }
    GetExitCodeThread(thread, &code);
    CloseHandle(thread);
    VirtualFreeEx(process, remote, 0, MEM_RELEASE);
    CloseHandle(process);
    if (!code) {
        printf("error LoadLibraryW returned NULL\n");
        fflush(stdout);
        return 4;
    }
    printf("ok %lx\n", code);
    fflush(stdout);
    return 0;
}

int main(void)
{
    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);

    if (argv && argc == 2 && !wcscmp(argv[1], L"list"))
        return list_processes();
    if (argv && (argc == 4 || argc == 5) && !wcscmp(argv[1], L"inject")) {
        DWORD pid = wcstoul(argv[2], NULL, 10);
        ULONG_PTR address = argc == 5 ? (ULONG_PTR)wcstoull(argv[4], NULL, 16) : 0;
        return inject(pid, argv[3], address);
    }
    printf("usage: grembyte-inject list | inject <pid> <dll> [LoadLibraryW hex]\n");
    return 1;
}
