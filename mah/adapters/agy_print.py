"""Launch and collect the official agy print CLI under an existing grant."""

from dataclasses import asdict, dataclass
import errno
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
from shutil import which
import signal
import subprocess
from subprocess import Popen
from time import monotonic
from uuid import UUID

from ..config import user_local_appdata
from ..context_bundle import _workspace_snapshot, _worktree_baseline, bundle_manifest
from ..db import transaction
from ..ledger import (AGY_OUTPUT_CONFLICT, PRE_SUBMIT_REASONS, READ_ONLY_TEMPLATES, TARGET_ID, TEMPLATE_TIMEOUTS,
                      TERMINAL_STATES, TEMPLATES,
                      LedgerError, event, require_bound_workspace, timestamp, valid_id, workspace_binding)
from ..run_manager import _bytes, _publish, _read, _result_document
from ..verifier import TEMPLATES_DIR, Verifier, _template_valid, requirements_for


TOKEN_FIELDS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
CAPTURE_STATES = {"LAUNCHING", "RUNNING", "COLLECTING", "SUBMISSION_UNKNOWN", "TIMED_OUT"}


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
WORKER_ENV = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT",
              "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
              "PROGRAMFILES", "PROGRAMFILES(X86)", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}


def _worker_environment():
    return {key: value for key, value in os.environ.items() if key.upper() in WORKER_ENV}


def _bundle_workspace_snapshot(workspace):
    binding = workspace_binding(workspace)
    with os.scandir(workspace) as entries:
        nonempty = next(entries, None) is not None
    return sha256(_bytes(binding | {"empty": not nonempty})).hexdigest(), nonempty


def _require_bundle_directory(workspace, repo, state):
    if (workspace.is_relative_to(repo) or workspace.is_relative_to(state) or state.is_relative_to(workspace)
            or any(os.path.lexists(parent / ".git") for parent in (workspace, *workspace.parents))):
        raise LedgerError(20, "agy Bundle workspace must stay outside Git and Harness state")


def _invocation_snapshot(plan):
    if plan.workspace_kind == "read_only":
        return _bundle_workspace_snapshot(plan.cwd)
    return _workspace_snapshot(plan.cwd)


def _response_schema(run_id, template, max_summary_chars):
    payload = _json((TEMPLATES_DIR / template / "result.schema.json").read_bytes())
    properties = {
        "schema_version": {"const": "2.0"}, "run_id": {"const": run_id}, "template": {"const": template},
        "status": {"enum": ["completed", "partial", "blocked", "failed"]},
        "summary": {"type": "string", "maxLength": max_summary_chars},
        "blocked_reason": {"type": ["string", "null"]},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "template_payload": payload["properties"]["template_payload"],
    }
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


@dataclass(frozen=True)
class InvocationPlan:
    run_id: str
    argv: tuple[str, ...]
    cwd: Path
    workspace_kind: str
    timeout_s: int
    metadata: bytes
    baseline: str
    task_sha256: str
    model: str
    effort: str
    template: str
    stdin_file: None = None


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


@dataclass(frozen=True)
class LaunchHandle:
    plan: InvocationPlan
    process: Popen | None
    process_identity: dict | None
    started_at: str
    clock_started: float
    launch_error: str | None = None

    @property
    def pid(self):
        return self.process.pid if self.process is not None else None


def _unique_fields(pairs):
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("Duplicate JSON field")
    return data


def _invalid_constant(value):
    raise ValueError("Non-JSON numeric constant")


def _json(value):
    return json.loads(value, object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)


def _text(value):
    if isinstance(value, str):
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            pass
    return None


def _uuid(value):
    if isinstance(value, str):
        try:
            if str(UUID(value)) == value.lower():
                return value.lower()
        except ValueError:
            pass
    return None


def _count(value):
    return value if type(value) is int and value >= 0 else None


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


def _response_agrees(response, structured, schema_echo, expected_schema):
    """Allow complete draft objects before the display copy of the structured result."""
    text = response.strip() if response else ""
    decoder = json.JSONDecoder(object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)
    try:
        if schema_echo is not None and _bytes(schema_echo) != _bytes(expected_schema):
            return False
        if not text or text == structured["summary"].strip():
            return True
        canonical = _bytes(structured)
        while text:
            final, end = decoder.raw_decode(text)
            if not isinstance(final, dict):
                return False
            text = text[end:].lstrip()
        return _bytes(final) == canonical
    except (ValueError, RecursionError):
        return False


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


class AgyPrintAdapter:
    """Synchronous official agy print Adapter with resumable local evidence."""

    id = "agy-print"

    def __init__(self, manager, *, command=None, max_task_bytes=32768):
        self.manager = manager
        self.command = command
        self.max_task_bytes = max_task_bytes

    @classmethod
    def from_config(cls, manager, config):
        command = config.values["adapters"]["agy_command"]
        # A single executable path relative to the local TOML, or a command on PATH.
        if Path(command).is_absolute() or "/" in command or chr(92) in command:
            command = str((config.path.parent / Path(command)).resolve())
        return cls(manager, command=command, max_task_bytes=config.values["limits"]["max_task_bytes"])

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

    def find_executable(self):
        """Read-only lookup for preflight: the resolved executable, or None. Never runs it."""
        try:
            return self._executable()
        except (LedgerError, OSError):
            return None

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

    def launch(self, plan):
        # The claim is the sole Popen boundary, also when called directly via the Adapter.
        _check_readonly_flags(plan)
        run = self.manager.ledger.get_run(plan.run_id)
        if run is None or (run["adapter"], run["template"], run["model_requested"], run["effort_requested"],
                           run["task_sha256"], Path(run["workspace_path"]), run["workspace_kind"]) != (
                self.id, plan.template, plan.model, plan.effort, plan.task_sha256, plan.cwd, plan.workspace_kind):
            raise LedgerError(10, "agy invocation does not match its reservation")
        directory = self.manager._directory(plan.run_id)
        metadata = _json(plan.metadata)
        task = _read(directory / "task.md")
        if not 1 <= plan.timeout_s <= TEMPLATE_TIMEOUTS[plan.template]:
            raise LedgerError(2, "agy timeout exceeds template limits")
        if plan.workspace_kind == "read_only":
            _require_bundle_directory(plan.cwd, Path(run["repo_root"]), self.manager.database.parent)
        baseline, status = _invocation_snapshot(plan)
        if status or baseline != plan.baseline:
            raise LedgerError(20, "agy worker directory changed before launch")
        expected = self.build_invocation(
            run_id=plan.run_id, template=run["template"], model=run["model_requested"],
            effort=run["effort_requested"], task=task, task_text=task.decode("utf-8-sig"),
            cwd=plan.cwd, directory=directory, timeout_s=plan.timeout_s, executable=self._executable(),
            baseline=baseline, cli_version=metadata["cli_version"],
            requirements=requirements_for(metadata["verification_requirements"]),
        )
        if expected != plan:
            raise LedgerError(20, "agy launch arguments do not match the prepared invocation")
        if plan.workspace_kind == "read_only":
            grant = self.manager.ledger.get_grant(run["grant_id"])
            if grant is None or grant["max_dispatches"] != 1:
                raise LedgerError(10, "agy Context-Bundle requires a grant limited to one dispatch")
        scope = self.manager.claim_launch(plan.run_id, invocation=plan.metadata)
        started_at = timestamp(self.manager.ledger.clock())
        clock_started = monotonic()
        with (directory / "agy.stdout.pending").open("xb") as stdout, (
            directory / "agy.stderr.pending"
        ).open("xb") as stderr:
            try:
                require_bound_workspace(plan.cwd, scope)
                if plan.workspace_kind == "read_only":
                    _require_bundle_directory(plan.cwd, Path(run["repo_root"]), self.manager.database.parent)
                    if _bundle_workspace_snapshot(plan.cwd) != (plan.baseline, False):
                        raise LedgerError(20, "agy Bundle workspace changed after launch claim")
                elif plan.workspace_kind == "disposable_worktree":
                    if _workspace_snapshot(plan.cwd) != (plan.baseline, b""):
                        raise LedgerError(20, "agy disposable worktree changed after launch claim")
            except (LedgerError, OSError) as exc:
                self.manager.ledger.transition(plan.run_id, "LAUNCHING", "SUBMISSION_UNKNOWN")
                raise LedgerError(21, "Worker directory changed after launch claim; preserve the run for review") from exc
            try:
                process = Popen(plan.argv, cwd=plan.cwd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                env=_worker_environment(), shell=False)
            except OSError as exc:
                confirmed = (exc.errno in (errno.ENOENT, errno.EACCES, errno.ENOEXEC)
                             or getattr(exc, "winerror", None) in (2, 3, 5, 193))
                error = "process_not_started" if confirmed else "process_launch_unknown"
                return LaunchHandle(plan, None, None, started_at, clock_started, error)
        try:
            identity = self.manager.mark_running(plan.run_id, pid=process.pid)
        except LedgerError:
            # A very short-lived process may already be gone when its creation identity is queried.
            identity = {"pid": process.pid, "identity": None}
        return LaunchHandle(plan, process, identity, started_at, clock_started)

    def observe(self, handle):
        """Wait locally once until exit or the approved deadline, then seal any log evidence."""
        process = handle.process
        termination = "exited"
        exit_code = None
        if process is None:
            termination = "not_started" if handle.launch_error == "process_not_started" else "interrupted"
        else:
            try:
                remaining = max(0, handle.clock_started + handle.plan.timeout_s - monotonic())
                exit_code = process.wait(timeout=remaining)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                exit_code = process.poll()
                if exit_code is None:
                    termination = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "interrupted"
                    try:
                        process.kill()
                    except OSError:
                        if process.poll() is None:
                            raise
                    exit_code = process.wait(timeout=5)
            if termination == "exited" and (exit_code in (-signal.SIGINT, -signal.SIGTERM)
                                            or exit_code & 0xFFFFFFFF == 0xC000013A):
                termination = "interrupted"
        wall_milliseconds = round((monotonic() - handle.clock_started) * 1000)
        directory = self.manager._directory(handle.plan.run_id)
        after = None
        if process is not None:
            try:
                after, _ = _invocation_snapshot(handle.plan)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        changed = None if after is None else after != handle.plan.baseline
        stdout = _read(directory / "agy.stdout.pending")
        log_submission = None
        if process is not None and termination in ("timeout", "interrupted"):
            observed = normalize_stdout(stdout)
            if not (observed and observed["raw_status"]):
                log_submission = _log_submission(directory / "agy.cli.log")
        return AgyCapture(
            handle.plan.run_id, handle.plan.task_sha256, sha256(handle.plan.metadata).hexdigest(),
            stdout, _read(directory / "agy.stderr.pending"), exit_code,
            termination=termination, pre_submit_reason=handle.launch_error if termination == "not_started" else None,
            wall_milliseconds=wall_milliseconds, workspace_changed=changed, workspace_after_sha256=after,
            log_submission=log_submission,
        )

    def persist_capture(self, handle, capture):
        """Seal stream hashes before normalization so recovery never needs to relaunch."""
        directory = self.manager._directory(capture.run_id)
        record = asdict(capture)
        if capture.log_submission is None:
            record.pop("log_submission")
        record["stdout"] = sha256(capture.stdout).hexdigest()
        record["stderr"] = sha256(capture.stderr).hexdigest()
        record["process"] = handle.process_identity
        record["started_at"] = handle.started_at
        record["launch_error"] = handle.launch_error
        _publish(directory / "agy.process.json", _bytes(record))

    def collect_saved(self, run_id, *, replay_output=False):
        """Recover an already sealed observation. This entry point cannot start a process."""
        directory = self.manager._directory(run_id)
        try:
            raw_receipt = _read(directory / "agy.process.json")
            record = _json(raw_receipt)
        except FileNotFoundError:
            if replay_output:
                raise LedgerError(30, "Output replay requires the sealed agy process receipt") from None
            return self.manager.collect(run_id)
        if record["run_id"] != run_id:
            raise LedgerError(30, "agy process receipt belongs to a different run")
        capture = {}
        for name in ("stdout", "stderr"):
            data = _read(directory / f"agy.{name}.pending")
            if sha256(data).hexdigest() != record[name]:
                raise LedgerError(30, "agy process receipt does not match its streams")
            capture[name] = data
        for key in ("process", "started_at", "launch_error"):
            record.pop(key)
        record["tool_events"] = tuple(record["tool_events"])
        return self.collect(run_id, AgyCapture(**(record | capture)),
                            process_receipt_sha256=sha256(raw_receipt).hexdigest(), replay_output=replay_output)

    def classify_failure(self, exit_code, stderr, events):
        """Only the sealed observer classification can support a refund."""
        final = next((item for item in reversed(events) if item.get("type") == "worker_exit"), {})
        if final.get("classification") == "FAILED_PRE_SUBMIT" and final.get("termination") == "not_started":
            return "PRE_SUBMIT"
        return "FAILED" if final.get("classification") == "FAILED" else "UNKNOWN"

    def resolve_evidence(self, run):
        directory = self.manager._directory(run["run_id"])
        try:
            receipt = _json(_read(directory / "agy.capture.json"))
        except (OSError, ValueError):
            receipt = {}
        return {"has_done": self.manager._completion(run)[0] == "verified", "conversation_exists": None,
                "workspace_changed": receipt.get("workspace_changed"), "notes": "Local evidence only"}

    def _classify(self, capture, observed, run, tool_reason):
        if capture.termination == "not_started":
            return "FAILED_PRE_SUBMIT", capture.pre_submit_reason, None
        if tool_reason in ("required_action_denied", "required_action_failed"):
            return "FAILED", tool_reason, None
        if capture.workspace_changed is True and run["template"] != "delegate-implement":
            return "FAILED", "readonly_violation", None
        submitted = run["conversation_id"] or capture.log_submission or (
            observed and observed["conversation_id"] and (observed["num_turns"] or 0) > 0
        )
        if not submitted:
            return "SUBMISSION_UNKNOWN", "submission_evidence_missing", None
        if capture.termination == "timeout" or capture.log_submission is not None:
            return "TIMED_OUT", "process_timed_out", None
        if capture.termination != "exited" or capture.exit_code is None:
            return "SUBMISSION_UNKNOWN", "process_completion_unknown", None
        if capture.exit_code != 0:
            return "FAILED", "process_failed", None
        if observed is None or not observed["raw_status"]:
            return "FAILED", "schema_invalid", None
        if observed["raw_status"] != "SUCCESS":
            return "FAILED", "worker_status_not_success", None
        response = observed["response"]
        structured = "structured_output" in observed
        if not structured:
            if not response or not response.strip():
                return "FAILED", "schema_invalid", None
            if not response.lstrip().startswith(("{", "[")):
                return "COLLECTING", "structured_result_missing", None
        try:
            result = observed["structured_output"] if structured else _json(response)
            valid = _result_document(_bytes(result), run, self.manager.max_summary_chars)
            if valid is not None and not _template_valid(run["template"], valid["template_payload"]):
                valid = None
        except (ValueError, RecursionError):
            valid = None
        if valid is None:
            return "FAILED", "schema_invalid", None
        if structured:
            expected_schema = _response_schema(run["run_id"], run["template"], self.manager.max_summary_chars)
            if "json_schema" in observed:
                try:
                    if _bytes(observed["json_schema"]) != _bytes(expected_schema):
                        return "FAILED", "schema_invalid", None
                except (ValueError, RecursionError):
                    return "FAILED", "schema_invalid", None
            if observed.get("response_invalid") or not _response_agrees(
                response, valid, observed.get("json_schema"), expected_schema,
            ):
                return "FAILED", AGY_OUTPUT_CONFLICT, None
        if valid["status"] in ("failed", "blocked"):
            return "FAILED", "worker_" + valid["status"], None
        if run["workspace_kind"] == "read_only" and capture.workspace_changed is None:
            return "COLLECTING", "bundle_workspace_evidence_missing", None
        return "COLLECTING", "structured_result_collected", valid

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
            run = self._apply(db, run, state, reason, result, conversation, receipt)
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

    def _apply(self, db, run, state, reason, result, conversation, receipt):
        manager = self.manager
        current = run["state"]
        if current in TERMINAL_STATES:
            return run
        evidence = {"agy_capture_sha256": sha256(_bytes(receipt)).hexdigest()}
        exit_code = receipt["exit_code"]
        if state in ("FAILED_PRE_SUBMIT", "SUBMISSION_UNKNOWN"):
            if current in ("LAUNCHING", "RUNNING"):
                return manager._change(db, run, state, reason=reason, evidence=evidence, exit_code=exit_code)
            return run
        if current == "LAUNCHING":
            if conversation is None:
                run = manager._change(db, run, "SUBMISSION_UNKNOWN", reason="missing_launch_receipt")
            else:
                run = manager._change(db, run, "RUNNING", reason="agy_conversation_observed",
                                      evidence=evidence, conversation_id=conversation)
            current = run["state"]
        if state == "TIMED_OUT":
            if current == "SUBMISSION_UNKNOWN" and conversation is not None:
                run = manager._change(db, run, "RUNNING", reason="agy_conversation_observed",
                                      evidence=evidence, conversation_id=conversation)
            if run["state"] == "RUNNING":
                return manager._change(db, run, state, reason=reason, evidence=evidence,
                                       conversation_id=conversation, exit_code=exit_code)
            return run
        if state == "FAILED" and current in ("SUBMISSION_UNKNOWN", "TIMED_OUT"):
            return manager._change(db, run, state, reason=reason, evidence=evidence,
                                   conversation_id=conversation, exit_code=exit_code)
        if current != "COLLECTING":
            run = manager._change(db, run, "COLLECTING", reason=reason, evidence=evidence,
                                  conversation_id=conversation, exit_code=exit_code,
                                  result_sha256=receipt["result_sha256"])
        elif result is not None and (run["exit_code"] is None or
                                     (run["conversation_id"] is None and conversation is not None)):
            # Recovery can see DONE before the original SQLite transaction is committed.
            db.execute("UPDATE runs SET exit_code = ?, conversation_id = COALESCE(conversation_id, ?) WHERE run_id = ?",
                       (exit_code, conversation, run["run_id"]))
            event(db, run["run_id"], timestamp(manager.ledger.clock()), current, current, evidence, actor="adapter")
            run = manager._run(db, run["run_id"])
        if state == "FAILED":
            return manager._change(db, run, state, reason=reason, evidence=evidence)
        if result is not None:
            manager._publish_result(run, result)
        return run
