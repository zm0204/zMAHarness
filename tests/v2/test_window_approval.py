import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mah.db import SCHEMA
from mah.fake_adapter import FakeAdapter
from mah.ledger import GrantLedger, LedgerError
from mah.run_manager import RunManager
from mah.verifier import VerificationEvidence, Verifier
from mah.window_approval import review_window


REPO = Path(__file__).resolve().parents[2]
RUN_ID = "desktop-window-" + "a" * 32
TASK = "離線合成審查任務".encode()


class WindowApprovalTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="mah-window-", dir=REPO.parent)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "ledger.sqlite3"
        self.config = self.root / "harness.local.toml"
        self.config.write_bytes(b'[paths]\nstate_dir = "."\n')
        self.now = datetime.now(UTC)
        self.ledger = GrantLedger(self.database, clock=lambda: self.now)
        self.manager = RunManager(self.ledger)
        self.workspace = self.manager._directory(RUN_ID) / "workspace"
        self.workspace.mkdir(parents=True)
        self.adapter = FakeAdapter(payload={"findings": []})

    def card(self, card_id="window-1", *, adapter="fake", max_dispatches=1):
        return self.ledger.create_card(
            card_id, repo_root=self.source, task_key=card_id, trigger_ids=["T5"], prepare_mode="explicit",
            allowed_templates=["review"], allowed_targets=[{"adapter": adapter, "model": "synthetic", "effort_max": "low"}],
            ttl_minutes=1, max_dispatches=max_dispatches, timeout_seconds=10, workspace_path=self.workspace,
            task_sha256=sha256(TASK).hexdigest(),
        )

    def answer(self, decision, digest, card_id="window-1"):
        with patch("mah.window_approval.show_window", return_value=decision):
            return review_window(self.ledger, card_id, digest)

    def dispatch(self, grant, **changes):
        options = dict(repo_root=self.source, template="review", model="synthetic", effort="low",
                       task=TASK, timeout_seconds=10)
        return self.adapter.dispatch(self.manager, grant, RUN_ID, **(options | changes))

    def test_approval_binds_scope_once_and_fake_requires_independent_verification(self):
        digest = self.card()
        grant_id = self.answer("approved", digest)
        grant = self.ledger.get_grant(grant_id)
        self.assertEqual((grant["approved_via"], grant["max_dispatches"]), ("windows_gui", 1))
        self.assertEqual(grant["approval_evidence"], "local_window_interaction")
        self.assertEqual(json.loads(grant["dispatch_scope_json"])["card_sha256"], digest)
        for changes in ({"task": b"changed"}, {"timeout_seconds": 11}, {"model": "other"}):
            with self.subTest(changes=changes), self.assertRaises(LedgerError):
                self.dispatch(grant_id, **changes)
        self.assertEqual(self.ledger.get_grant(grant_id)["used_dispatches"], 0)
        self.assertEqual(self.dispatch(grant_id)["state"], "COLLECTING")
        verifier = Verifier(self.manager)
        self.assertEqual(verifier.verify(RUN_ID)["state"], "COLLECTING")
        result = self.manager._directory(RUN_ID) / "result.json"
        evidence = VerificationEvidence(sha256(TASK).hexdigest(), sha256(result.read_bytes()).hexdigest(),
                                        confirmed=True, workspace_changed=False, reviewed_findings=0)
        complete = verifier.verify(RUN_ID, evidence=evidence)
        self.assertEqual((complete["state"], complete["acceptance_status"]), ("SUCCEEDED", "PENDING"))
        self.assertEqual(self.ledger.get_grant(grant_id)["used_dispatches"], 1)
        with self.assertRaises(LedgerError):
            self.answer("approved", digest)

    def test_decline_cancel_and_window_timeout_do_not_grant(self):
        for decision, status in (("rejected", "REJECTED"), ("cancelled", "PROPOSED"), ("expired", "PROPOSED")):
            with self.subTest(decision=decision):
                digest = self.card(decision)
                with self.assertRaises(LedgerError):
                    self.answer(decision, digest, card_id=decision)
                self.assertEqual(self.ledger.get_card(decision)["status"], status)
        self.assertEqual(self.ledger.list_grants(), [])

    def test_expired_window_and_expired_grant_do_not_dispatch(self):
        digest = self.card()
        with patch("mah.window_approval.show_window", return_value="approved"), patch(
                "mah.window_approval.monotonic", side_effect=[0, 10000]):
            with self.assertRaises(LedgerError):
                review_window(self.ledger, "window-1", digest)
        self.assertEqual(self.ledger.list_grants(), [])
        grant = self.answer("approved", digest)
        self.now += timedelta(minutes=1)
        with patch.object(self.adapter, "launch", side_effect=AssertionError("must not start")):
            with self.assertRaises(LedgerError) as error:
                self.dispatch(grant)
        self.assertEqual(error.exception.code, 12)
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 0)

    def test_reopened_window_recovers_from_transaction_failure(self):
        digest = self.card()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TRIGGER fail_window BEFORE INSERT ON grants BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.answer("approved", digest)
        self.assertEqual(self.ledger.get_card("window-1")["status"], "PROPOSED")
        self.assertEqual(self.ledger.list_grants(), [])
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TRIGGER fail_window")
        self.assertIsNotNone(self.answer("approved", digest))
        self.assertEqual(len(self.ledger.list_grants()), 1)

    def test_untrusted_parameters_and_scope_do_not_open_the_window(self):
        digest = self.card()
        with self.assertRaises(LedgerError):
            self.ledger.approve("window-1", digest, via="windows_gui", evidence="local_window_interaction")
        args = [sys.executable, "-B", "-m", "mah", "--config", str(self.config), "grant"]
        for command in (["approve", "window-1", "--sha", digest, "--via", "windows_gui"],
                        ["approve-window", "window-1", "--sha", digest, "--decision", "approved"]):
            result = subprocess.run(args + command, input="approved", encoding="utf-8",
                                    capture_output=True, cwd=REPO, timeout=10)
            self.assertNotEqual(result.returncode, 0)
        with patch("mah.window_approval.show_window", side_effect=AssertionError("must not open")):
            for card_id, card_hash in (("window-1", "0" * 64),
                                       ("non-fake", self.card("non-fake", adapter="agy-print")),
                                       ("multi", self.card("multi", max_dispatches=2))):
                with self.subTest(card_id=card_id), self.assertRaises(LedgerError):
                    review_window(self.ledger, card_id, card_hash)
        self.assertEqual(self.ledger.list_grants(), [])

    def test_v2_migration_preserves_grant_and_run_references(self):
        self.card()
        old = self.ledger.approve("window-1", self.ledger.get_card("window-1")["content_sha256"], via="manual_cli")
        self.dispatch(old)
        # Rebuild a test copy with the old CHECK constraint.
        legacy = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as db, db:
            for statement in SCHEMA:
                db.execute(statement.replace(", 'windows_gui'", ""))
            db.execute("INSERT INTO schema_meta VALUES ('ledger_schema_version', '2')")
            db.execute("ATTACH DATABASE ? AS current", (str(self.database),))
            for table in ("cards", "grants", "runs", "run_events"):
                db.execute(f"INSERT INTO {table} SELECT * FROM current.{table}")
        migrated = GrantLedger(legacy)
        self.assertEqual(migrated.get_grant(old)["approved_via"], "manual_cli")
        self.assertEqual(migrated.get_run(RUN_ID)["grant_id"], old)
        with closing(sqlite3.connect(legacy)) as db, db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
