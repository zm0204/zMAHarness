"""Sealing a process receipt from the .pending streams after mah was hard-killed (refactor spec 2b).

mah's death is simulated by an observe() that waits for the stand-in Worker and then raises a BaseException,
so neither observe nor persist_capture finishes. Recovery runs with Popen replaced by a stub that fails the
test: no step may start a process.
"""
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch

from mah.adapters import registry
from mah.adapters.agy_print import AgyCapture
from mah.adapters.base import AdapterPolicy
from mah.adapters.cli_worker import CliCapture, check_recovered
from mah.cli import main
from mah.ledger import LedgerError
from mah.process_identity import probe_process
from mah.run_manager import RunManager

import test_agy_process
from test_cli_worker import WorkerFixture


class HardKill(BaseException):
    """mah is terminated while it waits for the Worker."""


def killed_observe(*, kill_after=None):
    """Wait for the Worker (or, with kill_after, until that file has content, then kill it) and die."""
    def observe(handle):
        if kill_after is not None:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not (kill_after.is_file() and kill_after.stat().st_size):
                time.sleep(0.05)
            handle.process.kill()
        handle.process.wait(timeout=30)
        if getattr(handle, "job", None) is not None:
            handle.job.close()  # the OS closes it when mah dies
        raise HardKill
    return observe


def recorded_worker(database, run_id):
    with closing(sqlite3.connect(database)) as db:
        detail = db.execute("SELECT detail_json FROM run_events WHERE run_id = ? AND to_state = 'RUNNING' "
                            "ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()[0]
    return json.loads(detail)["process"]


def files_of(directory):
    return {item.name: item.read_bytes() for item in sorted(directory.iterdir()) if item.is_file()}


class NoLaunch:
    """Both Popen names the CLI Worker lifecycle can reach."""

    def __enter__(self):
        self.patches = [patch(target, side_effect=AssertionError("recovery must not start a process"))
                        for target in ("mah.adapters.cli_worker.Popen", "mah.adapters.agy_print.Popen")]
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in self.patches:
            item.stop()


class ReceiptCheckTests(unittest.TestCase):
    def test_a_recovered_receipt_cannot_claim_an_exit_or_a_launch_error(self):
        base = CliCapture("r", "t", "i", b"", b"", None, termination="interrupted", recovered="pending")
        check_recovered(base)
        for changes in ({"exit_code": 0}, {"termination": "exited"}, {"termination": "timeout"},
                        {"launch_error": "process_launch_unknown"}, {"wall_milliseconds": 5},
                        {"recovered": "yes"}):
            with self.subTest(changes=changes), self.assertRaises(LedgerError) as error:
                check_recovered(replace(base, **changes))
            self.assertEqual(error.exception.code, 30)
        check_recovered(replace(base, recovered=None, exit_code=0, termination="exited"))
        agy = AgyCapture("r", "t", "i", b"", b"", None, termination="interrupted", recovered="pending")
        check_recovered(agy)
        with self.assertRaises(LedgerError):
            check_recovered(replace(agy, pre_submit_reason="process_not_started"))


class PendingRecoveryTests(WorkerFixture, unittest.TestCase):
    def setUp(self):
        registry.register("test-cli", AdapterPolicy(requires_dispatch_scope=True, template_verified=True))
        self.addCleanup(registry.unregister, "test-cli")
        super().setUp()
        self.worker_status = "gone"

    def kill_mah(self, scenario="success"):
        with patch.object(self.adapter, "observe", side_effect=killed_observe()), \
                self.assertRaises(HardKill):
            self.dispatch(scenario)
        run = self.ledger.get_run(self.run_id)
        self.assertEqual(run["state"], "RUNNING")
        self.assertFalse((self.directory() / "testcli.process.json").exists())
        self.worker = recorded_worker(self.manager.database, self.run_id)
        # The launching mah (this test process) is reported gone; the Worker as the test chooses.
        self.manager.process_probe = self.probe

    def probe(self, pid):
        if pid == os.getpid():
            return "gone", None
        if pid == self.worker["pid"]:
            return {"gone": ("gone", None), "alive": ("alive", self.worker["identity"]),
                    "reused": ("alive", "win:reused"), "unknown": ("unknown", None)}[self.worker_status]
        return probe_process(pid)

    def recover(self):
        with NoLaunch():
            return self.adapter.collect_saved(self.run_id)

    def process_record(self):
        return json.loads((self.directory() / "testcli.process.json").read_bytes())

    def test_complete_output_is_recovered_and_verified(self):
        self.kill_mah()
        view = self.recover()
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "recovered_result_collected"))
        record = self.process_record()
        self.assertEqual((record["recovered"], record["termination"], record["exit_code"]),
                         ("pending", "interrupted", None))
        self.assertEqual(record["process"], self.worker)
        self.assertIsNone(record["wall_milliseconds"])
        receipt = view["testcli_capture"]
        self.assertEqual((receipt["classification"], receipt["reason"]), ("COLLECTING", "recovered_result_collected"))
        self.assertEqual(receipt["process_receipt_sha256"],
                         sha256((self.directory() / "testcli.process.json").read_bytes()).hexdigest())
        self.assertIsNone(view["exit_code"])
        self.assertEqual(self.verify()["state"], "SUCCEEDED")
        self.assertEqual(len(self.started), 1)

    def test_recovery_after_scan_marked_the_worker_gone(self):
        self.kill_mah()
        self.manager.scan()
        run = self.ledger.get_run(self.run_id)
        self.assertEqual((run["state"], run["state_reason"]), ("SUBMISSION_UNKNOWN", "process_gone"))
        view = self.recover()
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "recovered_result_collected"))
        self.assertEqual(self.verify()["state"], "SUCCEEDED")

    def resolve_cli(self, *args):
        """mah resolve in this process, with this fixture's Adapter standing in for the registered one."""
        config = self.root / "harness.local.toml"
        config.write_text("[paths]\nstate_dir = " + json.dumps(str(self.state)) + "\nworktree_root = "
                          + json.dumps(str(self.trees)) + "\n", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with NoLaunch(), patch("mah.cli.has_factory", return_value=True), \
                patch("mah.cli.create_adapter", return_value=self.adapter), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--config", str(config), "resolve", self.run_id, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_recovered_result_takes_priority(self):
        code, out, err = self.resolve_cli("--mark-failed", "--reason", "thought it died")
        self.assertEqual(code, 0, err)
        view = json.loads(out)
        self.assertEqual((view["state"], view["state_reason"], view["completion_evidence"]),
                         ("COLLECTING", "recovered_result_collected", "verified"))
        self.assertIn("已救回結果", view["resolution_notice"])
        self.assertIn("--mark-failed", err)
        self.assertEqual(self.ledger.get_grant(self.grant_id)["used_dispatches"], 1)

    def test_mark_failed_keeps_a_result_recovered_in_the_same_call(self):
        self.kill_mah()
        self.manager.scan()
        self.assertEqual(self.ledger.get_run(self.run_id)["state"], "SUBMISSION_UNKNOWN")
        self.assert_recovered_result_takes_priority()
        self.assertEqual(self.verify()["state"], "SUCCEEDED")
        self.assertEqual(len(self.started), 1)

    def test_mark_failed_right_after_mah_was_killed_keeps_the_recovered_result(self):
        self.kill_mah()
        self.assertEqual(self.ledger.get_run(self.run_id)["state"], "RUNNING")
        # The CLI's own scan finds the Worker gone; collect_saved then recovers the .pending result.
        probed = lambda ledger, **options: RunManager(ledger, process_probe=self.probe, **options)
        with patch("mah.cli.RunManager", side_effect=probed):
            self.assert_recovered_result_takes_priority()
        # Marking it failed again, deliberately, ends the Run without a refund.
        code, out, err = self.resolve_cli("--mark-failed", "--reason", "結果不採用")
        self.assertEqual(code, 0, err)
        self.assertEqual((json.loads(out)["state"], json.loads(out)["state_reason"]), ("FAILED", "user_marked_failed"))
        self.assertEqual(self.ledger.get_grant(self.grant_id)["used_dispatches"], 1)

    def test_incomplete_output_stays_submission_unknown(self):
        self.kill_mah()
        stdout = self.directory() / "testcli.stdout.pending"
        stdout.write_bytes(stdout.read_bytes()[:40])
        view = self.recover()
        self.assertEqual(view["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(self.process_record()["recovered"], "pending")
        self.assertFalse((self.directory() / "DONE").exists())
        self.assertEqual(view["resolution_actions"], ["mark_failed", "abandon"])

    def assert_not_recovered_while_worker_is(self, status):
        self.kill_mah()
        self.worker_status = status
        view = self.recover()
        self.assertNotEqual(view["state"], "COLLECTING")
        self.assertFalse((self.directory() / "testcli.process.json").exists())
        self.assertFalse((self.directory() / "testcli.capture.json").exists())

    def test_alive_worker_is_not_recovered(self):
        self.assert_not_recovered_while_worker_is("alive")
        self.assertEqual(self.ledger.get_run(self.run_id)["state"], "RUNNING")

    def test_unknown_worker_is_not_recovered(self):
        self.assert_not_recovered_while_worker_is("unknown")

    def test_reused_worker_pid_is_not_recovered(self):
        self.assert_not_recovered_while_worker_is("reused")
        self.assertEqual(self.ledger.get_run(self.run_id)["state_reason"], "process_reused")

    def test_live_launching_mah_is_not_recovered(self):
        self.kill_mah()
        self.manager.process_probe = probe_process  # the launching process (this test) is alive
        view = self.recover()
        self.assertEqual(view["state"], "SUBMISSION_UNKNOWN")
        self.assertFalse((self.directory() / "testcli.process.json").exists())

    def test_changed_workspace_is_a_readonly_violation(self):
        self.kill_mah("dirty")
        view = self.recover()
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "readonly_violation"))
        self.assertTrue(self.process_record()["workspace_changed"])

    def test_recollection_after_recovery_keeps_every_byte(self):
        self.kill_mah()
        first = self.recover()
        files = files_of(self.directory())
        again = self.recover()
        self.assertEqual((again["state"], again["state_reason"]), (first["state"], first["state_reason"]))
        self.assertEqual(files_of(self.directory()), files)

    def assert_not_recovered_without(self, name):
        self.kill_mah()
        (self.directory() / f"testcli.{name}.pending").unlink()
        view = self.recover()
        self.assertEqual(view["state"], "SUBMISSION_UNKNOWN")
        self.assertFalse((self.directory() / "testcli.process.json").exists())

    def test_missing_stdout_pending_is_not_recovered(self):
        self.assert_not_recovered_without("stdout")

    def test_missing_stderr_pending_is_not_recovered(self):
        self.assert_not_recovered_without("stderr")

    def test_changed_invocation_is_not_recovered(self):
        self.kill_mah()
        invocation = self.directory() / "invocation.json"
        metadata = json.loads(invocation.read_bytes())
        invocation.write_bytes(json.dumps(metadata | {"run_id": "other-" + "0" * 32}).encode())
        view = self.recover()
        self.assertEqual(view["state"], "SUBMISSION_UNKNOWN")
        self.assertFalse((self.directory() / "testcli.process.json").exists())

    def test_replay_never_recovers(self):
        self.kill_mah()
        with NoLaunch(), self.assertRaises(LedgerError):
            self.adapter.collect_saved(self.run_id, replay_output=True)
        self.assertFalse((self.directory() / "testcli.process.json").exists())


class AgyPendingRecoveryTests(test_agy_process.AgyProcessTests):
    """agy recovers through the same base path and still seals its CLI log submission evidence."""

    def kill_mah(self, scenario, *, kill_after=None, timeout=5):
        if scenario != "success":
            self.grant = self.make_grant(scenario, timeout=timeout)
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute), \
                patch.object(self.adapter, "observe", side_effect=killed_observe(kill_after=kill_after)), \
                self.assertRaises(HardKill):
            self.run_dispatch(scenario, timeout_s=timeout)
        self.assertEqual(self.ledger.get_run(self.run)["state"], "RUNNING")
        worker = recorded_worker(self.manager.database, self.run)
        self.manager.process_probe = lambda pid: ("gone", None) if pid in (os.getpid(), worker["pid"]) else (
            probe_process(pid))

    def test_agy_complete_output_is_recovered(self):
        self.kill_mah("success")
        with NoLaunch():
            view = self.adapter.collect_saved(self.run)
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "recovered_result_collected"))
        record = json.loads((self.directory / "agy.process.json").read_bytes())
        self.assertEqual((record["recovered"], record["termination"], record["launch_error"]),
                         ("pending", "interrupted", None))
        self.assertNotIn("log_submission", record)
        self.assertEqual(len(self.started), 1)

    def test_agy_log_submission_is_sealed_on_recovery(self):
        self.kill_mah("logged_timeout", kill_after=self.directory / "agy.stdout.pending")
        with NoLaunch():
            view = self.adapter.collect_saved(self.run)
        self.assertEqual((view["state"], view["state_reason"]), ("TIMED_OUT", "process_timed_out"))
        record = json.loads((self.directory / "agy.process.json").read_bytes())
        self.assertEqual(record["recovered"], "pending")
        self.assertEqual(record["log_submission"]["conversation_id"], "12345678-1234-4234-8234-123456789abc")
        with NoLaunch():
            self.assertEqual(self.adapter.collect_saved(self.run)["state"], "TIMED_OUT")
        self.assertEqual(len(self.started), 1)


def load_tests(loader, tests, pattern):
    """Run only the tests defined here, not the ones AgyPendingRecoveryTests inherits."""
    suite = unittest.TestSuite()
    for case in (ReceiptCheckTests, PendingRecoveryTests, AgyPendingRecoveryTests):
        for name in loader.getTestCaseNames(case):
            if name in vars(case):
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
