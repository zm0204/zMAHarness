"""Launch and collect the official agy print CLI under an existing grant."""

from dataclasses import asdict, dataclass
from hashlib import sha256
from math import isfinite
import os
from pathlib import Path
import re
from ..executable_path import which
import subprocess
from subprocess import Popen
from time import monotonic

from ..config import user_local_appdata
from ..context_bundle import _workspace_snapshot, _worktree_baseline, bundle_manifest
from ..db import transaction
from ..ledger import (AGY_OUTPUT_CONFLICT, PRE_SUBMIT_REASONS, READ_ONLY_TEMPLATES, TARGET_ID, TEMPLATE_TIMEOUTS,
                      TERMINAL_STATES, TEMPLATES,
                      LedgerError, valid_id)
from ..run_manager import _bytes, _publish, _read, record_observation
from ..verifier import Verifier, requirements_for
# Shared helpers, re-exported under their historical names (imports and test patch targets use them).
from .cli_common import (CAPTURE_STATES, WORKER_ENV, InvocationPlan, _bundle_workspace_snapshot,  # noqa: F401
                         _contains_object, _count, _invalid_constant, _json, _object_sequence_final,
                         _require_bundle_directory, _response_agrees, _response_schema, _text, _unique_fields, _uuid)
from .cli_worker import CliLaunchHandle, CliWorkerAdapter


TOKEN_FIELDS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")


AGY_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
REQUIRED_FLAGS = {"--print", "--output-format", "--model", "--effort", "--mode", "--sandbox",
                  "--settings", "--print-timeout", "--json-schema", "--disable-slash-commands", "--log-file"}
READ_ONLY_DENY = ("write_file(*)", "command(*)", "mcp(*)", "read_url(*)", "execute_url(*)")
BUNDLE_SETTINGS = {
    "toolPermission": "request-review",
    "enableTerminalSandbox": True,
    "disableSlashCommands": True,
    "allowNonWorkspaceAccess": False,
    "useG1Credits": False,
    "permissions": {
        "allow": [],
        "ask": [],
        "deny": ["read_file(*)", *READ_ONLY_DENY],
    },
}
DISPOSABLE_WORKTREE_SETTINGS = {
    **BUNDLE_SETTINGS,
    "disableSlashCommands": False,
    "permissions": {"allow": ["read_file(*)"], "ask": [], "deny": list(READ_ONLY_DENY)},
}


def _worker_environment():
    return {key: value for key, value in os.environ.items() if key.upper() in WORKER_ENV}


def _invocation_snapshot(plan):
    """Kept here (not imported) so that patching agy_print._bundle_workspace_snapshot also applies to it."""
    if plan.workspace_kind == "read_only":
        return _bundle_workspace_snapshot(plan.cwd)
    return _workspace_snapshot(plan.cwd)


def _check_readonly_flags(plan):
    if plan.workspace_kind not in ("read_only", "disposable_worktree"):
        return
    # The final argument is prompt text, not a flag.
    options = plan.argv[1:-1]
    names = [arg.split("=", 1)[0] for arg in options]
    if plan.workspace_kind == "read_only":
        required = ("--sandbox", "--disable-slash-commands")
        if "--mode" in names or any(names.count(flag) != 1 or flag not in options for flag in required):
            raise LedgerError(17, "agy Bundle launch requires --sandbox and --disable-slash-commands without --mode")
        profile, label = BUNDLE_SETTINGS, "Bundle"
    else:
        if "--disable-slash-commands" in names:
            raise LedgerError(17, "agy --mode plan conflicts with --disable-slash-commands; read-only launch blocked")
        modes = [i for i, name in enumerate(names) if name == "--mode"]
        if (len(modes) != 1 or options[modes[0]:modes[0] + 2] != ("--mode", "plan")
                or names.count("--sandbox") != 1 or "--sandbox" not in options):
            raise LedgerError(17, "agy read-only launch requires explicit --mode plan and --sandbox")
        profile, label = DISPOSABLE_WORKTREE_SETTINGS, "Disposable Worktree"
    settings = [i for i, name in enumerate(names) if name == "--settings"]
    if len(settings) != 1 or options[settings[0]:settings[0] + 2] != ("--settings", _bytes(profile).decode()):
        raise LedgerError(17, f"agy {label} session settings differ from the fixed profile")


# Tests and recovery tools build handles positionally; launch_detail keeps its default.
LaunchHandle = CliLaunchHandle


def normalize_stdout(raw):
    """Read the observed 1.5.5 envelope. Raw bytes remain the authoritative record."""
    try:
        data = _json(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    tokens = {}
    if isinstance(usage, dict):
        tokens = {key: usage[key] for key in TOKEN_FIELDS if _count(usage.get(key)) is not None}
    duration = data.get("duration_seconds")
    if _count(duration) is None and not (type(duration) is float and isfinite(duration) and duration >= 0):
        duration = None
    optional = {key: data[key] for key in ("structured_output", "json_schema") if key in data}
    response = _text(data.get("response"))
    if "response" in data and response is None:
        optional["response_invalid"] = True
    return {
        "conversation_id": _uuid(data.get("conversation_id")),
        "raw_status": _text(data.get("status")),
        "response": response,
        "duration_seconds": duration,
        "num_turns": _count(data.get("num_turns")),
        "usage": tokens or None,
        "model_observed": "unknown",
        "effort_observed": "unknown",
        "rate_limits": None,
    } | optional


AGY_LOG_PREFIX = re.compile(rb"^[IWEF]\d{4}\s+\d\d:\d\d:\d\d\.\d+\s+\d+\s+[^\]\r\n]+\.go:\d+\]\s+")
AGY_LOG_CONVERSATION = re.compile(rb"Created conversation ([0-9A-Fa-f-]{36})(?:\s|$)")
AGY_LOG_SEND = re.compile(rb"Sending user message(?:\s|$)")
AGY_LOG_STREAM = re.compile(rb"(?:URL: https://[^\s?]+/[^\s?]*)?streamGenerateContent(?:\?|\s|$)")


def _log_submission(path):
    """Accept one run-local conversation followed by an outbound CLI log marker."""
    try:
        raw = _read(path)
    except OSError:
        return None
    conversation = marker = None
    for line in raw.splitlines():
        message = AGY_LOG_PREFIX.sub(b"", line, count=1)
        if created := AGY_LOG_CONVERSATION.match(message):
            candidate = _uuid(created[1].decode("ascii"))
            if candidate is None or conversation not in (None, candidate):
                return None
            conversation = candidate
        elif conversation is not None and marker is None:
            if AGY_LOG_SEND.match(message):
                marker = "Sending user message"
            elif AGY_LOG_STREAM.match(message):
                marker = "streamGenerateContent"
    if marker is not None:
        return {"conversation_id": conversation, "marker": marker, "sha256": sha256(raw).hexdigest()}
    return None


@dataclass(frozen=True)
class AgyCapture:
    """Receipt from the local process observer, bound before the process is started.

    Termination and tool events must come from the observer, never the model.
    The single-JSON agy envelope provides no tool events.
    """

    run_id: str
    task_sha256: str
    invocation_sha256: str
    stdout: bytes
    stderr: bytes
    exit_code: int | None
    termination: str = "exited"
    pre_submit_reason: str | None = None
    tool_events: tuple = ()
    wall_milliseconds: int | None = None
    workspace_changed: bool | None = None
    workspace_after_sha256: str | None = None
    log_submission: dict | None = None


def _check_capture(capture):
    if (not isinstance(capture.stdout, bytes) or not isinstance(capture.stderr, bytes)
            or (capture.exit_code is not None and type(capture.exit_code) is not int)
            or (capture.wall_milliseconds is not None and _count(capture.wall_milliseconds) is None)
            or capture.termination not in ("exited", "timeout", "interrupted", "not_started")):
        raise LedgerError(2, "Invalid agy process capture")
    if capture.termination == "not_started":
        if (capture.pre_submit_reason not in PRE_SUBMIT_REASONS or capture.exit_code is not None
                or capture.stdout or capture.tool_events):
            raise LedgerError(20, "Pre-submit receipt must prove the process did not start")
    elif capture.pre_submit_reason is not None:
        raise LedgerError(20, "A started process cannot claim a pre-submit refund")
    if capture.log_submission is not None and (
            capture.termination not in ("timeout", "interrupted") or not isinstance(capture.log_submission, dict)
            or capture.log_submission.keys() != {"conversation_id", "marker", "sha256"}):
        raise LedgerError(30, "Invalid agy log submission receipt")
    for event in capture.tool_events:
        if (not isinstance(event, dict) or event.keys() != {"type", "name", "status", "exit_code"}
                or event["type"] != "tool" or not isinstance(event["name"], str) or not event["name"]
                or event["status"] not in ("completed", "failed", "denied")
                or (event["exit_code"] is not None and type(event["exit_code"]) is not int)):
            raise LedgerError(2, "Invalid local tool event")


def capture_matches(directory, run, result_sha256):
    """Check that Verifier is reading the result and audit stream from the same capture."""
    try:
        raw = _read(directory / "agy.capture.json")
        receipt = _json(raw)
        if (receipt["adapter"] != "agy-print" or receipt["run_id"] != run["run_id"]
                or receipt["task_sha256"] != run["task_sha256"] or receipt["exit_code"] != run["exit_code"]):
            return False
        if receipt["classification"] == "FAILED" and receipt["reason"] == AGY_OUTPUT_CONFLICT:
            if (not receipt["process_receipt_sha256"] or run["result_sha256"] != result_sha256
                    or _read(directory / "agy.replay.json") != _replay_receipt(run, raw, result_sha256)):
                return False
        elif receipt["classification"] != "COLLECTING" or receipt["result_sha256"] != result_sha256:
            return False
        observed_id = receipt["observation"]["conversation_id"]
        if observed_id is not None and observed_id != run["conversation_id"]:
            return False
        process_sha = receipt.get("process_receipt_sha256")
        if process_sha is not None and sha256(_read(directory / "agy.process.json")).hexdigest() != process_sha:
            return False
        files = {"invocation.json": "invocation_sha256", "agy.stdout.json": "stdout_sha256",
                 "agy.stderr.txt": "stderr_sha256", "events.jsonl": "events_sha256"}
        return all(sha256(_read(directory / name)).hexdigest() == receipt[field] for name, field in files.items())
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return False


def _replay_receipt(run, original, result_sha256):
    return _bytes({"run_id": run["run_id"], "original_capture_sha256": sha256(original).hexdigest(),
                   "result_sha256": result_sha256})


class AgyPrintAdapter(CliWorkerAdapter):
    """Synchronous official agy print Adapter with resumable local evidence.

    launch, observe, persist_capture, collect_saved, _classify and _apply come from CliWorkerAdapter. agy keeps
    its own probe, invocation (the prompt is the last argument, not stdin), log-file submission evidence,
    pre-submit refund, structured-output replay and collect, so its receipts stay byte for byte the same.
    """

    id = "agy-print"
    prefix = "agy"
    capture_reason = "agy_capture_integrity"
    efforts = AGY_EFFORTS
    command_setting = "agy_command"
    prompt_via_stdin = False
    replays_output = True
    evidence_key = "agy_capture_sha256"
    conversation_reason = "agy_conversation_observed"
    messages = {
        "reservation_mismatch": "agy invocation does not match its reservation",
        "timeout": "agy timeout exceeds template limits",
        "changed_before_launch": "agy worker directory changed before launch",
        "bundle_grant": "agy Context-Bundle requires a grant limited to one dispatch",
        "bundle_changed_after_claim": "agy Bundle workspace changed after launch claim",
        "worktree_changed_after_claim": "agy disposable worktree changed after launch claim",
        "receipt_other_run": "agy process receipt belongs to a different run",
        "receipt_streams": "agy process receipt does not match its streams",
        "replay_receipt_missing": "Output replay requires the sealed agy process receipt",
    }
    # The registry calls the module function; the base classmethod knows no replay receipt.
    capture_matches = staticmethod(capture_matches)

    def _executable(self):
        command = which(str(self.command)) if self.command else None
        if command is None and self.command == "agy" and os.name == "nt":
            local_app_data = user_local_appdata()
            if local_app_data and local_app_data.is_absolute():
                installed = local_app_data / "agy" / "bin" / "agy.exe"
                if installed.is_file():
                    command = installed
        if not command:
            raise LedgerError(14, "Configured agy executable is unavailable")
        executable = Path(command).resolve(strict=True)
        if not executable.is_file() or (os.name == "nt" and executable.suffix.lower() != ".exe"):
            raise LedgerError(14, "agy must resolve to a native executable")
        return executable

    def probe(self, *, cwd=None):
        """Probe local binary capabilities without auth or inference."""
        try:
            exe = self._executable()
            reports = [subprocess.run([str(exe), option], stdin=subprocess.DEVNULL, capture_output=True,
                                      shell=False, cwd=cwd, env=_worker_environment(), timeout=10)
                       for option in ("--version", "--help")]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {"available": False, "cli_version": None, "flags": {}, "auth_state": "unknown",
                    "notes": "agy version or help probe failed"}
        version = reports[0].stdout.decode("utf-8", errors="replace").strip()
        help_text = b"\n".join((reports[1].stdout, reports[1].stderr)).decode("utf-8", errors="replace")
        options = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", help_text))
        flags = {name: name in options for name in sorted(REQUIRED_FLAGS)}
        return {"available": bool(version) and all(report.returncode == 0 for report in reports),
                "cli_version": version or None, "flags": flags, "auth_state": "unknown",
                "notes": "Only --version and --help were used"}

    @staticmethod
    def _bundle_for_launch(task, template):
        bundle = bundle_manifest(task)
        if bundle is not None and (bundle[0]["version"] != 2 or template not in READ_ONLY_TEMPLATES):
            raise LedgerError(17, "agy requires a sealed Manifest v2 bundle and a read-only analysis template")
        return bundle

    def prepare_invocation(self, *, run_id, repo_root, template, model, effort, task, workspace,
                           worktree_root, timeout_s, requirements):
        if template not in TEMPLATES or not valid_id(TARGET_ID, model) or effort not in AGY_EFFORTS:
            raise LedgerError(2, "Invalid agy template, model or effort")
        if not isinstance(task, bytes) or not 0 < len(task) <= self.max_task_bytes:
            raise LedgerError(16, "Task exceeds the configured limit or is empty")
        task_text = task.decode("utf-8-sig")
        if "\0" in task_text or not task_text.strip():
            raise LedgerError(2, "Task must be nonempty UTF-8 without NUL")
        timeout_s = TEMPLATE_TIMEOUTS[template] if timeout_s is None else timeout_s
        if type(timeout_s) is not int or not 1 <= timeout_s <= TEMPLATE_TIMEOUTS[template]:
            raise LedgerError(2, "agy timeout exceeds template limits")
        requirements = requirements_for({} if requirements is None else requirements)
        directory = self.manager._directory(run_id)
        cwd = Path(workspace).resolve()
        repo = Path(repo_root).resolve()
        root = Path(worktree_root).resolve()
        bundle = self._bundle_for_launch(task, template)
        if bundle is None:
            baseline = _worktree_baseline(repo, cwd, root, template, run_id)
        else:
            _require_bundle_directory(cwd, repo, self.manager.database.parent)
            if cwd == root or not cwd.is_relative_to(root):
                raise LedgerError(20, "agy Bundle workspace must be a directory under worktree_root")
            baseline, nonempty = _bundle_workspace_snapshot(cwd)
            if nonempty:
                raise LedgerError(20, "agy Bundle workspace must start empty")
        if directory.is_relative_to(cwd):
            raise LedgerError(20, "agy evidence directory must stay outside the worker worktree")
        exe = self._executable()
        capabilities = self.probe(cwd=cwd if bundle is not None else None)
        if not capabilities["available"]:
            raise LedgerError(14, "agy version or help probe failed")
        if bundle is not None:
            flags = REQUIRED_FLAGS - {"--mode"}
        elif template != "delegate-implement":
            flags = REQUIRED_FLAGS - {"--disable-slash-commands"}
        else:
            flags = REQUIRED_FLAGS
        if not all(capabilities["flags"].get(flag, False) for flag in flags):
            raise LedgerError(15, "agy does not report all required print flags")
        plan = self.build_invocation(
            run_id=run_id, template=template, model=model, effort=effort, task=task, task_text=task_text,
            cwd=cwd, directory=directory, timeout_s=timeout_s, executable=exe, baseline=baseline,
            cli_version=capabilities["cli_version"], requirements=requirements,
        )
        _check_readonly_flags(plan)
        return plan

    def build_invocation(self, *, run_id, template, model, effort, task, task_text, cwd, directory, timeout_s,
                         executable, baseline, cli_version, requirements):
        """Build a fixed argument vector. No probe, shell expansion, or launch."""
        bundle = self._bundle_for_launch(task, template)
        if bundle is not None:
            kind = "read_only"
            mode_flags = ()
            settings = _bytes(BUNDLE_SETTINGS).decode()
        elif template == "delegate-implement":
            kind = "worktree"
            mode_flags = ("--mode", "accept-edits")
            settings = '{"allowNonWorkspaceAccess":false,"useG1Credits":false}'
        else:
            kind = "disposable_worktree"
            mode_flags = ("--mode", "plan")
            settings = _bytes(DISPOSABLE_WORKTREE_SETTINGS).decode()
        slash_flags = () if kind == "disposable_worktree" else ("--disable-slash-commands",)
        schema = _response_schema(run_id, template, self.manager.max_summary_chars)
        prompt = f"MultiAgentHarness V2 run_id={run_id}, template={template}\n" + (
            "請依 JSON Schema 回傳單一結果物件。\n\n" + task_text
        )
        argv = (str(executable), "--model", model, "--effort", effort, *mode_flags,
                "--sandbox", "--settings", settings,
                "--output-format", "json", "--print-timeout", f"{timeout_s}s",
                "--json-schema", _bytes(schema).decode(), *slash_flags,
                "--log-file", str(directory / "agy.cli.log"), "-p", prompt)
        if any(any(part in value.lower() for part in ("dangerously", "skip-permissions", "bypass"))
               for value in argv[1:-1] if value.startswith("-")):
            raise LedgerError(17, "Forbidden agy flag")
        if os.name == "nt" and len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32767:
            raise LedgerError(16, "Task and schema exceed the Windows command-line limit")
        task_sha = sha256(task).hexdigest()
        metadata = {
            "run_id": run_id, "task_sha256": task_sha, "cli_version": cli_version,
            "argv": [*argv[:-1], "<task.md plus run header>"], "prompt_sha256": sha256(prompt.encode()).hexdigest(),
            "cwd": str(cwd), "workspace_baseline_sha256": baseline, "workspace_kind": kind,
            "timeout_s": timeout_s, "verification_requirements": requirements, "stdin_file": None,
        }
        if bundle is not None:
            metadata.update(launch_profile="context-bundle", bundle_manifest_sha256=bundle[1],
                            permissions_observed="unknown", sandbox_observed="unknown", tools_observed="unknown")
        return InvocationPlan(run_id, argv, cwd, kind, timeout_s, _bytes(metadata), baseline, task_sha,
                              model, effort, template)

    # ---- CliWorkerAdapter hooks ---------------------------------------------------------------------
    def resolve_executable(self):
        return self._executable()

    def worker_environment(self):
        return _worker_environment()

    def normalize_stdout(self, raw):
        return normalize_stdout(raw)

    def check_readonly(self, plan):
        _check_readonly_flags(plan)

    def _guard_argv(self, plan):
        # agy is checked by its fixed read-only profile only, as before the move onto the base.
        _check_readonly_flags(plan)

    def invocation_mismatch(self):
        return LedgerError(20, "agy launch arguments do not match the prepared invocation")

    def _bundle_directory(self, cwd, repo):
        _require_bundle_directory(cwd, repo, self.manager.database.parent)

    # This module's names are looked up at call time, so patches of agy_print.Popen, _invocation_snapshot
    # and monotonic keep applying to the shared lifecycle.
    def _spawn(self, argv, **options):
        return Popen(argv, **options)

    def _snapshot(self, plan):
        return _invocation_snapshot(plan)

    def _clock(self):
        return monotonic()

    def make_capture(self, handle, *, exit_code, termination, wall_milliseconds, after):
        """After a timeout or interruption without a stdout status, seal the run-local CLI log evidence."""
        directory = self.manager._directory(handle.plan.run_id)
        stdout = _read(directory / "agy.stdout.pending")
        log_submission = None
        if handle.process is not None and termination in ("timeout", "interrupted"):
            observed = normalize_stdout(stdout)
            if not (observed and observed["raw_status"]):
                log_submission = _log_submission(directory / "agy.cli.log")
        return AgyCapture(
            handle.plan.run_id, handle.plan.task_sha256, sha256(handle.plan.metadata).hexdigest(),
            stdout, _read(directory / "agy.stderr.pending"), exit_code,
            termination=termination, pre_submit_reason=handle.launch_error if termination == "not_started" else None,
            wall_milliseconds=wall_milliseconds, workspace_changed=None if after is None else after != handle.plan.baseline,
            workspace_after_sha256=after, log_submission=log_submission,
        )

    def process_record(self, handle, capture):
        record = asdict(capture)
        if capture.log_submission is None:
            record.pop("log_submission")
        record["stdout"] = sha256(capture.stdout).hexdigest()
        record["stderr"] = sha256(capture.stderr).hexdigest()
        record["process"] = handle.process_identity
        record["started_at"] = handle.started_at
        record["launch_error"] = handle.launch_error
        return self._containment_record(handle, record)

    def capture_from_record(self, directory, record, streams):
        record.pop("job_object_error", None)
        for key in ("process", "started_at", "launch_error"):
            record.pop(key)
        record["tool_events"] = tuple(record["tool_events"])
        return AgyCapture(**(record | streams))

    def collect_sealed(self, run_id, capture, *, process_receipt_sha256, replay_output):
        return self.collect(run_id, capture, process_receipt_sha256=process_receipt_sha256,
                            replay_output=replay_output)

    def not_started_outcome(self, capture):
        return "FAILED_PRE_SUBMIT", capture.pre_submit_reason

    def extra_submission(self, capture):
        return capture.log_submission is not None

    def readonly_violation(self, capture, run):
        return capture.workspace_changed is True and run["template"] != "delegate-implement"

    def collect(self, run_id, capture, *, process_receipt_sha256=None, replay_output=False):
        """Consume an already captured local process receipt. This method cannot launch."""
        _check_capture(capture)
        if capture.run_id != run_id:
            raise LedgerError(30, "agy capture belongs to a different run")
        manager = self.manager
        directory = manager._directory(run_id)
        with transaction(manager.database) as db:
            run = manager._run(db, run_id)
            if (run["adapter"] != self.id or run["task_sha256"] != capture.task_sha256
                    or manager._task_evidence(run) != "verified"):
                raise LedgerError(30, "agy capture does not match the reserved task")
            invocation = _read(directory / "invocation.json")
            if sha256(invocation).hexdigest() != capture.invocation_sha256:
                raise LedgerError(30, "agy capture does not match the invocation")
            metadata = _json(invocation.decode("utf-8"))
            if (metadata["run_id"], metadata["task_sha256"]) != (run_id, run["task_sha256"]):
                raise LedgerError(30, "agy invocation belongs to a different task")
            required = requirements_for(metadata["verification_requirements"])["required_tools"]
            tool_reason, _ = Verifier._check_tools(capture.tool_events, required)
            observed = normalize_stdout(capture.stdout)
            response = observed["response"] if observed else None
            conversation = observed["conversation_id"] if observed else None
            log_submission = capture.log_submission
            if log_submission is not None:
                if (process_receipt_sha256 is None or (observed and observed["raw_status"])
                        or _log_submission(directory / "agy.cli.log") != log_submission
                        or conversation not in (None, log_submission["conversation_id"])):
                    raise LedgerError(30, "agy log submission differs from the sealed process capture")
                conversation = log_submission["conversation_id"]
            if conversation and (run["conversation_id"] not in (None, conversation) or db.execute(
                "SELECT 1 FROM runs WHERE adapter = ? AND conversation_id = ? AND run_id <> ?",
                (self.id, conversation, run_id),
            ).fetchone()):
                raise LedgerError(30, "agy conversation belongs to a different capture")
            if run["exit_code"] is not None and run["exit_code"] != capture.exit_code:
                raise LedgerError(30, "agy exit code conflicts with the stored receipt")
            receipt_path = directory / "agy.capture.json"
            try:
                previous = _read(receipt_path)
            except FileNotFoundError:
                previous = None
            terminal = run["state"] in TERMINAL_STATES
            if terminal and previous is None:
                raise LedgerError(20, "Terminal agy run has no original capture")
            original = _json(previous) if previous is not None else None
            output_conflict = (original is not None and original["classification"] == "FAILED"
                               and original["reason"] == AGY_OUTPUT_CONFLICT)
            replayed = output_conflict and run["result_sha256"] is not None
            if replay_output and not (output_conflict and (terminal or replayed)):
                raise LedgerError(20, "Only a recorded structured output conflict supports replay")
            if terminal or output_conflict:
                state, reason, result_hash = (original[key] for key in ("classification", "reason", "result_sha256"))
                result = None
            else:
                state, reason, result = self._classify(capture, observed, run, tool_reason)
                result_hash = sha256(_bytes(result)).hexdigest() if result is not None else None
            observation = {key: value for key, value in (observed or {}).items()
                           if key not in {"response", "structured_output", "json_schema", "response_invalid"}}
            receipt = {
                "adapter": self.id, "run_id": run_id, "task_sha256": run["task_sha256"],
                "invocation_sha256": capture.invocation_sha256,
                "stdout_sha256": sha256(capture.stdout).hexdigest(),
                "stderr_sha256": sha256(capture.stderr).hexdigest(),
                "exit_code": capture.exit_code, "termination": capture.termination,
                "wall_milliseconds": capture.wall_milliseconds,
                "workspace_changed": capture.workspace_changed,
                "workspace_after_sha256": capture.workspace_after_sha256,
                "process_receipt_sha256": process_receipt_sha256,
                "model_requested": run["model_requested"], "effort_requested": run["effort_requested"],
                "cli_version": metadata.get("cli_version"), "observation": observation,
                "classification": state, "reason": reason,
                "result_sha256": result_hash,
            }
            if log_submission is not None:
                receipt["log_submission"] = log_submission
            events = ({"type": "worker_exit", **receipt}, *capture.tool_events)
            journal = b"".join(_bytes(event | {"run_id": run_id}) + b"\n" for event in events)
            receipt["events_sha256"] = sha256(journal).hexdigest()
            serialized = _bytes(receipt)
            if previous is not None and previous != serialized:
                raise LedgerError(30, "agy capture differs from the recorded receipt")
            files = {"agy.stdout.json": capture.stdout, "agy.stderr.txt": capture.stderr, "events.jsonl": journal}
            if response is not None:
                files["last_message.txt"] = response.encode("utf-8")
            if terminal or replayed:
                if any(_read(directory / name) != content for name, content in files.items()):
                    raise LedgerError(30, "Stored agy evidence differs from the original capture")
                if replay_output and run["state"] == "FAILED" and run["result_sha256"] is None:
                    run = self._recollect_output(db, run, capture, observed, tool_reason, previous,
                                                 process_receipt_sha256)
                if output_conflict and run["result_sha256"] is not None:
                    if not capture_matches(directory, run, run["result_sha256"]):
                        raise LedgerError(30, "Recollected result does not match the original agy evidence")
                return manager._summary(db, run) | {"agy_capture": receipt}
            if run["state"] not in CAPTURE_STATES:
                raise LedgerError(20, "agy capture requires a claimed run")
            if run["state"] == "COLLECTING" and previous is None:
                raise LedgerError(30, "An existing collection needs its original agy capture")
            if (state == "FAILED_PRE_SUBMIT" and run["state"] != "FAILED_PRE_SUBMIT"
                    and (run["state"] != "LAUNCHING" or run["conversation_id"] is not None)):
                raise LedgerError(20, "Pre-submit refund requires an unsubmitted launch")
            _publish(receipt_path, serialized)
            for name, content in files.items():
                _publish(directory / name, content)
            record_observation(db, run_id, observed)
            run = self._apply(db, manager._run(db, run_id), state, reason, result, conversation, receipt)
            return manager._summary(db, run) | {"agy_capture": receipt}

    def _recollect_output(self, db, run, capture, observed, tool_reason, original, process_sha):
        directory = self.manager._directory(run["run_id"])
        if not process_sha or sha256(_read(directory / "agy.process.json")).hexdigest() != process_sha:
            raise LedgerError(30, "Output replay requires the original sealed agy process receipt")
        state, _, result = self._classify(capture, observed, run, tool_reason)
        if state != "COLLECTING" or result is None or result["status"] != "completed":
            raise LedgerError(30, "The saved agy output still does not contain a collectable completed result")
        result_sha = sha256(_bytes(result)).hexdigest()
        replay = _replay_receipt(run, original, result_sha)
        run = self.manager._change(db, run, "COLLECTING", reason="agy_output_recollected",
                                   evidence={"agy_replay_sha256": sha256(replay).hexdigest()},
                                   result_sha256=result_sha)
        _publish(directory / "agy.replay.json", replay)
        self.manager._publish_result(run, result)
        return run
