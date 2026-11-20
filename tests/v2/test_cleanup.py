"""mah cleanup and privacy.retain_run_dirs_days (refactor spec 2d). Stand-in Worker only."""
from contextlib import closing as closing_db, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from mah.adapters import registry
from mah.adapters.base import AdapterPolicy
from mah.cleanup import cleanup, doctor_summary, read_only_manager
from mah.cli import main
from mah.config import ConfigError, load_config
from mah.context_bundle import prepare_worktree
from mah.ledger import LedgerError

import test_cli
from test_cli_worker import WorkerFixture

GIT = ["git", "-c", "user.name=Harness Test", "-c", "user.email=harness@example.invalid"]


def junction(link, target):
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def tree_state(*roots):
    """Every path with its bytes and modification time, without following links."""
    state = {}
    for root in roots:
        for folder, directories, files in os.walk(root, followlinks=False):
            for name in directories + files:
                path = Path(folder) / name
                info = path.lstat()
                state[str(path)] = (info.st_mtime_ns, path.read_bytes() if path.is_file() and not path.is_symlink()
                                    else None)
    return state


class CleanupTests(WorkerFixture, unittest.TestCase):
    def setUp(self):
        registry.register("test-cli", AdapterPolicy(requires_dispatch_scope=True, template_verified=True))
        self.addCleanup(registry.unregister, "test-cli")
        super().setUp()
        self.retain = 30
        self.custom_worktree = None

    def config(self):
        return SimpleNamespace(values={"privacy": {"retain_run_dirs_days": self.retain},
                                       "paths": {"worktree_root": self.trees}})

    def worktree(self):
        if self.custom_worktree is not None:
            return self.custom_worktree()
        return Path(prepare_worktree(self.repo, self.trees, self.next_run_id)["worktree_path"])

    def make_run(self, scenario="success", *, outcome="accepted"):
        """A finished Run: accepted, pending acceptance, failed, or left unfinished (COLLECTING)."""
        self.next_run_id = f"test-{uuid4().hex}"
        view = self.dispatch(scenario, run_id=self.next_run_id)
        if outcome in ("accepted", "pending"):
            self.assertEqual(self.verify()["state"], "SUCCEEDED")
        if outcome == "accepted":
            self.manager.record_acceptance(self.run_id, accepted=True, reason="synthetic", adopted=0, total=0)
        if outcome == "failed":
            self.assertEqual(view["state"], "FAILED")
        return self.run_id, self.workspace, self.directory()

    def clean(self, **options):
        return cleanup(self.manager, self.config(), **({"older_than_days": 0} | options))

    def entry(self, report, run_id):
        return next(item for item in report["runs"] if item["run_id"] == run_id)

    def registered(self):
        return subprocess.run(["git", "-C", str(self.repo), "worktree", "list", "--porcelain"], check=True,
                              capture_output=True, text=True, timeout=15).stdout

    # ---- deletion -----------------------------------------------------------------------------------
    def test_accepted_run_and_its_worktree_are_deleted_on_apply_only(self):
        run_id, workspace, directory = self.make_run()
        report = self.clean()
        self.assertEqual(self.entry(report, run_id)["action"], "delete")
        self.assertTrue(directory.is_dir() and workspace.is_dir())
        report = self.clean(apply=True)
        self.assertEqual(self.entry(report, run_id)["action"], "deleted")
        self.assertEqual(report["summary"]["runs"], 1)
        self.assertEqual(report["summary"]["worktrees"], 1)
        self.assertGreater(report["summary"]["bytes"], 0)
        self.assertFalse(directory.exists())
        self.assertFalse(workspace.exists())
        self.assertNotIn(str(workspace).replace("\\", "/").lower(), self.registered().lower())
        # The Ledger row stays as the audit record.
        status = self.manager.status(run_id)
        self.assertEqual((status["state"], status["acceptance_status"]), ("SUCCEEDED", "ACCEPTED"))
        self.assertEqual(status["task_evidence"], "missing")

    def test_snapshot_commit_worktree_is_verified_by_its_commit_message(self):
        (self.repo / "sample.txt").write_text("uncommitted change\n", encoding="utf-8")
        run_id, workspace, directory = self.make_run()
        subject = subprocess.run(["git", "-C", str(workspace), "log", "-1", "--format=%s"], check=True,
                                 capture_output=True, text=True, timeout=15).stdout.strip()
        self.assertEqual(subject, f"mah-snapshot:{run_id}")
        self.assertEqual(self.entry(self.clean(apply=True), run_id)["action"], "deleted")
        self.assertFalse(workspace.exists())

    def test_failed_run_with_pending_acceptance_is_cleanable(self):
        run_id, _, directory = self.make_run("schema", outcome="failed")
        self.assertEqual(self.ledger.get_run(run_id)["acceptance_status"], "PENDING")
        self.assertEqual(self.entry(self.clean(apply=True), run_id)["action"], "deleted")
        self.assertFalse(directory.exists())

    # ---- refusals -----------------------------------------------------------------------------------
    def assert_kept(self, run_id, reason, *paths, **options):
        for apply in (False, True):
            entry = self.entry(self.clean(apply=apply, **options), run_id)
            self.assertEqual((entry["action"], entry["reason"]), ("keep", reason))
        for path in paths:
            self.assertTrue(path.exists(), path)

    def test_unfinished_run_is_never_deleted(self):
        run_id, workspace, directory = self.make_run(outcome="unfinished")
        self.assertEqual(self.ledger.get_run(run_id)["state"], "COLLECTING")
        self.assert_kept(run_id, "run_not_finished", directory, workspace)

    def test_succeeded_run_with_pending_acceptance_is_never_deleted(self):
        run_id, workspace, directory = self.make_run(outcome="pending")
        self.assert_kept(run_id, "acceptance_pending", directory, workspace)

    def test_unexpired_run_is_kept(self):
        run_id, workspace, directory = self.make_run()
        self.assert_kept(run_id, "not_expired", directory, workspace, older_than_days=1)
        self.retain = 1
        entry = self.entry(cleanup(self.manager, self.config()), run_id)
        self.assertEqual(entry["reason"], "not_expired")

    def test_worktree_outside_the_run_id_path_keeps_the_run(self):
        self.custom_worktree = lambda: WorkerFixture.worktree(self)
        run_id, workspace, directory = self.make_run()
        self.assertNotEqual(workspace.name, run_id)
        self.assert_kept(run_id, "worktree_present", directory, workspace)

    def test_worktree_moved_to_another_commit_keeps_the_run(self):
        run_id, workspace, directory = self.make_run()
        (workspace / "new.txt").write_text("not the snapshot\n", encoding="utf-8")
        subprocess.run([*GIT, "-C", str(workspace), "add", "new.txt"], check=True, capture_output=True, timeout=15)
        subprocess.run([*GIT, "-C", str(workspace), "commit", "-q", "-m", "user work"], check=True,
                       capture_output=True, timeout=15)
        self.assert_kept(run_id, "worktree_present", directory, workspace)

    def test_failed_worktree_removal_keeps_the_run(self):
        run_id, workspace, directory = self.make_run()
        with patch("mah.cleanup._remove_worktree", return_value=False):
            entry = self.entry(self.clean(apply=True), run_id)
        self.assertEqual((entry["action"], entry["reason"]), ("keep", "worktree_present"))
        self.assertTrue(directory.is_dir())

    @unittest.skipUnless(os.name == "nt", "Junctions are a Windows feature")
    def test_run_directory_junction_is_refused(self):
        run_id, workspace, directory = self.make_run()
        moved = self.root / "moved-run"
        shutil.move(str(directory), moved)
        junction(directory, moved)
        self.addCleanup(lambda: os.path.lexists(directory) and os.rmdir(directory))
        entry = self.entry(self.clean(apply=True), run_id)
        self.assertEqual(entry["action"], "keep")
        self.assertIn(entry["reason"], ("outside_state", "link_refused"))
        self.assertTrue((moved / "task.md").is_file())
        self.assertTrue(workspace.is_dir())

    @unittest.skipUnless(os.name == "nt", "Junctions are a Windows feature")
    def test_link_inside_the_run_directory_is_refused(self):
        run_id, workspace, directory = self.make_run()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("must survive", encoding="utf-8")
        junction(directory / "link", outside)
        self.addCleanup(lambda: os.path.lexists(directory / "link") and os.rmdir(directory / "link"))
        self.assert_kept(run_id, "contains_link", directory, workspace)
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "must survive")

    # ---- dry run, settings, orphans and prune ---------------------------------------------------------
    def test_dry_run_writes_nothing(self):
        self.make_run()
        orphan = f"orphan-{uuid4().hex}"
        (self.repo / "sample.txt").write_text("snapshot change\n", encoding="utf-8")
        prepare_worktree(self.repo, self.trees, orphan)
        before = tree_state(self.state, self.trees, self.repo / ".git")
        report = self.clean()
        self.assertEqual(report["summary"]["runs"], 1)
        self.assertEqual(tree_state(self.state, self.trees, self.repo / ".git"), before)

    def test_zero_retention_lists_but_refuses_apply(self):
        run_id, _, directory = self.make_run()
        self.retain = 0
        report = cleanup(self.manager, self.config())
        self.assertTrue(report["disabled"])
        self.assertEqual(self.entry(report, run_id)["action"], "delete")
        for days in (None, 0, 5):
            with self.subTest(days=days), self.assertRaises(LedgerError) as error:
                cleanup(self.manager, self.config(), older_than_days=days, apply=True)
            self.assertEqual(error.exception.code, 2)
        self.assertTrue(directory.is_dir())

    def test_negative_days_are_refused(self):
        with self.assertRaises(LedgerError) as error:
            self.clean(older_than_days=-1)
        self.assertEqual(error.exception.code, 2)

    def test_orphan_snapshot_worktree_is_listed_and_removed(self):
        orphan = f"orphan-{uuid4().hex}"
        (self.repo / "sample.txt").write_text("snapshot change\n", encoding="utf-8")
        path = Path(prepare_worktree(self.repo, self.trees, orphan)["worktree_path"])
        entry = next(item for item in self.clean()["orphan_worktrees"] if item["worktree"] == str(path))
        self.assertEqual(entry["action"], "delete")
        entry = next(item for item in self.clean(apply=True)["orphan_worktrees"] if item["worktree"] == str(path))
        self.assertEqual(entry["action"], "deleted")
        self.assertFalse(path.exists())

    def test_orphan_without_a_snapshot_commit_is_kept(self):
        orphan = f"orphan-{uuid4().hex}"
        path = Path(prepare_worktree(self.repo, self.trees, orphan)["worktree_path"])
        for apply in (False, True):
            entry = next(item for item in self.clean(apply=apply)["orphan_worktrees"]
                         if item["worktree"] == str(path))
            self.assertEqual((entry["action"], entry["reason"]), ("keep", "snapshot_unverified"))
        self.assertTrue(path.is_dir())
        entry = next(item for item in self.clean(older_than_days=1)["orphan_worktrees"]
                     if item["worktree"] == str(path))
        self.assertEqual(entry["reason"], "not_expired")

    def stale_registration(self, parent):
        path = parent / f"stale-{uuid4().hex}"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "--detach", str(path), "HEAD"],
                       check=True, capture_output=True, timeout=15)
        shutil.rmtree(path)
        return path

    def test_prune_runs_only_when_every_prunable_entry_is_under_worktree_root(self):
        self.make_run()
        stale = self.stale_registration(self.trees)
        report = self.clean(apply=True)
        self.assertEqual(len(report["prune"]), 1)
        self.assertTrue(report["prune"][0]["pruned"])
        self.assertNotIn(stale.name, self.registered())

    def test_doctor_summary_counts_without_deleting(self):
        run_id, workspace, directory = self.make_run()
        self.make_run("schema", outcome="failed")
        config = SimpleNamespace(values=self.config().values, database=self.ledger.database)
        before = tree_state(self.state, self.trees)
        summary = doctor_summary(config)
        self.assertEqual((summary["runs"], summary["worktrees"], summary["older_than_days"], summary["disabled"]),
                         (0, 0, 30, False))
        self.retain = 0
        config = SimpleNamespace(values=self.config().values, database=self.ledger.database)
        summary = doctor_summary(config)
        self.assertEqual((summary["runs"], summary["worktrees"], summary["disabled"]), (2, 2, True))
        self.assertGreater(summary["bytes"], 0)
        self.assertEqual(tree_state(self.state, self.trees), before)
        self.assertTrue(directory.is_dir() and workspace.is_dir())

    def test_read_only_manager_never_creates_a_ledger(self):
        missing = SimpleNamespace(database=self.root / "absent" / "ledger.sqlite3")
        self.assertIsNone(read_only_manager(missing))
        self.assertFalse((self.root / "absent").exists())

    def test_prunable_entry_outside_worktree_root_blocks_prune(self):
        self.make_run()
        outside = self.root / "elsewhere"
        outside.mkdir()
        stale = self.stale_registration(outside)
        report = self.clean(apply=True)
        self.assertEqual(report["prune"][0]["reason"], "prunable_outside_worktree_root")
        self.assertFalse(report["prune"][0]["pruned"])
        self.assertIn(stale.name, self.registered())

    # ---- delete failures and the prune preview ------------------------------------------------------------
    @unittest.skipUnless(os.name == "nt", "An open handle locks a file only on Windows")
    def test_locked_file_keeps_the_run_as_delete_failed_and_cleanup_continues(self):
        locked_id, _, locked_directory = self.make_run()
        other_id, _, other_directory = self.make_run()
        with open(locked_directory / "task.md", "rb"):
            report = self.clean(apply=True)
        entry = self.entry(report, locked_id)
        self.assertEqual((entry["action"], entry["reason"]), ("keep", "delete_failed"))
        self.assertTrue(entry["delete_error"])
        self.assertTrue((locked_directory / "task.md").is_file())
        self.assertEqual(self.entry(report, other_id)["action"], "deleted")
        self.assertFalse(other_directory.exists())
        self.assertEqual((report["summary"]["runs"], report["summary"]["kept"]), (1, 1))
        self.assertEqual(self.entry(self.clean(apply=True), locked_id)["action"], "deleted")

    def test_delete_failure_is_reported_and_the_other_runs_are_still_deleted(self):
        failing_id, _, failing_directory = self.make_run()
        other_id, _, other_directory = self.make_run()
        real_rmtree = shutil.rmtree

        def rmtree(path, *args, **kwargs):
            if Path(path) == failing_directory:
                raise PermissionError(13, "locked", str(path))
            return real_rmtree(path, *args, **kwargs)

        with patch("mah.cleanup.shutil.rmtree", side_effect=rmtree):
            report = self.clean(apply=True)
        entry = self.entry(report, failing_id)
        self.assertEqual((entry["action"], entry["reason"]), ("keep", "delete_failed"))
        self.assertIn("locked", entry["delete_error"])
        self.assertTrue(failing_directory.is_dir())
        self.assertEqual(self.entry(report, other_id)["action"], "deleted")
        self.assertFalse(other_directory.exists())
        self.assertEqual(report["summary"]["kept"], 1)
        json.dumps(report, ensure_ascii=False)

    def test_listing_previews_the_prune_that_apply_runs(self):
        self.make_run()
        stale = self.stale_registration(self.trees)
        report = self.clean()
        self.assertEqual(len(report["prune"]), 1)
        self.assertEqual((report["prune"][0]["command"], report["prune"][0]["status"], report["prune"][0]["pruned"]),
                         ("git worktree prune", "將執行", False))
        self.assertIn(stale.name, self.registered())
        report = self.clean(apply=True)
        self.assertEqual((report["prune"][0]["status"], report["prune"][0]["pruned"]), ("已執行", True))
        self.assertNotIn(stale.name, self.registered())

    def test_listing_previews_the_prune_for_an_orphan_worktree_repository(self):
        orphan = f"orphan-{uuid4().hex}"
        (self.repo / "sample.txt").write_text("snapshot change\n", encoding="utf-8")
        prepare_worktree(self.repo, self.trees, orphan)
        stale = self.stale_registration(self.trees)
        before = tree_state(self.state, self.trees, self.repo / ".git")
        report = self.clean()
        self.assertEqual([item["status"] for item in report["prune"]], ["將執行"])
        self.assertEqual(tree_state(self.state, self.trees, self.repo / ".git"), before)
        self.assertIn(stale.name, self.registered())

    def test_listing_marks_a_blocked_prune_as_not_run(self):
        self.make_run()
        outside = self.root / "elsewhere"
        outside.mkdir()
        stale = self.stale_registration(outside)
        report = self.clean()
        self.assertEqual((report["prune"][0]["status"], report["prune"][0]["reason"]),
                         ("不執行", "prunable_outside_worktree_root"))
        self.assertIn(stale.name, self.registered())

    # ---- links, dirty trees and worktrees awaiting dispatch inside Disposable Worktrees ------------------
    def outside_files(self):
        outside = self.root / f"outside-{uuid4().hex}"
        (outside / "nested").mkdir(parents=True)
        (outside / "important.txt").write_text("must survive", encoding="utf-8")
        (outside / "nested" / "deep.txt").write_text("must survive too", encoding="utf-8")
        return outside

    def assert_outside_intact(self, outside):
        self.assertEqual((outside / "important.txt").read_text(encoding="utf-8"), "must survive")
        self.assertEqual((outside / "nested" / "deep.txt").read_text(encoding="utf-8"), "must survive too")

    def ignored_junction(self, workspace, outside, *parts):
        """A junction Git ignores, so git status stays clean and only the link scan can catch it."""
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        with exclude.open("a", encoding="utf-8") as file:
            file.write("ignored-junction\n")
        parent = workspace.joinpath(*parts)
        parent.mkdir(parents=True, exist_ok=True)
        link = parent / "ignored-junction"
        junction(link, outside)
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))
        return link

    def snapshot_orphan(self):
        orphan = f"orphan-{uuid4().hex}"
        (self.repo / "sample.txt").write_text(f"snapshot change {orphan}\n", encoding="utf-8")
        return Path(prepare_worktree(self.repo, self.trees, orphan)["worktree_path"])

    def orphan_entry(self, report, path):
        return next(item for item in report["orphan_worktrees"] if item["worktree"] == str(path))

    @unittest.skipUnless(os.name == "nt", "Junctions are a Windows feature")
    def test_junction_inside_a_failed_run_worktree_is_refused_and_outside_files_survive(self):
        run_id, workspace, directory = self.make_run("schema", outcome="failed")
        outside = self.outside_files()
        link = self.ignored_junction(workspace, outside)
        self.assertEqual(subprocess.run(["git", "-C", str(workspace), "status", "--porcelain"], check=True,
                                        capture_output=True, text=True, timeout=15).stdout, "")
        self.assert_kept(run_id, "worktree_contains_link", directory, workspace, link)
        self.assert_outside_intact(outside)
        report = self.clean(apply=True)
        self.assertEqual((report["summary"]["runs"], report["summary"]["worktrees"]), (0, 0))

    @unittest.skipUnless(os.name == "nt", "Junctions are a Windows feature")
    def test_junction_deep_inside_an_orphan_worktree_is_refused_and_outside_files_survive(self):
        path = self.snapshot_orphan()
        outside = self.outside_files()
        self.ignored_junction(path, outside, "a", "b", "c")
        for apply in (False, True):
            entry = self.orphan_entry(self.clean(apply=apply), path)
            self.assertEqual((entry["action"], entry["reason"]), ("keep", "worktree_contains_link"))
        self.assertTrue((path / "sample.txt").is_file())
        self.assert_outside_intact(outside)

    @unittest.skipUnless(os.name == "nt", "Junctions are a Windows feature")
    def test_deletion_never_follows_a_junction_that_appears_after_the_scan(self):
        """The deletion itself refuses links: a junction created between the scan and the delete stops it."""
        run_id, workspace, directory = self.make_run()
        path = self.snapshot_orphan()
        outside = self.outside_files()
        self.ignored_junction(workspace, outside, "late")
        self.ignored_junction(path, outside, "late")
        with patch("mah.cleanup._worktree_refusal", return_value=None):
            report = self.clean(apply=True)
        entry = self.entry(report, run_id)
        self.assertEqual((entry["action"], entry["reason"]), ("keep", "worktree_contains_link"))
        self.assertNotIn("worktree_removed", entry)
        self.assertTrue(directory.is_dir())
        entry = self.orphan_entry(report, path)
        self.assertEqual((entry["action"], entry["reason"]), ("keep", "worktree_contains_link"))
        self.assert_outside_intact(outside)
        self.assertEqual(report["summary"]["worktrees"], 0)

    def test_uncommitted_change_keeps_the_worktree(self):
        run_id, workspace, directory = self.make_run()
        (workspace / "sample.txt").write_text("user edit, not committed\n", encoding="utf-8")
        self.assert_kept(run_id, "worktree_dirty", directory, workspace)
        self.assertEqual((workspace / "sample.txt").read_text(encoding="utf-8"), "user edit, not committed\n")

    def test_untracked_file_keeps_the_worktree(self):
        run_id, workspace, directory = self.make_run()
        (workspace / "notes").mkdir()
        (workspace / "notes" / "user_notes.txt").write_text("review notes\n", encoding="utf-8")
        self.assert_kept(run_id, "worktree_dirty", directory, workspace)
        path = self.snapshot_orphan()
        (path / "debug.log").write_text("untracked\n", encoding="utf-8")
        for apply in (False, True):
            entry = self.orphan_entry(self.clean(apply=apply), path)
            self.assertEqual((entry["action"], entry["reason"]), ("keep", "worktree_dirty"))
        self.assertTrue((workspace / "notes" / "user_notes.txt").is_file() and (path / "debug.log").is_file())

    def test_ignored_and_read_only_files_are_deleted_with_a_clean_worktree(self):
        run_id, workspace, directory = self.make_run()
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text("__pycache__/\n", encoding="utf-8")
        (workspace / "__pycache__").mkdir()
        cached = workspace / "__pycache__" / "sample.pyc"
        cached.write_bytes(b"cache")
        os.chmod(cached, 0o444)
        report = self.clean(apply=True)
        self.assertEqual(self.entry(report, run_id)["action"], "deleted")
        self.assertFalse(workspace.exists() or directory.exists())
        self.assertNotIn(str(workspace).replace("\\", "/").lower(), self.registered().lower())

    def scoped_card(self, path):
        card_id = f"card-{uuid4().hex}"
        digest = self.ledger.create_card(
            card_id, repo_root=self.repo, task_key=card_id, trigger_ids=["T5"], prepare_mode="explicit",
            allowed_templates=["review"], allowed_targets=[{"adapter": self.adapter_name, "model": self.model,
                                                             "effort_max": "low"}],
            max_dispatches=1, ttl_minutes=10, timeout_seconds=30, workspace_path=path, task_sha256="0" * 64)
        return card_id, digest

    def test_orphan_worktree_of_an_open_card_awaits_dispatch(self):
        path = self.snapshot_orphan()
        other = self.snapshot_orphan()
        card_id, _ = self.scoped_card(path)
        for apply in (False, True):
            entry = self.orphan_entry(self.clean(apply=apply), path)
            self.assertEqual((entry["action"], entry["reason"]), ("keep", "awaiting_dispatch"))
        self.assertTrue(path.is_dir())
        # Only the worktree the card names is held back.
        self.assertFalse(other.exists())
        self.ledger.reject_card(card_id)
        self.assertEqual(self.orphan_entry(self.clean(apply=True), path)["action"], "deleted")
        self.assertFalse(path.exists())

    def test_orphan_worktree_of_an_active_grant_awaits_dispatch(self):
        path = self.snapshot_orphan()
        card_id, digest = self.scoped_card(path)
        grant_id = self.ledger.approve(card_id, digest, via="codex_prompt")
        self.assertEqual(self.ledger.get_card(card_id)["status"], "APPROVED")
        for apply in (False, True):
            entry = self.orphan_entry(self.clean(apply=apply), path)
            self.assertEqual((entry["action"], entry["reason"]), ("keep", "awaiting_dispatch"))
        self.assertTrue(path.is_dir())
        self.ledger.close(grant_id)
        self.assertEqual(self.orphan_entry(self.clean(apply=True), path)["action"], "deleted")

    # ---- report counts, uninspectable paths and the read-only Ledger ----------------------------------
    def test_removed_worktree_is_counted_when_the_run_directory_fails_to_delete(self):
        run_id, workspace, directory = self.make_run()
        with patch("mah.cleanup.shutil.rmtree", side_effect=PermissionError(13, "locked")):
            report = self.clean(apply=True)
        entry = self.entry(report, run_id)
        self.assertEqual((entry["action"], entry["reason"], entry["worktree_removed"]),
                         ("keep", "delete_failed", True))
        self.assertFalse(workspace.exists())
        self.assertTrue(directory.is_dir())
        self.assertEqual((report["summary"]["runs"], report["summary"]["worktrees"], report["summary"]["kept"]),
                         (0, 1, 1))

    def test_run_with_only_its_worktree_left_counts_no_run_directory(self):
        # A snapshot commit verifies the worktree without the Run directory's invocation.json.
        (self.repo / "sample.txt").write_text("uncommitted change\n", encoding="utf-8")
        run_id, workspace, directory = self.make_run()
        shutil.rmtree(directory)
        listing = self.clean()
        self.assertIsNone(self.entry(listing, run_id)["run_directory"])
        self.assertEqual((listing["summary"]["runs"], listing["summary"]["worktrees"]), (0, 1))
        report = self.clean(apply=True)
        self.assertEqual(self.entry(report, run_id)["action"], "deleted")
        self.assertEqual((report["summary"]["runs"], report["summary"]["worktrees"]), (0, 1))
        self.assertFalse(workspace.exists())

    def test_uninspectable_path_counts_as_a_link_and_cleanup_continues(self):
        blocked_id, blocked_workspace, blocked_directory = self.make_run()
        other_id, _, other_directory = self.make_run()
        real_lstat = os.lstat
        blocked = blocked_workspace / "sample.txt"

        def lstat(path, *args, **kwargs):
            if Path(path) == blocked:
                raise PermissionError(13, "access denied", str(path))
            return real_lstat(path, *args, **kwargs)

        with patch("mah.cleanup.os.lstat", side_effect=lstat):
            report = self.clean(apply=True)
        entry = self.entry(report, blocked_id)
        self.assertEqual((entry["action"], entry["reason"]), ("keep", "worktree_contains_link"))
        self.assertTrue(blocked.is_file() and blocked_directory.is_dir())
        self.assertEqual(self.entry(report, other_id)["action"], "deleted")
        self.assertFalse(other_directory.exists())

    def test_cleanup_opens_the_ledger_read_only(self):
        from mah import cleanup as module
        from mah.db import connect_read_only
        import sqlite3

        run_id, _, _ = self.make_run()
        opened = []

        def record(database):
            opened.append(connect_read_only(database))
            return opened[-1]

        with patch.object(module, "connect_read_only", side_effect=record):
            self.assertEqual(self.entry(self.clean(), run_id)["action"], "delete")
        self.assertEqual(len(opened), 1)
        with closing_db(connect_read_only(self.ledger.database)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE runs SET state_reason = 'x'")
        with self.assertRaises(sqlite3.OperationalError):
            connect_read_only(self.root / "absent.sqlite3").execute("SELECT 1")
        self.assertFalse((self.root / "absent.sqlite3").exists())


class CleanupCommandTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="mah-cleanup-cli-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def write_config(self, days):
        path = self.root / "harness.local.toml"
        path.write_text(f'[paths]\nstate_dir = "state"\nworktree_root = "worktrees"\n\n'
                        f"[privacy]\nretain_run_dirs_days = {days}\n", encoding="utf-8")
        return path

    def run_main(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--config", str(argv[0]), *argv[1:]])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_without_a_ledger_creates_nothing(self):
        config = self.write_config(30)
        code, out, _ = self.run_main(config, "cleanup")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual((report["apply"], report["runs"], report["older_than_days"]), (False, [], 30))
        self.assertFalse((self.root / "state").exists())
        code, out, _ = self.run_main(config, "cleanup", "--older-than-days", "7")
        self.assertEqual(json.loads(out)["older_than_days"], 7)

    def test_zero_retention_refuses_apply_with_exit_code_2(self):
        config = self.write_config(0)
        self.assertEqual(self.run_main(config, "cleanup")[0], 0)
        code, _, err = self.run_main(config, "cleanup", "--apply")
        self.assertEqual(code, 2)
        self.assertIn("retain_run_dirs_days = 0", err)

    def test_retention_setting_accepts_zero_but_not_negative(self):
        self.assertEqual(load_config(self.write_config(0)).values["privacy"]["retain_run_dirs_days"], 0)
        with self.assertRaises(ConfigError):
            load_config(self.write_config(-1))


class DoctorCleanupTests(test_cli.SkillInstallationTests):
    def test_doctor_reports_cleanable_data_without_failing(self):
        self.run_script("setup.ps1")
        output = self.assert_doctor_passes()
        line = next(item for item in output.splitlines() if "舊資料清理" in item)
        self.assertTrue(line.startswith("[通過] 舊資料清理"), output)
        self.assertIn("30 天", line)


def load_tests(loader, tests, pattern):
    """Run the tests defined here, not the ones DoctorCleanupTests inherits."""
    suite = unittest.TestSuite()
    for case in (CleanupTests, CleanupCommandTests, DoctorCleanupTests):
        for name in loader.getTestCaseNames(case):
            if name in vars(case):
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
