"""Offline run lifecycle. This module never starts or reattaches a worker."""
from contextlib import closing
from datetime import datetime
from hashlib import sha256
import json
from os import getpid
from pathlib import Path
from subprocess import TimeoutExpired

from .adapters.registry import policy_for
from .atomic_io import atomic_write
from .context_bundle import _head, bundle_manifest
from .db import connect, transaction
from .executable_path import refuse_inside_roots
from .ledger import (READ_ONLY_TEMPLATES, BUSY_QUERY, RUN_ID, SHA256, SNAPSHOT_HEAD_FIELD, TARGET_ID, TERMINAL_STATES,
                     LedgerError, event, path_identity, require_dispatch_scope, timestamp, valid_id)
from .process_identity import probe_process

RESULT_STATES = {"RUNNING", "TIMED_OUT", "SUBMISSION_UNKNOWN", "COLLECTING"}


def _read(path):
    if path.is_symlink():
        raise OSError("Run artifacts must not be symbolic links")
    return path.read_bytes()


def _publish(path, content):
    try:
        previous = _read(path)
    except FileNotFoundError:
        atomic_write(path, content)
    else:
        if previous != content:
            raise FileExistsError(f"Run artifact already contains different bytes: {path.name}")


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


LEDGER_TEXT_CHARS = 200


def _ledger_text(value):
    """An observed single-line value for the Ledger columns; "unknown", empty or malformed values are NULL."""
    if (not isinstance(value, str) or not value.strip() or value == "unknown" or len(value) > LEDGER_TEXT_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        return None
    return value


def record_observation(db, run_id, observed):
    """Write model_observed and effort_observed once (COALESCE), inside the caller's collect transaction."""
    observed = observed or {}
    model, effort = (_ledger_text(observed.get(key)) for key in ("model_observed", "effort_observed"))
    if model is not None or effort is not None:
        db.execute("""UPDATE runs SET model_observed = COALESCE(model_observed, ?),
                      effort_observed = COALESCE(effort_observed, ?) WHERE run_id = ?""", (model, effort, run_id))


SAVED_EVIDENCE_ERROR_CHARS = 1000
TRUNCATED_MARKER = " …[truncated]… "


def _truncate_error(message, limit=SAVED_EVIDENCE_ERROR_CHARS):
    """Keep the exception type at the head and the file name at the tail, marking the cut."""
    if len(message) <= limit:
        return message
    head = 100
    return message[:head] + TRUNCATED_MARKER + message[-(limit - head - len(TRUNCATED_MARKER)):]


def _result_document(data, run, max_summary_chars):
    try:
        result = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    if (not isinstance(result, dict) or result.keys() != {
        "schema_version", "run_id", "template", "status", "summary",
        "blocked_reason", "open_questions", "template_payload",
    } or result["schema_version"] != "2.0" or result["run_id"] != run["run_id"]
        or result["template"] != run["template"]
        or result["status"] not in ("completed", "partial", "blocked", "failed")
        or not isinstance(result["summary"], str) or len(result["summary"]) > max_summary_chars
        or (result["blocked_reason"] is not None and not isinstance(result["blocked_reason"], str))
        or not isinstance(result["open_questions"], list)
        or any(not isinstance(item, str) for item in result["open_questions"])
        or not isinstance(result["template_payload"], dict)):
        return None
    return result


class RunManager:
    def __init__(self, ledger, *, process_probe=probe_process, max_summary_chars=2000):
        self.ledger = ledger
        self.database = ledger.database
        self.root = self.database.parent / "runs"
        self.process_probe = process_probe
        self.max_summary_chars = max_summary_chars

    def _directory(self, run_id):
        if not valid_id(RUN_ID, run_id):
            raise LedgerError(2, "Invalid run ID")
        path = self.root / run_id
        if path.resolve() != path:
            raise LedgerError(20, "Run directory must stay within the state directory")
        return path

    @staticmethod
    def _run(db, run_id):
        run = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise LedgerError(2, "Run does not exist")
        return run

    def _change(self, db, run, state, **details):
        self.ledger._transition(db, run, state, **details)
        return self._run(db, run["run_id"])

    def prepare(self, run_id, task):
        directory = self._directory(run_id)
        task.decode("utf-8")
        bundle = bundle_manifest(task)
        with transaction(self.database) as db:
            run = self._run(db, run_id)
            if bundle is not None and run["template"] not in READ_ONLY_TEMPLATES:
                raise LedgerError(30, "Context-Bundle requires a read-only analysis template")
            if run["state"] not in ("RESERVED", "PREPARED") or sha256(task).hexdigest() != run["task_sha256"]:
                raise LedgerError(20, "Task does not match the reservation")
            if run["state"] == "PREPARED":
                if self._task_evidence(run) != "verified":
                    raise LedgerError(20, "Published task is missing or changed")
                return
            directory.mkdir(parents=True, exist_ok=True)
            _publish(directory / "task.md", task)
            self._change(db, run, "PREPARED", reason="task_published")

    def claim_launch(self, run_id, *, invocation=None):
        directory = self._directory(run_id)
        with transaction(self.database) as db:
            run = self._run(db, run_id)
            if run["state"] != "PREPARED" or self._task_evidence(run) != "verified":
                raise LedgerError(20, "A prepared run with an intact task is required")
            for name in ("DONE", "result.json"):
                try:
                    (directory / name).lstat()
                except FileNotFoundError:
                    continue
                raise LedgerError(20, "Result artifacts already exist; do not launch")
            grant = db.execute("SELECT * FROM grants WHERE grant_id = ?", (run["grant_id"],)).fetchone()
            scope = None
            if (invocation is not None or policy_for(run["adapter"]).requires_dispatch_scope
                    or (grant and grant["dispatch_scope_json"])):
                if not grant or grant["status"] != "ACTIVE" or grant["expires_at"] <= timestamp(self.ledger.clock()):
                    raise LedgerError(12, "Grant is no longer active at launch")
                if invocation is None:
                    raise LedgerError(10, "A bound invocation is required to claim launch")
                metadata = json.loads(invocation)
                if ((metadata.get("run_id"), metadata.get("task_sha256")) != (run_id, run["task_sha256"])
                        or path_identity(metadata.get("cwd")) != run["workspace_path"]):
                    raise LedgerError(10, "Invocation differs from the reserved task or worktree")
                scope = require_dispatch_scope(
                    db, grant, run["adapter"], workspace_path=metadata["cwd"], task_sha256=run["task_sha256"],
                    timeout_seconds=metadata.get("timeout_s"),
                )
                self._require_snapshot_head(run, scope, metadata["cwd"])
                _publish(directory / "invocation.json", invocation)
                # Written once at the claim; receipts are unchanged and terminal Runs are never backfilled.
                version = _ledger_text(metadata.get("cli_version"))
                if version is not None:
                    db.execute("UPDATE runs SET cli_version = COALESCE(cli_version, ?) WHERE run_id = ?",
                               (version, run_id))
            owner = self._process(getpid())
            self._change(db, run, "LAUNCHING", evidence={"process": owner})
            return scope

    @staticmethod
    def _require_snapshot_head(run, scope, cwd):
        """A card bound to a snapshot commit launches only on that commit (spec 2e); still PREPARED, so the
        caller cancels the Run and the Grant is refunded."""
        if not scope or SNAPSHOT_HEAD_FIELD not in scope:
            return
        if run["workspace_kind"] != "disposable_worktree":
            raise LedgerError(10, "snapshot_head only applies to a Disposable Worktree")
        try:
            head = _head(Path(cwd))
        except (OSError, ValueError):
            head = None
        if head != scope[SNAPSHOT_HEAD_FIELD]:
            raise LedgerError(10, "Disposable worktree HEAD differs from the approved snapshot_head")

    def dispatch(self, adapter, *, grant_id, run_id, repo_root, template, model, effort, task,
                 workspace, worktree_root, timeout_s=None, requirements=None):
        """Reserve once, publish the task and synchronously collect through the Adapter."""
        if adapter.manager is not self:
            raise LedgerError(20, "Adapter is bound to a different Run Manager")
        self._directory(run_id)
        # Before prepare_invocation, whose --version/--help probe already runs the executable.
        find_executable = getattr(adapter, "find_executable", None)
        if find_executable is not None:
            refuse_inside_roots(find_executable(), (repo_root, workspace, worktree_root))
        plan = adapter.prepare_invocation(
            run_id=run_id, repo_root=repo_root, template=template, model=model, effort=effort,
            task=task, workspace=workspace, worktree_root=worktree_root,
            timeout_s=timeout_s, requirements=requirements,
        )
        self.ledger.check_and_reserve(
            grant_id, run_id, repo_root=repo_root, template=template, adapter=adapter.id, model=model,
            effort=effort, workspace_kind=plan.workspace_kind, workspace_path=plan.cwd,
            task_sha256=sha256(task).hexdigest(), timeout_seconds=plan.timeout_s,
        )
        try:
            self.prepare(run_id, task)
            handle = adapter.launch(plan)
        except (OSError, ValueError, KeyboardInterrupt):
            # Only work that never reached LAUNCHING can be cancelled here.
            run = self.ledger.get_run(run_id)
            if run["state"] in ("RESERVED", "PREPARED"):
                self.cancel(run_id)
            raise
        try:
            capture = adapter.observe(handle)
            adapter.persist_capture(handle, capture)
        except (OSError, TimeoutExpired) as exc:
            with transaction(self.database) as db:
                run = self._run(db, run_id)
                if run["state"] in ("LAUNCHING", "RUNNING"):
                    self._change(db, run, "SUBMISSION_UNKNOWN", reason="process_observation_unsealed")
            raise LedgerError(21, "Cannot seal the process observation; keep the partial evidence") from exc
        return adapter.collect_saved(run_id)

    def mark_running(self, run_id, *, pid=None, conversation_id=None):
        if conversation_id is not None and not valid_id(TARGET_ID, conversation_id):
            raise LedgerError(2, "Invalid conversation ID")
        if pid is None and conversation_id is None:
            raise LedgerError(20, "A process or conversation receipt is required")
        with transaction(self.database) as db:
            run = self._run(db, run_id)
            if run["state"] != "LAUNCHING":
                raise LedgerError(20, "Launch receipt requires LAUNCHING")
            process = self._process(pid) if pid is not None else None
            self._change(db, run, "RUNNING", pid=pid, conversation_id=conversation_id,
                         evidence={"process": process, "conversation_id": conversation_id})
            return process

    def _process(self, pid):
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
            raise LedgerError(2, "Invalid process ID")
        status, identity = self.process_probe(pid)
        if status != "alive" or not identity:
            raise LedgerError(20, "A live process identity could not be established")
        return {"pid": pid, "identity": identity}

    def cancel(self, run_id, *, gui_not_submitted=False):
        with transaction(self.database) as db:
            run = self._run(db, run_id)
            if gui_not_submitted:
                if run["state"] != "LAUNCHING" or not policy_for(run["adapter"]).gui_submission:
                    raise LedgerError(20, "A GUI launch and explicit not-submitted evidence are required")
                self._change(db, run, "CANCELLED", reason="gui_not_submitted")
            elif run["state"] in ("RESERVED", "PREPARED"):
                self._change(db, run, "CANCELLED", reason="cancelled_before_launch")
            else:
                raise LedgerError(20, "Cannot refund a run whose submission is possible")

    def timeout(self, run_id):
        self.ledger.transition(run_id, "RUNNING", "TIMED_OUT")

    def record_exit(self, run_id, exit_code, *, malformed_output=False):
        if type(exit_code) is not int:
            raise LedgerError(2, "Process exit code must be an integer")
        with transaction(self.database) as db:
            run = self._run(db, run_id)
            if run["state"] != "RUNNING":
                raise LedgerError(20, "A running process is required to record an exit")
            run = self._change(db, run, "COLLECTING", reason="process_exited", exit_code=exit_code)
            if malformed_output and exit_code == 0:
                self._change(db, run, "FAILED", reason="schema_invalid")

    def publish_result(self, run_id, result):
        with transaction(self.database) as db:
            return self._publish_result(self._run(db, run_id), result)

    def _publish_result(self, run, result):
        """Publish under the caller's database transaction, with DONE last."""
        directory = self._directory(run["run_id"])
        content = _bytes(result)
        digest = sha256(content).hexdigest()
        if run["state"] not in RESULT_STATES:
            raise LedgerError(20, "Run cannot publish a result in this state")
        if _result_document(content, run, self.max_summary_chars) is None:
            raise LedgerError(30, "Result does not match the common V2 schema")
        existing, previous, _ = self._completion(run)
        if existing == "verified":
            if previous != digest:
                raise LedgerError(20, "A different result is already committed")
            return digest
        if existing != "missing":
            raise LedgerError(20, "Existing completion evidence cannot be overwritten")
        _publish(directory / "result.json", content)
        _publish(directory / "DONE", _bytes({
            "run_id": run["run_id"], "result_sha256": digest, "finished_at": timestamp(self.ledger.clock()),
        }))
        return digest

    def _task_evidence(self, run):
        try:
            task = _read(self._directory(run["run_id"]) / "task.md")
            task.decode("utf-8")
            if sha256(task).hexdigest() != run["task_sha256"]:
                return "mismatch"
            if bundle_manifest(task) is not None and run["template"] not in READ_ONLY_TEMPLATES:
                return "invalid_bundle_template"
            return "verified"
        except FileNotFoundError:
            return "missing"
        except (OSError, UnicodeError, LedgerError):
            return "unreadable"

    def _completion(self, run):
        try:
            directory = self._directory(run["run_id"])
            marker = json.loads(_read(directory / "DONE").decode("utf-8"))
        except FileNotFoundError:
            return "missing", None, None
        except (ValueError, OSError):
            return "unreadable", None, None
        if (not isinstance(marker, dict) or marker.keys() != {"run_id", "result_sha256", "finished_at"}
                or marker["run_id"] != run["run_id"] or not valid_id(SHA256, marker["result_sha256"])
                or not isinstance(marker["finished_at"], str)):
            return "invalid_done", None, None
        try:
            if datetime.fromisoformat(marker["finished_at"]).tzinfo is None:
                return "invalid_done", None, None
            data = _read(directory / "result.json")
            digest = sha256(data).hexdigest()
            if digest == marker["result_sha256"] and run["result_sha256"] in (None, digest):
                return "verified", digest, data
            return "result_mismatch", None, None
        except FileNotFoundError:
            return "missing_result", None, None
        except (ValueError, OSError):
            return "unreadable", None, None

    def _last_process(self, db, run, state):
        event = db.execute(
            "SELECT detail_json FROM run_events WHERE run_id = ? AND to_state = ? ORDER BY id DESC LIMIT 1",
            (run["run_id"], state),
        ).fetchone()
        evidence = json.loads(event["detail_json"] or "{}").get("process") if event else None
        if evidence is None:
            return "missing", None
        status, identity = self.process_probe(evidence["pid"])
        if status == "alive" and identity != evidence["identity"]:
            status = "reused" if identity else "unknown"
        return status, evidence

    def recoverable_process(self, run_id):
        """Read-only check for the .pending recovery (spec 2b): (run, recorded Worker) when the Run is RUNNING
        or fell to SUBMISSION_UNKNOWN because that Worker is gone, the Worker is confirmed gone, and the mah
        process that claimed the launch is no longer alive; otherwise None."""
        with closing(connect(self.database)) as db:
            run = self._run(db, run_id)
            if not (run["state"] == "RUNNING"
                    or (run["state"] == "SUBMISSION_UNKNOWN" and run["state_reason"] == "process_gone")):
                return None
            worker_status, worker = self._last_process(db, run, "RUNNING")
            owner_status, _ = self._last_process(db, run, "LAUNCHING")
            if worker_status != "gone" or owner_status not in ("gone", "reused"):
                return None
            return run, worker

    def _reconcile(self, db, run, *, resume_observation=False):
        state = run["state"]
        if state == "RESERVED" and self._task_evidence(run) == "verified":
            return self._change(db, run, "PREPARED", reason="recovered_task")
        if state not in ("LAUNCHING", "RUNNING", "TIMED_OUT", "SUBMISSION_UNKNOWN"):
            return run
        completion, digest, _ = self._completion(run)
        if completion == "verified":
            if state == "LAUNCHING":
                run = self._change(db, run, "SUBMISSION_UNKNOWN", reason="missing_launch_receipt")
            return self._change(db, run, "COLLECTING", reason="verified_done", result_sha256=digest)
        if state in ("LAUNCHING", "RUNNING"):
            status, process = self._last_process(db, run, state)
            if status in ("gone", "reused") or (state == "LAUNCHING" and status == "missing"):
                return self._change(db, run, "SUBMISSION_UNKNOWN", reason="process_" + status,
                                    evidence={"process": process, "completion": completion})
        elif resume_observation:
            status, process = self._last_process(db, run, "RUNNING")
            if status == "alive":
                return self._change(db, run, "RUNNING", reason="reattached_observation",
                                    evidence={"process": process})
        return run

    def _summary(self, db, run):
        summary = dict(run)
        summary["task_evidence"] = self._task_evidence(run)
        summary["completion_evidence"] = self._completion(run)[0]
        state = run["state"]
        summary["resolution_actions"] = {
            "SUBMISSION_UNKNOWN": ["mark_failed", "abandon"], "TIMED_OUT": ["mark_failed"],
            # From COLLECTING with --reason, unless verify.json already records a verified result.
            "COLLECTING": ["mark_failed"],
        }.get(state, [])
        if state in ("LAUNCHING", "RUNNING", "SUBMISSION_UNKNOWN", "TIMED_OUT"):
            source = "LAUNCHING" if state == "LAUNCHING" else "RUNNING"
            process_status, process = self._last_process(db, run, source)
            if process_status == "missing" and state == "SUBMISSION_UNKNOWN":
                process_status, process = self._last_process(db, run, "LAUNCHING")
            summary["process_evidence"] = {"status": process_status, "recorded": process}
        return summary

    def status(self, run_id=None):
        with closing(connect(self.database)) as db:
            if run_id is not None:
                return self._summary(db, self._run(db, run_id))
            return [self._summary(db, run) for run in db.execute(BUSY_QUERY.replace("SELECT 1", "SELECT *"),
                                                            TERMINAL_STATES)]

    def scan(self):
        with transaction(self.database) as db:
            runs = db.execute(BUSY_QUERY.replace("SELECT 1", "SELECT *"), TERMINAL_STATES).fetchall()
            return [self._summary(db, self._reconcile(db, run)) for run in runs]

    def _verified_by_report(self, run):
        """True when verify.json already records a verified decision for the current result."""
        completion, digest, _ = self._completion(run)
        try:
            report = json.loads(_read(self._directory(run["run_id"]) / "verify.json"))
            return (completion == "verified" and report["run_id"] == run["run_id"]
                    and report["decision"] == "verified" and report["result_sha256"] == digest)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _mark_collecting_failed(self, db, run, reason, extra):
        if reason is None:
            raise LedgerError(2, "從 COLLECTING 標記失敗須以 --reason 說明原因")
        if self._verified_by_report(run):
            raise LedgerError(30, "This result has already been verified; run mah verify again instead of marking it failed")
        return self._change(db, run, "FAILED", reason="user_marked_failed",
                            evidence={"from_state": "COLLECTING", "user_reason": reason} | extra)

    def resolve(self, run_id, *, action=None, reason=None, saved_evidence_error=None):
        """Apply a user resolution.

        `saved_evidence_error` records why the sealed Worker evidence could not be recovered. Only the
        closing actions accept it, and they still end in FAILED or ABANDONED without refunding the Grant.
        """
        with transaction(self.database) as db:
            run = self._run(db, run_id)
            if action not in (None, "mark_failed", "abandon"):
                raise LedgerError(2, "Attach and redispatch require a verified external adapter")
            if reason is not None and (action != "mark_failed" or not isinstance(reason, str)
                                       or not reason.strip() or len(reason) > 500):
                raise LedgerError(2, "--reason is only for --mark-failed and must be a short nonempty text")
            if saved_evidence_error is not None and (action is None or not isinstance(saved_evidence_error, str)):
                raise LedgerError(2, "Saved evidence errors are recorded only by --mark-failed or --abandon")
            extra = ({} if saved_evidence_error is None
                     else {"saved_evidence_error": _truncate_error(saved_evidence_error)})
            # Only a run that was already COLLECTING may be marked failed from there. A result that this
            # call recovers from SUBMISSION_UNKNOWN or TIMED_OUT still takes priority over a manual failure,
            # unless the sealed Worker evidence is damaged: that result can never be collected, so the
            # closing action ends the Run in this same call and keeps the error.
            previous_state = run["state"]
            run = self._reconcile(db, run, resume_observation=action is None)
            if action == "mark_failed" and previous_state == "COLLECTING" and run["state"] == "COLLECTING":
                return self._summary(db, self._mark_collecting_failed(db, run, reason, extra))
            if (action == "mark_failed" and saved_evidence_error is not None and run["state"] == "COLLECTING"
                    and previous_state in ("SUBMISSION_UNKNOWN", "TIMED_OUT")):
                if self._verified_by_report(run):
                    raise LedgerError(30, "This result has already been verified; run mah verify again instead "
                                          "of marking it failed")
                evidence = ({"from_state": "COLLECTING", "recovered_from": previous_state}
                            | ({"user_reason": reason} if reason is not None else {}) | extra)
                return self._summary(db, self._change(db, run, "FAILED", reason="user_marked_failed",
                                                      evidence=evidence))
            if action is not None and run["state"] == "COLLECTING" and self._completion(run)[0] == "verified":
                return self._summary(db, run)
            if action == "mark_failed" and run["state"] in ("SUBMISSION_UNKNOWN", "TIMED_OUT"):
                evidence = ({"user_reason": reason} if reason is not None else {}) | extra
                run = self._change(db, run, "FAILED", reason="user_marked_failed", evidence=evidence or None)
            elif action == "abandon" and run["state"] == "SUBMISSION_UNKNOWN":
                run = self._change(db, run, "ABANDONED", reason="user_abandoned", evidence=extra or None)
            elif action is not None:
                raise LedgerError(20, "Resolution action is not legal for this state")
            return self._summary(db, run)

    def collect(self, run_id, *, verified_sha256=None, required_action_denied=False):
        """Collect locally; adapter-backed runs use the template Verifier for success."""
        with transaction(self.database) as db:
            run = self._run(db, run_id)
            if verified_sha256 is not None and policy_for(run["adapter"]).template_verified:
                raise LedgerError(30, "Template verification is required")
            return self._collect(db, run, verified_sha256=verified_sha256,
                                 required_action_denied=required_action_denied)

    def _collect(self, db, run, *, verified_sha256=None, required_action_denied=False,
                 verification_failure=None):
        run = self._reconcile(db, run)
        if run["state"] != "COLLECTING":
            return self._summary(db, run)
        if required_action_denied:
            completion, digest, _ = self._completion(run)
            return self._summary(db, self._change(
                db, run, "FAILED", reason="required_action_denied",
                result_sha256=digest if completion == "verified" else None,
            ))
        if run["exit_code"] not in (None, 0):
            return self._summary(db, self._change(db, run, "FAILED", reason="process_failed"))
        completion, digest, data = self._completion(run)
        if completion != "verified":
            return self._summary(db, run)
        result = _result_document(data, run, self.max_summary_chars)
        if result is None or result["status"] in ("blocked", "failed"):
            reason = "schema_invalid" if result is None else "worker_" + result["status"]
            return self._summary(db, self._change(db, run, "FAILED", reason=reason, result_sha256=digest))
        if verified_sha256 is not None:
            if verified_sha256 != digest:
                raise LedgerError(30, "Verifier evidence does not match the result")
            if verification_failure is not None:
                run = self._change(db, run, "FAILED", reason=verification_failure, result_sha256=digest)
            elif result["status"] == "completed":
                run = self._change(db, run, "SUCCEEDED", reason="result_verified", result_sha256=digest)
            else:
                raise LedgerError(30, "Verifier evidence does not match a completed result")
        return self._summary(db, run)

    def record_acceptance(self, run_id, *, accepted, reason, adopted=None, total=None):
        with transaction(self.database) as db:
            return self._record_acceptance(db, self._run(db, run_id), accepted=accepted,
                                           reason=reason, adopted=adopted, total=total)

    def _record_acceptance(self, db, run, *, accepted, reason, adopted=None, total=None):
        if type(accepted) is not bool or not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise LedgerError(2, "An acceptance decision and a short reason are required")
        if (adopted is not None or total is not None) and (
            not accepted or type(adopted) is not int or type(total) is not int or not 0 <= adopted <= total
        ):
            raise LedgerError(2, "Provide valid adopted and total counts together")
        target = "ACCEPTED" if accepted else "REJECTED"
        if accepted:
            completion, _, data = self._completion(run)
            if run["state"] != "SUCCEEDED" or completion != "verified":
                raise LedgerError(30, "Acceptance requires a verified successful result")
            if (total is not None and policy_for(run["adapter"]).template_verified
                    and run["template"] in ("review", "second-opinion")):
                key = "findings" if run["template"] == "review" else "hypotheses"
                if total != len(json.loads(data)["template_payload"][key]):
                    raise LedgerError(2, "Adoption total does not match the verified result")
        if not accepted and run["state"] not in ("COLLECTING", "SUCCEEDED", "FAILED"):
            raise LedgerError(30, "A result must be collected before rejection")
        current = run["acceptance_status"]
        detail = {"acceptance_from": "PENDING", "acceptance_to": target, "adopted": adopted, "total": total}
        if current == target and run["acceptance_reason"] == reason:
            previous = db.execute(
                "SELECT detail_json FROM run_events WHERE run_id = ? AND actor = 'controller' ORDER BY id DESC LIMIT 1",
                (run["run_id"],),
            ).fetchone()
            if previous and json.loads(previous["detail_json"]) == detail:
                return self._summary(db, run)
        if current != "PENDING":
            raise LedgerError(30, "Acceptance has already been decided")
        db.execute("UPDATE runs SET acceptance_status = ?, acceptance_reason = ? WHERE run_id = ?",
                   (target, reason, run["run_id"]))
        event(db, run["run_id"], timestamp(self.ledger.clock()), run["state"], run["state"],
              detail, actor="controller")
        return self._summary(db, self._run(db, run["run_id"]))
