"""mah cleanup and privacy.retain_run_dirs_days (refactor spec 2d). Stand-in Worker only."""
from contextlib import redirect_stderr, redirect_stdout
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
from mah.cleanup import cleanup
from mah.cli import main
from mah.config import ConfigError, load_config
from mah.context_bundle import prepare_worktree
from mah.ledger import LedgerError

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
                              capture_output=True, text=True).stdout

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
                                 capture_output=True, text=True).stdout.strip()
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
        subprocess.run([*GIT, "-C", str(workspace), "add", "new.txt"], check=True, capture_output=True)
        subprocess.run([*GIT, "-C", str(workspace), "commit", "-q", "-m", "user work"], check=True,
                       capture_output=True)
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
                       check=True, capture_output=True)
        shutil.rmtree(path)
        return path

    def test_prune_runs_only_when_every_prunable_entry_is_under_worktree_root(self):
        self.make_run()
        stale = self.stale_registration(self.trees)
        report = self.clean(apply=True)
        self.assertEqual(len(report["prune"]), 1)
        self.assertTrue(report["prune"][0]["pruned"])
        self.assertNotIn(stale.name, self.registered())

    def test_prunable_entry_outside_worktree_root_blocks_prune(self):
        self.make_run()
        outside = self.root / "elsewhere"
        outside.mkdir()
        stale = self.stale_registration(outside)
        report = self.clean(apply=True)
        self.assertEqual(report["prune"][0]["reason"], "prunable_outside_worktree_root")
        self.assertFalse(report["prune"][0]["pruned"])
        self.assertIn(stale.name, self.registered())


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


if __name__ == "__main__":
    unittest.main()
