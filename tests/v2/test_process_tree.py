"""Windows Job Object containment of the Worker process tree (refactor spec 2c).

Only stand-in Python processes are started; no model CLI is called.
"""
from contextlib import redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import MagicMock, patch

from mah import process_tree
from mah.adapters import registry
from mah.adapters.agy_print import AgyCapture, AgyPrintAdapter, LaunchHandle
from mah.adapters.base import AdapterPolicy
from mah.process_identity import probe_process

from test_cli_worker import WorkerFixture

REPO = Path(__file__).resolve().parents[2]
# A Worker that starts a grandchild, records the grandchild's PID, then either waits or exits at once.
TREE = r"""
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(120)"])
with open(sys.argv[1], "w", encoding="ascii") as file:
    file.write(str(child.pid))
if sys.argv[2] == "wait":
    time.sleep(120)
"""
# Plays mah: starts the Worker tree, contains it, then waits until it is hard-killed.
CONTROLLER = r"""
import subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from mah import process_tree
worker = subprocess.Popen([sys.executable, "-I", "-B", sys.argv[2], sys.argv[3], "wait"])
job, error = process_tree.contain(worker)
print(worker.pid if job is not None and error is None else f"error {error}", flush=True)
time.sleep(120)
"""


def wait_gone(pid, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if probe_process(pid)[0] == "gone":
            return True
        time.sleep(0.1)
    return False


def read_pid(path, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            return int(path.read_text(encoding="ascii"))
        except (FileNotFoundError, ValueError):
            time.sleep(0.05)
    raise AssertionError("the stand-in never reported its grandchild")


def kill_quietly(pid):
    try:
        os.kill(pid, 9)
    except OSError:
        pass


class ContainmentUnitTests(unittest.TestCase):
    def test_stand_in_processes_are_not_contained(self):
        for process in (object(), MagicMock(), MagicMock(spec=subprocess.Popen), None):
            with self.subTest(process=type(process).__name__):
                self.assertEqual(process_tree.contain(process), (None, None))

    def test_non_windows_is_not_contained(self):
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", "pass"])
        self.addCleanup(process.wait)
        with patch.object(process_tree, "_supported", return_value=False):
            self.assertEqual(process_tree.contain(process), (None, None))

    def test_a_closed_job_never_terminates_or_raises(self):
        job = process_tree.Job(MagicMock(), None)
        self.assertTrue(job.closed)
        self.assertFalse(job.terminate())
        job.close()

    def test_agy_process_record_carries_the_failure_only_when_present(self):
        adapter = AgyPrintAdapter(None)
        capture = AgyCapture("r", "t", "i", b"out", b"err", 0)
        plain = LaunchHandle(None, None, {"pid": 1, "identity": None}, "2026-11-19T00:00:00+00:00", 0.0)
        failed = replace(plain, job_error="AssignProcessToJobObject failed (winerror 5)")
        self.assertNotIn("job_object_error", adapter.process_record(plain, capture))
        record = adapter.process_record(failed, capture)
        self.assertEqual(record["job_object_error"], failed.job_error)
        streams = {"stdout": b"out", "stderr": b"err"}
        self.assertEqual(adapter.capture_from_record(None, record, streams), capture)


@unittest.skipUnless(os.name == "nt", "Job Objects are a Windows API")
class ProcessTreeTests(WorkerFixture, unittest.TestCase):
    def setUp(self):
        registry.register("test-cli", AdapterPolicy(requires_dispatch_scope=True, template_verified=True))
        self.addCleanup(registry.unregister, "test-cli")
        super().setUp()
        self.tree = self.root / "tree.py"
        self.tree.write_text(TREE, encoding="utf-8")
        self.pid_file = self.root / "grandchild.pid"

    def tree_popen(self, mode):
        def start(argv, **kwargs):
            self.started.append((tuple(argv), dict(kwargs["env"])))
            return self.real_popen([sys.executable, "-I", "-B", str(self.tree), str(self.pid_file), mode], **kwargs)
        return start

    def process_record(self):
        return json.loads((self.directory() / "testcli.process.json").read_bytes())

    def test_timeout_terminates_the_worker_and_its_grandchild(self):
        view = self.dispatch("timeout", timeout=2, popen=self.tree_popen("wait"))
        grandchild = read_pid(self.pid_file)
        self.addCleanup(kill_quietly, grandchild)
        self.assertEqual(view["state"], "SUBMISSION_UNKNOWN")
        record = self.process_record()
        self.assertEqual(record["termination"], "timeout")
        self.assertNotIn("job_object_error", record)
        self.assertTrue(wait_gone(view["pid"]))
        self.assertTrue(wait_gone(grandchild), "the grandchild outlived the Worker's timeout")

    def test_normal_exit_clears_a_leftover_grandchild(self):
        self.dispatch(timeout=30, popen=self.tree_popen("exit"))
        grandchild = read_pid(self.pid_file)
        self.addCleanup(kill_quietly, grandchild)
        self.assertEqual(self.process_record()["termination"], "exited")
        self.assertTrue(wait_gone(grandchild), "closing the job did not end the leftover grandchild")

    def test_hard_killed_controller_takes_the_tree_with_it(self):
        script = self.root / "controller.py"
        script.write_text(CONTROLLER, encoding="utf-8")
        controller = subprocess.Popen([sys.executable, "-I", "-B", str(script), str(REPO), str(self.tree),
                                       str(self.pid_file)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(controller.wait)
        self.addCleanup(kill_quietly, controller.pid)
        line = controller.stdout.readline().strip()
        self.assertTrue(line.isdigit(), line)
        worker = int(line)
        self.addCleanup(kill_quietly, worker)
        grandchild = read_pid(self.pid_file)
        self.addCleanup(kill_quietly, grandchild)
        self.assertEqual(probe_process(grandchild)[0], "alive")
        controller.kill()  # TerminateProcess: no cleanup code runs in the controller
        controller.wait(timeout=10)
        controller.stdout.close()
        self.assertTrue(wait_gone(worker), "the Worker outlived its controller")
        self.assertTrue(wait_gone(grandchild), "the grandchild outlived its controller")

    def assert_failure_recorded(self, view, message, stderr):
        record = self.process_record()
        self.assertEqual(record["job_object_error"].split(" (")[0], message)
        self.assertIn("Job Object", stderr.getvalue())
        # The sealed receipt binds the failure, and re-collection reads the receipt without relaunching.
        receipt = json.loads((self.directory() / "testcli.capture.json").read_bytes())
        self.assertIsNotNone(receipt["process_receipt_sha256"])
        files = {item.name: item.read_bytes() for item in self.directory().iterdir()}
        with patch("mah.adapters.cli_worker.Popen", side_effect=AssertionError("recollection never launches")):
            again = self.adapter.collect_saved(self.run_id)
        self.assertEqual((again["state"], again["state_reason"]), (view["state"], view["state_reason"]))
        self.assertEqual({item.name: item.read_bytes() for item in self.directory().iterdir()}, files)

    def test_assign_failure_keeps_the_dispatch_and_records_the_reason(self):
        stderr = io.StringIO()
        with patch.object(process_tree, "_assign_process", return_value=0), redirect_stderr(stderr):
            view = self.dispatch()
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "structured_result_collected"))
        self.assert_failure_recorded(view, "AssignProcessToJobObject failed", stderr)
        self.assertEqual(self.verify()["state"], "SUCCEEDED")

    def test_create_failure_keeps_the_dispatch_and_records_the_reason(self):
        stderr = io.StringIO()
        with patch.object(process_tree, "_create_job", return_value=None), redirect_stderr(stderr):
            view = self.dispatch()
        self.assertEqual(view["state"], "COLLECTING")
        self.assert_failure_recorded(view, "CreateJobObjectW failed", stderr)

    def test_assign_failure_falls_back_to_killing_the_worker_on_timeout(self):
        stderr = io.StringIO()
        with patch.object(process_tree, "_assign_process", return_value=0), redirect_stderr(stderr):
            view = self.dispatch("timeout", timeout=2)
        self.assertEqual(view["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(self.process_record()["termination"], "timeout")
        self.assertTrue(wait_gone(view["pid"]))
        self.assert_failure_recorded(view, "AssignProcessToJobObject failed", stderr)

    def test_failed_terminate_falls_back_to_kill(self):
        with patch.object(process_tree.Job, "terminate", return_value=False) as terminate:
            view = self.dispatch("timeout", timeout=2)
        terminate.assert_called_once()
        self.assertEqual(self.process_record()["termination"], "timeout")
        self.assertNotIn("job_object_error", self.process_record())
        self.assertTrue(wait_gone(view["pid"]))


if __name__ == "__main__":
    unittest.main()
