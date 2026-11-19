"""Open-source hardening: executable lookup, deferred delegate-implement, approval mode,
COLLECTING resolution, snapshot exclusions and pre-card login checks. Offline only."""
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from mah import login_status
from mah.adapters.agy_print import AgyPrintAdapter
from mah.adapters.claude_print import resolve_claude_executable
from mah.adapters.codex_exec import resolve_codex_executable
from mah.context_bundle import prepare_bundle, prepare_worktree
from mah.executable_path import EXECUTABLE_INSIDE_PROJECT, which
from mah.ledger import DELEGATE_DEFERRED, GrantLedger, LedgerError
from mah.preflight import preflight
from mah.run_manager import RunManager
from mah.verifier import Verifier
from test_cli import CONSOLE_FIXTURE

ROOT = Path(__file__).resolve().parents[2]
GIT = ["git", "-c", "user.name=Harness Test", "-c", "user.email=harness@example.invalid"]
NO_CWD_SEARCH = "NoDefaultCurrentDirectoryInExePath"


def git_repo(path, files):
    path.mkdir(parents=True)
    subprocess.run([*GIT, "init", "-q", str(path)], check=True, capture_output=True)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding="utf-8")
    subprocess.run([*GIT, "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run([*GIT, "-C", str(path), "commit", "-q", "-m", "seed"], check=True, capture_output=True)
    return path


class Scratch(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="mah-hardening-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def chdir(self, path):
        previous = os.getcwd()
        os.chdir(path)
        self.addCleanup(os.chdir, previous)

    def environment(self, **values):
        """Patch the process environment; the current-directory protection is always removed."""
        patcher = patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(NO_CWD_SEARCH, None)


# ---- A1 -------------------------------------------------------------------------------------------
class ExecutableLookupTests(Scratch):
    def plant(self, directory, *names):
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / name).write_bytes(b"planted; never executed")
        return directory

    def test_planted_exe_in_current_directory_is_ignored(self):
        planted = self.plant(self.root / "project", "claude.exe", "codex.exe", "agy.exe")
        empty = self.plant(self.root / "empty-path-dir")
        self.environment(PATH=str(empty), LOCALAPPDATA=str(self.root / "no-local-appdata"))
        self.chdir(planted)
        if os.name == "nt":
            # Control: the standard library resolves the planted file from the current directory.
            self.assertEqual(Path(shutil.which("claude")).parent.resolve(), planted.resolve())
        for name in ("claude", "codex", "agy"):
            self.assertIsNone(which(name), name)
        self.assertIsNone(resolve_claude_executable("claude"))
        self.assertIsNone(resolve_codex_executable("codex"))
        self.assertIsNone(AgyPrintAdapter(None, command="agy").find_executable())

    def test_relative_and_dot_path_entries_are_skipped(self):
        project = self.plant(self.root / "project", "tool.exe")
        self.plant(project / "rel", "tool.exe")
        trusted = self.plant(self.root / "trusted", "tool.exe")
        self.environment(PATH=os.pathsep.join((".", "rel", str(project), str(trusted))))
        self.chdir(project)
        # ".", the relative "rel" and the absolute entry naming the current directory are all skipped.
        self.assertEqual(Path(which("tool.exe")).resolve(), (trusted / "tool.exe").resolve())
        self.assertEqual(Path(which(str(trusted / "tool.exe"))), trusted / "tool.exe")

    def test_executable_inside_repo_or_worktree_root_is_refused_before_probe(self):
        state = self.root / "state"
        state.mkdir()
        manager = RunManager(GrantLedger(state / "ledger.sqlite3"))
        repo, trees = self.root / "repo", self.root / "trees"
        workspace = trees / "bundle"
        workspace.mkdir(parents=True)
        repo.mkdir()

        class Stub:
            id = "agy-print"

            def __init__(self, executable):
                self.manager = manager
                self.executable = executable

            def find_executable(self):
                return self.executable

            def prepare_invocation(self, **_):
                raise AssertionError("the probe must not run")

        request = dict(grant_id="none", run_id=f"agy-{uuid4().hex}", repo_root=repo, template="review", model="m",
                       effort="low", task=b"t", workspace=workspace, worktree_root=trees)
        for planted in (repo / "agy.exe", workspace / "agy.exe", trees / "agy.exe"):
            with self.subTest(planted=planted), self.assertRaises(LedgerError) as error:
                manager.dispatch(Stub(planted), **request)
            self.assertEqual(error.exception.code, 14)
            self.assertIn("已拒絕", str(error.exception))
        with self.assertRaisesRegex(AssertionError, "probe"):
            manager.dispatch(Stub(self.root / "bin" / "agy.exe"), **request)
        self.assertIsNone(manager.ledger.get_run(request["run_id"]))

    def test_preflight_refuses_an_executable_inside_the_project(self):
        repo = git_repo(self.root / "repo", {"a.txt": "a\n"})
        inside = self.plant(repo / "tools", "agy.exe") / "agy.exe"
        config = self.root / "harness.local.toml"
        config.write_text(f'[adapters]\nenabled = ["agy-print"]\nagy_command = {json.dumps(str(inside))}\n',
                          encoding="utf-8")
        report = preflight(repo, "agy-print", config)
        self.assertFalse(report["ok"])
        self.assertIn(EXECUTABLE_INSIDE_PROJECT.format(path=inside.resolve()), report["reasons"])


# ---- A2 -------------------------------------------------------------------------------------------
class DelegateDeferredTests(Scratch):
    def setUp(self):
        super().setUp()
        self.ledger = GrantLedger(self.root / "ledger.sqlite3")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def card(self, adapter, card_id=None):
        return self.ledger.create_card(
            card_id or f"card-{uuid4().hex}", repo_root=self.root, task_key=f"t-{uuid4().hex}", trigger_ids=["T5"],
            prepare_mode="explicit", allowed_templates=["review", "delegate-implement"],
            allowed_targets=[{"adapter": adapter, "model": "m", "effort_max": "low"}], max_dispatches=1,
            ttl_minutes=10, timeout_seconds=600, workspace_path=self.workspace, task_sha256=sha256(b"t").hexdigest())

    def test_delegate_implement_is_deferred_for_every_worker_adapter(self):
        for adapter in ("agy-print", "claude-print", "codex-exec", "gui-terra"):
            with self.subTest(adapter=adapter), self.assertRaises(LedgerError) as error:
                self.card(adapter)
            self.assertEqual((error.exception.code, str(error.exception)), (2, DELEGATE_DEFERRED))
        with closing(sqlite3.connect(self.ledger.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM cards").fetchone()[0], 0)
        # The internal test double keeps the Verifier's delegate-implement path testable.
        self.assertEqual(len(self.card("fake")), 64)

    def test_stored_delegate_card_and_grant_are_refused_at_approval_and_reservation(self):
        with patch("mah.ledger.delegate_deferred", return_value=False):
            pending = self.card("agy-print", "legacy-card")
            approved_digest = self.card("agy-print", "legacy-grant")
            grant = self.ledger.approve("legacy-grant", approved_digest, via="manual_cli")
        with self.assertRaises(LedgerError) as error:
            self.ledger.approve("legacy-card", pending, via="manual_cli")
        self.assertEqual((error.exception.code, str(error.exception)), (10, DELEGATE_DEFERRED))
        self.assertEqual(self.ledger.get_card("legacy-card")["status"], "PROPOSED")
        run_id = f"agy-{uuid4().hex}"
        with self.assertRaises(LedgerError) as error:
            self.ledger.check_and_reserve(
                grant, run_id, repo_root=self.root, template="delegate-implement", adapter="agy-print", model="m",
                effort="low", workspace_kind="worktree", workspace_path=self.workspace,
                task_sha256=sha256(b"t").hexdigest(), timeout_seconds=600)
        self.assertEqual((error.exception.code, str(error.exception)), (10, DELEGATE_DEFERRED))
        self.assertIsNone(self.ledger.get_run(run_id))
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 0)


# ---- A3 -------------------------------------------------------------------------------------------
class ApprovalModeTests(Scratch):
    def cli(self, config, *args, console=None):
        command = [sys.executable, "-B"] + (["-c", CONSOLE_FIXTURE, console] if console else ["-m", "mah"])
        return subprocess.run([*command, "--config", str(config), *args], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", timeout=30)

    def test_manual_cli_mode_refuses_controller_recorded_approval(self):
        config = self.root / "harness.local.toml"
        config.write_text('[paths]\nstate_dir = "state"\n[approval]\nmode = "manual_cli"\n', encoding="utf-8")
        card = self.root / "card.json"
        card.write_text(json.dumps({
            "card_id": "mode-1", "repo_root": str(ROOT), "task_key": "mode", "trigger_ids": ["T2"],
            "prepare_mode": "card", "allowed_templates": ["review"],
            "allowed_targets": [{"adapter": "gui-terra", "model": "synthetic", "effort_max": "low"}],
            "max_dispatches": 1, "timeout_seconds": 60, "workspace_path": str(self.root),
            "task_sha256": sha256(b"review").hexdigest()}), encoding="utf-8")
        created = self.cli(config, "card", "create", "--file", str(card))
        self.assertEqual(created.returncode, 0, created.stderr)
        digest = next(line.split("SHA-256 ", 1)[1] for line in created.stdout.splitlines()
                      if line.startswith("SHA-256 "))
        args = ("grant", "approve", "mode-1", "--sha", digest)
        refused = self.cli(config, *args, "--via", "codex_prompt")
        self.assertEqual(refused.returncode, 10, refused.stderr)
        self.assertIn("manual_cli", refused.stderr)
        self.assertEqual(json.loads(self.cli(config, "grant", "list").stdout), [])
        approved = self.cli(config, *args, "--via", "manual_cli", console="confirm")
        self.assertEqual(approved.returncode, 0, approved.stderr)
        grant = json.loads(approved.stdout[approved.stdout.index("\n{") + 1:])
        self.assertEqual((grant["approved_via"], grant["approval_evidence"]),
                         ("manual_cli", "interactive_terminal_confirmation"))


# ---- A4 -------------------------------------------------------------------------------------------
class CollectingResolutionTests(Scratch):
    def setUp(self):
        super().setUp()
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "harness.local.toml"
        self.config.write_text("[paths]\nstate_dir = " + json.dumps(str(self.state)) + "\n", encoding="utf-8")
        self.ledger = GrantLedger(self.state / "ledger.sqlite3")
        self.manager = RunManager(self.ledger)
        digest = self.ledger.create_card(
            "card-a4", repo_root=self.state, task_key="a4", trigger_ids=["T2"], prepare_mode="card",
            allowed_templates=["review"],
            allowed_targets=[{"adapter": "legacy-fixture", "model": "fixture", "effort_max": "low"}],
            max_dispatches=3, ttl_minutes=10)
        self.grant = self.ledger.approve("card-a4", digest, via="manual_cli")

    def reserve(self):
        return self.ledger.check_and_reserve(
            self.grant, f"review-{uuid4().hex}", repo_root=self.state, template="review", adapter="legacy-fixture",
            model="fixture", effort="low", workspace_kind="read_only", workspace_path=self.state,
            task_sha256=sha256(b"task").hexdigest())

    def partial_run(self):
        run = self.reserve()
        self.manager.prepare(run, b"task")
        self.manager.claim_launch(run)
        self.manager.mark_running(run, pid=os.getpid())
        self.manager.record_exit(run, 0)
        self.manager.publish_result(run, {
            "schema_version": "2.0", "run_id": run, "template": "review", "status": "partial", "summary": "half",
            "blocked_reason": None, "open_questions": [], "template_payload": {"findings": []}})
        return run

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "mah", "--config", str(self.config), *args], cwd=ROOT,
                              capture_output=True, encoding="utf-8", timeout=30)

    def test_partial_collecting_run_can_be_marked_failed_and_unblocks_dispatch(self):
        run = self.partial_run()
        self.assertEqual(Verifier(self.manager).verify(run)["verification"]["decision"], "pending")
        self.assertEqual(self.manager.status(run)["resolution_actions"], ["mark_failed"])
        with self.assertRaises(LedgerError) as busy:
            self.reserve()
        self.assertEqual(busy.exception.code, 13)
        resolved = self.cli("resolve", run, "--mark-failed", "--reason", "Worker 只回傳 partial，主控改以自行審查")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        view = json.loads(resolved.stdout)
        self.assertEqual((view["state"], view["state_reason"], view["dispatch_consumed"]),
                         ("FAILED", "user_marked_failed", 1))
        with closing(sqlite3.connect(self.ledger.database)) as db:
            detail = json.loads(db.execute("SELECT detail_json FROM run_events WHERE run_id = ? ORDER BY id DESC",
                                           (run,)).fetchone()[0])
        self.assertEqual(detail, {"refund": False, "reason": "user_marked_failed", "from_state": "COLLECTING",
                                  "user_reason": "Worker 只回傳 partial，主控改以自行審查"})
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)
        self.assertTrue(self.reserve())

    def test_mark_failed_from_collecting_requires_reason(self):
        run = self.partial_run()
        missing = self.cli("resolve", run, "--mark-failed")
        self.assertEqual(missing.returncode, 2, missing.stderr)
        self.assertIn("--reason", missing.stderr)
        self.assertEqual(self.cli("resolve", run, "--reason", "x").returncode, 2)
        self.assertEqual(self.ledger.get_run(run)["state"], "COLLECTING")

    def test_verified_collecting_run_cannot_be_marked_failed(self):
        run = self.partial_run()
        directory = self.manager._directory(run)
        digest = sha256((directory / "result.json").read_bytes()).hexdigest()
        # A verify.json recording "verified" whose state change was interrupted.
        (directory / "verify.json").write_text(json.dumps({"run_id": run, "decision": "verified",
                                                           "result_sha256": digest}), encoding="utf-8")
        with self.assertRaises(LedgerError) as error:
            self.manager.resolve(run, action="mark_failed", reason="不採用")
        self.assertEqual(error.exception.code, 30)
        self.assertEqual(self.ledger.get_run(run)["state"], "COLLECTING")

    def damaged_claude_run(self, *, streams):
        """A COLLECTING claude-print Run whose sealed receipt exists but its streams are missing or changed."""
        run = self.partial_run()
        with closing(sqlite3.connect(self.ledger.database)) as db, db:
            db.execute("UPDATE runs SET adapter = 'claude-print' WHERE run_id = ?", (run,))
        directory = self.manager._directory(run)
        (directory / "claude.process.json").write_text(json.dumps({
            "run_id": run, "stdout": sha256(b"sealed stdout").hexdigest(),
            "stderr": sha256(b"sealed stderr").hexdigest()}), encoding="utf-8")
        if streams:
            (directory / "claude.stdout.pending").write_bytes(b"changed stdout")
            (directory / "claude.stderr.pending").write_bytes(b"sealed stderr")
        return run

    def assert_damaged_run_can_be_closed(self, run, expected_error):
        collected = self.cli("collect", run)
        self.assertNotEqual(collected.returncode, 0, collected.stdout)
        self.assertNotEqual(self.cli("resolve", run).returncode, 0)
        self.assertEqual(self.ledger.get_run(run)["state"], "COLLECTING")
        with self.assertRaises(LedgerError) as busy:
            self.reserve()
        self.assertEqual(busy.exception.code, 13)

        resolved = self.cli("resolve", run, "--mark-failed", "--reason", "封存的串流檔遺失，改由主控處理")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        view = json.loads(resolved.stdout)
        self.assertEqual((view["state"], view["state_reason"], view["dispatch_consumed"]),
                         ("FAILED", "user_marked_failed", 1))
        with closing(sqlite3.connect(self.ledger.database)) as db:
            detail = json.loads(db.execute("SELECT detail_json FROM run_events WHERE run_id = ? ORDER BY id DESC",
                                           (run,)).fetchone()[0])
        self.assertEqual({key: detail[key] for key in ("refund", "reason", "from_state", "user_reason")},
                         {"refund": False, "reason": "user_marked_failed", "from_state": "COLLECTING",
                          "user_reason": "封存的串流檔遺失，改由主控處理"})
        self.assertIn(expected_error, detail["saved_evidence_error"])
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)
        self.assertTrue(self.reserve())
        self.assertNotEqual(self.cli("collect", run).returncode, 0)

    def test_missing_sealed_streams_do_not_trap_mark_failed(self):
        run = self.damaged_claude_run(streams=False)
        self.assert_damaged_run_can_be_closed(run, "FileNotFoundError")

    def test_changed_sealed_streams_do_not_trap_mark_failed(self):
        run = self.damaged_claude_run(streams=True)
        self.assert_damaged_run_can_be_closed(run, "does not match its streams")

    def test_saved_evidence_error_is_only_recorded_by_closing_actions(self):
        run = self.partial_run()
        with self.assertRaises(LedgerError) as error:
            self.manager.resolve(run, saved_evidence_error="FileNotFoundError: x")
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.ledger.get_run(run)["state"], "COLLECTING")

    def interrupted_claude_run(self, state, receipt):
        """A claude-print Run in SUBMISSION_UNKNOWN or TIMED_OUT whose process receipt holds `receipt` bytes."""
        run = self.reserve()
        self.manager.prepare(run, b"task")
        self.manager.claim_launch(run)
        self.manager.mark_running(run, pid=os.getpid())
        if state == "TIMED_OUT":
            self.manager.timeout(run)
        else:
            self.ledger.transition(run, "RUNNING", "SUBMISSION_UNKNOWN")
        with closing(sqlite3.connect(self.ledger.database)) as db, db:
            db.execute("UPDATE runs SET adapter = 'claude-print' WHERE run_id = ?", (run,))
        (self.manager._directory(run) / "claude.process.json").write_bytes(receipt)
        return run

    def last_detail(self, run):
        with closing(sqlite3.connect(self.ledger.database)) as db:
            return json.loads(db.execute("SELECT detail_json FROM run_events WHERE run_id = ? ORDER BY id DESC",
                                         (run,)).fetchone()[0])

    def assert_strict_paths_refuse(self, run, state):
        self.assertEqual(self.cli("resolve", run).returncode, 2)
        self.assertEqual(self.cli("collect", run).returncode, 2)
        self.assertEqual(self.ledger.get_run(run)["state"], state)

    DAMAGED_RECEIPTS = {
        "empty_object": (b"{}", "KeyError"),
        "duplicate_key": (b'{"run_id": "a", "run_id": "b"}', "ValueError: Duplicate JSON field"),
        "not_json": (b"not json", "JSONDecodeError"),
        "wrong_type": (b"[]", "TypeError"),
    }

    def test_damaged_receipts_can_be_marked_failed(self):
        digest = self.ledger.create_card(
            "card-receipts", repo_root=self.state, task_key="receipts", trigger_ids=["T2"], prepare_mode="card",
            allowed_templates=["review"],
            allowed_targets=[{"adapter": "legacy-fixture", "model": "fixture", "effort_max": "low"}],
            max_dispatches=len(self.DAMAGED_RECEIPTS), ttl_minutes=10)
        self.grant = self.ledger.approve("card-receipts", digest, via="manual_cli")
        for name, (receipt, expected) in self.DAMAGED_RECEIPTS.items():
            with self.subTest(name):
                run = self.interrupted_claude_run("SUBMISSION_UNKNOWN", receipt)
                self.assert_strict_paths_refuse(run, "SUBMISSION_UNKNOWN")
                resolved = self.cli("resolve", run, "--mark-failed")
                self.assertEqual(resolved.returncode, 0, resolved.stderr)
                view = json.loads(resolved.stdout)
                self.assertEqual((view["state"], view["state_reason"], view["dispatch_consumed"]),
                                 ("FAILED", "user_marked_failed", 1))
                self.assertTrue(self.last_detail(run)["saved_evidence_error"].startswith(expected),
                                self.last_detail(run))

    def test_damaged_receipt_can_be_abandoned_from_submission_unknown(self):
        run = self.interrupted_claude_run("SUBMISSION_UNKNOWN", b"{}")
        resolved = self.cli("resolve", run, "--abandon")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads(resolved.stdout)["state"], "ABANDONED")
        detail = self.last_detail(run)
        self.assertEqual((detail["reason"], detail["refund"]), ("user_abandoned", False))
        self.assertTrue(detail["saved_evidence_error"].startswith("KeyError"))

    def test_damaged_receipt_can_be_marked_failed_from_timed_out(self):
        run = self.interrupted_claude_run("TIMED_OUT", b"{}")
        self.assert_strict_paths_refuse(run, "TIMED_OUT")
        self.assertEqual(self.cli("resolve", run, "--abandon").returncode, 20)
        resolved = self.cli("resolve", run, "--mark-failed", "--reason", "封存收據損毀")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads(resolved.stdout)["state"], "FAILED")
        detail = self.last_detail(run)
        self.assertEqual(detail["user_reason"], "封存收據損毀")
        self.assertTrue(detail["saved_evidence_error"].startswith("KeyError"))

    def test_damaged_receipt_closes_a_result_recovered_in_the_same_call(self):
        run = self.interrupted_claude_run("SUBMISSION_UNKNOWN", b"{}")
        self.manager.publish_result(run, {
            "schema_version": "2.0", "run_id": run, "template": "review", "status": "completed", "summary": "s",
            "blocked_reason": None, "open_questions": [], "template_payload": {"findings": []}})
        # The CLI scan would reconcile first; this is the order in which collect_saved publishes the
        # result and then fails, leaving the reconcile to this resolve call.
        view = self.manager.resolve(run, action="mark_failed", saved_evidence_error="LedgerError: damaged")
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "user_marked_failed"))
        detail = self.last_detail(run)
        self.assertEqual(detail, {"refund": False, "reason": "user_marked_failed", "from_state": "COLLECTING",
                                  "recovered_from": "SUBMISSION_UNKNOWN",
                                  "saved_evidence_error": "LedgerError: damaged"})
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def sealed_receipt(self, run, **changes):
        """A claude-print process receipt that is valid JSON with matching streams, with `changes` applied."""
        directory = self.manager._directory(run)
        streams = {"stdout": b"sealed stdout", "stderr": b"sealed stderr"}
        for name, data in streams.items():
            (directory / f"claude.{name}.pending").write_bytes(data)
        record = {"run_id": run, "task_sha256": sha256(b"task").hexdigest(), "invocation_sha256": "0" * 64,
                  "stdout": sha256(streams["stdout"]).hexdigest(), "stderr": sha256(streams["stderr"]).hexdigest(),
                  "exit_code": None, "termination": "interrupted", "launch_error": None, "launch_detail": None,
                  "tool_events": [], "wall_milliseconds": None, "workspace_changed": None,
                  "workspace_after_sha256": None, "process": None, "started_at": None} | changes
        (directory / "claude.process.json").write_text(json.dumps(record), encoding="utf-8")

    DAMAGED_CONTENT = {
        # _check_capture: LedgerError(2)
        "invalid_termination": ({"termination": "corrupted"}, None),
        "invalid_tool_event": ({"tool_events": [{"type": "tool"}]}, None),
        # _check_capture: LedgerError(20)
        "launch_error_on_exited": ({"termination": "exited", "exit_code": 0,
                                    "launch_error": "process_not_started"}, None),
        # invocation.json verification_requirements: LedgerError(2)
        "invalid_requirements": ({}, {"verification_requirements": "broken"}),
    }

    def test_damaged_receipt_content_is_evidence_damage_that_closing_actions_record(self):
        digest = self.ledger.create_card(
            "card-content", repo_root=self.state, task_key="content", trigger_ids=["T2"], prepare_mode="card",
            allowed_templates=["review"],
            allowed_targets=[{"adapter": "legacy-fixture", "model": "fixture", "effort_max": "low"}],
            max_dispatches=len(self.DAMAGED_CONTENT), ttl_minutes=10)
        self.grant = self.ledger.approve("card-content", digest, via="manual_cli")
        for index, (name, (changes, invocation)) in enumerate(self.DAMAGED_CONTENT.items()):
            with self.subTest(name):
                run = self.interrupted_claude_run("SUBMISSION_UNKNOWN", b"{}")
                self.sealed_receipt(run, **changes)
                if invocation is not None:
                    (self.manager._directory(run) / "invocation.json").write_text(json.dumps(invocation),
                                                                                  encoding="utf-8")
                # mah collect and a resolve without an action stay strict, now as evidence damage.
                for args in (("resolve", run), ("collect", run)):
                    refused = self.cli(*args)
                    self.assertEqual(refused.returncode, 30, refused.stderr)
                    self.assertIn("Sealed Worker evidence is damaged", refused.stderr)
                self.assertEqual(self.ledger.get_run(run)["state"], "SUBMISSION_UNKNOWN")
                action, expected = (("--abandon", "ABANDONED") if index % 2 else ("--mark-failed", "FAILED"))
                resolved = self.cli("resolve", run, action)
                self.assertEqual(resolved.returncode, 0, resolved.stderr)
                view = json.loads(resolved.stdout)
                self.assertEqual((view["state"], view["dispatch_consumed"]), (expected, 1))
                detail = self.last_detail(run)
                self.assertFalse(detail["refund"])
                self.assertTrue(detail["saved_evidence_error"].startswith(
                    "LedgerError: Sealed Worker evidence is damaged"), detail)
                self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], index + 1)
                self.assertNotEqual(self.cli("collect", run).returncode, 0)
                self.assertEqual(self.ledger.get_run(run)["state"], expected)

    def test_abandon_with_a_result_and_a_damaged_receipt_explains_the_state(self):
        run = self.interrupted_claude_run("SUBMISSION_UNKNOWN", b"{}")
        self.manager.publish_result(run, {
            "schema_version": "2.0", "run_id": run, "template": "review", "status": "completed", "summary": "s",
            "blocked_reason": None, "open_questions": [], "template_payload": {"findings": []}})
        abandoned = self.cli("resolve", run, "--abandon")
        self.assertEqual(abandoned.returncode, 20, abandoned.stdout)
        self.assertIn("不能 --abandon", abandoned.stderr)
        self.assertIn("--mark-failed --reason", abandoned.stderr)
        self.assertNotEqual(self.ledger.get_run(run)["state"], "ABANDONED")
        resolved = self.cli("resolve", run, "--mark-failed", "--reason", "封存收據損毀，結果不採用")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        view = json.loads(resolved.stdout)
        self.assertEqual((view["state"], view["state_reason"], view["dispatch_consumed"]),
                         ("FAILED", "user_marked_failed", 1))
        self.assertTrue(self.last_detail(run)["saved_evidence_error"].startswith("KeyError"))
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_each_adapter_checks_its_sealed_receipts_with_its_own_capture_rules(self):
        from mah.adapters import agy_print, cli_worker

        self.assertIs(AgyPrintAdapter.check_capture, agy_print._check_capture)
        self.assertIs(cli_worker.CliWorkerAdapter.check_capture, cli_worker._check_capture)

    def test_abandon_of_a_collecting_result_says_nothing_changed(self):
        run = self.partial_run()
        view = self.manager.resolve(run, action="abandon")
        self.assertEqual(view["state"], "COLLECTING")
        self.assertIn("--abandon 不適用", view["resolution_notice"])

    def test_result_recovered_before_resolve_takes_priority_over_mark_failed(self):
        run = self.partial_run()
        view = self.manager.resolve(run, action="mark_failed", reason="以為 Worker 已經死了",
                                    state_before_collect="SUBMISSION_UNKNOWN")
        self.assertEqual((view["state"], view["completion_evidence"]), ("COLLECTING", "verified"))
        self.assertIn("已救回結果", view["resolution_notice"])
        self.assertIn(f"mah resolve {run} --mark-failed", view["resolution_notice"])
        # The second, deliberate call ends it.
        view = self.manager.resolve(run, action="mark_failed", reason="確定不採用")
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "user_marked_failed"))
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_long_saved_evidence_error_keeps_its_tail(self):
        run = self.interrupted_claude_run("SUBMISSION_UNKNOWN", b"{}")
        message = "OSError: " + "x" * 2000 + r"C:\state\runs\claude.process.json"
        self.manager.resolve(run, action="mark_failed", saved_evidence_error=message)
        recorded = self.last_detail(run)["saved_evidence_error"]
        self.assertEqual(len(recorded), 1000)
        self.assertTrue(recorded.startswith("OSError: "))
        self.assertTrue(recorded.endswith(r"C:\state\runs\claude.process.json"))
        self.assertIn("[truncated]", recorded)


# ---- A5 -------------------------------------------------------------------------------------------
class SnapshotExclusionTests(Scratch):
    AI_SETTINGS = (".claude/settings.json", "CLAUDE.md", ".mcp.json", "AGENTS.md", ".agents/skills/s/SKILL.md",
                   ".gemini/settings.json", "GEMINI.md", "docs/AGENTS.md")

    def test_snapshot_excludes_project_ai_settings(self):
        repo = git_repo(self.root / "src", {**{name: "{}\n" for name in self.AI_SETTINGS}, "app.py": "print(1)\n"})
        for name in ("CLAUDE.local.md", "AGENTS.override.md", "sub/Claude.md"):
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text("uncommitted\n", encoding="utf-8")
        (repo / "app.py").write_text("print(2)\n", encoding="utf-8")
        report = prepare_worktree(repo, self.root / "trees", f"review-{uuid4().hex}")
        tree = Path(report["worktree_path"])
        self.addCleanup(subprocess.run, ["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
                        capture_output=True)
        present = sorted(path.relative_to(tree).as_posix() for path in tree.rglob("*")
                         if path.is_file() and ".git" not in path.parts)
        self.assertEqual(present, ["app.py"])
        self.assertEqual((tree / "app.py").read_text(encoding="utf-8"), "print(2)\n")
        # The source keeps every file, and a Context-Bundle can still carry them as data.
        self.assertTrue(all((repo / name).is_file() for name in self.AI_SETTINGS))
        instructions = self.root / "instructions.md"
        instructions.write_text("Review CLAUDE.md.", encoding="utf-8")
        subprocess.run([*GIT, "-C", str(repo), "stash", "-u", "-q"], check=True, capture_output=True)
        sealed = prepare_bundle(repo, ["CLAUDE.md"], instructions, self.root / "bundle", max_bytes=32768)
        self.assertEqual([item["path"] for item in sealed["manifest"]["files"]], ["CLAUDE.md"])

    def test_snapshot_excludes_antigravity_agent_directory(self):
        """Antigravity keeps workspace rules and workflows under the singular `.agent/`."""
        repo = git_repo(self.root / "src", {".agent/rules/r.md": "rule\n", "pkg/.Agent/workflows/w.md": "flow\n",
                                            "agent/notes.md": "kept\n", "app.py": "print(1)\n"})
        report = prepare_worktree(repo, self.root / "trees", f"review-{uuid4().hex}")
        tree = Path(report["worktree_path"])
        self.addCleanup(subprocess.run, ["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
                        capture_output=True)
        present = sorted(path.relative_to(tree).as_posix() for path in tree.rglob("*")
                         if path.is_file() and ".git" not in path.parts)
        self.assertEqual(present, ["agent/notes.md", "app.py"])


class BundleOutputTests(Scratch):
    def test_missing_output_parent_has_a_clear_error(self):
        repo = git_repo(self.root / "repo", {"a.txt": "a\n"})
        instructions = self.root / "instructions.md"
        instructions.write_text("Review a.txt.", encoding="utf-8")
        missing = self.root / "missing-parent" / "bundle"
        with self.assertRaises(LedgerError) as error:
            prepare_bundle(repo, ["a.txt"], instructions, missing, max_bytes=32768)
        self.assertEqual(error.exception.code, 2)
        self.assertIn("上層必須已存在", str(error.exception))
        self.assertFalse(missing.parent.exists())
        self.assertEqual(prepare_bundle(repo, ["a.txt"], instructions, self.root / "bundle",
                                        max_bytes=32768)["output_dir"], str((self.root / "bundle").resolve()))


# ---- A6 -------------------------------------------------------------------------------------------
def completed(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(["status"], returncode, stdout=stdout, stderr=stderr)


class LoginStatusTests(Scratch):
    SUBSCRIPTION = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                    "subscriptionType": "pro", "loginExpiresAt": "2099-01-01T00:00:00.000Z"}

    def claude(self, data, returncode=0, **options):
        with patch("mah.login_status._status", return_value=completed(
                returncode, json.dumps(data).encode())) as status:
            reasons = login_status.claude_login_reasons(Path("claude.exe"), **options)
        return reasons, status

    def test_preflight_accepts_subscription_logins(self):
        self.environment(ANTHROPIC_API_KEY="sk-ant-should-not-pass", OPENAI_API_KEY="sk-should-not-pass")
        reasons, status = self.claude(self.SUBSCRIPTION)
        self.assertEqual(reasons, [])
        argv, environment = status.call_args.args
        self.assertEqual(argv[1:], ["auth", "status", "--json"])
        self.assertFalse(any(key.upper().startswith("ANTHROPIC_") for key in environment))
        stderr = b"WARNING: proceeding, even though we could not create PATH aliases\nLogged in using ChatGPT\n"
        with patch("mah.login_status._status", return_value=completed(0, stderr=stderr)) as status:
            self.assertEqual(login_status.codex_login_reasons(Path("codex.exe")), [])
        argv, environment = status.call_args.args
        self.assertEqual(argv[1:], ["login", "status"])
        self.assertFalse(any(key.upper().startswith("OPENAI_") for key in environment))

    def test_preflight_rejects_claude_api_key_or_logged_out(self):
        cases = {
            "logged out": ({"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}, 1, "尚未登入"),
            "api key": ({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty",
                         "apiKeySource": "ANTHROPIC_API_KEY"}, 0, "api_key"),
            "key helper": ({"loggedIn": True, "authMethod": "api_key_helper", "apiProvider": "firstParty"}, 0,
                           "api_key_helper"),
            "third party": ({"loggedIn": True, "authMethod": "third_party", "apiProvider": "bedrock"}, 0,
                            "third_party"),
            "key beside login": (self.SUBSCRIPTION | {"apiKeySource": "/login managed key"}, 0, "claude.ai"),
            "expired": (self.SUBSCRIPTION | {"loginExpiresAt": "2020-01-01T00:00:00.000Z"}, 0, "到期"),
        }
        for name, (data, code, words) in cases.items():
            with self.subTest(name):
                reasons, _ = self.claude(data, code)
                self.assertEqual(len(reasons), 1)
                self.assertIn(words, reasons[0])
        reasons, _ = self.claude(self.SUBSCRIPTION | {"loginExpiresAt": "2026-11-14T00:00:00+00:00"},
                                 now=datetime(2026, 11, 13, tzinfo=UTC))
        self.assertEqual(reasons, [])
        with patch("mah.login_status._status", return_value=completed(0, b"not json")):
            self.assertIn("無法確認", login_status.claude_login_reasons(Path("claude.exe"))[0])

    def test_preflight_rejects_codex_api_key_or_logged_out(self):
        api_key = completed(0, stderr=b"WARNING: x\nLogged in using an API key - sk-fake-***00000\n")
        with patch("mah.login_status._status", return_value=api_key):
            reason, = login_status.codex_login_reasons(Path("codex.exe"))
        self.assertIn("an API key", reason)
        self.assertNotIn("sk-", reason)
        with patch("mah.login_status._status", return_value=completed(1, stderr=b"Not logged in\n")):
            self.assertIn("尚未登入", login_status.codex_login_reasons(Path("codex.exe"))[0])
        with patch("mah.login_status._status", side_effect=subprocess.TimeoutExpired(["codex"], 20)):
            self.assertIn("TimeoutExpired", login_status.codex_login_reasons(Path("codex.exe"))[0])

    def test_preflight_report_carries_login_reason_without_new_fields(self):
        repo = git_repo(self.root / "repo", {"a.txt": "a\n"})
        native = self.root / "bin" / "claude.exe"
        native.parent.mkdir()
        native.write_bytes(b"stand-in; the status call is patched")
        config = self.root / "harness.local.toml"
        config.write_text(f'[adapters]\nenabled = ["claude-print"]\nclaude_command = {json.dumps(str(native))}\n',
                          encoding="utf-8")
        logged_out = json.dumps({"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}).encode()
        with patch("mah.login_status._status", return_value=completed(1, logged_out)):
            report = preflight(repo, "claude-print", config)
        self.assertFalse(report["ok"])
        self.assertTrue(report["adapter_executable_found"])
        self.assertEqual(len(report["reasons"]), 1)
        self.assertIn("claude auth login", report["reasons"][0])
        self.assertNotIn("login", set(report))


# The real account/rateLimits/read reply shape from codex-cli 0.169.0 (identifiers removed).
EXHAUSTED = {"id": 2, "result": {
    "ordinaryUsageAllowed": False,
    "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 100, "windowDurationMins": 300,
                                                  "resetsAt": 1794684431},
                   "secondary": {"usedPercent": 16, "windowDurationMins": 10080, "resetsAt": 1795271231},
                   "planType": "plus", "rateLimitReachedType": "rate_limit_reached"},
    "rateLimitUpsell": {"reset_at": 1794684431},
}}
QUOTA_STAND_IN = r"""
import json, sys
log, mode = sys.argv[1], sys.argv[2]
seen = []
for raw in sys.stdin:
    message = json.loads(raw)
    seen.append(message.get("method"))
    open(log, "w").write(json.dumps({"methods": seen, "argv": sys.argv[3:]}))
    if mode == "silent":
        continue
    if message.get("id") == 1:
        print(json.dumps({"id": 1, "result": {}}), flush=True)
    if message.get("id") == 2:
        print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
        print(sys.argv[3] if mode == "garbled" else json.dumps({"id": 2, "result": {
            "ordinaryUsageAllowed": mode == "available"}}), flush=True)
"""


class CodexQuotaTests(Scratch):
    def stand_in(self, mode):
        log = self.root / f"quota-{mode}.json"
        real = subprocess.Popen

        def launch(argv, **kwargs):
            self.assertEqual([str(part) for part in argv][1:], ["app-server"])
            self.assertFalse(any(key.upper().startswith("OPENAI_") for key in kwargs["env"]))
            self.assertFalse(kwargs["shell"])
            return real([sys.executable, "-I", "-B", "-c", QUOTA_STAND_IN, str(log), mode, "not json"], **kwargs)

        return log, patch("mah.login_status.subprocess.Popen", side_effect=launch)

    def test_quota_query_sends_only_the_read_only_messages(self):
        self.environment(OPENAI_API_KEY="sk-should-not-pass")
        for mode, expected in (("available", ("available", None)), ("exhausted", ("exhausted", None))):
            log, launched = self.stand_in(mode)
            with self.subTest(mode=mode), launched:
                self.assertEqual(login_status.codex_quota_status(Path("codex.exe"), timeout=10), expected)
                self.assertEqual(json.loads(log.read_text())["methods"],
                                 ["initialize", "initialized", "account/rateLimits/read"])

    def test_failed_or_unclear_quota_query_never_blocks(self):
        for mode, timeout in (("silent", 1), ("garbled", 10)):
            _, launched = self.stand_in(mode)
            with self.subTest(mode=mode), launched:
                state, _ = login_status.codex_quota_status(Path("codex.exe"), timeout=timeout)
                self.assertEqual(state, "unknown")
        with patch("mah.login_status.subprocess.Popen", side_effect=OSError("missing")):
            self.assertEqual(login_status.codex_quota_status(Path("codex.exe")), ("unknown", "無法查詢"))
            self.assertEqual(login_status.codex_quota_reasons(Path("codex.exe")), [])
        for reply in (None, {"id": 2, "error": {"code": -1}}, {"id": 2, "result": []},
                      {"id": 2, "result": {"ordinaryUsageAllowed": "no"}}, {"id": 2, "result": {}}):
            with self.subTest(reply=reply), patch("mah.login_status._quota_reply", return_value=reply):
                self.assertEqual(login_status.codex_quota_status(Path("codex.exe"))[0], "unknown")
                self.assertEqual(login_status.codex_quota_reasons(Path("codex.exe")), [])

    def test_confirmed_exhaustion_names_the_local_reset_time(self):
        with patch("mah.login_status._quota_reply", return_value=EXHAUSTED):
            self.assertEqual(login_status.codex_quota_status(Path("codex.exe")), ("exhausted", 1794684431))
            reason, = login_status.codex_quota_reasons(Path("codex.exe"))
        expected = datetime.fromtimestamp(1794684431, UTC).astimezone().strftime("%Y-%m-%d %H:%M（UTC%z）")
        self.assertEqual(reason, f"ChatGPT 額度已用完，重設時間 {expected}")
        no_time = {"id": 2, "result": {"ordinaryUsageAllowed": False}}
        with patch("mah.login_status._quota_reply", return_value=no_time):
            self.assertEqual(login_status.codex_quota_reasons(Path("codex.exe")), ["ChatGPT 額度已用完，重設時間 未知"])
        with patch("mah.login_status._quota_reply", return_value={"id": 2, "result": {"ordinaryUsageAllowed": True}}):
            self.assertEqual(login_status.codex_quota_reasons(Path("codex.exe")), [])

    def test_quota_is_read_only_after_a_subscription_login(self):
        logged_out = completed(1, stderr=b"Not logged in\n")
        with patch("mah.login_status._status", return_value=logged_out), \
                patch("mah.login_status._quota_reply") as quota:
            self.assertIn("尚未登入", login_status.codex_reasons(Path("codex.exe"))[0])
        quota.assert_not_called()
        # No code path may consume a rate-limit reset credit.
        for source in (ROOT / "mah").rglob("*.py"):
            text = source.read_text(encoding="utf-8")
            self.assertNotIn("rateLimitResetCredit", text, source)
            self.assertNotIn("reset_usage", text, source)

    @unittest.skipUnless(os.name == "nt", "codex must resolve to a native .exe on Windows")
    def test_preflight_report_carries_quota_reason_without_new_fields(self):
        repo = git_repo(self.root / "repo", {"a.txt": "a\n"})
        native = self.root / "bin" / "codex.exe"
        native.parent.mkdir()
        native.write_bytes(b"stand-in; the status calls are patched")
        config = self.root / "harness.local.toml"
        config.write_text(f'[adapters]\nenabled = ["codex-exec"]\ncodex_command = {json.dumps(str(native))}\n',
                          encoding="utf-8")
        signed_in = completed(0, stderr=b"Logged in using ChatGPT\n")
        with patch("mah.login_status._status", return_value=signed_in), \
                patch("mah.login_status._quota_reply", return_value=EXHAUSTED):
            report = preflight(repo, "codex-exec", config)
        self.assertFalse(report["ok"])
        reason, = report["reasons"]
        self.assertTrue(reason.startswith("ChatGPT 額度已用完，重設時間 "))
        self.assertEqual(set(report), {"repo_root", "config_path", "config_found", "adapter", "adapter_enabled",
                                       "adapter_executable_found", "sandbox_redirect_detected",
                                       "needs_unsandboxed_dispatch", "ok", "reasons"})
        for reply in (None, {"id": 2, "result": {"ordinaryUsageAllowed": True}}):
            with self.subTest(reply=reply), patch("mah.login_status._status", return_value=signed_in), \
                    patch("mah.login_status._quota_reply", return_value=reply):
                self.assertEqual(preflight(repo, "codex-exec", config)["reasons"], [])


if __name__ == "__main__":
    unittest.main()
