"""Contain a Worker and every process it starts in a Windows Job Object (refactor spec 2c).

The job is created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: a timeout or interruption terminates the whole
tree, closing the job after a normal exit clears leftover grandchildren, and if mah itself is killed the OS
closes the handle and ends the tree. This is cleanup hardening, not a safety gate: when the job cannot be
created or assigned, the dispatch continues with the previous behavior (only the Worker is killed) and the
reason is returned for the process receipt.

Popen returns before the job is assigned, so a process started in those microseconds could escape; avoiding
that needs CREATE_SUSPENDED and a hand-written CreateProcessW, which the spec declines.
"""
import os
import subprocess

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
TERMINATED_EXIT_CODE = 1


def _supported():
    return os.name == "nt"


def _kernel32():
    import ctypes
    from ctypes import wintypes

    # A private instance, so these signatures never leak into other ctypes users of kernel32.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _limit_information():
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimitInformation(ctypes.Structure):
        # LARGE_INTEGER is 64-bit; SIZE_T and ULONG_PTR follow the pointer size (c_size_t).
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimitInformation), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    return info


# Each Win32 step is a separate function so a test can make exactly one of them fail.
def _create_job(kernel):
    return kernel.CreateJobObjectW(None, None)


def _set_kill_on_close(kernel, job):
    import ctypes

    info = _limit_information()
    return kernel.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                          ctypes.sizeof(info))


def _assign_process(kernel, job, process_handle):
    return kernel.AssignProcessToJobObject(job, process_handle)


def _last_error():
    import ctypes

    return ctypes.get_last_error()


class Job:
    """An open kill-on-close job handle. terminate() and close() are idempotent and never raise."""

    def __init__(self, kernel, handle):
        self._kernel = kernel
        self._handle = handle

    @property
    def closed(self):
        return self._handle is None

    def terminate(self):
        """End every process in the job; False when the caller must fall back to killing the Worker."""
        if self._handle is None:
            return False
        try:
            return bool(self._kernel.TerminateJobObject(self._handle, TERMINATED_EXIT_CODE))
        except OSError:
            return False

    def close(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                self._kernel.CloseHandle(handle)
            except OSError:
                pass


def _process_handle(process):
    """The Win32 handle of a real subprocess.Popen; stand-ins without one are not contained."""
    if not isinstance(process, subprocess.Popen):
        return None
    handle = getattr(process, "_handle", None)
    if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
        return None
    return int(handle)


def contain(process):
    """Return (job, error). Both are None where containment does not apply (not Windows, a stand-in
    process, or a Worker that already exited); error is a short reason when a Win32 call failed."""
    if not _supported():
        return None, None
    process_handle = _process_handle(process)
    if process_handle is None:
        return None, None
    try:
        kernel = _kernel32()
    except (OSError, AttributeError) as exc:
        return None, f"kernel32 unavailable: {type(exc).__name__}"
    job_handle = _create_job(kernel)
    if not job_handle:
        return None, f"CreateJobObjectW failed (winerror {_last_error()})"
    job = Job(kernel, job_handle)
    if not _set_kill_on_close(kernel, job_handle):
        error = f"SetInformationJobObject failed (winerror {_last_error()})"
        job.close()
        return None, error
    if not _assign_process(kernel, job_handle, process_handle):
        error = f"AssignProcessToJobObject failed (winerror {_last_error()})"
        job.close()
        # A Worker that already exited has no tree left to contain.
        return (None, None) if process.poll() is not None else (None, error)
    return job, None
