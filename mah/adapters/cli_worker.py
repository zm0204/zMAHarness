"""Shared flow for read-only Workers that run an external CLI as a local subprocess.

The lifecycle (launch, observe, persist_capture, collect_saved, classification and state changes) was
ported once from AgyPrintAdapter and parameterized by a receipt file prefix. agy_print.py itself is left
unchanged on purpose; a later pure-refactor phase moves agy onto this base under its existing tests.

Subclasses provide only: the receipt prefix, executable resolution, required flags, the argument
vector, the read-only argument check, the worker environment, the stdout normalizer and the
receipt-integrity reason string.
"""
from dataclasses import asdict, dataclass, field
import errno
from hashlib import sha256
import os
from pathlib import Path
import re
import signal
import subprocess
from subprocess import Popen
from time import monotonic

from ..context_bundle import _workspace_snapshot, _worktree_baseline, bundle_manifest
from ..db import transaction
from ..ledger import (AGY_OUTPUT_CONFLICT, READ_ONLY_TEMPLATES, TARGET_ID, TEMPLATE_TIMEOUTS, TERMINAL_STATES,
                      LedgerError, event, require_bound_workspace, timestamp, valid_id)
from ..run_manager import _bytes, _publish, _read, _result_document
from ..verifier import Verifier, _template_valid, requirements_for
from .agy_print import (CAPTURE_STATES, InvocationPlan, _bundle_workspace_snapshot, _count, _invocation_snapshot,
                        _json, _require_bundle_directory, _response_agrees, _response_schema)

# The ledger constant's value carries no Adapter name; it is shared on purpose.
STRUCTURED_OUTPUT_CONFLICT = AGY_OUTPUT_CONFLICT
TERMINATIONS = ("exited", "timeout", "interrupted", "not_started")


@dataclass(frozen=True)
class CliCapture:
    """Receipt from the local process observer. Termination and tool events never come from the model."""

    run_id: str
    task_sha256: str
    invocation_sha256: str
    stdout: bytes
    stderr: bytes
    exit_code: int | None
    termination: str = "exited"
    launch_error: str | None = None
    launch_detail: dict | None = None
    tool_events: tuple = ()
    wall_milliseconds: int | None = None
    workspace_changed: bool | None = None
    workspace_after_sha256: str | None = None
    # Files the CLI itself writes into the run directory (for example a last-message file), by name.
    outputs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CliLaunchHandle:
    plan: InvocationPlan
    process: Popen | None
    process_identity: dict | None
    started_at: str
    clock_started: float
    launch_error: str | None = None
    launch_detail: dict | None = None

    @property
    def pid(self):
        return self.process.pid if self.process is not None else None


def _neutral(exc):
    """Re-raise shared-helper errors without the agy wording, keeping the exit code."""
    message = str(exc)
    if message.startswith("agy "):
        message = "Worker " + message[4:]
    return LedgerError(exc.code, message)


def _check_capture(capture):
    if (not isinstance(capture.stdout, bytes) or not isinstance(capture.stderr, bytes)
            or (capture.exit_code is not None and type(capture.exit_code) is not int)
            or (capture.wall_milliseconds is not None and _count(capture.wall_milliseconds) is None)
            or capture.termination not in TERMINATIONS
            or (capture.launch_detail is not None and not isinstance(capture.launch_detail, dict))
            or not isinstance(capture.outputs, dict)
            or any(not isinstance(name, str) or not isinstance(data, bytes) for name, data in capture.outputs.items())):
        raise LedgerError(2, "Invalid Worker process capture")
    if capture.termination == "not_started":
        if (capture.launch_error != "process_not_started" or capture.exit_code is not None
                or capture.stdout or capture.tool_events):
            raise LedgerError(20, "A not-started receipt must prove the process did not start")
    elif capture.launch_error not in (None, "process_launch_unknown") or (
            capture.launch_error is not None and capture.termination != "interrupted"):
        raise LedgerError(20, "A started process cannot carry a launch error")
    for item in capture.tool_events:
        if (not isinstance(item, dict) or item.keys() != {"type", "name", "status", "exit_code"}
                or item["type"] != "tool" or not isinstance(item["name"], str) or not item["name"]
                or item["status"] not in ("completed", "failed", "denied")
                or (item["exit_code"] is not None and type(item["exit_code"]) is not int)):
            raise LedgerError(2, "Invalid local tool event")


class CliWorkerAdapter:
    """Synchronous read-only CLI Worker with resumable local evidence."""

    id = None
    prefix = None
    capture_reason = None
    efforts = frozenset()
    # Parse the full `--help` result text for flag names; a flag must be listed to be used.
    flag_pattern = re.compile(r"(?<![\w-])--[a-zA-Z][a-zA-Z-]*")
    success_status = "SUCCESS"
    # When False, only the CLI's structured result is accepted; free text is never parsed as a result.
    check_response_agreement = True
    # Run-directory files the CLI writes itself; they are sealed into the capture after exit.
    output_files = ()

    def __init__(self, manager, *, command=None, max_task_bytes=32768):
        self.manager = manager
        self.command = command
        self.max_task_bytes = max_task_bytes

    # ---- Adapter-specific hooks -------------------------------------------------------------------
    def resolve_executable(self):
        """Return the native executable Path or raise LedgerError(14)."""
        raise NotImplementedError

    def flags_for(self, kind):
        raise NotImplementedError

    def build_argv(self, executable, *, model, effort, kind, schema):
        raise NotImplementedError

    def check_readonly(self, plan):
        """Raise LedgerError(17) unless the vector is exactly the fixed read-only profile."""
        raise NotImplementedError

    def worker_environment(self):
        raise NotImplementedError

    def normalize_stdout(self, raw):
        """Map CLI stdout to conversation_id, raw_status, response, num_turns, usage, model_observed,
        effort_observed and, when present, structured_output / json_schema / response_invalid."""
        raise NotImplementedError

    # ---- Optional hooks (defaults keep metadata, process records and receipts unchanged) -----------
    def invocation_profile(self, plan):
        """Collect adapter-specific prelaunch evidence. The default adds no evidence."""
        return None

    def invocation_mismatch(self):
        return LedgerError(20, f"{self.id} launch arguments do not match the prepared invocation")

    def argv_context(self, *, run_id, directory, cwd, profile=None):
        """Extra keyword arguments for build_argv, for example run-directory or worker-directory paths."""
        return {}

    def invocation_files(self, *, run_id, template):
        """Run-directory files published before the launch claim, as {name: bytes}. Their hashes are
        bound into invocation.json under launch_files."""
        return {}

    def normalize_capture(self, capture):
        """Normalize a sealed capture; the default reads stdout only."""
        return self.normalize_stdout(capture.stdout)

    # ---- Shared lifecycle --------------------------------------------------------------------------
    def _file(self, suffix):
        return f"{self.prefix}.{suffix}"

    def find_executable(self):
        """Read-only lookup for preflight: the resolved executable, or None. Never runs it."""
        try:
            return self.resolve_executable()
        except (LedgerError, OSError):
            return None

    def probe(self, *, cwd=None):
        """Run only --version and --help; no authentication or inference."""
        try:
            exe = self.resolve_executable()
            reports = [subprocess.run([str(exe), option], stdin=subprocess.DEVNULL, capture_output=True,
                                      shell=False, cwd=cwd, env=self.worker_environment(), timeout=20)
                       for option in ("--version", "--help")]
        except (OSError, ValueError, LedgerError, subprocess.TimeoutExpired):
            return {"available": False, "cli_version": None, "flags": set()}
        version = reports[0].stdout.decode("utf-8", errors="replace").strip()
        help_text = b"\n".join((reports[1].stdout, reports[1].stderr)).decode("utf-8", errors="replace")
        return {"available": bool(version) and all(report.returncode == 0 for report in reports),
                "cli_version": version or None, "flags": set(self.flag_pattern.findall(help_text))}

    @staticmethod
    def _bundle_for_launch(task, template):
        bundle = bundle_manifest(task)
        if bundle is not None and (bundle[0]["version"] != 2 or template not in READ_ONLY_TEMPLATES):
            raise LedgerError(17, "Worker requires a sealed Manifest v2 bundle and a read-only analysis template")
        return bundle

    @staticmethod
    def prompt_bytes(run_id, template, task_text):
        return (f"MultiAgentHarness V2 run_id={run_id}, template={template}\n"
                "請依 JSON Schema 回傳單一結果物件。\n\n" + task_text).encode("utf-8")

    def _guard_argv(self, plan):
        if not plan.argv or not str(plan.argv[0]).lower().endswith(".exe"):
            raise LedgerError(17, f"{self.id} must launch a native .exe directly")
        # Re-resolved at prepare and at launch; any other executable (for example cmd.exe) is refused.
        if str(plan.argv[0]) != str(self.resolve_executable()):
            raise LedgerError(17, f"{self.id} executable differs from the resolved native .exe")
        self.check_readonly(plan)

    def _bundle_directory(self, cwd, repo):
        try:
            _require_bundle_directory(cwd, repo, self.manager.database.parent)
        except LedgerError as exc:
            raise _neutral(exc) from None

    def prepare_invocation(self, *, run_id, repo_root, template, model, effort, task, workspace,
                           worktree_root, timeout_s, requirements):
        if template not in READ_ONLY_TEMPLATES:
            raise LedgerError(2, f"{self.id} only supports read-only analysis templates")
        if not valid_id(TARGET_ID, model) or effort not in self.efforts:
            raise LedgerError(2, f"Invalid {self.id} model or effort")
        if not isinstance(task, bytes) or not 0 < len(task) <= self.max_task_bytes:
            raise LedgerError(16, "Task exceeds the configured limit or is empty")
        task_text = task.decode("utf-8-sig")
        if "\0" in task_text or not task_text.strip():
            raise LedgerError(2, "Task must be nonempty UTF-8 without NUL")
        timeout_s = TEMPLATE_TIMEOUTS[template] if timeout_s is None else timeout_s
        if type(timeout_s) is not int or not 1 <= timeout_s <= TEMPLATE_TIMEOUTS[template]:
            raise LedgerError(2, "Worker timeout exceeds template limits")
        requirements = requirements_for({} if requirements is None else requirements)
        directory = self.manager._directory(run_id)
        cwd, repo, root = Path(workspace).resolve(), Path(repo_root).resolve(), Path(worktree_root).resolve()
        bundle = self._bundle_for_launch(task, template)
        if bundle is None:
            try:
                baseline = _worktree_baseline(repo, cwd, root, template, run_id)
            except LedgerError as exc:
                raise _neutral(exc) from None
        else:
            self._bundle_directory(cwd, repo)
            if cwd == root or not cwd.is_relative_to(root):
                raise LedgerError(20, "Bundle workspace must be a directory under worktree_root")
            baseline, nonempty = _bundle_workspace_snapshot(cwd)
            if nonempty:
                raise LedgerError(20, "Bundle workspace must start empty")
        if directory.is_relative_to(cwd):
            raise LedgerError(20, "Worker evidence directory must stay outside the worker worktree")
        exe = self.resolve_executable()
        capabilities = self.probe(cwd=cwd if bundle is not None else None)
        if not capabilities["available"]:
            raise LedgerError(14, f"{self.id} version or help probe failed")
        kind = "read_only" if bundle is not None else "disposable_worktree"
        if not self.flags_for(kind) <= capabilities["flags"]:
            raise LedgerError(15, f"{self.id} does not report all required flags")
        plan = self.prepare_plan(
            run_id=run_id, template=template, model=model, effort=effort, task=task, task_text=task_text,
            cwd=cwd, directory=directory, timeout_s=timeout_s, executable=exe, baseline=baseline,
            cli_version=capabilities["cli_version"], requirements=requirements,
        )
        self._guard_argv(plan)
        return plan

    def prepare_plan(self, **inputs):
        plan = self.build_invocation(**inputs)
        profile = self.invocation_profile(plan)
        return plan if profile is None else self.build_invocation(**inputs, profile=profile)

    def build_invocation(self, *, run_id, template, model, effort, task, task_text, cwd, directory, timeout_s,
                         executable, baseline, cli_version, requirements, profile=None):
        """Build a fixed argument vector and stdin prompt. No probe, shell expansion, or launch."""
        bundle = self._bundle_for_launch(task, template)
        kind = "read_only" if bundle is not None else "disposable_worktree"
        schema = _response_schema(run_id, template, self.manager.max_summary_chars)
        argv = self.build_argv(executable, model=model, effort=effort, kind=kind, schema=schema,
                               **self.argv_context(run_id=run_id, directory=directory, cwd=cwd, profile=profile))
        if os.name == "nt" and len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32767:
            raise LedgerError(16, "Schema exceeds the Windows command-line limit")
        prompt = self.prompt_bytes(run_id, template, task_text)
        stdin_name = self._file("prompt.txt")
        metadata = {
            "run_id": run_id, "task_sha256": sha256(task).hexdigest(), "cli_version": cli_version,
            "executable": str(executable), "argv": list(argv), "stdin_file": stdin_name,
            "prompt_sha256": sha256(prompt).hexdigest(), "cwd": str(cwd),
            "workspace_baseline_sha256": baseline, "workspace_kind": kind, "timeout_s": timeout_s,
            "verification_requirements": requirements,
        }
        launch_files = self.invocation_files(run_id=run_id, template=template)
        if launch_files:
            metadata["launch_files"] = {name: sha256(content).hexdigest() for name, content in launch_files.items()}
        if self.output_files:
            metadata["output_files"] = list(self.output_files)
        if bundle is not None:
            metadata.update(launch_profile="context-bundle", bundle_manifest_sha256=bundle[1],
                            permissions_observed="unknown", sandbox_observed="unknown", tools_observed="unknown")
        if profile is not None:
            metadata["preflight"] = profile
        return InvocationPlan(run_id, argv, cwd, kind, timeout_s, _bytes(metadata), baseline,
                              sha256(task).hexdigest(), model, effort, template, directory / stdin_name)

    def launch(self, plan):
        # The claim is the sole Popen boundary, also when called directly via the Adapter.
        self._guard_argv(plan)
        manager = self.manager
        run = manager.ledger.get_run(plan.run_id)
        if run is None or (run["adapter"], run["template"], run["model_requested"], run["effort_requested"],
                           run["task_sha256"], Path(run["workspace_path"]), run["workspace_kind"]) != (
                self.id, plan.template, plan.model, plan.effort, plan.task_sha256, plan.cwd, plan.workspace_kind):
            raise LedgerError(10, f"{self.id} invocation does not match its reservation")
        directory = manager._directory(plan.run_id)
        metadata = _json(plan.metadata)
        task = _read(directory / "task.md")
        if not 1 <= plan.timeout_s <= TEMPLATE_TIMEOUTS[plan.template]:
            raise LedgerError(2, "Worker timeout exceeds template limits")
        if plan.workspace_kind == "read_only":
            self._bundle_directory(plan.cwd, Path(run["repo_root"]))
        baseline, status = _invocation_snapshot(plan)
        if status or baseline != plan.baseline:
            raise LedgerError(20, "Worker directory changed before launch")
        task_text = task.decode("utf-8-sig")
        expected = self.prepare_plan(
            run_id=plan.run_id, template=run["template"], model=run["model_requested"],
            effort=run["effort_requested"], task=task, task_text=task_text, cwd=plan.cwd, directory=directory,
            timeout_s=plan.timeout_s, executable=self.resolve_executable(), baseline=baseline,
            cli_version=metadata["cli_version"], requirements=requirements_for(metadata["verification_requirements"]),
        )
        if expected != plan:
            raise self.invocation_mismatch()
        if plan.workspace_kind == "read_only":
            grant = manager.ledger.get_grant(run["grant_id"])
            if grant is None or grant["max_dispatches"] != 1:
                raise LedgerError(10, "Context-Bundle requires a grant limited to one dispatch")
        prompt = self.prompt_bytes(plan.run_id, plan.template, task_text)
        if sha256(prompt).hexdigest() != metadata["prompt_sha256"] or plan.stdin_file.parent != directory:
            raise LedgerError(20, "Worker prompt does not match the prepared invocation")
        _publish(plan.stdin_file, prompt)
        launch_files = self.invocation_files(run_id=plan.run_id, template=plan.template)
        if {name: sha256(content).hexdigest() for name, content in launch_files.items()} != metadata.get(
                "launch_files", {}):
            raise LedgerError(20, "Worker launch files do not match the prepared invocation")
        for name, content in launch_files.items():
            _publish(directory / name, content)
        for name in self.output_files:
            if os.path.lexists(directory / name):
                raise LedgerError(20, "Worker output file already exists; do not launch")
        scope = manager.claim_launch(plan.run_id, invocation=plan.metadata)
        started_at = timestamp(manager.ledger.clock())
        clock_started = monotonic()
        with (directory / self._file("stdout.pending")).open("xb") as stdout, (
            directory / self._file("stderr.pending")
        ).open("xb") as stderr, plan.stdin_file.open("rb") as stdin:
            try:
                require_bound_workspace(plan.cwd, scope)
                if plan.workspace_kind == "read_only":
                    self._bundle_directory(plan.cwd, Path(run["repo_root"]))
                    if _bundle_workspace_snapshot(plan.cwd) != (plan.baseline, False):
                        raise LedgerError(20, "Bundle workspace changed after launch claim")
                elif _workspace_snapshot(plan.cwd) != (plan.baseline, b""):
                    raise LedgerError(20, "Disposable worktree changed after launch claim")
            except (LedgerError, OSError) as exc:
                manager.ledger.transition(plan.run_id, "LAUNCHING", "SUBMISSION_UNKNOWN")
                raise LedgerError(21, "Worker directory changed after launch claim; preserve the run for review") from exc
            try:
                process = Popen(plan.argv, cwd=plan.cwd, stdin=stdin, stdout=stdout, stderr=stderr,
                                env=self.worker_environment(), shell=False)
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                confirmed = (exc.errno in (errno.ENOENT, errno.EACCES, errno.ENOEXEC) or winerror in (2, 3, 5, 193))
                detail = {"errno": exc.errno, "winerror": winerror}
                error = "process_not_started" if confirmed else "process_launch_unknown"
                return CliLaunchHandle(plan, None, None, started_at, clock_started, error, detail)
        try:
            identity = manager.mark_running(plan.run_id, pid=process.pid)
        except LedgerError:
            # A very short-lived process may already be gone when its creation identity is queried.
            identity = {"pid": process.pid, "identity": None}
        return CliLaunchHandle(plan, process, identity, started_at, clock_started)

    def observe(self, handle):
        """Wait locally once until exit or the approved deadline."""
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
        outputs = {}
        for name in self.output_files:
            try:
                outputs[name] = _read(directory / name)
            except FileNotFoundError:
                pass
        return CliCapture(
            handle.plan.run_id, handle.plan.task_sha256, sha256(handle.plan.metadata).hexdigest(),
            _read(directory / self._file("stdout.pending")), _read(directory / self._file("stderr.pending")),
            exit_code, termination=termination, launch_error=handle.launch_error, launch_detail=handle.launch_detail,
            wall_milliseconds=wall_milliseconds, workspace_changed=None if after is None else after != handle.plan.baseline,
            workspace_after_sha256=after, outputs=outputs,
        )

    def persist_capture(self, handle, capture):
        """Seal stream hashes before normalization so recovery never needs to relaunch."""
        record = asdict(capture)
        record["stdout"] = sha256(capture.stdout).hexdigest()
        record["stderr"] = sha256(capture.stderr).hexdigest()
        # Recorded only for Adapters that declare output files, so other receipts keep their exact bytes.
        if self.output_files:
            record["outputs"] = {name: sha256(data).hexdigest() for name, data in capture.outputs.items()}
        else:
            record.pop("outputs")
        record["process"] = handle.process_identity
        record["started_at"] = handle.started_at
        _publish(self.manager._directory(capture.run_id) / self._file("process.json"), _bytes(record))

    def collect_saved(self, run_id, *, replay_output=False):
        """Recover an already sealed observation. This entry point cannot start a process."""
        if replay_output:
            raise LedgerError(20, f"Output replay is not supported for {self.id}")
        directory = self.manager._directory(run_id)
        try:
            raw_receipt = _read(directory / self._file("process.json"))
        except FileNotFoundError:
            return self.manager.collect(run_id)
        record = _json(raw_receipt)
        if record["run_id"] != run_id:
            raise LedgerError(30, "Worker process receipt belongs to a different run")
        streams = {}
        for name in ("stdout", "stderr"):
            data = _read(directory / self._file(f"{name}.pending"))
            if sha256(data).hexdigest() != record[name]:
                raise LedgerError(30, "Worker process receipt does not match its streams")
            streams[name] = data
        outputs = {}
        for name, digest in record.pop("outputs", {}).items():
            if name not in self.output_files:
                raise LedgerError(30, "Worker process receipt names an undeclared output file")
            data = _read(directory / name)
            if sha256(data).hexdigest() != digest:
                raise LedgerError(30, "Worker process receipt does not match its output files")
            outputs[name] = data
        streams["outputs"] = outputs
        for key in ("process", "started_at"):
            record.pop(key)
        record["tool_events"] = tuple(record["tool_events"])
        return self.collect(run_id, CliCapture(**(record | streams)),
                            process_receipt_sha256=sha256(raw_receipt).hexdigest())

    def _classify(self, capture, observed, run, tool_reason):
        if capture.termination == "not_started":
            # The ledger refund is reserved for policies with refunds_unstarted_process; the Grant stays used.
            return "SUBMISSION_UNKNOWN", "process_not_started", None
        if tool_reason in ("required_action_denied", "required_action_failed"):
            return "FAILED", tool_reason, None
        if capture.workspace_changed is True:
            return "FAILED", "readonly_violation", None
        submitted = run["conversation_id"] or (
            observed and observed["conversation_id"] and (observed["num_turns"] or 0) > 0
        )
        if not submitted:
            return "SUBMISSION_UNKNOWN", "submission_evidence_missing", None
        if capture.termination == "timeout":
            return "TIMED_OUT", "process_timed_out", None
        if capture.termination != "exited" or capture.exit_code is None:
            return "SUBMISSION_UNKNOWN", "process_completion_unknown", None
        if capture.exit_code != 0:
            return "FAILED", "process_failed", None
        if observed is None or not observed["raw_status"]:
            return "FAILED", "schema_invalid", None
        if observed["raw_status"] != self.success_status:
            return "FAILED", "worker_status_not_success", None
        response = observed["response"]
        structured = "structured_output" in observed
        if not structured:
            if not self.check_response_agreement:
                return "COLLECTING", "structured_result_missing", None
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
            if self.check_response_agreement and (observed.get("response_invalid") or not _response_agrees(
                response, valid, observed.get("json_schema"), expected_schema,
            )):
                return "FAILED", STRUCTURED_OUTPUT_CONFLICT, None
        if valid["status"] in ("failed", "blocked"):
            return "FAILED", "worker_" + valid["status"], None
        if run["workspace_kind"] == "read_only" and capture.workspace_changed is None:
            return "COLLECTING", "bundle_workspace_evidence_missing", None
        return "COLLECTING", "structured_result_collected", valid

    def collect(self, run_id, capture, *, process_receipt_sha256=None):
        """Consume an already captured local process receipt. This method cannot launch."""
        _check_capture(capture)
        if capture.run_id != run_id:
            raise LedgerError(30, "Worker capture belongs to a different run")
        manager = self.manager
        directory = manager._directory(run_id)
        with transaction(manager.database) as db:
            run = manager._run(db, run_id)
            if (run["adapter"] != self.id or run["task_sha256"] != capture.task_sha256
                    or manager._task_evidence(run) != "verified"):
                raise LedgerError(30, "Worker capture does not match the reserved task")
            invocation = _read(directory / "invocation.json")
            if sha256(invocation).hexdigest() != capture.invocation_sha256:
                raise LedgerError(30, "Worker capture does not match the invocation")
            metadata = _json(invocation.decode("utf-8"))
            if (metadata["run_id"], metadata["task_sha256"]) != (run_id, run["task_sha256"]):
                raise LedgerError(30, "Worker invocation belongs to a different task")
            required = requirements_for(metadata["verification_requirements"])["required_tools"]
            tool_reason, _ = Verifier._check_tools(capture.tool_events, required)
            observed = self.normalize_capture(capture)
            response = observed["response"] if observed else None
            conversation = observed["conversation_id"] if observed else None
            if conversation and (run["conversation_id"] not in (None, conversation) or db.execute(
                "SELECT 1 FROM runs WHERE adapter = ? AND conversation_id = ? AND run_id <> ?",
                (self.id, conversation, run_id),
            ).fetchone()):
                raise LedgerError(30, "Worker conversation belongs to a different capture")
            if run["exit_code"] is not None and run["exit_code"] != capture.exit_code:
                raise LedgerError(30, "Worker exit code conflicts with the stored receipt")
            receipt_path = directory / self._file("capture.json")
            try:
                previous = _read(receipt_path)
            except FileNotFoundError:
                previous = None
            terminal = run["state"] in TERMINAL_STATES
            if terminal and previous is None:
                raise LedgerError(20, "Terminal Worker run has no original capture")
            if terminal:
                original = _json(previous)
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
                "launch_error": capture.launch_error, "launch_detail": capture.launch_detail,
                "wall_milliseconds": capture.wall_milliseconds,
                "workspace_changed": capture.workspace_changed,
                "workspace_after_sha256": capture.workspace_after_sha256,
                "process_receipt_sha256": process_receipt_sha256,
                "model_requested": run["model_requested"], "effort_requested": run["effort_requested"],
                "cli_version": metadata.get("cli_version"), "executable": metadata.get("executable"),
                "observation": observation, "classification": state, "reason": reason,
                "result_sha256": result_hash,
            }
            if self.output_files:
                receipt["outputs_sha256"] = {name: sha256(data).hexdigest() for name, data in capture.outputs.items()}
            events = ({"type": "worker_exit", **receipt}, *capture.tool_events)
            journal = b"".join(_bytes(item | {"run_id": run_id}) + b"\n" for item in events)
            receipt["events_sha256"] = sha256(journal).hexdigest()
            serialized = _bytes(receipt)
            if previous is not None and previous != serialized:
                raise LedgerError(30, "Worker capture differs from the recorded receipt")
            files = {self._file("stdout.json"): capture.stdout, self._file("stderr.txt"): capture.stderr,
                     "events.jsonl": journal}
            if response is not None:
                files["last_message.txt"] = response.encode("utf-8")
            summary_key = self._file("capture").replace(".", "_")
            if terminal:
                if any(_read(directory / name) != content for name, content in files.items()):
                    raise LedgerError(30, "Stored Worker evidence differs from the original capture")
                return manager._summary(db, run) | {summary_key: receipt}
            if run["state"] not in CAPTURE_STATES:
                raise LedgerError(20, "Worker capture requires a claimed run")
            if run["state"] == "COLLECTING" and previous is None:
                raise LedgerError(30, "An existing collection needs its original Worker capture")
            _publish(receipt_path, serialized)
            for name, content in files.items():
                _publish(directory / name, content)
            run = self._apply(db, run, state, reason, result, conversation, receipt)
            return manager._summary(db, run) | {summary_key: receipt}

    def _apply(self, db, run, state, reason, result, conversation, receipt):
        manager = self.manager
        current = run["state"]
        if current in TERMINAL_STATES:
            return run
        evidence = {"capture_sha256": sha256(_bytes(receipt)).hexdigest()}
        exit_code = receipt["exit_code"]
        if state == "SUBMISSION_UNKNOWN":
            if current in ("LAUNCHING", "RUNNING"):
                return manager._change(db, run, state, reason=reason, evidence=evidence, exit_code=exit_code)
            return run
        if current == "LAUNCHING":
            if conversation is None:
                run = manager._change(db, run, "SUBMISSION_UNKNOWN", reason="missing_launch_receipt")
            else:
                run = manager._change(db, run, "RUNNING", reason="conversation_observed",
                                      evidence=evidence, conversation_id=conversation)
            current = run["state"]
        if state == "TIMED_OUT":
            if current == "SUBMISSION_UNKNOWN" and conversation is not None:
                run = manager._change(db, run, "RUNNING", reason="conversation_observed",
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

    @classmethod
    def capture_matches(cls, directory, run, result_sha256):
        """Check that Verifier is reading the result and audit stream from the same capture."""
        try:
            raw = _read(directory / f"{cls.prefix}.capture.json")
            receipt = _json(raw)
            if (receipt["adapter"] != cls.id or receipt["run_id"] != run["run_id"]
                    or receipt["task_sha256"] != run["task_sha256"] or receipt["exit_code"] != run["exit_code"]
                    or receipt["classification"] != "COLLECTING" or receipt["result_sha256"] != result_sha256):
                return False
            observed_id = receipt["observation"]["conversation_id"]
            if observed_id is not None and observed_id != run["conversation_id"]:
                return False
            process_sha = receipt.get("process_receipt_sha256")
            if (process_sha is not None
                    and sha256(_read(directory / f"{cls.prefix}.process.json")).hexdigest() != process_sha):
                return False
            files = {"invocation.json": "invocation_sha256", f"{cls.prefix}.stdout.json": "stdout_sha256",
                     f"{cls.prefix}.stderr.txt": "stderr_sha256", "events.jsonl": "events_sha256"}
            if not all(sha256(_read(directory / name)).hexdigest() == receipt[field] for name, field in files.items()):
                return False
            if not cls.output_files:
                return "outputs_sha256" not in receipt
            outputs = receipt["outputs_sha256"]
            if not isinstance(outputs, dict) or not set(outputs) <= set(cls.output_files):
                return False
            return all(sha256(_read(directory / name)).hexdigest() == digest for name, digest in outputs.items())
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return False
