"""Worker executable lookup that never searches the current directory.

On Windows, ``shutil.which`` searches the current directory before PATH unless
``NoDefaultCurrentDirectoryInExePath`` is set. A target project could then plant ``claude.exe``,
``codex.exe`` or ``agy.exe`` in its root and have it run as the Worker, already during the probe.
This lookup walks only absolute PATH entries and skips ``.``, relative entries and the current
directory. A command with a directory component is an explicitly configured path and keeps the
standard-library behavior.
"""
import os
from os.path import normcase
from pathlib import Path
import shutil

from .ledger import LedgerError

EXECUTABLE_INSIDE_PROJECT = "Worker 執行檔位於目標專案、工作樹或 Bundle 目錄內，已拒絕：{path}"


def _key(path):
    return normcase(os.fspath(path))


def path_directories():
    """Absolute PATH directories in order, without relative entries or the current directory."""
    try:
        current = _key(Path.cwd().resolve())
    except (OSError, RuntimeError):
        current = None
    seen = set()
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        try:
            key = _key(Path(entry).resolve())
        except (OSError, RuntimeError, ValueError):
            continue
        if key == current or key in seen:
            continue
        seen.add(key)
        yield entry


def _candidates(command):
    if os.name != "nt":
        return [command]
    extensions = [ext for ext in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if ext]
    names = [command + ext for ext in extensions]
    # As cmd.exe does: the bare name is tried only when it already ends with an executable extension.
    if any(command.upper().endswith(ext.upper()) for ext in extensions):
        names.insert(0, command)
    return names


def which(command):
    """Drop-in for ``shutil.which(command)`` that never resolves from the current directory."""
    if not command:
        return None
    command = os.fspath(command)
    if os.path.dirname(command):
        return shutil.which(command)
    for directory in path_directories():
        for name in _candidates(command):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def inside_roots(executable, roots):
    """True when the executable resolves inside any of the given directories."""
    resolved = Path(executable).resolve()
    for root in roots:
        if root is None:
            continue
        root = Path(root).resolve()
        if resolved == root or resolved.is_relative_to(root):
            return True
    return False


def refuse_inside_roots(executable, roots):
    """Raise LedgerError(14) when the resolved Worker executable lies in the project or Worker directories."""
    if executable is not None and inside_roots(executable, roots):
        raise LedgerError(14, EXECUTABLE_INSIDE_PROJECT.format(path=Path(executable).resolve()))
