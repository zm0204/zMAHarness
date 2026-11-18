"""Generate the agy golden Run directories (refactor spec §1.6).

Run once against the agy code from before the move onto the shared CLI Worker base:

    python -B tests/v2/tools/make_agy_golden.py

Each scenario becomes tests/v2/fixtures/agy-golden/<scenario>/ with the complete Run directory under run/
and the minimal Ledger (cards, grants, runs, run_events) in ledger.json. test_agy_golden.py imports this
module to regenerate every scenario and compares the bytes with the committed fixture.

Deterministic inputs: a fixed Ledger clock, Grant ID, owner PID, process identity and monotonic clock, and
synthetic paths for the repository, worker directory, executable and Run directory, so no local path is
recorded. The real launch() is not used (its Popen and directory checks need real paths); the invocation
comes from build_invocation and claim_launch, and observe, persist_capture, collect_saved, collect,
_classify and _apply run unchanged. Stdout fixtures are synthetic or already de-identified.
"""
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from hashlib import sha256
import json
from os.path import normcase
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mah.adapters import agy_print  # noqa: E402
from mah.adapters.agy_print import AgyPrintAdapter, LaunchHandle, _check_readonly_flags  # noqa: E402
from mah.context_bundle import prepare_bundle  # noqa: E402
from mah.ledger import AGY_OUTPUT_CONFLICT, GrantLedger  # noqa: E402
from mah.run_manager import RunManager, _bytes  # noqa: E402
from mah.verifier import requirements_for  # noqa: E402

FIXTURES = TESTS / "fixtures"
GOLDEN = FIXTURES / "agy-golden"
REAL_REVIEW = FIXTURES / "agy-1.5.5-review-smoke-redacted.json"

CLOCK = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
STARTED_AT = "2026-11-18T09:00:00.000000+00:00"
GRANT_ID = UUID(int=0x601D)
OWNER_PID = 4242
WORKER_PID = 4343
MODEL = "gemini-3.8-flash-low"
TIMEOUT = 60
CLI_VERSION = "1.5.5"
BASELINE = "b" * 64
# Synthetic Windows paths; only their text is recorded.
SYNTHETIC = Path(r"C:\zmah-golden")
REPO = SYNTHETIC / "repo"
EXECUTABLE = SYNTHETIC / "agy" / "bin" / "agy.exe"
WORKSPACES = {"disposable_worktree": SYNTHETIC / "worktrees" / "review",
              "read_only": SYNTHETIC / "worktrees" / "bundle"}
WORKSPACE_IDENTITY = [7, 11]
REVIEW_RUN = "smoke-00000000000000000000000000000000"
SYNTHETIC_TASK = "# 合成任務\n".encode()
REVIEW_STDERR = b"warning: --mode plan has no effect while slash command expansion is disabled.\n"
LOGGED_CONVERSATION = "12345678-1234-4234-8234-123456789abc"
LOG = ("I1112 22:46:56.253018       1 server.go:1233] Created conversation " + LOGGED_CONVERSATION + "\n"
       "I1112 22:46:56.254706       1 server.go:1817] Sending user message\n").encode()


def _run_id(number):
    return f"agy-{number:032x}"


# process: "exited" (wait returns exit_code), "timeout", "interrupted", or None (Popen was refused).
SCENARIOS = {
    "success": {"run_id": REVIEW_RUN, "process": "exited", "wall": 10.492},
    "output_conflict_replay": {"run_id": REVIEW_RUN, "process": "exited", "wall": 10.492, "replay": True},
    "process_not_started": {"run_id": _run_id(3), "process": None},
    "logged_timeout": {"run_id": _run_id(4), "process": "timeout", "wall": 60.25},
    "interrupted_unsubmitted": {"run_id": _run_id(5), "process": "interrupted", "wall": 3.5},
    "bundle_evidence_missing": {"run_id": _run_id(6), "process": "exited", "wall": 7.75, "bundle": True},
}
RUN_FILE_INPUTS = ("task.md", "agy.stdout.pending", "agy.stderr.pending", "agy.cli.log")


def _review_stdout(changed_draft=False):
    raw = REAL_REVIEW.read_bytes()
    if not changed_draft:
        return raw
    document = json.loads(raw)
    final = document["structured_output"]
    draft = final | {"schema_version": "v2", "summary": "Different draft"}
    document["response"] = _bytes(draft).decode() + "\n" + _bytes(final).decode()
    return _bytes(document)


def _bundle_task(scratch):
    """A sealed Manifest v2 task from a synthetic repository with fixed commit dates."""
    repo = scratch / "bundle-source"
    env = os.environ | {"GIT_AUTHOR_DATE": "2026-11-18T09:00:00Z", "GIT_COMMITTER_DATE": "2026-11-18T09:00:00Z"}
    git = ["git", "-c", "user.name=Golden", "-c", "user.email=golden@example.invalid", "-C", str(repo)]
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, env=env)
    (repo / "驗證文件.txt").write_bytes("驗證字串=0123456789abcdef0123456789abcdef\n".encode())
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run([*git, "commit", "-q", "-m", "seed"], check=True, capture_output=True, env=env)
    instructions = scratch / "instructions.md"
    instructions.write_bytes("只分析 Context-Bundle 內的驗證文件.txt。findings 回傳空陣列。".encode())
    prepare_bundle(repo, ["驗證文件.txt"], instructions, scratch / "bundle", max_bytes=32768)
    return (scratch / "bundle" / "task.md").read_bytes()


def _bundle_stdout(run_id):
    result = {"schema_version": "2.0", "run_id": run_id, "template": "review", "status": "completed",
              "summary": "隔離測試｜0123456789abcdef0123456789abcdef", "blocked_reason": None,
              "open_questions": [], "template_payload": {"findings": []}}
    envelope = {"conversation_id": "00000000-0000-4000-8000-000000000006", "status": "SUCCESS",
                "response": json.dumps(result, ensure_ascii=False), "structured_output": result,
                "num_turns": 1, "duration_seconds": 0.1, "usage": {"input_tokens": 1, "output_tokens": 1}}
    return json.dumps(envelope, ensure_ascii=False).encode()


def build_inputs(name, scratch):
    """The original inputs of one scenario: {Run file name: bytes}."""
    spec = SCENARIOS[name]
    if name in ("success", "output_conflict_replay"):
        return {"task.md": SYNTHETIC_TASK, "agy.stdout.pending": _review_stdout(spec.get("replay", False)),
                "agy.stderr.pending": REVIEW_STDERR}
    if name == "bundle_evidence_missing":
        return {"task.md": _bundle_task(scratch), "agy.stdout.pending": _bundle_stdout(spec["run_id"]),
                "agy.stderr.pending": "合成警告\r\n".encode()}
    inputs = {"task.md": SYNTHETIC_TASK, "agy.stdout.pending": b"", "agy.stderr.pending": b""}
    if name == "logged_timeout":
        inputs |= {"agy.stdout.pending": b"partial", "agy.cli.log": LOG}
    return inputs


def fixture_inputs(name):
    run = GOLDEN / name / "run"
    return {item: (run / item).read_bytes() for item in RUN_FILE_INPUTS if (run / item).is_file()}


class FakeProcess:
    """A finished, timed-out or interrupted Worker as seen by observe(); it never runs anything."""

    def __init__(self, behavior):
        self.pid = WORKER_PID
        self.behavior = behavior
        self.killed = False

    def wait(self, timeout=None):
        if self.behavior == "timeout" and not self.killed:
            raise subprocess.TimeoutExpired("agy", timeout)
        if self.behavior == "interrupted" and not self.killed:
            raise KeyboardInterrupt
        return 1 if self.killed else 0

    def poll(self):
        return 1 if self.killed else None

    def kill(self):
        self.killed = True


def _fake_binding(value):
    return {"workspace_path": normcase(str(Path(value).resolve())), "workspace_identity": list(WORKSPACE_IDENTITY)}


def _probe(pid):
    return "alive", f"golden-{pid}"


def generate(name, state, inputs):
    """Produce scenario `name` into `state` (ledger.sqlite3 and runs/<run_id>) from its original inputs."""
    spec = SCENARIOS[name]
    run_id = spec["run_id"]
    task = inputs["task.md"]
    task_sha = sha256(task).hexdigest()
    kind = "read_only" if spec.get("bundle") else "disposable_worktree"
    workspace = WORKSPACES[kind]
    with ExitStack() as stack:
        stack.enter_context(patch("mah.ledger.workspace_binding", _fake_binding))
        stack.enter_context(patch("mah.ledger.uuid4", return_value=GRANT_ID))
        stack.enter_context(patch("mah.run_manager.getpid", return_value=OWNER_PID))
        stack.enter_context(patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("golden never launches")))
        ledger = GrantLedger(state / "ledger.sqlite3", clock=lambda: CLOCK)
        manager = RunManager(ledger, process_probe=_probe)
        adapter = AgyPrintAdapter(manager)
        content = ledger.create_card(
            "golden-card", repo_root=REPO, task_key="golden", trigger_ids=["T5"], prepare_mode="explicit",
            allowed_templates=["review"], allowed_targets=[{"adapter": "agy-print", "model": MODEL, "effort_max": "low"}],
            max_dispatches=1, ttl_minutes=10, timeout_seconds=TIMEOUT, workspace_path=workspace, task_sha256=task_sha)
        grant = ledger.approve("golden-card", content, via="manual_cli")
        ledger.check_and_reserve(grant, run_id, repo_root=REPO, template="review", adapter="agy-print", model=MODEL,
                                 effort="low", workspace_kind=kind, workspace_path=workspace, task_sha256=task_sha,
                                 timeout_seconds=TIMEOUT)
        manager.prepare(run_id, task)
        directory = manager._directory(run_id)
        plan = adapter.build_invocation(
            run_id=run_id, template="review", model=MODEL, effort="low", task=task,
            task_text=task.decode("utf-8-sig"), cwd=workspace, directory=SYNTHETIC / "state" / "runs" / run_id,
            timeout_s=TIMEOUT, executable=EXECUTABLE, baseline=BASELINE, cli_version=CLI_VERSION,
            requirements=requirements_for({}))
        _check_readonly_flags(plan)
        manager.claim_launch(run_id, invocation=plan.metadata)
        for item in ("agy.stdout.pending", "agy.stderr.pending", "agy.cli.log"):
            if item in inputs:
                (directory / item).write_bytes(inputs[item])
        if spec["process"] is None:
            handle = LaunchHandle(plan, None, None, STARTED_AT, 100.0, "process_not_started")
            ticks = iter((101.0,))  # observe reads the clock once, for the wall time
        else:
            process = FakeProcess(spec["process"])
            identity = manager.mark_running(run_id, pid=process.pid)
            handle = LaunchHandle(plan, process, identity, STARTED_AT, 100.0)
            ticks = iter((101.0, 100.0 + spec["wall"]))  # the remaining budget, then the wall time
        snapshot = ((lambda plan: (_ for _ in ()).throw(OSError("worker directory unavailable")))
                    if spec.get("bundle") else (lambda plan: (BASELINE, False)))
        with patch("mah.adapters.agy_print.monotonic", side_effect=lambda: next(ticks)), patch(
                "mah.adapters.agy_print._invocation_snapshot", side_effect=snapshot):
            capture = adapter.observe(handle)
        adapter.persist_capture(handle, capture)
        if spec.get("replay"):
            # The classification an earlier release recorded for this envelope.
            with patch.object(adapter, "_classify", return_value=("FAILED", AGY_OUTPUT_CONFLICT, None)):
                adapter.collect_saved(run_id)
            adapter.collect_saved(run_id, replay_output=True)
        else:
            adapter.collect_saved(run_id)
    return run_id


TABLES = {"cards": "card_id", "grants": "grant_id", "runs": "run_id", "run_events": "id"}


def dump_ledger(database):
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY {key}")]
                for table, key in TABLES.items()}


def restore_ledger(database, data):
    GrantLedger(database)
    with closing(sqlite3.connect(database)) as db, db:
        for table in TABLES:
            for row in data[table]:
                columns = ", ".join(row)
                db.execute(f"INSERT INTO {table} ({columns}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def run_files(directory):
    return {item.name: item.read_bytes() for item in sorted(directory.iterdir()) if item.is_file()}


def ledger_bytes(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def main():
    if GOLDEN.exists():
        raise SystemExit(f"{GOLDEN} already exists; the golden fixture is generated only once")
    with TemporaryDirectory(prefix="agy-golden-") as scratch:
        scratch = Path(scratch)
        for name in SCENARIOS:
            state = scratch / name / "state"
            state.mkdir(parents=True)
            run_id = generate(name, state, build_inputs(name, scratch / name))
            target = GOLDEN / name
            shutil.copytree(state / "runs" / run_id, target / "run")
            (target / "ledger.json").write_bytes(ledger_bytes(dump_ledger(state / "ledger.sqlite3")))
            print(name, run_id)


if __name__ == "__main__":
    main()
