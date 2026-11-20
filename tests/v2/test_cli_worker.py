"""State handling of the shared CLI Worker base, tested with a minimal subclass and a stand-in process."""
import errno
import json
import os
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from mah.adapters import registry
from mah.adapters.base import AdapterPolicy
from mah.adapters.cli_worker import CliWorkerAdapter
from mah.context_bundle import prepare_bundle
from mah.ledger import GrantLedger, LedgerError
from mah.run_manager import RunManager
from mah.verifier import VerificationEvidence, Verifier

# Reads the prompt from stdin like a print-mode CLI, then follows the scenario named in the task.
STAND_IN = r"""
import json, re, sys, time, uuid
from pathlib import Path
argv = sys.argv[1:]
prompt = sys.stdin.buffer.read().decode("utf-8")
assert prompt.startswith("MultiAgentHarness V2 run_id=")
scenario = re.search(r"scenario=(\w+)", prompt)[1]
schema = json.loads(argv[argv.index("--schema") + 1] if "--schema" in argv else argv[argv.index("--json-schema") + 1])
run_id = schema["properties"]["run_id"]["const"]
template = schema["properties"]["template"]["const"]
if scenario == "timeout":
    time.sleep(60)
if scenario == "corrupt":
    sys.stdout.buffer.write(b"\xff{")
    raise SystemExit(0)
if scenario == "failed":
    raise SystemExit(3)
if scenario == "dirty":
    Path("side-effect.txt").write_text("synthetic", encoding="utf-8")
result = dict(schema_version="2.0", run_id=run_id, template=template, status="completed", summary="synthetic review",
              blocked_reason=None, open_questions=[], template_payload={"findings": []})
if scenario == "schema":
    result["template_payload"] = {"unexpected": True}
envelope = {"session": str(uuid.uuid4()), "status": "ok", "turns": 1, "result": result}
if "--json-schema" in argv:  # the claude -p --output-format json envelope
    envelope = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                "session_id": str(uuid.uuid4()), "result": json.dumps(result, ensure_ascii=False),
                "structured_output": result, "modelUsage": {"claude-stand-in": {}},
                "usage": {"input_tokens": 3, "output_tokens": 5}}
sys.stdout.buffer.write(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
"""


class MinimalWorker(CliWorkerAdapter):
    """Only the hooks a CLI Worker must provide; no Claude knowledge."""

    id = "test-cli"
    prefix = "testcli"
    capture_reason = "testcli_capture_integrity"
    efforts = frozenset({"low"})
    check_response_agreement = False

    def resolve_executable(self):
        return Path(self.command)

    def flags_for(self, kind):
        return {"--schema", "--mode"}

    def build_argv(self, executable, *, model, effort, kind, schema):
        return (str(executable), "--mode", kind, "--schema", json.dumps(schema, sort_keys=True))

    def check_readonly(self, plan):
        if tuple(plan.argv[1:3]) != ("--mode", plan.workspace_kind):
            raise LedgerError(17, "Minimal worker profile differs")

    def worker_environment(self):
        return {key: os.environ[key] for key in ("SYSTEMROOT", "PATH") if key in os.environ}

    def normalize_stdout(self, raw):
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            return None
        return {"conversation_id": data["session"], "raw_status": "SUCCESS" if data["status"] == "ok" else "ERROR",
                "response": None, "num_turns": data["turns"], "usage": None, "model_observed": "unknown",
                "effort_observed": "unknown", "structured_output": data["result"]}


class WorkerFixture:
    """A real linked worktree, a scoped single-dispatch Grant and a stand-in process behind Popen."""

    adapter_name = "test-cli"
    model = "synthetic"
    cli_version = "stand-in 1.0"

    def make_adapter(self):
        return MinimalWorker(self.manager, command=self.executable)

    def setUp(self):
        scratch = TemporaryDirectory(prefix="mah-cli-worker-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git = ["git", "-c", "user.name=Harness Test", "-c", "user.email=harness@example.invalid"]
        subprocess.run([*git, "init", "-q", str(self.repo)], check=True, capture_output=True, timeout=15)
        (self.repo / "sample.txt").write_text("synthetic source\n", encoding="utf-8")
        subprocess.run([*git, "-C", str(self.repo), "add", "sample.txt"], check=True, capture_output=True, timeout=15)
        subprocess.run([*git, "-C", str(self.repo), "commit", "-q", "-m", "seed"], check=True, capture_output=True,
                       timeout=15)
        self.trees = self.root / "trees"
        self.trees.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.executable = self.root / "bin" / "worker.exe"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"stand-in path; never executed")
        self.stand_in = self.root / "stand_in.py"
        self.stand_in.write_text(STAND_IN, encoding="utf-8")
        self.ledger = GrantLedger(self.state / "ledger.sqlite3")
        self.manager = RunManager(self.ledger)
        self.adapter = self.make_adapter()
        self.started = []
        self.real_popen = subprocess.Popen

    def worktree(self):
        workspace = self.trees / f"tree-{uuid4().hex}"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "--detach", str(workspace), "HEAD"],
                       check=True, capture_output=True, timeout=15)
        return workspace

    def bundle_task(self, scenario="success"):
        instructions = self.root / f"instructions-{uuid4().hex}.md"
        instructions.write_text(f"Review sample.txt. scenario={scenario}", encoding="utf-8")
        output = self.root / f"bundle-{uuid4().hex}"
        prepare_bundle(self.repo, ["sample.txt"], instructions, output, max_bytes=32768)
        workspace = self.trees / f"bundle-{uuid4().hex}"
        workspace.mkdir()
        return (output / "task.md").read_bytes(), workspace

    def grant(self, task, workspace, *, timeout=30):
        card_id = f"card-{uuid4().hex}"
        digest = self.ledger.create_card(
            card_id, repo_root=self.repo, task_key=card_id, trigger_ids=["T5"], prepare_mode="explicit",
            allowed_templates=["review"],
            allowed_targets=[{"adapter": self.adapter_name, "model": self.model, "effort_max": "low"}],
            max_dispatches=1, ttl_minutes=10, timeout_seconds=timeout, workspace_path=workspace,
            task_sha256=sha256(task).hexdigest())
        return self.ledger.approve(card_id, digest, via="codex_prompt")

    def substitute(self, argv, **kwargs):
        self.assertFalse(kwargs["shell"])
        self.assertTrue(str(argv[0]).endswith(".exe"))
        self.started.append((tuple(argv), dict(kwargs["env"])))
        return self.real_popen([sys.executable, "-I", "-B", str(self.stand_in), *argv[1:]], **kwargs)

    def dispatch(self, scenario="success", *, bundle=False, timeout=30, template="review", popen=None, run_id=None):
        if bundle:
            task, workspace = self.bundle_task(scenario)
        else:
            task, workspace = f"Review the worktree. scenario={scenario}".encode(), self.worktree()
        self.task, self.workspace = task, workspace
        self.grant_id = self.grant(task, workspace, timeout=timeout)
        self.run_id = run_id or f"{self.adapter_name.split('-')[0]}-{uuid4().hex}"
        probe = {"available": True, "cli_version": self.cli_version, "flags": set(self.adapter.flags_for(
            "read_only" if bundle else "disposable_worktree"))}
        with patch.object(self.adapter, "probe", return_value=probe), \
                patch("mah.adapters.cli_worker.Popen", side_effect=popen or self.substitute):
            return self.manager.dispatch(
                self.adapter, grant_id=self.grant_id, run_id=self.run_id, repo_root=self.repo, template=template,
                model=self.model, effort="low", task=task, workspace=workspace, worktree_root=self.trees,
                timeout_s=timeout)

    def directory(self):
        return self.manager._directory(self.run_id)

    def verify(self):
        result = (self.directory() / "result.json").read_bytes()
        evidence = VerificationEvidence(task_sha256=sha256(self.task).hexdigest(),
                                        result_sha256=sha256(result).hexdigest(), confirmed=True,
                                        workspace_changed=False, reviewed_findings=0,
                                        bundle_manifest_sha256=self.bundle_sha())
        return Verifier(self.manager).verify(self.run_id, evidence=evidence)

    def bundle_sha(self):
        from mah.context_bundle import bundle_manifest

        sealed = bundle_manifest(self.task)
        return sealed[1] if sealed else None


class CliWorkerStateTests(WorkerFixture, unittest.TestCase):
    def setUp(self):
        registry.register("test-cli", AdapterPolicy(requires_dispatch_scope=True, template_verified=True))
        self.addCleanup(registry.unregister, "test-cli")
        super().setUp()

    def test_success_reaches_collecting_and_verifies(self):
        view = self.dispatch()
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "structured_result_collected"))
        argv, environment = self.started[0]
        self.assertEqual(argv[1:3], ("--mode", "disposable_worktree"))
        self.assertTrue((self.directory() / "testcli.prompt.txt").read_bytes().startswith(b"MultiAgentHarness V2"))
        receipt = json.loads((self.directory() / "testcli.capture.json").read_bytes())
        self.assertEqual((receipt["adapter"], receipt["classification"]), ("test-cli", "COLLECTING"))
        self.assertTrue(MinimalWorker.capture_matches(self.directory(), self.ledger.get_run(self.run_id),
                                                      receipt["result_sha256"]))
        self.assertEqual(self.verify()["state"], "SUCCEEDED")

    def test_bundle_dispatch_uses_an_empty_workspace(self):
        view = self.dispatch(bundle=True)
        self.assertEqual(view["state"], "COLLECTING")
        self.assertEqual(self.started[0][0][1:3], ("--mode", "read_only"))
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(self.verify()["state"], "SUCCEEDED")

    def test_timeout_kills_the_process_and_keeps_the_run_for_review(self):
        view = self.dispatch("timeout", timeout=2)
        self.assertEqual((view["state"], view["state_reason"]), ("SUBMISSION_UNKNOWN", "submission_evidence_missing"))
        receipt = json.loads((self.directory() / "testcli.capture.json").read_bytes())
        self.assertEqual(receipt["termination"], "timeout")

    def test_launch_failures_are_not_started_and_keep_the_grant_used(self):
        for error in (FileNotFoundError(errno.ENOENT, "missing"), PermissionError(errno.EACCES, "denied")):
            with self.subTest(error=type(error).__name__):
                view = self.dispatch(popen=lambda *args, **kwargs: (_ for _ in ()).throw(error))
                self.assertEqual((view["state"], view["state_reason"]), ("SUBMISSION_UNKNOWN", "process_not_started"))
                self.assertEqual(self.ledger.get_grant(self.grant_id)["used_dispatches"], 1)
                receipt = json.loads((self.directory() / "testcli.capture.json").read_bytes())
                self.assertEqual((receipt["termination"], receipt["launch_detail"]["errno"]),
                                 ("not_started", error.errno))
                with patch("mah.adapters.cli_worker.Popen", side_effect=AssertionError("must not relaunch")):
                    again = self.adapter.collect_saved(self.run_id)
                self.assertEqual(again["state"], "SUBMISSION_UNKNOWN")
                resolved = self.manager.resolve(self.run_id, action="mark_failed")
                self.assertEqual((resolved["state"], resolved["state_reason"]), ("FAILED", "user_marked_failed"))
                self.assertEqual(self.ledger.get_grant(self.grant_id)["used_dispatches"], 1)

    def test_output_outside_the_schema_fails(self):
        view = self.dispatch("schema")
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "schema_invalid"))

    def test_corrupt_output_and_failed_exit_without_submission_evidence(self):
        for scenario in ("corrupt", "failed"):
            with self.subTest(scenario=scenario):
                view = self.dispatch(scenario)
                self.assertEqual((view["state"], view["state_reason"]),
                                 ("SUBMISSION_UNKNOWN", "submission_evidence_missing"))
                self.manager.resolve(self.run_id, action="mark_failed")

    def test_worktree_change_is_a_readonly_violation(self):
        view = self.dispatch("dirty")
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "readonly_violation"))

    def test_interrupted_collection_recovers_without_relaunching(self):
        with patch.object(self.adapter, "collect", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                self.dispatch()
        self.assertEqual(len(self.started), 1)
        self.assertTrue((self.directory() / "testcli.process.json").is_file())
        with patch("mah.adapters.cli_worker.Popen", side_effect=AssertionError("must not relaunch")):
            view = self.adapter.collect_saved(self.run_id)
        self.assertEqual(view["state"], "COLLECTING")
        self.assertEqual(len(self.started), 1)

    def test_replay_is_not_supported(self):
        self.dispatch()
        with self.assertRaises(LedgerError) as error:
            self.adapter.collect_saved(self.run_id, replay_output=True)
        self.assertEqual(error.exception.code, 20)

    def test_delegate_implement_is_refused_before_reservation(self):
        workspace = self.worktree()
        with self.assertRaises(LedgerError) as error:
            self.adapter.prepare_invocation(
                run_id=f"test-{uuid4().hex}", repo_root=self.repo, template="delegate-implement", model="m",
                effort="low", task=b"task", workspace=workspace, worktree_root=self.trees, timeout_s=None,
                requirements=None)
        self.assertEqual(error.exception.code, 2)

    def test_launch_refuses_a_non_exe_first_argument(self):
        self.adapter.command = self.root / "bin" / "worker.cmd"
        with self.assertRaises(LedgerError) as error:
            self.dispatch()
        self.assertEqual(error.exception.code, 17)
        self.assertEqual(self.started, [])
        self.assertIsNone(self.ledger.get_run(self.run_id))


class SharedModuleTests(unittest.TestCase):
    """The shared helpers live in cli_common; agy_print re-exports them under the same names."""

    NAMES = ("CAPTURE_STATES", "WORKER_ENV", "InvocationPlan", "_bundle_workspace_snapshot", "_count", "_json",
             "_require_bundle_directory", "_response_agrees", "_response_schema", "_text", "_unique_fields", "_uuid")

    def test_agy_print_reexports_the_shared_helpers(self):
        from mah.adapters import agy_print, cli_common, cli_worker
        for name in self.NAMES:
            with self.subTest(name):
                self.assertIs(getattr(agy_print, name), getattr(cli_common, name))
        self.assertIs(cli_worker.InvocationPlan, cli_common.InvocationPlan)

    def test_base_hook_defaults_keep_the_existing_receipts(self):
        self.assertEqual((CliWorkerAdapter.evidence_key, CliWorkerAdapter.conversation_reason),
                         ("capture_sha256", "conversation_observed"))
        self.assertTrue(CliWorkerAdapter.prompt_via_stdin)
        self.assertFalse(CliWorkerAdapter.replays_output)
        self.assertEqual(CliWorkerAdapter.not_started_outcome(None, None), ("SUBMISSION_UNKNOWN", "process_not_started"))
        self.assertFalse(CliWorkerAdapter.extra_submission(None, None))


if __name__ == "__main__":
    unittest.main()
