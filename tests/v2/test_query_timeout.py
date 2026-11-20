"""Tests for Job Object containment, non-blocking pipe drain, and overall query deadlines."""
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from mah import login_status, model_detect, process_tree, quota
from mah.config import load_config
from mah.model_detect import PARTIAL_TIMEOUT_WARNING, cache_path, models_report
from mah.process_identity import probe_process
from mah.route import route

# Child spawns a grandchild inheriting stdout/stderr and sleeping, records grandchild PID, then sleeps or exits.
GRANDCHILD_SCRIPT = r"""
import subprocess, sys, time
pid_file, mode, sleep_s = sys.argv[1], sys.argv[2], float(sys.argv[3])
child = subprocess.Popen([sys.executable, "-I", "-B", "-c", f"import time; time.sleep({sleep_s})"])
with open(pid_file, "w", encoding="ascii") as handle:
    handle.write(str(child.pid))
if mode == "wait":
    time.sleep(sleep_s)
"""


def _wait_for_pid(path, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="ascii").strip()
            if text:
                return int(text)
        except (FileNotFoundError, ValueError):
            time.sleep(0.05)
    return None


def _wait_gone(pid, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if probe_process(pid)[0] == "gone":
            return True
        time.sleep(0.05)
    return False


def _kill_pid_quietly(pid):
    if pid is None:
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


class GrandchildPipeTimeoutTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-qtimeout-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.script = self.root / "grandchild_standin.py"
        self.script.write_text(GRANDCHILD_SCRIPT, encoding="utf-8")
        self.real_popen = subprocess.Popen

    def _make_popen(self, pid_file, *, mode="wait", sleep_s=30):
        def launcher(argv, **kwargs):
            return self.real_popen(
                [sys.executable, "-I", "-B", str(self.script), str(pid_file), mode, str(sleep_s)],
                **kwargs,
            )
        return launcher

    def test_every_query_returns_near_timeout_when_grandchild_holds_stdout_and_terminates_tree(self):
        timeout_s = 1.5
        max_elapsed = 4.5
        cases = (
            ("list_agy_models", "mah.model_detect.subprocess.Popen",
             lambda: model_detect.list_agy_models(None, Path("agy.exe"), timeout=timeout_s).status, "unknown"),
            ("list_codex_models", "mah.model_detect.subprocess.Popen",
             lambda: model_detect.list_codex_models(None, Path("codex.exe"), timeout=timeout_s).status, "unknown"),
            ("login_status._quota_reply", "mah.login_status.subprocess.Popen",
             lambda: login_status._quota_reply(Path("codex.exe"), os.environ.copy(), timeout_s), None),
            ("query_claude_quota", "mah.quota.subprocess.Popen",
             lambda: quota.query_claude_quota(Path("claude.exe"), timeout=timeout_s)[0].state, "unknown"),
        )
        for label, target, call, expected in cases:
            with self.subTest(query=label):
                pid_file = self.root / f"{label.replace('.', '_')}.pid"
                t0 = time.monotonic()
                with patch(target, side_effect=self._make_popen(pid_file, mode="wait", sleep_s=30)):
                    result = call()
                elapsed = time.monotonic() - t0
                grandchild_pid = _wait_for_pid(pid_file, seconds=0.2)
                self.addCleanup(_kill_pid_quietly, grandchild_pid)
                self.assertEqual(result, expected)
                self.assertLess(elapsed, max_elapsed, f"{label} hung for {elapsed:.2f}s")
                if os.name == "nt" and grandchild_pid is not None:
                    self.assertTrue(_wait_gone(grandchild_pid), f"{label} left grandchild {grandchild_pid} alive")

        # login_status._status raises TimeoutExpired promptly and terminates grandchild.
        pid_file = self.root / "login_status_cmd.pid"
        t0 = time.monotonic()
        with (patch("mah.login_status.subprocess.Popen", side_effect=self._make_popen(pid_file, mode="wait", sleep_s=30)),
              patch.object(login_status, "TIMEOUT_SECONDS", timeout_s)):
            with self.assertRaises(subprocess.TimeoutExpired):
                login_status._status([Path("claude.exe"), "auth", "status"], os.environ.copy())
        elapsed = time.monotonic() - t0
        grandchild_pid = _wait_for_pid(pid_file, seconds=0.2)
        self.addCleanup(_kill_pid_quietly, grandchild_pid)
        self.assertLess(elapsed, max_elapsed, f"_status hung for {elapsed:.2f}s")
        if os.name == "nt" and grandchild_pid is not None:
            self.assertTrue(_wait_gone(grandchild_pid), f"_status left grandchild {grandchild_pid} alive")

    def test_early_child_exit_with_grandchild_holding_stdout_does_not_hang(self):
        pid_file = self.root / "early_exit.pid"
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            process_tree.run_contained(
                [sys.executable, "-I", "-B", str(self.script), str(pid_file), "exit", "30"],
                timeout=1.2,
            )
        elapsed = time.monotonic() - t0
        grandchild_pid = _wait_for_pid(pid_file, seconds=0.2)
        self.addCleanup(_kill_pid_quietly, grandchild_pid)
        self.assertLess(elapsed, 4.0, f"run_contained hung for {elapsed:.2f}s after early child exit")
        if os.name == "nt" and grandchild_pid is not None:
            self.assertTrue(_wait_gone(grandchild_pid), "Job Object did not terminate surviving grandchild")

    def test_job_object_failure_falls_back_to_kill_and_does_not_hang_on_pipe_close(self):
        # Even when Job Object creation fails (e.g. restricted sandbox), neither run_contained nor
        # _quota_reply may block on closing a pipe still held by a grandchild.
        pid1 = self.root / "no_job_run.pid"
        pid2 = self.root / "no_job_quota.pid"
        with patch.object(process_tree, "contain", return_value=(None, "simulated failure")):
            t0 = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                process_tree.run_contained(
                    [sys.executable, "-I", "-B", str(self.script), str(pid1), "wait", "8"],
                    timeout=1.0,
                )
            elapsed1 = time.monotonic() - t0
            self.addCleanup(_kill_pid_quietly, _wait_for_pid(pid1, seconds=0.2))
            self.assertLess(elapsed1, 3.0, f"run_contained without Job hung for {elapsed1:.2f}s")

            t1 = time.monotonic()
            with patch("mah.login_status.subprocess.Popen", side_effect=self._make_popen(pid2, mode="wait", sleep_s=8)):
                reply = login_status._quota_reply(Path("codex.exe"), os.environ.copy(), 1.0)
            elapsed2 = time.monotonic() - t1
            self.addCleanup(_kill_pid_quietly, _wait_for_pid(pid2, seconds=0.2))
            self.assertIsNone(reply)
            self.assertLess(elapsed2, 3.0, f"_quota_reply without Job hung for {elapsed2:.2f}s")

    def test_run_contained_terminates_tree_on_keyboard_interrupt_during_wait(self):
        pid_file = self.root / "kbd_wait.pid"
        launched = []

        def interrupting_popen(argv, **kwargs):
            proc = self.real_popen(
                [sys.executable, "-I", "-B", str(self.script), str(pid_file), "wait", "30"],
                **kwargs,
            )
            launched.append(proc)
            real_wait = proc.wait
            calls = {"count": 0}

            def wait_once(timeout=None):
                calls["count"] += 1
                if calls["count"] == 1:
                    _wait_for_pid(pid_file, seconds=3.0)
                    raise KeyboardInterrupt("simulated ctrl-c in wait")
                return real_wait(timeout=timeout)

            proc.wait = wait_once
            return proc

        try:
            with patch.object(process_tree, "terminate_tree", wraps=process_tree.terminate_tree) as term_spy:
                with self.assertRaises(KeyboardInterrupt):
                    process_tree.run_contained(["standin"], timeout=10, popen=interrupting_popen)
            term_spy.assert_called_once()
            grandchild_pid = _wait_for_pid(pid_file, seconds=0.2)
            self.addCleanup(_kill_pid_quietly, grandchild_pid)
            if os.name == "nt" and grandchild_pid is not None:
                self.assertTrue(_wait_gone(grandchild_pid), "KeyboardInterrupt in wait left grandchild alive")
        finally:
            grandchild_pid = _wait_for_pid(pid_file, seconds=0.1)
            _kill_pid_quietly(grandchild_pid)
            for proc in launched:
                _kill_pid_quietly(proc.pid)

    def test_run_contained_terminates_tree_on_keyboard_interrupt_during_join(self):
        pid_file = self.root / "kbd_join.pid"
        launched = []

        def exit_popen(argv, **kwargs):
            proc = self.real_popen(
                [sys.executable, "-I", "-B", str(self.script), str(pid_file), "exit", "30"],
                **kwargs,
            )
            launched.append(proc)
            return proc

        real_join = threading.Thread.join
        join_calls = {"count": 0}

        def interrupting_join(thread_self, timeout=None):
            join_calls["count"] += 1
            if join_calls["count"] == 1:
                _wait_for_pid(pid_file, seconds=3.0)
                raise KeyboardInterrupt("simulated ctrl-c in join")
            return real_join(thread_self, timeout=timeout)

        try:
            with (patch.object(process_tree, "terminate_tree", wraps=process_tree.terminate_tree) as term_spy,
                  patch.object(threading.Thread, "join", new=interrupting_join)):
                with self.assertRaises(KeyboardInterrupt):
                    process_tree.run_contained(["standin"], timeout=10, popen=exit_popen)
            term_spy.assert_called_once()
            grandchild_pid = _wait_for_pid(pid_file, seconds=0.2)
            self.addCleanup(_kill_pid_quietly, grandchild_pid)
            if os.name == "nt" and grandchild_pid is not None:
                self.assertTrue(_wait_gone(grandchild_pid), "KeyboardInterrupt in join left grandchild alive")
        finally:
            grandchild_pid = _wait_for_pid(pid_file, seconds=0.1)
            _kill_pid_quietly(grandchild_pid)
            for proc in launched:
                _kill_pid_quietly(proc.pid)

    def test_run_parallel_with_deadline_ignores_late_writes_after_timeout(self):
        release = threading.Event()
        late_ok_done = threading.Event()
        late_err_done = threading.Event()

        def late_ok():
            release.wait(timeout=5.0)
            try:
                return "late-value"
            finally:
                late_ok_done.set()

        def late_err():
            release.wait(timeout=5.0)
            try:
                raise RuntimeError("late-error")
            finally:
                late_err_done.set()

        try:
            results, errors, timed_out = process_tree.run_parallel_with_deadline(
                {"fast": lambda: "ok", "late_ok": late_ok, "late_err": late_err},
                timeout=0.05,
            )
            self.assertEqual(results, {"fast": "ok"})
            self.assertEqual(errors, {})
            self.assertEqual(set(timed_out), {"late_ok", "late_err"})

            release.set()
            self.assertTrue(late_ok_done.wait(timeout=2.0))
            self.assertTrue(late_err_done.wait(timeout=2.0))
            time.sleep(0.05)

            self.assertEqual(results, {"fast": "ok"})
            self.assertEqual(errors, {})
        finally:
            release.set()


class OverallDeadlineTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-deadline-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.agy = self.tools / "agy.exe"
        self.codex = self.tools / "codex.exe"
        self.claude = self.tools / "claude.exe"
        for exe in (self.agy, self.codex, self.claude):
            exe.write_bytes(b"fake")
        self.cfg_path = self.root / "harness.local.toml"
        self.cfg_path.write_text(
            '[paths]\nstate_dir = "state"\n[adapters]\n'
            f'agy_command = "{self.agy.as_posix()}"\n'
            f'codex_command = "{self.codex.as_posix()}"\n'
            f'claude_command = "{self.claude.as_posix()}"\n',
            encoding="utf-8",
        )
        self.config = load_config(self.cfg_path)
        self.now = datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)

    def test_mah_route_overall_deadline_returns_json_when_all_queries_stall(self):
        def stall(*args, **kwargs):
            time.sleep(20)
            return {}

        # Case 1 (sandbox scenario): every CLI query stalls (login_status._status, detect, quota),
        # while agy-print has no login CLI check -> fail-open still recommends Neon within deadline.
        t0 = time.monotonic()
        with (patch("mah.preflight.git_toplevel", return_value=(str(self.repo), None)),
              patch("mah.login_status._status", side_effect=stall),
              patch("mah.route.detect_adapters", side_effect=stall),
              patch("mah.route.get_quota_states", side_effect=stall)):
            report = route(
                self.repo, task="review", level="standard", mode="bundle", controller="codex",
                config_path=self.cfg_path, now=self.now, overall_timeout=1.2,
            )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 3.5, f"route exceeded overall_timeout: {elapsed:.2f}s")
        json.dumps(report, ensure_ascii=False)
        self.assertTrue(report["ok"])
        self.assertTrue(report["card"])
        self.assertEqual(report["recommended"]["model"], "gemini-4-neon-medium")
        self.assertIn(PARTIAL_TIMEOUT_WARNING, report["warnings"])

        # Case 2: even when every adapter check itself stalls, route still exits within deadline with JSON.
        t1 = time.monotonic()
        with (patch("mah.preflight.git_toplevel", return_value=(str(self.repo), None)),
              patch("mah.route.adapter_checks", side_effect=stall),
              patch("mah.route.detect_adapters", side_effect=stall),
              patch("mah.route.get_quota_states", side_effect=stall)):
            stalled = route(
                self.repo, task="review", level="standard", mode="bundle", controller="codex",
                config_path=self.cfg_path, now=self.now, overall_timeout=1.2,
            )
        elapsed2 = time.monotonic() - t1
        self.assertLess(elapsed2, 3.5, f"route exceeded overall_timeout on full stall: {elapsed2:.2f}s")
        json.dumps(stalled, ensure_ascii=False)
        self.assertFalse(stalled["ok"])
        self.assertIn(PARTIAL_TIMEOUT_WARNING, stalled["warnings"])

    def test_mah_models_overall_deadline_keeps_completed_queries_and_cached_quota(self):
        # Seed a stale tight ChatGPT quota snapshot so timeout falls back to cache (D13).
        cpath = cache_path(self.config)
        cpath.parent.mkdir(parents=True, exist_ok=True)
        reset_later = (self.now + timedelta(hours=4)).isoformat(timespec="seconds")
        cpath.write_text(json.dumps({
            "version": 1, "detect": {}, "breakers": {"claude_usage": None},
            "quota": {
                "chatgpt": {
                    "state": "tight", "source": "app_server",
                    "fetched_at": (self.now - timedelta(hours=1)).isoformat(timespec="seconds"),
                    "ordinary_usage_allowed": True,
                    "windows": [{"name": "five_hour", "used_percent": 94, "resets_at": reset_later}],
                },
            },
        }), encoding="utf-8")

        def fake_detect(config, adapters, **kwargs):
            if "agy-print" in adapters:
                return {"agy-print": model_detect.DetectionRecord(
                    adapter="agy-print", status="ok", source="live",
                    fetched_at=self.now.isoformat(timespec="seconds"),
                    ids=("gemini-4-neon-medium", "gemini-3.8-flash-low"), executable_found=True,
                )}
            time.sleep(20)
            return {}

        def stall_quota(config, catalog=None, pools=None, **kwargs):
            if pools and any(p.startswith("agy:") for p in pools):
                return {p: quota.QuotaState(pool=p, state="unknown") for p in pools}, []
            time.sleep(20)
            return {}, []

        t0 = time.monotonic()
        with (patch("mah.model_detect.detect_adapters", side_effect=fake_detect),
              patch("mah.model_detect._claude_alias_status", side_effect=lambda *a, **k: time.sleep(20)),
              patch("mah.quota.get_quota_states", side_effect=stall_quota)):
            report = models_report(self.config, now=self.now, overall_timeout=1.2)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 3.5, f"models_report exceeded overall_timeout: {elapsed:.2f}s")
        json.dumps(report, ensure_ascii=False)
        self.assertEqual(report["detection"]["agy-print"]["status"], "ok")
        self.assertEqual(report["detection"]["codex-exec"]["status"], "unknown")
        self.assertEqual(report["quota"]["chatgpt"]["state"], "tight")
        self.assertEqual(report["quota"]["claude"]["state"], "unknown")
        self.assertIn(PARTIAL_TIMEOUT_WARNING, report["warnings"])


if __name__ == "__main__":
    unittest.main()
