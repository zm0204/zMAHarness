"""mah cleanup: list (the default) or delete expired Run directories and Disposable Worktrees (spec 2d).

Nothing is removed unless --apply is given. Only what the Harness itself created is touched: Run
directories under the state directory and Disposable Worktrees at worktree_root/<run_id> whose snapshot
is verified. Ledger rows are kept as the audit record, so `mah status <run_id>` still answers afterwards.

A Run directory is deleted only when every condition holds; otherwise the report gives the first reason:
- run_not_finished: the Run is not in a terminal state;
- acceptance_pending: a SUCCEEDED Run whose result was never accepted or rejected (never cleaned);
- not_expired: finished_at is not older than the threshold;
- outside_state / link_refused / contains_link: the directory escapes the state directory, is a symbolic
  link or junction, or contains one;
- worktree_present: the Run still has a worktree that is not verified for removal, or its removal failed;
- delete_failed: deleting the Run directory failed part way (for example a locked file); delete_error holds
  the error and the other entries are still processed.

Listing collects the same repositories as --apply, so the prune entries preview the git worktree prune that
--apply would run (status "將執行") without running it.
"""
from contextlib import closing
from datetime import datetime, timedelta
from hashlib import sha256
import json
import os
from os.path import normcase
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace

from .context_bundle import _git, _git_path, _head
from .db import connect
from .ledger import RUN_ID, TERMINAL_STATES, LedgerError, timestamp, utc_now, valid_id
from .run_manager import RunManager, _read

UNACCEPTED_CLEANABLE = {"FAILED", "FAILED_PRE_SUBMIT", "CANCELLED", "ABANDONED"}
WORKTREE_KINDS = {"worktree", "disposable_worktree"}
DISABLED = "privacy.retain_run_dirs_days = 0 停用清理：只能列出，不能 --apply"


def _is_link(path):
    """A symbolic link, a junction or any other reparse point."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _scan(directory):
    """(bytes, contains_link) without following any link."""
    size, linked = 0, False
    for root, directories, files in os.walk(directory, followlinks=False):
        for name in directories + files:
            path = os.path.join(root, name)
            if _is_link(path):
                linked = True
                continue
            if name in files:
                size += os.lstat(path).st_size
        directories[:] = [name for name in directories if not _is_link(os.path.join(root, name))]
    return size, linked


def _same_path(left, right):
    return normcase(str(Path(left).resolve())) == normcase(str(Path(right).resolve()))


def _linked_worktree_of(path, repo):
    """True when path is a linked worktree (not the main one) of repo's object store."""
    try:
        return ((path / ".git").is_file() and _same_path(_git_path(path, "--show-toplevel"), path)
                and _same_path(_git_path(path, "--git-common-dir"), _git_path(repo, "--git-common-dir"))
                and not _same_path(_git_path(path, "--git-dir"), _git_path(repo, "--git-dir")))
    except (OSError, ValueError):
        return False


def _snapshot_verified(path, run_id, baseline):
    """The worktree HEAD is the Harness snapshot commit, or the commit the Run was launched on."""
    try:
        if _git(path, "log", "-1", "--format=%s").decode("utf-8", "replace").strip() == f"mah-snapshot:{run_id}":
            return True
        # A clean launch baseline is sha256(HEAD + NUL + empty status), see context_bundle._workspace_snapshot.
        return baseline is not None and sha256(_head(path).encode("ascii") + b"\0").hexdigest() == baseline
    except (OSError, ValueError, UnicodeError):
        return False


def _launch_baseline(directory):
    try:
        return json.loads(_read(directory / "invocation.json"))["workspace_baseline_sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _remove_worktree(repo, path):
    try:
        _git(repo, "worktree", "remove", "--force", "--", str(path), timeout=60)
    except (OSError, ValueError):
        pass
    return not os.path.lexists(path)


def _prunable(repo):
    entries, current = [], None
    for line in _git(repo, "worktree", "list", "--porcelain").decode("utf-8", "replace").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):]
        elif line == "prunable" or line.startswith("prunable "):
            entries.append(current)
    return entries


def _check_options(config, older_than_days, apply):
    retain = config.values["privacy"]["retain_run_dirs_days"]
    if older_than_days is not None and (type(older_than_days) is not int or older_than_days < 0):
        raise LedgerError(2, "--older-than-days 必須是非負整數")
    if apply and retain == 0:
        raise LedgerError(2, DISABLED)
    return retain


class Cleanup:
    def __init__(self, manager, config, *, older_than_days=None, apply=False):
        retain = _check_options(config, older_than_days, apply)
        # Without a Ledger (state never created) only orphan worktrees can be listed.
        self.manager = manager
        self.apply = apply
        self.days = retain if older_than_days is None else older_than_days
        self.disabled = retain == 0
        self.now = manager.ledger.clock() if manager is not None else utc_now()
        self.cutoff = timestamp(self.now - timedelta(days=self.days))
        self.worktree_root = config.values["paths"]["worktree_root"].resolve()
        self.repos = {}

    def expired(self, finished_at):
        # Ledger timestamps share one ISO format in UTC, so they compare as text.
        return finished_at is not None and finished_at < self.cutoff

    def run_entry(self, run):
        run_id = run["run_id"]
        entry = {"run_id": run_id, "state": run["state"], "acceptance_status": run["acceptance_status"],
                 "finished_at": run["finished_at"]}
        try:
            directory = self.manager._directory(run_id)
        except LedgerError:
            return entry | {"action": "keep", "reason": "outside_state", "run_directory": None}
        workspace = Path(run["workspace_path"])
        has_worktree = (run["workspace_kind"] in WORKTREE_KINDS and not workspace.is_relative_to(directory)
                        and os.path.lexists(workspace))
        has_directory = os.path.lexists(directory)
        if not has_directory and not has_worktree:
            return None
        entry["run_directory"] = str(directory) if has_directory else None
        entry["worktree"] = str(workspace) if has_worktree else None
        reason = None
        if run["state"] not in TERMINAL_STATES:
            reason = "run_not_finished"
        elif run["acceptance_status"] == "PENDING" and run["state"] not in UNACCEPTED_CLEANABLE:
            reason = "acceptance_pending"
        elif not self.expired(run["finished_at"]):
            reason = "not_expired"
        elif has_directory and _is_link(directory):
            reason = "link_refused"
        if has_directory and reason != "link_refused":
            entry["bytes"], linked = _scan(directory)
            if reason is None and linked:
                reason = "contains_link"
        if reason is None and has_worktree:
            repo = Path(run["repo_root"])
            if not (run["workspace_kind"] == "disposable_worktree"
                    and _same_path(workspace, self.worktree_root / run_id) and not _is_link(workspace)
                    and _linked_worktree_of(workspace, repo)
                    and _snapshot_verified(workspace, run_id, _launch_baseline(directory))):
                reason = "worktree_present"
            else:
                self.repos.setdefault(normcase(str(repo)), repo)
                if self.apply and not _remove_worktree(repo, workspace):
                    reason = "worktree_present"
                    entry["worktree_error"] = "git worktree remove failed"
        if reason is not None:
            return entry | {"action": "keep", "reason": reason}
        if self.apply and has_directory:
            # Checked again right before the irreversible step.
            if _is_link(directory) or _scan(directory)[1] or self.manager._directory(run_id) != directory:
                return entry | {"action": "keep", "reason": "link_refused"}
            try:
                shutil.rmtree(directory)
            except OSError as error:
                return entry | {"action": "keep", "reason": "delete_failed", "delete_error": str(error)}
        return entry | {"action": "deleted" if self.apply else "delete"}

    def orphan_entries(self, known):
        entries = []
        if not self.worktree_root.is_dir():
            return entries
        for path in sorted(self.worktree_root.iterdir()):
            name = path.name
            if not valid_id(RUN_ID, name) or name in known or _is_link(path) or not path.is_dir():
                continue
            modified = timestamp(datetime.fromtimestamp(path.stat().st_mtime, self.now.tzinfo))
            entry = {"worktree": str(path), "modified_at": modified}
            repo = None
            try:
                if (path / ".git").is_file():
                    repo = _git_path(path, "--git-common-dir").parent
            except (OSError, ValueError):
                repo = None
            if not self.expired(modified):
                entries.append(entry | {"action": "keep", "reason": "not_expired"})
            elif (repo is None or not _linked_worktree_of(path, repo)
                  or not _snapshot_verified(path, name, None)):
                entries.append(entry | {"action": "keep", "reason": "snapshot_unverified"})
            elif not self.apply:
                self.repos.setdefault(normcase(str(repo)), repo)
                entries.append(entry | {"action": "delete", "repo": str(repo)})
            else:
                self.repos.setdefault(normcase(str(repo)), repo)
                removed = _remove_worktree(repo, path)
                entries.append(entry | {"action": "deleted" if removed else "keep", "repo": str(repo)}
                               | ({} if removed else {"reason": "git worktree remove failed"}))
        return entries

    def prune_entries(self):
        """git worktree prune is repository-wide: run it only when every prunable entry is ours.
        status: 將執行 (listing, --apply would run it), 已執行, 執行失敗, 不執行 (an entry is not ours)."""
        entries = []
        for repo in self.repos.values():
            try:
                prunable = _prunable(repo)
            except (OSError, ValueError):
                entries.append({"repo": str(repo), "pruned": False, "reason": "worktree_list_failed"})
                continue
            if not prunable:
                continue
            ours = all(item and Path(item).resolve().is_relative_to(self.worktree_root) for item in prunable)
            pruned, status = False, "不執行"
            if ours and not self.apply:
                status = "將執行"
            elif ours:
                try:
                    _git(repo, "worktree", "prune", timeout=60)
                    pruned, status = True, "已執行"
                except (OSError, ValueError):
                    status = "執行失敗"
            entries.append({"repo": str(repo), "prunable": prunable, "command": "git worktree prune",
                            "status": status, "pruned": pruned,
                            **({} if ours else {"reason": "prunable_outside_worktree_root"})})
        return entries

    def run(self):
        runs = []
        if self.manager is not None:
            with closing(connect(self.manager.database)) as db:
                runs = db.execute("SELECT * FROM runs ORDER BY created_at, run_id").fetchall()
        entries = [entry for entry in (self.run_entry(run) for run in runs) if entry is not None]
        orphans = self.orphan_entries({run["run_id"] for run in runs})
        report = {
            "apply": self.apply, "disabled": self.disabled, "older_than_days": self.days, "cutoff": self.cutoff,
            "runs": entries, "orphan_worktrees": orphans, "prune": self.prune_entries(),
        }
        action = "deleted" if self.apply else "delete"
        report["summary"] = {
            "runs": sum(entry["action"] == action for entry in entries),
            "worktrees": sum(entry["action"] == action and entry.get("worktree") is not None for entry in entries)
            + sum(entry["action"] == action for entry in orphans),
            "bytes": sum(entry.get("bytes", 0) for entry in entries if entry["action"] == action),
            "kept": sum(entry["action"] == "keep" for entry in entries + orphans),
        }
        return report


def cleanup(manager, config, *, older_than_days=None, apply=False):
    return Cleanup(manager, config, older_than_days=older_than_days, apply=apply).run()


def read_only_manager(config):
    """A Run Manager over the existing Ledger that never creates, migrates or writes it; None without one.
    Cleanup only reads the runs table, so it needs no GrantLedger initialization."""
    if not config.database.is_file():
        return None
    return RunManager(SimpleNamespace(database=config.database, clock=utc_now))


def doctor_summary(config):
    """Counts for doctor.ps1: what `mah cleanup --apply` would remove right now. Never deletes anything."""
    report = cleanup(read_only_manager(config), config)
    return report["summary"] | {"older_than_days": report["older_than_days"], "disabled": report["disabled"]}
