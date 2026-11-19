import json
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from uuid import uuid4

from handoff_sim import LocalCoordinator
from mah.ledger import GrantLedger, LedgerError, Limits


class LedgerTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.database = self.directory / "receipts.sqlite3"
        self.now = datetime(2026, 11, 11, tzinfo=UTC)
        self.ledger = GrantLedger(self.database, clock=lambda: self.now)

    def make_card(self, card_id="card-1", **overrides):
        options = dict(
            repo_root=self.directory,
            task_key="review-test",
            trigger_ids=["T2"],
            prepare_mode="card",
            allowed_templates=["review"],
            allowed_targets=[{"adapter": "legacy-fixture", "model": "fixture", "effort_max": "medium"}],
            max_dispatches=2,
            ttl_minutes=1,
        )
        return self.ledger.create_card(card_id, **(options | overrides))

    def approve(self, card_id="card-1", **overrides):
        return self.ledger.approve(
            card_id, self.make_card(card_id, **overrides), via="manual_cli"
        )

    def reserve(self, grant, run=None, **overrides):
        run = run or f"review-test-{uuid4().hex}"
        options = dict(
            repo_root=self.directory,
            template="review",
            adapter="legacy-fixture",
            model="fixture",
            effort="low",
            workspace_kind="read_only",
            workspace_path=self.directory,
            task_sha256=sha256(b"synthetic task content").hexdigest(),
        )
        return self.ledger.check_and_reserve(grant, run, **(options | overrides))

    def assert_code(self, code, action, *args, **kwargs):
        with self.assertRaises(LedgerError) as failure:
            action(*args, **kwargs)
        self.assertEqual(failure.exception.code, code)

    def count(self, table):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def launch(self, run):
        self.ledger.transition(run, "RESERVED", "PREPARED")
        self.ledger.transition(run, "PREPARED", "LAUNCHING")

    def test_unapproved_card_cannot_reserve(self):
        self.make_card()
        self.assert_code(10, self.reserve, "card-1")
        self.assertEqual(self.count("runs"), 0)

    def test_hash_and_approval_mode_are_required(self):
        digest = self.make_card()
        self.assert_code(10, self.ledger.approve, "card-1", "0" * 64, via="manual_cli")
        for mode in ("chat_ack", "codex_prompt"):
            with self.subTest(mode=mode):
                self.assert_code(10, self.ledger.approve, "card-1", digest, via=mode)
        self.assertEqual(self.count("grants"), 0)
        grant = self.ledger.approve("card-1", digest, via="manual_cli")
        self.assert_code(10, self.ledger.approve, "card-1", digest, via="manual_cli")
        self.assertEqual(self.ledger.get_grant(grant)["approved_via"], "manual_cli")

    def test_card_scope_digest_covers_stored_content(self):
        digest = self.make_card()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE cards SET content_json = ?", (json.dumps({"tampered": True}),))
        self.assert_code(10, self.ledger.approve, "card-1", digest, via="manual_cli")
        self.assertEqual(self.count("grants"), 0)

    def test_card_hard_limits(self):
        self.ledger = GrantLedger(self.database, limits=Limits(max_dispatches=2, ttl_minutes=3))
        for overrides in (
            {"max_dispatches": 3},
            {"ttl_minutes": 4},
            {"allowed_targets": [{"adapter": "legacy-fixture", "model": "fixture", "effort_max": "xhigh"}]},
        ):
            with self.subTest(overrides=overrides):
                self.assert_code(16, self.make_card, **overrides)
        self.assertEqual(self.count("cards"), 0)

    def test_agy_orchestrator_card_is_refused_before_it_is_stored(self):
        for model in ("gemini-pro-orchestrator-neon", "gemini-pro-orchestrator-g1-pro", "Gemini-Pro-Orchestrator-Neon"):
            with self.subTest(model=model):
                with self.assertRaises(LedgerError) as failure:
                    self.make_card(allowed_targets=[{"adapter": "agy-print", "model": model, "effort_max": "high"}])
                self.assertEqual(failure.exception.code, 2)
                self.assertIn(f"Orchestrator 模型 {model}", str(failure.exception))
                self.assertIn("子代理", str(failure.exception))
        self.assertEqual(self.count("cards"), 0)

    def test_agy_neon_hydrogen_and_other_adapters_are_not_refused(self):
        scope = dict(timeout_seconds=60, workspace_path=self.directory, task_sha256=sha256(b"task").hexdigest())
        for index, (adapter, model) in enumerate((
                ("agy-print", "gemini-4-neon-high"), ("agy-print", "gemini-4-hydrogen-high"),
                ("agy-print", "gemini-pro-neon"), ("legacy-fixture", "gemini-pro-orchestrator-neon"))):
            with self.subTest(adapter=adapter, model=model):
                self.make_card(f"card-ok-{index}", task_key=f"ok-{index}", **scope,
                               allowed_targets=[{"adapter": adapter, "model": model, "effort_max": "high"}])
        self.assertEqual(self.count("cards"), 4)

    def test_expiration_blocks_at_boundary(self):
        grant = self.approve()
        self.now += timedelta(minutes=1)
        self.assert_code(12, self.reserve, grant)
        self.assertEqual(self.ledger.get_grant(grant)["status"], "EXPIRED")
        self.assertEqual(self.count("runs"), 0)

    def test_scope_checks_each_dimension_before_debit(self):
        grant = self.approve()
        for overrides in (
            {"repo_root": self.directory / "other"},
            {"template": "research"}, {"adapter": "agy-print"},
            {"model": "alternate"}, {"effort": "high"}, {"effort": "unknown"},
        ):
            with self.subTest(overrides=overrides):
                self.assert_code(10, self.reserve, grant, **overrides)
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 0)
        self.assertEqual(self.count("runs"), 0)

    def test_new_card_supersedes_only_the_same_repo(self):
        old = self.approve("old")
        run = self.reserve(old)
        other = self.approve("other", repo_root=self.directory / "other")
        current = self.approve("current")
        self.assertEqual(self.ledger.get_grant(old)["superseded_by"], current)
        self.assertEqual(self.ledger.get_grant(old)["status"], "SUPERSEDED")
        self.assertEqual(self.ledger.get_grant(other)["status"], "ACTIVE")
        self.assertEqual(self.ledger.get_grant(current)["used_dispatches"], 0)
        self.assert_code(10, self.reserve, old)
        self.assert_code(13, self.reserve, current)
        self.ledger.transition(run, "RESERVED", "CANCELLED")
        self.assertEqual(self.ledger.get_grant(old)["status"], "SUPERSEDED")

    def test_closed_grant_is_unavailable(self):
        grant = self.approve()
        self.ledger.close(grant)
        self.assertEqual(self.ledger.get_grant(grant)["status"], "CLOSED")
        self.assert_code(12, self.reserve, grant)

    def test_cancelled_run_cannot_be_reused_or_refunded_twice(self):
        grant = self.approve()
        run = self.reserve(grant)
        self.ledger.transition(run, "RESERVED", "CANCELLED")
        self.assert_code(20, self.ledger.transition, run, "RESERVED", "CANCELLED")
        self.assert_code(2, self.reserve, grant, run)
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 0)
        self.assertEqual(self.ledger.get_run(run)["dispatch_consumed"], 0)
        self.assertEqual(self.count("run_events"), 2)

    def test_launch_is_a_one_time_transition(self):
        grant = self.approve()
        run = self.reserve(grant)
        self.launch(run)
        self.assert_code(20, self.ledger.transition, run, "PREPARED", "LAUNCHING")
        self.assert_code(20, self.ledger.transition, run, "LAUNCHING", "CANCELLED")
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 1)

    def test_unknown_run_survives_restart_and_blocks_new_reservation(self):
        grant = self.approve(max_dispatches=2)
        run = self.reserve(grant)
        self.launch(run)
        self.ledger.transition(run, "LAUNCHING", "SUBMISSION_UNKNOWN")
        self.ledger = GrantLedger(self.database, clock=lambda: self.now)
        self.assertEqual(self.ledger.get_run(run)["state"], "SUBMISSION_UNKNOWN")
        self.assert_code(13, self.reserve, grant)
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 1)

    def test_abandon_unknown_keeps_debit_and_exhausts_grant(self):
        grant = self.approve(max_dispatches=1)
        run = self.reserve(grant)
        self.launch(run)
        self.ledger.transition(run, "LAUNCHING", "SUBMISSION_UNKNOWN")
        self.ledger.transition(run, "SUBMISSION_UNKNOWN", "ABANDONED")
        self.assert_code(11, self.reserve, grant)
        self.assertEqual(self.ledger.get_grant(grant)["status"], "EXHAUSTED")
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 1)

    def test_only_recognized_pre_submit_failure_returns_debit(self):
        grant = self.approve(max_dispatches=1)
        run = self.reserve(grant)
        self.launch(run)
        self.assert_code(
            20, self.ledger.transition, run, "LAUNCHING", "FAILED_PRE_SUBMIT", reason="blocked_by_policy"
        )
        self.ledger.transition(run, "LAUNCHING", "FAILED_PRE_SUBMIT", reason="auth_missing")
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 0)
        self.assertEqual(self.ledger.get_run(run)["dispatch_consumed"], 0)
        self.assertNotEqual(self.reserve(grant), run)

    def test_run_insert_and_debit_roll_back_when_event_insert_fails(self):
        grant = self.approve()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""
                CREATE TRIGGER synthetic_failure BEFORE INSERT ON run_events
                BEGIN SELECT RAISE(ABORT, 'synthetic event failure'); END
            """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic event failure"):
            self.reserve(grant)
        self.assertEqual(self.count("runs"), 0)
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 0)

    def test_transition_and_refund_roll_back_when_event_insert_fails(self):
        grant = self.approve()
        run = self.reserve(grant)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""
                CREATE TRIGGER synthetic_failure BEFORE INSERT ON run_events
                BEGIN SELECT RAISE(ABORT, 'synthetic event failure'); END
            """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic event failure"):
            self.ledger.transition(run, "RESERVED", "CANCELLED")
        self.assertEqual(self.ledger.get_run(run)["state"], "RESERVED")
        self.assertEqual(self.ledger.get_run(run)["dispatch_consumed"], 1)
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 1)

    def test_concurrent_reservations_have_one_winner(self):
        self.ledger = GrantLedger(self.database)
        grant = self.approve()
        gate = Barrier(2, timeout=5)

        script = """
import sys
from hashlib import sha256
from uuid import uuid4
from mah.ledger import GrantLedger, LedgerError

database, repo, grant = sys.argv[1:]
try:
    GrantLedger(database).check_and_reserve(
        grant, f"review-test-{uuid4().hex}", repo_root=repo, template="review",
        adapter="legacy-fixture", model="fixture", effort="low", workspace_kind="read_only",
        workspace_path=repo, task_sha256=sha256(b"synthetic").hexdigest(),
    )
except LedgerError as exc:
    raise SystemExit(exc.code)
"""

        def contender():
            gate.wait()
            return subprocess.run(
                [sys.executable, "-B", "-c", script, str(self.database), str(self.directory), grant],
                cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=10, check=False,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contender) for _ in range(2)]
            processes = [future.result() for future in futures]
        self.assertEqual(sorted(process.returncode for process in processes), [0, 13],
                         [process.stderr for process in processes])
        self.assertEqual(self.count("runs"), 1)
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 1)

    def test_failed_approval_leaves_old_grant_active_and_card_pending(self):
        old_grant = self.approve("old")
        digest = self.make_card("current")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""
                CREATE TRIGGER synthetic_failure BEFORE INSERT ON grants
                BEGIN SELECT RAISE(ABORT, 'synthetic approval failure'); END
            """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic approval failure"):
            self.ledger.approve("current", digest, via="manual_cli")
        self.assertEqual(self.ledger.get_grant(old_grant)["status"], "ACTIVE")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute(
                "SELECT status FROM cards WHERE card_id = 'current'"
            ).fetchone(), ("PROPOSED",))

    def test_four_column_legacy_schema_is_not_migrated_by_v2(self):
        legacy = self.directory / "original"
        legacy.mkdir()
        path = legacy / "receipts.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE receipts (
                task_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL
            )""")
            db.execute("INSERT INTO receipts VALUES ('old', 'digest', 'TIMEOUT', 'original')")
            original_schema = db.execute("PRAGMA table_info(receipts)").fetchall()
        GrantLedger(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA table_info(receipts)").fetchall(), original_schema)
        v1 = LocalCoordinator(legacy)
        self.assertEqual(v1.recover("old").status, "TIMEOUT")
        self.assertEqual(len(list(legacy.glob("receipts.sqlite3.v1.bak.*"))), 1)

    def test_existing_v1_receipts_are_backed_up_without_changes(self):
        legacy = self.directory / "legacy"
        legacy.mkdir()
        v1 = LocalCoordinator(legacy)
        with closing(v1.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "INSERT INTO receipts (task_id, fingerprint, status, detail) VALUES (?, ?, ?, ?)",
                ("old", "digest", "TIMEOUT", "original"),
            )
            db.commit()
            before = db.execute("SELECT * FROM receipts").fetchone()
            GrantLedger(v1.database)
            GrantLedger(v1.database)
        self.assertEqual(tuple(v1.receipt("old")), tuple(before))
        self.assertEqual(v1.recover("old").status, "TIMEOUT")
        backups = list(legacy.glob("receipts.sqlite3.v1.bak.*"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("SELECT * FROM receipts").fetchone(), tuple(before))
            self.assertIsNone(backup.execute(
                "SELECT name FROM sqlite_master WHERE name = 'grants'"
            ).fetchone())


if __name__ == "__main__":
    unittest.main()
