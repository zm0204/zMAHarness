"""Disposable Worktree cards bound to the snapshot HEAD (refactor spec 2e). Stand-in Worker only."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import uuid4

from mah.adapters import registry
from mah.adapters.base import AdapterPolicy
from mah.cli import read_card
from mah.context_bundle import _head
from mah.ledger import GrantLedger, LedgerError, digest

from test_cli_worker import WorkerFixture

GIT = ["git", "-c", "user.name=Harness Test", "-c", "user.email=harness@example.invalid"]


class SnapshotHeadTests(WorkerFixture, unittest.TestCase):
    def setUp(self):
        registry.register("test-cli", AdapterPolicy(requires_dispatch_scope=True, template_verified=True))
        self.addCleanup(registry.unregister, "test-cli")
        super().setUp()
        self.snapshot_head = None
        self.after_approval = None

    def grant(self, task, workspace, *, timeout=30):
        card_id = f"card-{uuid4().hex}"
        head = self.snapshot_head(workspace) if callable(self.snapshot_head) else self.snapshot_head
        self.card_sha = self.ledger.create_card(
            card_id, repo_root=self.repo, task_key=card_id, trigger_ids=["T5"], prepare_mode="explicit",
            allowed_templates=["review"],
            allowed_targets=[{"adapter": self.adapter_name, "model": self.model, "effort_max": "low"}],
            max_dispatches=1, ttl_minutes=10, timeout_seconds=timeout, workspace_path=workspace,
            task_sha256=sha256(task).hexdigest(), snapshot_head=head)
        self.card_id = card_id
        grant_id = self.ledger.approve(card_id, self.card_sha, via="codex_prompt")
        if self.after_approval is not None:
            self.after_approval(grant_id, workspace)
        return grant_id

    def scope(self, grant_id):
        return json.loads(self.ledger.get_grant(grant_id)["dispatch_scope_json"])

    def edit_scope(self, grant_id, change):
        scope = change(self.scope(grant_id))
        with closing(sqlite3.connect(self.ledger.database)) as db, db:
            db.execute("UPDATE grants SET dispatch_scope_json = ? WHERE grant_id = ?",
                       (json.dumps(scope, sort_keys=True), grant_id))

    def other_commit(self):
        (self.repo / "other.txt").write_text("another commit\n", encoding="utf-8")
        subprocess.run([*GIT, "-C", str(self.repo), "add", "other.txt"], check=True, capture_output=True)
        subprocess.run([*GIT, "-C", str(self.repo), "commit", "-q", "-m", "other"], check=True, capture_output=True)
        return _head(self.repo)

    def assert_refused_before_launch(self, code=10):
        with self.assertRaises(LedgerError) as error:
            self.dispatch()
        self.assertEqual(error.exception.code, code)
        self.assertEqual(self.started, [])
        return error.exception

    def test_matching_head_dispatches_and_binds_the_scope(self):
        self.snapshot_head = _head
        view = self.dispatch()
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "structured_result_collected"))
        self.assertEqual(self.scope(self.grant_id)["snapshot_head"], _head(self.workspace))
        content = json.loads(self.ledger.get_card(self.card_id)["content_json"])
        self.assertEqual(content["snapshot_head"], _head(self.workspace))
        self.assertEqual(self.verify()["state"], "SUCCEEDED")

    def test_checkout_after_approval_is_refused_at_claim_and_refunded(self):
        self.snapshot_head = _head
        other = _head(self.repo)
        self.other_commit()  # the worktree is cut from this newer commit and later moved back to `other`

        def checkout(grant_id, workspace):
            self.assertNotEqual(_head(workspace), other)
            subprocess.run(["git", "-C", str(workspace), "checkout", "-q", "--detach", other],
                           check=True, capture_output=True)

        self.after_approval = checkout
        error = self.assert_refused_before_launch()
        self.assertIn("snapshot_head", str(error))
        run = self.ledger.get_run(self.run_id)
        self.assertEqual((run["state"], run["state_reason"]), ("CANCELLED", "cancelled_before_launch"))
        self.assertEqual(self.ledger.get_grant(self.grant_id)["used_dispatches"], 0)
        self.assertFalse((self.directory() / "invocation.json").exists())

    def test_old_card_without_the_field_still_dispatches(self):
        view = self.dispatch()
        self.assertEqual(view["state"], "COLLECTING")
        self.assertNotIn("snapshot_head", self.scope(self.grant_id))
        card = self.ledger.get_card(self.card_id)
        self.assertNotIn("snapshot_head", json.loads(card["content_json"]))
        self.assertEqual(card["content_sha256"], digest(card["content_json"]))

    def test_card_without_the_field_keeps_its_hash(self):
        workspace = self.worktree()
        common = dict(repo_root=self.repo, task_key="same", trigger_ids=["T5"], prepare_mode="explicit",
                      allowed_templates=["review"],
                      allowed_targets=[{"adapter": "test-cli", "model": self.model, "effort_max": "low"}],
                      max_dispatches=1, ttl_minutes=10, timeout_seconds=30, workspace_path=workspace,
                      task_sha256="a" * 64)
        other = GrantLedger(self.root / "other.sqlite3")
        self.assertEqual(self.ledger.create_card("card-a", **common),
                         other.create_card("card-a", **common, snapshot_head=None))

    def test_malformed_head_is_refused_at_card_creation(self):
        workspace = self.worktree()
        for value in ("abc", "A" * 40, "g" * 40, "a" * 41, 12, ""):
            with self.subTest(value=value), self.assertRaises(LedgerError) as error:
                self.ledger.create_card(
                    f"card-{uuid4().hex}", repo_root=self.repo, task_key=f"t-{uuid4().hex}", trigger_ids=["T5"],
                    prepare_mode="explicit", allowed_templates=["review"],
                    allowed_targets=[{"adapter": "test-cli", "model": self.model, "effort_max": "low"}],
                    max_dispatches=1, ttl_minutes=10, timeout_seconds=30, workspace_path=workspace,
                    task_sha256="a" * 64, snapshot_head=value)
            self.assertEqual(error.exception.code, 2)

    def test_head_without_a_dispatch_scope_is_refused(self):
        with self.assertRaises(LedgerError) as error:
            self.ledger.create_card(
                "card-x", repo_root=self.repo, task_key="x", trigger_ids=["T5"], prepare_mode="explicit",
                allowed_templates=["review"], allowed_targets=[{"adapter": "fake", "model": "m", "effort_max": "low"}],
                max_dispatches=1, ttl_minutes=10, snapshot_head="a" * 40)
        self.assertEqual(error.exception.code, 2)

    def test_head_only_on_the_card_is_refused(self):
        self.snapshot_head = _head
        self.after_approval = lambda grant_id, workspace: self.edit_scope(
            grant_id, lambda scope: {key: value for key, value in scope.items() if key != "snapshot_head"})
        self.assert_refused_before_launch()
        self.assertIsNone(self.ledger.get_run(self.run_id))
        self.assertEqual(self.ledger.get_grant(self.grant_id)["used_dispatches"], 0)

    def test_head_only_on_the_grant_is_refused(self):
        self.after_approval = lambda grant_id, workspace: self.edit_scope(
            grant_id, lambda scope: scope | {"snapshot_head": _head(workspace)})
        self.assert_refused_before_launch()
        self.assertIsNone(self.ledger.get_run(self.run_id))

    def test_bundle_with_a_head_is_refused_at_claim(self):
        self.snapshot_head = "a" * 40
        with self.assertRaises(LedgerError) as error:
            self.dispatch(bundle=True)
        self.assertEqual(error.exception.code, 10)
        self.assertEqual(self.started, [])
        self.assertEqual(self.ledger.get_run(self.run_id)["state"], "CANCELLED")
        self.assertEqual(self.ledger.get_grant(self.grant_id)["used_dispatches"], 0)


class ReadCardTests(unittest.TestCase):
    def test_card_file_accepts_snapshot_head(self):
        with TemporaryDirectory(prefix="mah-card-") as scratch:
            path = Path(scratch) / "card.json"
            card = {"card_id": "c", "repo_root": scratch, "task_key": "k", "trigger_ids": ["T5"],
                    "prepare_mode": "explicit", "allowed_templates": ["review"],
                    "allowed_targets": [{"adapter": "agy-print", "model": "m", "effort_max": "low"}],
                    "max_dispatches": 1, "workspace_path": scratch, "task_sha256": "a" * 64,
                    "snapshot_head": "b" * 40}
            path.write_text(json.dumps(card), encoding="utf-8")
            parsed = read_card(path, SimpleNamespace(ledger_limits=SimpleNamespace(ttl_minutes=5)))
            self.assertEqual(parsed["snapshot_head"], "b" * 40)
            path.write_text(json.dumps(card | {"unexpected": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_card(path, SimpleNamespace(ledger_limits=SimpleNamespace(ttl_minutes=5)))


if __name__ == "__main__":
    unittest.main()
