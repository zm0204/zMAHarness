import json
import os
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from handoff_sim import LocalCoordinator
from mah.atomic_io import atomic_write
from mah.ledger import GrantLedger, LedgerError
from mah.run_manager import RunManager

ROOT = Path(__file__).resolve().parents[2]
TASK = "# 合成任務\n只檢查中文路徑。\n".encode()
CHILD = """
import os, sys
from mah.ledger import GrantLedger, LedgerError
from mah.run_manager import RunManager

database, run, action = sys.argv[1:]
ledger = GrantLedger(database)
manager = RunManager(ledger)
try:
    if action == "before_commit":
        transition = ledger._transition
        def interrupted(*args, **kwargs):
            transition(*args, **kwargs)
            os._exit(73)
        ledger._transition = interrupted
        manager.prepare(run, "# 合成任務\\n只檢查中文路徑。\\n".encode())
    elif action == "reserve_then_exit":
        from hashlib import sha256
        grant = ledger.list_grants()[0]["grant_id"]
        workspace = ledger.database.parent
        ledger.check_and_reserve(
            grant, run, repo_root=workspace, template="review", adapter="legacy-fixture",
            model="fixture", effort="low", workspace_kind="read_only",
            workspace_path=workspace, task_sha256=sha256(b"synthetic").hexdigest(),
        )
        os._exit(73)
    elif action == "launch_then_exit":
        manager.claim_launch(run)
        os._exit(73)
    elif action == "done_then_exit":
        from mah import run_manager
        original = run_manager.atomic_write
        def interrupted(path, content):
            original(path, content)
            if path.name == "DONE":
                os._exit(73)
        run_manager.atomic_write = interrupted
        manager.publish_result(run, {
            "schema_version": "2.0", "run_id": run, "template": "review",
            "status": "completed", "summary": "synthetic", "blocked_reason": None,
            "open_questions": [], "template_payload": {},
        })
    else:
        manager.claim_launch(run)
except LedgerError as error:
    sys.exit(error.code)
"""


def result(run):
    return {"schema_version": "2.0", "run_id": run, "template": "review",
            "status": "completed", "summary": "合成回覆", "blocked_reason": None,
            "open_questions": [], "template_payload": {}}


class RunManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "中文 狀態"
        self.state.mkdir()
        self.database = self.state / "ledger.sqlite3"
        self.ledger = GrantLedger(self.database)
        self.manager = RunManager(self.ledger)
        digest = self.ledger.create_card(
            "card-1", repo_root=self.state, task_key="review", trigger_ids=["T2"],
            prepare_mode="card", allowed_templates=["review"],
            allowed_targets=[{"adapter": "legacy-fixture", "model": "fixture", "effort_max": "low"}],
            max_dispatches=2, ttl_minutes=10,
        )
        self.grant = self.ledger.approve("card-1", digest, via="manual_cli")

    def reserve(self):
        return self.ledger.check_and_reserve(
            self.grant, f"review-{uuid4().hex}", repo_root=self.state,
            template="review", adapter="legacy-fixture", model="fixture", effort="low",
            workspace_kind="read_only", workspace_path=self.state,
            task_sha256=sha256(TASK).hexdigest(),
        )

    def child(self, run, action):
        return subprocess.run(
            [sys.executable, "-B", "-c", CHILD, str(self.database), run, action],
            cwd=ROOT, capture_output=True, timeout=15,
        )

    def prepared(self):
        run = self.reserve()
        self.manager.prepare(run, TASK)
        return run

    def running(self):
        run = self.prepared()
        self.manager.claim_launch(run)
        self.manager.mark_running(run, pid=os.getpid())
        return run

    def restart(self):
        return RunManager(GrantLedger(self.database))

    def state_of(self, run):
        return self.ledger.get_run(run)["state"]

    def debit(self):
        return self.ledger.get_grant(self.grant)["used_dispatches"]

    def events(self, run):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute(
                "SELECT from_state, to_state, detail_json FROM run_events WHERE run_id=? ORDER BY id", (run,),
            ).fetchall()

    def files(self, run):
        return self.state / "runs" / run

    def test_normal_flow_requires_verified_result_not_exit_code_or_worker_claim(self):
        run = self.running()
        self.assertEqual((self.files(run) / "task.md").read_bytes(), TASK)
        self.manager.record_exit(run, 0)
        self.assertEqual(self.manager.collect(run)["state"], "COLLECTING")
        digest = self.manager.publish_result(run, result(run))
        self.assertEqual(self.manager.collect(run)["state"], "COLLECTING")
        self.assertEqual(self.manager.collect(run, verified_sha256=digest)["state"], "SUCCEEDED")
        self.assertEqual(self.ledger.get_run(run)["acceptance_status"], "PENDING")
        self.assertEqual(self.debit(), 1)
        self.assertEqual([row[1] for row in self.events(run)],
                         ["RESERVED", "PREPARED", "LAUNCHING", "RUNNING", "COLLECTING", "SUCCEEDED"])

    def test_illegal_transition_and_immutable_task(self):
        run = self.reserve()
        with self.assertRaises(LedgerError):
            self.manager.claim_launch(run)
        with self.assertRaises(LedgerError):
            self.manager.prepare(run, b"changed")
        self.manager.prepare(run, TASK)
        self.manager.prepare(run, TASK)
        with self.assertRaises(LedgerError):
            self.manager.prepare(run, b"changed")
        self.assertEqual(len(self.events(run)), 2)

    def test_process_dies_after_reservation_before_task_can_be_cancelled_once(self):
        run = f"review-{uuid4().hex}"
        child = self.child(run, "reserve_then_exit")
        self.assertEqual(child.returncode, 73, child.stderr)
        manager = self.restart()
        self.assertEqual(manager.scan()[0]["state"], "RESERVED")
        self.assertEqual(self.debit(), 1)
        manager.cancel(run)
        with self.assertRaises(LedgerError):
            manager.cancel(run)
        self.assertEqual(self.debit(), 0)

    def test_interruption_before_sqlite_commit_recovers_published_task(self):
        run = self.reserve()
        child = self.child(run, "before_commit")
        self.assertEqual(child.returncode, 73, child.stderr)
        self.assertEqual(self.state_of(run), "RESERVED")
        self.assertEqual((self.files(run) / "task.md").read_bytes(), TASK)
        restarted = self.restart()
        self.assertEqual(restarted.scan()[0]["state"], "PREPARED")
        restarted.scan()
        self.assertEqual(self.debit(), 1)
        self.assertEqual(len(self.events(run)), 2)

    def test_orphaned_launch_is_unknown_and_cannot_launch_or_refund(self):
        run = self.prepared()
        child = self.child(run, "launch_then_exit")
        self.assertEqual(child.returncode, 73, child.stderr)
        self.assertEqual(self.state_of(run), "LAUNCHING")
        restarted = self.restart()
        self.assertEqual(restarted.scan()[0]["state"], "SUBMISSION_UNKNOWN")
        before = self.events(run)
        self.assertEqual(restarted.resolve(run)["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(restarted.resolve(run)["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(self.events(run), before)
        for action in (restarted.claim_launch, restarted.cancel):
            with self.assertRaises(LedgerError):
                action(run)
        restarted.resolve(run, action="abandon")
        self.assertEqual(self.debit(), 1)

    def test_active_launcher_and_worker_are_not_mistaken_for_orphans(self):
        run = self.prepared()
        self.manager.claim_launch(run)
        self.assertEqual(self.restart().scan()[0]["state"], "LAUNCHING")
        self.manager.mark_running(run, pid=os.getpid())
        self.assertEqual(self.restart().scan()[0]["state"], "RUNNING")

    def test_pid_reuse_does_not_prove_the_old_worker_is_alive(self):
        run = self.running()
        manager = RunManager(self.ledger, process_probe=lambda pid: ("alive", "other-start"))
        self.assertEqual(manager.scan()[0]["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(self.debit(), 1)

    def test_timeout_recovers_from_existing_result_without_another_dispatch(self):
        run = self.running()
        self.manager.timeout(run)
        self.assertEqual(self.state_of(run), "TIMED_OUT")
        digest = self.manager.publish_result(run, result(run))
        self.assertEqual(self.restart().resolve(run)["state"], "COLLECTING")
        self.assertEqual(self.restart().collect(run, verified_sha256=digest)["state"], "SUCCEEDED")
        self.assertEqual(self.debit(), 1)

    def test_done_failure_can_repeat_publication_but_partial_result_is_not_collected(self):
        run = self.running()
        from mah import run_manager

        original = run_manager.atomic_write

        def locked(path, content):
            if path.name == "DONE":
                raise PermissionError("simulated Windows file lock")
            original(path, content)

        with patch("mah.run_manager.atomic_write", side_effect=locked):
            with self.assertRaises(PermissionError):
                self.manager.publish_result(run, result(run))
        self.assertTrue((self.files(run) / "result.json").is_file())
        self.assertFalse((self.files(run) / "DONE").exists())
        self.assertEqual(self.restart().scan()[0]["state"], "RUNNING")
        with self.assertRaises((LedgerError, FileExistsError)):
            self.manager.publish_result(run, result(run) | {"summary": "另一份結果"})
        digest = self.manager.publish_result(run, result(run))
        self.assertEqual(self.manager.publish_result(run, result(run)), digest)
        self.assertEqual(self.restart().scan()[0]["state"], "COLLECTING")
        self.assertEqual(self.debit(), 1)

    def test_temporary_wrong_id_and_mismatched_hash_are_not_completion(self):
        run = self.running()
        directory = self.files(run)
        (directory / "DONE.tmp").write_bytes(b'{"run_id":')
        self.assertEqual(self.restart().scan()[0]["state"], "RUNNING")
        self.manager.publish_result(run, result(run))
        done = directory / "DONE"
        marker = json.loads(done.read_bytes())
        done.write_text(json.dumps(marker | {"run_id": "another-run"}), encoding="utf-8")
        self.assertEqual(self.restart().scan()[0]["state"], "RUNNING")
        done.write_text(json.dumps(marker), encoding="utf-8")
        (directory / "result.json").write_bytes(b'{"partial":true}')
        self.assertEqual(self.restart().scan()[0]["state"], "RUNNING")

    def test_policy_denial_with_exit_zero_is_failure_without_refund(self):
        run = self.running()
        self.manager.record_exit(run, 0)
        digest = self.manager.publish_result(run, result(run))
        self.assertEqual(self.manager.collect(run, verified_sha256=digest,
                                              required_action_denied=True)["state"], "FAILED")
        self.assertEqual(self.debit(), 1)
        self.assertEqual(self.ledger.get_run(run)["state_reason"], "required_action_denied")

    def test_competing_processes_only_claim_launch_once(self):
        run = self.prepared()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.child, run, "launch") for _ in range(2)]
            processes = [job.result() for job in jobs]
        self.assertEqual(sorted(p.returncode for p in processes), [0, 20],
                         [p.stderr for p in processes])
        self.assertEqual(sum(row[1] == "LAUNCHING" for row in self.events(run)), 1)
        self.assertEqual(self.debit(), 1)

    def test_resume_observation_keeps_old_launch_when_worker_identity_matches(self):
        run = self.running()
        self.manager.timeout(run)
        self.assertEqual(self.restart().scan()[0]["state"], "TIMED_OUT")
        self.assertEqual(self.restart().resolve(run)["state"], "RUNNING")
        with self.assertRaises(LedgerError):
            self.manager.claim_launch(run)
        self.assertEqual(self.debit(), 1)

    def test_inaccessible_process_does_not_count_as_a_gone_process(self):
        run = self.running()
        manager = RunManager(self.ledger, process_probe=lambda pid: ("unknown", None))
        status = manager.scan()[0]
        self.assertEqual(status["state"], "RUNNING")
        self.assertEqual(status["process_evidence"]["status"], "unknown")
        self.assertEqual(self.debit(), 1)

    def test_replacement_failure_keeps_previous_file_and_removes_temporary_file(self):
        run = self.prepared()
        task = self.files(run) / "task.md"
        with patch("pathlib.Path.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                atomic_write(task, b"replacement")
        self.assertEqual(task.read_bytes(), TASK)
        self.assertEqual(list(task.parent.glob("*.tmp")), [])
        self.assertEqual(self.state_of(run), "PREPARED")

    def test_schema_failure_and_unmatched_verification_never_succeed(self):
        run = self.running()
        self.manager.publish_result(run, result(run))
        with self.assertRaises(LedgerError) as failure:
            self.manager.collect(run, verified_sha256="0" * 64)
        self.assertEqual(failure.exception.code, 30)
        self.assertEqual(self.state_of(run), "RUNNING")
        directory = self.files(run)
        data = b'{"status":"completed"}'
        (directory / "result.json").write_bytes(data)
        marker = json.loads((directory / "DONE").read_bytes())
        marker["result_sha256"] = sha256(data).hexdigest()
        (directory / "DONE").write_text(json.dumps(marker), encoding="utf-8")
        completed = self.restart().collect(run)
        self.assertEqual((completed["state"], completed["state_reason"]), ("FAILED", "schema_invalid"))
        self.assertEqual(self.debit(), 1)

    def test_recorded_nonzero_exit_cannot_become_success(self):
        run = self.running()
        self.manager.record_exit(run, 17)
        digest = self.manager.publish_result(run, result(run))
        finished = self.manager.collect(run, verified_sha256=digest)
        self.assertEqual((finished["state"], finished["state_reason"]), ("FAILED", "process_failed"))
        self.assertEqual(self.debit(), 1)

    def test_scan_of_shared_database_leaves_v1_timeout_unchanged(self):
        legacy = self.state / "v1"
        legacy.mkdir()
        v1 = LocalCoordinator(legacy)
        with closing(v1.connect()) as db, db:
            db.execute("INSERT INTO receipts (task_id, fingerprint, status, detail) VALUES (?, ?, ?, ?)",
                       ("old", "digest", "TIMEOUT", "original"))
        before = tuple(v1.receipt("old"))
        self.assertEqual(RunManager(GrantLedger(v1.database)).scan(), [])
        self.assertEqual(tuple(v1.receipt("old")), before)
        self.assertEqual(v1.recover("old").status, "TIMEOUT")

    def test_cli_startup_scan_and_resolution_have_no_dispatch_path(self):
        # The CLI requires its state directory to be outside the Git workspace.
        fixture = TemporaryDirectory(dir=ROOT.parent)
        self.addCleanup(fixture.cleanup)
        state = Path(fixture.name) / "外部 狀態"
        state.mkdir()
        config = state.parent / "harness.local.toml"
        config.write_text("[paths]\nstate_dir = " + json.dumps(str(state)) + "\n", encoding="utf-8")
        self.state = state
        self.database = state / "ledger.sqlite3"
        self.ledger = GrantLedger(self.database)
        self.manager = RunManager(self.ledger)
        digest = self.ledger.create_card(
            "card-2", repo_root=self.state, task_key="review", trigger_ids=["T2"],
            prepare_mode="card", allowed_templates=["review"],
            allowed_targets=[{"adapter": "legacy-fixture", "model": "fixture", "effort_max": "low"}],
            max_dispatches=2, ttl_minutes=10,
        )
        self.grant = self.ledger.approve("card-2", digest, via="manual_cli")
        run = self.prepared()
        child = self.child(run, "launch_then_exit")
        self.assertEqual(child.returncode, 73, child.stderr)

        def cli(*args):
            return subprocess.run(
                [sys.executable, "-B", "-m", "mah", "--config", str(config), *args],
                cwd=ROOT, capture_output=True, encoding="utf-8", timeout=15,
            )

        status = cli("status", run)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(len(json.loads(cli("status", "--active").stdout)), 1)
        self.assertEqual(cli("collect", run).returncode, 21)
        self.assertEqual(cli("resolve", run, "--redispatch").returncode, 2)
        self.assertEqual(json.loads(cli("resolve", run, "--mark-failed").stdout)["state"], "FAILED")
        self.assertEqual(self.debit(), 1)

    def test_interruption_after_done_rename_recovers_committed_result(self):
        run = self.running()
        child = self.child(run, "done_then_exit")
        self.assertEqual(child.returncode, 73, child.stderr)
        self.assertEqual(self.state_of(run), "RUNNING")
        manager = self.restart()
        recovered = manager.scan()[0]
        self.assertEqual((recovered["state"], recovered["completion_evidence"]),
                         ("COLLECTING", "verified"))
        before = self.events(run)
        manager.scan()
        self.assertEqual(self.events(run), before)
        self.assertEqual(self.debit(), 1)

    def test_result_recovered_from_unknown_takes_priority_over_manual_failure(self):
        run = self.prepared()
        child = self.child(run, "launch_then_exit")
        self.assertEqual(child.returncode, 73, child.stderr)
        manager = self.restart()
        self.assertEqual(manager.scan()[0]["state"], "SUBMISSION_UNKNOWN")
        digest = manager.publish_result(run, result(run))
        self.assertEqual(manager.resolve(run, action="mark_failed")["state"], "COLLECTING")
        self.assertEqual(manager.collect(run, verified_sha256=digest)["state"], "SUCCEEDED")
        self.assertEqual(self.debit(), 1)

    def test_gui_launch_refund_requires_explicit_not_submitted_evidence(self):
        digest = self.ledger.create_card(
            "gui-card", repo_root=self.state, task_key="gui", trigger_ids=["T2"],
            prepare_mode="card", allowed_templates=["review"],
            allowed_targets=[{"adapter": "gui-terra", "model": "fixture", "effort_max": "low"}],
            max_dispatches=1, ttl_minutes=10,
        )
        self.grant = self.ledger.approve("gui-card", digest, via="manual_cli")
        run = self.ledger.check_and_reserve(
            self.grant, f"gui-{uuid4().hex}", repo_root=self.state,
            template="review", adapter="gui-terra", model="fixture", effort="low",
            workspace_kind="gui_fixed", workspace_path=self.state,
            task_sha256=sha256(TASK).hexdigest(),
        )
        self.manager.prepare(run, TASK)
        self.manager.claim_launch(run)
        with self.assertRaises(LedgerError):
            self.manager.cancel(run)
        self.manager.cancel(run, gui_not_submitted=True)
        self.assertEqual(self.state_of(run), "CANCELLED")
        self.assertEqual(self.ledger.get_run(run)["state_reason"], "gui_not_submitted")
        self.assertEqual(self.debit(), 0)
        self.assertEqual(self.ledger.get_grant(self.grant)["status"], "CLOSED")

    def test_keyboard_interrupt_in_prepare_cancels_and_closes_single_dispatch_grant(self):
        from types import SimpleNamespace

        digest = self.ledger.create_card(
            "interrupt-card", repo_root=self.state, task_key="interrupt", trigger_ids=["T2"],
            prepare_mode="card", allowed_templates=["review"],
            allowed_targets=[{"adapter": "legacy-fixture", "model": "fixture", "effort_max": "low"}],
            max_dispatches=1, ttl_minutes=10,
        )
        grant = self.ledger.approve("interrupt-card", digest, via="manual_cli")
        run_id = f"review-{uuid4().hex}"
        adapter = SimpleNamespace(
            id="legacy-fixture",
            manager=self.manager,
            prepare_invocation=lambda **_kwargs: SimpleNamespace(
                workspace_kind="read_only", cwd=self.state, timeout_s=None
            ),
        )
        with patch.object(self.manager, "prepare", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.dispatch(
                    adapter, grant_id=grant, run_id=run_id, repo_root=self.state,
                    template="review", model="fixture", effort="low", task=TASK,
                    workspace=self.state, worktree_root=self.state,
                )
        run = self.ledger.get_run(run_id)
        self.assertEqual((run["state"], run["state_reason"], run["dispatch_consumed"]),
                         ("CANCELLED", "cancelled_before_launch", 0))
        grant_row = self.ledger.get_grant(grant)
        self.assertEqual((grant_row["used_dispatches"], grant_row["status"]), (0, "CLOSED"))
        with self.assertRaises(LedgerError) as error:
            self.ledger.check_and_reserve(
                grant, f"review-{uuid4().hex}", repo_root=self.state, template="review",
                adapter="legacy-fixture", model="fixture", effort="low",
                workspace_kind="read_only", workspace_path=self.state,
                task_sha256=sha256(TASK).hexdigest(),
            )
        self.assertEqual(error.exception.code, 12)
