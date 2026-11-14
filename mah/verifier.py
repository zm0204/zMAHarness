"""Template validation and independent, task-bound verification evidence."""
from dataclasses import dataclass
import json
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from .adapters.registry import capture_check, capture_reason
from .atomic_io import atomic_write
from .context_bundle import bundle_manifest
from .db import transaction
from .ledger import LedgerError, TEMPLATES
from .run_manager import _bytes, _read, _result_document

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


@dataclass(frozen=True)
class VerificationEvidence:
    """Controller or independent verifier observations, never parsed from worker output."""

    task_sha256: str
    result_sha256: str
    confirmed: bool | None = None
    workspace_changed: bool | None = None
    reviewed_findings: int | None = None
    experiments: tuple = ()
    checked_citations: tuple = ()
    invalid_citation: bool = False
    diff_sha256: str | None = None
    changed_files: tuple = ()
    command_results: tuple = ()
    bundle_manifest_sha256: str | None = None
    source_sha256: str | None = None


def _local_path(value):
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        return False
    path = PurePosixPath(value.replace("\\", "/"))
    return (bool(path.parts) and not path.is_absolute() and not PureWindowsPath(value).drive
            and ".." not in path.parts and ":" not in value)


def requirements_for(values):
    requirements = dict(required_tools=[], questions=[], allowed_files=[], commands=[])
    if not isinstance(values, dict) or values.keys() - requirements.keys():
        raise LedgerError(2, "Invalid verification requirements")
    requirements.update(values)
    for name, items in requirements.items():
        if not isinstance(items, (list, tuple)):
            raise LedgerError(2, "Verification requirements must be lists")
        if name == "commands":
            if any(not isinstance(argv, (list, tuple)) or not argv or
                   any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv) for argv in items):
                raise LedgerError(2, "Commands must be non-empty argument lists")
            requirements[name] = [list(argv) for argv in items]
        else:
            if any(not isinstance(item, str) or not item.strip() for item in items):
                raise LedgerError(2, "Invalid verification requirement")
            requirements[name] = list(items)
    if len(requirements["questions"]) > 3 or any(not _local_path(item) for item in requirements["allowed_files"]):
        raise LedgerError(2, "Invalid question count or file scope")
    return requirements


def _matches(value, schema):
    # The checked-in schemas only use these standard JSON Schema keywords.
    if type(value) is not {"object": dict, "array": list, "string": str, "integer": int}[schema["type"]]:
        return False
    if isinstance(value, dict):
        properties = schema["properties"]
        return value.keys() == properties.keys() and all(
            _matches(item, properties[key]) for key, item in value.items()
        )
    if isinstance(value, list):
        return (schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value))
                and all(_matches(item, schema["items"]) for item in value))
    if isinstance(value, int):
        return value >= schema.get("minimum", value)
    return (len(value.strip()) >= schema.get("minLength", 0)
            and ("enum" not in schema or value in schema["enum"])
            and ("pattern" not in schema or re.fullmatch(schema["pattern"], value) is not None))


def _template_valid(template, payload):
    if template not in TEMPLATES:
        return False
    schema = json.loads((TEMPLATES_DIR / template / "result.schema.json").read_bytes())
    if not _matches(payload, schema["properties"]["template_payload"]):
        return False
    if template == "review":
        return all(_local_path(item["file"]) for item in payload["findings"])
    if template == "research":
        for answer in payload["answers"]:
            for citation in answer["citations"]:
                lines = [int(line[1:]) for line in citation["lines"].split("-")]
                if not _local_path(citation["path"]) or lines != sorted(lines):
                    return False
        return True
    if template == "delegate-implement":
        return all(_local_path(path) for path in payload["changed_files"])
    return True


class Verifier:
    def __init__(self, manager, *, allowed_command_prefixes=()):
        self.manager = manager
        self.allowed_command_prefixes = tuple(tuple(prefix) for prefix in allowed_command_prefixes)

    @staticmethod
    def _read_requirements(directory, run):
        try:
            invocation = json.loads(_read(directory / "invocation.json"))
            if (invocation["run_id"], invocation["task_sha256"]) != (run["run_id"], run["task_sha256"]):
                return None
            return requirements_for(invocation["verification_requirements"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _tool_evidence(directory, run_id, required):
        if not required:
            return None, {}
        try:
            events = [json.loads(line) for line in _read(directory / "events.jsonl").splitlines()]
            if any(not isinstance(event, dict) or event.get("run_id") != run_id for event in events):
                return "required_tool_missing", {}
        except (OSError, ValueError):
            return "required_tool_missing", {}
        return Verifier._check_tools(events, required)

    @staticmethod
    def _check_tools(events, required):
        tools = {}
        for name in required:
            attempts = [entry for entry in events if entry.get("type") == "tool" and entry.get("name") == name]
            if any(entry.get("status") == "denied" for entry in attempts):
                tools[name] = "denied"
            elif any(entry.get("status") == "failed" or
                     type(entry.get("exit_code")) is int and entry["exit_code"] != 0 for entry in attempts):
                tools[name] = "failed"
            elif any(entry.get("status") == "completed" and type(entry.get("exit_code")) is int
                     and entry["exit_code"] == 0 for entry in attempts):
                tools[name] = "completed"
            else:
                tools[name] = "missing"
        for status, reason in (("denied", "required_action_denied"), ("failed", "required_action_failed"),
                               ("missing", "required_tool_missing")):
            if status in tools.values():
                return reason, tools
        return None, tools

    def _delegate(self, directory, payload, requirements, evidence):
        if not requirements["commands"] or not requirements["allowed_files"]:
            return "pending", "verification_plan_missing"
        if any(not any(prefix and tuple(argv[:len(prefix)]) == prefix
                       for prefix in self.allowed_command_prefixes) for argv in requirements["commands"]):
            return "failed", "command_not_allowed"
        try:
            diff = _read(directory / "diff.patch")
        except OSError:
            return "pending", "diff_missing"
        if evidence.diff_sha256 != sha256(diff).hexdigest():
            return "pending", "diff_review_missing"
        if any(path not in requirements["allowed_files"] for path in evidence.changed_files):
            return "failed", "out_of_scope_files"
        if set(payload["changed_files"]) != set(evidence.changed_files):
            return "failed", "diff_scope_mismatch"
        expected = {tuple(argv) for argv in requirements["commands"]}
        recorded = {}
        for argv, exit_code in evidence.command_results:
            argv = tuple(argv)
            if argv in expected:
                if type(exit_code) is not int or exit_code != 0:
                    return "failed", "acceptance_command_failed"
                recorded[argv] = exit_code
        if set(recorded) != expected:
            return "pending", "independent_commands_missing"
        return "verified", "verified"

    def _semantic(self, template, directory, payload, requirements, evidence):
        if evidence is None:
            return "pending", "controller_evidence_missing"
        if template != "delegate-implement" and evidence.workspace_changed is not False:
            return "pending", "readonly_evidence_missing"
        if template == "research" and evidence.invalid_citation:
            return "failed", "citation_invalid"
        if evidence.confirmed is False:
            return "failed", "controller_rejected"
        if evidence.confirmed is not True:
            return "pending", "controller_evidence_missing"
        if template == "review" and evidence.reviewed_findings != len(payload["findings"]):
            return "pending", "findings_review_missing"
        if template == "second-opinion":
            if not any(item["experiment"] in evidence.experiments for item in payload["hypotheses"]):
                return "pending", "experiment_missing"
        if template == "research":
            questions = [item["question"] for item in payload["answers"]]
            if not requirements["questions"]:
                return "pending", "research_questions_missing"
            if questions != requirements["questions"]:
                return "failed", "research_questions_mismatch"
            if not any((cite["path"], cite["lines"], cite["claim"]) in evidence.checked_citations
                       for item in payload["answers"] for cite in item["citations"]):
                return "pending", "citation_check_missing"
        if template == "delegate-implement":
            return self._delegate(directory, payload, requirements, evidence)
        return "verified", "verified"

    def _evaluate(self, run, directory, data, evidence, requirements, tool_reason, bundle):
        result = _result_document(data, run, self.manager.max_summary_chars)
        template = run["template"]
        if result is None or not _template_valid(template, result["template_payload"]):
            return "failed", "schema_invalid"
        if requirements is None:
            return "pending", "verification_plan_missing"
        if evidence is not None and template != "delegate-implement" and evidence.workspace_changed is True:
            return "failed", "readonly_violation"
        if tool_reason:
            return ("pending" if tool_reason == "required_tool_missing" else "failed"), tool_reason
        payload = result["template_payload"]
        if bundle is not None:
            paths = {file["path"] for file in bundle["files"]}
            if template == "review":
                for finding in payload["findings"]:
                    if finding["file"].replace(chr(92), "/") not in paths:
                        return "failed", "bundle_finding_out_of_scope"
            elif template == "research":
                for answer in payload["answers"]:
                    for citation in answer["citations"]:
                        if citation["path"].replace(chr(92), "/") not in paths:
                            return "failed", "bundle_citation_out_of_scope"
        if result["status"] != "completed":
            return "pending", "result_partial"
        if template == "delegate-implement" and run["workspace_kind"] != "worktree":
            return "failed", "worktree_required"
        return self._semantic(template, directory, payload, requirements, evidence)

    def verify(self, run_id, *, evidence=None):
        """Validate immutable artifacts and optionally apply trusted Controller evidence."""
        with transaction(self.manager.database) as db:
            run = self.manager._run(db, run_id)
            summary = self.manager._collect(db, run)
            directory = self.manager._directory(run_id)
            completion, digest, data = self.manager._completion(summary)
            task_verified = summary["task_evidence"] == "verified"
            bundle = bundle_sha = None
            if task_verified:
                task = _read(directory / "task.md")
                task_verified = sha256(task).hexdigest() == run["task_sha256"]
                if task_verified and (sealed := bundle_manifest(task)) is not None:
                    bundle, bundle_sha = sealed
            evidence_mismatch = evidence is not None and (
                evidence.task_sha256 != run["task_sha256"] or evidence.result_sha256 != digest
                or evidence.bundle_manifest_sha256 != bundle_sha
            )
            if summary["state"] == "SUCCEEDED" and completion == "verified" and evidence_mismatch:
                raise LedgerError(30, "Controller evidence belongs to a different task, result or bundle")
            if summary["state"] != "COLLECTING":
                decision = {"SUCCEEDED": "verified", "FAILED": "failed"}.get(summary["state"])
                try:
                    report = json.loads(_read(directory / "verify.json"))
                    if (report["run_id"] != run_id or report["decision"] != decision
                            or report["result_sha256"] != summary["result_sha256"]
                            or (report.get("bundle_manifest_sha256") is not None
                                and (report["bundle_manifest_sha256"] != bundle_sha or completion != "verified"))):
                        raise ValueError("Verification receipt does not match stored state")
                    summary["verification"] = report
                except (OSError, ValueError, KeyError, TypeError):
                    summary["verification"] = {
                        "decision": "failed" if decision == "failed" else "pending",
                        "reason": summary["state_reason"],
                    }
                return summary
            check = capture_check(summary["adapter"])
            if check is not None and completion == "verified":
                if not check(directory, summary, digest):
                    summary["verification"] = {"decision": "pending", "reason": capture_reason(summary["adapter"])}
                    return summary
            requirements = self._read_requirements(directory, run) if task_verified else None
            tool_reason, tools = self._tool_evidence(
                directory, run_id, requirements["required_tools"] if requirements else (),
            )
            if tool_reason == "required_action_denied":
                decision, reason = "failed", tool_reason
            elif completion != "verified" or not task_verified:
                summary["verification"] = {"decision": "pending", "reason": "artifact_integrity"}
                return summary
            else:
                if evidence_mismatch:
                    raise LedgerError(30, "Controller evidence belongs to a different task, result or bundle")
                decision, reason = self._evaluate(summary, directory, data, evidence, requirements, tool_reason, bundle)
            report = {"run_id": run_id, "task_sha256": run["task_sha256"], "result_sha256": digest,
                      "template": run["template"], "decision": decision, "reason": reason, "tools": tools}
            if bundle_sha is not None:
                report["bundle_manifest_sha256"] = bundle_sha
            if evidence is not None and evidence.source_sha256 is not None:
                report["controller_evidence_sha256"] = evidence.source_sha256
            if evidence is not None and reason != "required_action_denied":
                report.update(controller_confirmed=evidence.confirmed, workspace_changed=evidence.workspace_changed,
                              reviewed_findings=evidence.reviewed_findings, experiments=evidence.experiments,
                              checked_citations=evidence.checked_citations, diff_sha256=evidence.diff_sha256,
                              changed_files=evidence.changed_files, command_results=evidence.command_results)
            atomic_write(directory / "verify.json", _bytes(report))
            if decision != "pending":
                summary = self.manager._collect(
                    db, self.manager._run(db, run_id),
                    verified_sha256=digest, required_action_denied=reason == "required_action_denied",
                    verification_failure=reason if decision == "failed" else None,
                )
                if reason == "citation_invalid" and summary["acceptance_status"] == "PENDING":
                    summary = self.manager._record_acceptance(db, self.manager._run(db, run_id),
                                                              accepted=False, reason=reason)
            summary["verification"] = report
            return summary
