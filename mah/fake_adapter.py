"""A local-only Adapter exercising the production ledger and run lifecycle."""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

from .ledger import RUN_ID, LedgerError, require_bound_workspace, timestamp, utc_now, valid_id
from .run_manager import _bytes, _publish, _read
from .verifier import requirements_for

SCENARIOS = {"success", "failed", "timeout", "incomplete", "corrupt", "interrupted",
             "unknown", "denied", "missing_tool"}
WORKER = """
import os, sys, time
scenario = sys.argv[1]
data = sys.stdin.buffer.read()
if scenario == "timeout":
    time.sleep(60)
elif scenario in ("unknown", "interrupted"):
    os._exit(73)
elif scenario == "incomplete":
    sys.stdout.buffer.write(b'{"schema_version":')
elif scenario == "corrupt":
    sys.stdout.buffer.write(b'\\xff')
else:
    sys.stdout.buffer.write(data)
    if scenario == "failed":
        sys.exit(7)
"""


@dataclass
class LaunchHandle:
    process: subprocess.Popen
    started_at: str
    timeout_s: float
    content: bytes
    stdout: bytes = b""
    timed_out: bool = False

    @property
    def pid(self):
        return self.process.pid


def _isolated(path):
    path = Path(path).resolve()
    source = Path(__file__).resolve().parents[1]
    if path.is_relative_to(source) or any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise LedgerError(20, "Fake Adapter state and workspace must stay outside Git worktrees")
    return path


class FakeAdapter:
    id = "fake"

    def __init__(self, *, scenario="success", payload):
        if scenario not in SCENARIOS:
            raise ValueError("Unknown synthetic scenario")
        self.scenario = scenario
        self.payload = payload

    def probe(self):
        return {"available": True, "cli_version": "local-fixture", "flags": {},
                "auth_state": "ok", "notes": "Synthetic local Python worker; no external service"}

    def build_invocation(self, run, template, workspace):
        if run["adapter"] != self.id or run["template"] != template:
            raise LedgerError(20, "Run does not match the Fake Adapter")
        cwd = Path(workspace)
        if cwd.name != "workspace" or cwd.parent.name != run["run_id"] or cwd.parent.parent.name != "runs":
            raise LedgerError(20, "Fake worker requires a private run workspace")
        return {"argv": [sys.executable, "-I", "-B", "-c", WORKER, self.scenario],
                "cwd": str(cwd), "stdin_file": None, "env_overrides": {},
                "timeout_s": 0.2 if self.scenario == "timeout" else 10, "template": template}

    def launch(self, plan):
        cwd = _isolated(plan["cwd"])
        argv = [sys.executable, "-I", "-B", "-c", WORKER, self.scenario]
        if (plan["argv"] != argv or plan["env_overrides"] or cwd.name != "workspace"
                or cwd.parent.parent.name != "runs" or not valid_id(RUN_ID, cwd.parent.name)):
            raise LedgerError(20, "Fake Adapter can only launch its bundled worker")
        content = _bytes({"schema_version": "2.0", "run_id": cwd.parent.name, "template": plan["template"],
                          "status": "failed" if self.scenario == "failed" else "completed",
                          "summary": "合成 Adapter 輸出", "blocked_reason": None, "open_questions": [],
                          "template_payload": self.payload})
        started_at = timestamp(utc_now())
        environment = {key: os.environ[key] for key in ("SystemRoot",) if key in os.environ}
        process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return LaunchHandle(process, started_at, plan["timeout_s"], content)

    def observe(self, handle):
        try:
            handle.stdout, _ = handle.process.communicate(handle.content, timeout=handle.timeout_s)
        except subprocess.TimeoutExpired:
            handle.timed_out = True
            handle.process.kill()
            handle.stdout, _ = handle.process.communicate()
        return {"type": "worker_exit", "exit_code": handle.process.returncode, "timed_out": handle.timed_out,
                "conversation_id": None, "model_observed": "unknown", "effort_observed": "unknown",
                "usage": None, "rate_limits": None}

    def collect(self, run):
        directory = _isolated(run["workspace_path"]).parent
        try:
            result = json.loads(_read(directory / "last_message.txt"))
        except (OSError, ValueError):
            return None
        return result if isinstance(result, dict) else None

    def classify_failure(self, exit_code, stderr, events):
        if exit_code == 73 or any(event.get("timed_out") for event in events):
            return "UNKNOWN"
        return "FAILED"

    def resolve_evidence(self, run):
        directory = _isolated(run["workspace_path"]).parent
        return {"has_done": (directory / "DONE").is_file(), "conversation_exists": None,
                "workspace_changed": None, "notes": "Local fixture artifacts only"}

    def dispatch(self, manager, grant_id, run_id, *, repo_root, template, model, effort, task,
                 requirements=None, timeout_seconds=None):
        requirements = requirements_for({} if requirements is None else requirements)
        directory = _isolated(manager._directory(run_id))
        workspace = directory / "workspace"
        if _isolated(workspace) != workspace:
            raise LedgerError(20, "Fake workspace cannot point outside its run directory")
        if timeout_seconds is not None and (not workspace.is_dir() or any(workspace.iterdir())):
            raise LedgerError(20, "Scoped Fake Adapter requires an existing empty workspace")
        manager.ledger.check_and_reserve(
            grant_id, run_id, repo_root=repo_root, template=template, adapter=self.id, model=model,
            effort=effort, workspace_kind="worktree" if template == "delegate-implement" else "read_only",
            workspace_path=workspace, task_sha256=sha256(task).hexdigest(), timeout_seconds=timeout_seconds,
        )
        manager.prepare(run_id, task)
        if timeout_seconds is None:
            workspace.mkdir()
        run = manager.ledger.get_run(run_id)
        plan = self.build_invocation(run, template, workspace)
        if timeout_seconds is not None:
            plan["timeout_s"] = timeout_seconds
        invocation = _bytes(plan | {
            "run_id": run_id, "task_sha256": run["task_sha256"], "cli_version": "local-fixture",
            "verification_requirements": requirements, "synthetic": True,
        })
        if timeout_seconds is None:
            _publish(directory / "invocation.json", invocation)
        scope = manager.claim_launch(run_id, invocation=invocation if timeout_seconds is not None else None)
        handle = None
        try:
            if scope is not None:
                require_bound_workspace(workspace, scope)
                if any(workspace.iterdir()):
                    raise LedgerError(20, "Scoped Fake Adapter workspace changed before launch")
            handle = self.launch(plan)
            if self.scenario != "unknown":
                manager.mark_running(run_id, pid=handle.pid)
            observation = self.observe(handle)
            events = [{"type": "launch", "pid": handle.pid, "started_at": handle.started_at}, observation]
            if self.scenario in ("success", "denied"):
                for index, name in enumerate(requirements["required_tools"]):
                    denied = self.scenario == "denied" and index == 0
                    events.append({"type": "tool", "name": name, "status": "denied" if denied else "completed",
                                   "exit_code": None if denied else 0})
            _publish(directory / "last_message.txt", handle.stdout)
            _publish(directory / "events.jsonl", b"".join(
                _bytes(event | {"run_id": run_id, "synthetic": True}) + b"\n" for event in events
            ))
            if handle.timed_out:
                manager.timeout(run_id)
            elif self.classify_failure(observation["exit_code"], "", events) == "UNKNOWN":
                state = manager.ledger.get_run(run_id)["state"]
                manager.ledger.transition(run_id, state, "SUBMISSION_UNKNOWN")
            else:
                collected = self.collect(run)
                manager.record_exit(run_id, observation["exit_code"], malformed_output=collected is None)
                if collected is not None:
                    manager.publish_result(run_id, collected)
                manager.collect(run_id)
        except (OSError, ValueError):
            state = manager.ledger.get_run(run_id)["state"]
            if state in ("LAUNCHING", "RUNNING"):
                manager.ledger.transition(run_id, state, "SUBMISSION_UNKNOWN")
            raise
        finally:
            if handle is not None and handle.process.poll() is None:
                handle.process.kill()
                handle.process.communicate()
        return manager.status(run_id)
