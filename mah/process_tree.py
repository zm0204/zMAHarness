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
import threading
import time

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
TERMINATED_EXIT_CODE = 1
_REAL_POPEN = subprocess.Popen


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
    if not isinstance(process, _REAL_POPEN):
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


def _kill_quietly(process):
    try:
        process.kill()
    except OSError:
        pass


def _close_quietly(stream):
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass


def terminate_tree(process, job):
    """Terminate the contained Job Object tree, falling back to killing the direct child."""
    terminated = False
    if job is not None:
        terminated = job.terminate()
    if not terminated:
        _kill_quietly(process)
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if job is not None:
        job.close()


def run_contained(argv, *, env=None, cwd=None, timeout=10, popen=None):
    """Run a read-only CLI query inside a Job Object without hanging on grandchild-held pipes.

    Raises subprocess.TimeoutExpired if the child or any grandchild holding stdout/stderr does not
    finish within `timeout`. Never closes a pipe from the main thread while its daemon reader thread
    is still blocked in read().
    """
    cmd = [str(part) for part in argv]
    launcher = popen or subprocess.Popen
    process = launcher(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        shell=False, env=env, cwd=cwd,
    )
    if not isinstance(process, _REAL_POPEN):
        # Support unit-test MagicMock stand-ins that implement communicate(timeout=...).
        try:
            out, err = process.communicate(timeout=timeout)
        except BaseException:
            _kill_quietly(process)
            raise
        return subprocess.CompletedProcess(cmd, process.returncode, out, err)

    job, _ = contain(process)
    out_chunks = []
    err_chunks = []

    def _drain(stream, bucket):
        if stream is None:
            return
        try:
            data = stream.read()
            if data:
                bucket.append(data)
        except (OSError, ValueError):
            pass
        finally:
            # Close inside the reader thread once read() finishes, never from the main thread.
            _close_quietly(stream)

    t_out = threading.Thread(target=_drain, args=(process.stdout, out_chunks), daemon=True)
    t_err = threading.Thread(target=_drain, args=(process.stderr, err_chunks), daemon=True)
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        try:
            remaining = max(0.0, deadline - time.monotonic())
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True

        if not timed_out:
            for thread in (t_out, t_err):
                remaining = max(0.0, deadline - time.monotonic())
                thread.join(timeout=remaining)
            if t_out.is_alive() or t_err.is_alive():
                timed_out = True
    except BaseException:
        terminate_tree(process, job)
        raise

    if timed_out:
        terminate_tree(process, job)
        # Brief grace so reader threads see EOF once the Job Object ends the tree.
        t_out.join(timeout=0.3)
        t_err.join(timeout=0.3)
        raise subprocess.TimeoutExpired(cmd, timeout, output=b"".join(out_chunks), stderr=b"".join(err_chunks))

    if job is not None:
        job.close()
    return subprocess.CompletedProcess(cmd, process.returncode, b"".join(out_chunks), b"".join(err_chunks))


def run_parallel_with_deadline(tasks, *, timeout):
    """Run `{key: callable}` on daemon threads with a shared wall-clock deadline.

    Returns (results_dict, errors_dict, timed_out_keys_tuple). Daemon threads never block Python exit
    if a slow call outlives the deadline, and late completions cannot mutate the returned snapshots.
    """
    if not tasks:
        return {}, {}, ()

    results = {}
    errors = {}
    threads = {}
    lock = threading.Lock()
    closed = False

    def _worker(key, fn):
        try:
            value = fn()
        except Exception as exc:
            with lock:
                if not closed:
                    errors[key] = exc
        else:
            with lock:
                if not closed:
                    results[key] = value

    for key, fn in tasks.items():
        thread = threading.Thread(target=_worker, args=(key, fn), daemon=True)
        threads[key] = thread
        thread.start()

    deadline = time.monotonic() + max(0.0, timeout)
    for thread in threads.values():
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)

    with lock:
        closed = True
        results_snapshot = dict(results)
        errors_snapshot = dict(errors)
        timed_out = tuple(
            key for key in tasks
            if key not in results_snapshot and key not in errors_snapshot
        )
    return results_snapshot, errors_snapshot, timed_out
