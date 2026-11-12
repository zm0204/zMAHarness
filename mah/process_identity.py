"""Report process liveness with a creation identity, not just a PID."""
import os
from pathlib import Path


def probe_process(pid):
    if os.name == "nt":
        return _windows_process(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ("gone", None) if Path("/proc/self/stat").exists() else ("unknown", None)
    except OSError:
        return "unknown", None
    fields = stat.rsplit(")", 1)[-1].split()
    if len(fields) < 20:
        return "unknown", None
    if fields[0] == "Z":
        return "gone", None
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return "unknown", None
    return "alive", f"proc:{boot}:{fields[19]}"


def _windows_process(pid):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL

    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)  # query limited information, synchronize
    if not handle:
        return ("gone", None) if ctypes.get_last_error() == 87 else ("unknown", None)
    try:
        wait = kernel.WaitForSingleObject(handle, 0)
        if wait == 0:
            return "gone", None
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if wait != 258 or not kernel.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel_time), ctypes.byref(user_time),
        ):
            return "unknown", None
        return "alive", f"win:{created.dwHighDateTime}:{created.dwLowDateTime}"
    finally:
        kernel.CloseHandle(handle)
