"""Helpers shared by every CLI Worker Adapter.

They lived in agy_print.py, which made the shared base depend on the agy module. agy_print re-exports
every name, so its imports and test patch targets stay valid.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import UUID

from ..context_bundle import _workspace_snapshot
from ..ledger import LedgerError, workspace_binding
from ..run_manager import _bytes
from ..verifier import TEMPLATES_DIR

CAPTURE_STATES = {"LAUNCHING", "RUNNING", "COLLECTING", "SUBMISSION_UNKNOWN", "TIMED_OUT"}
WORKER_ENV = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT",
              "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
              "PROGRAMFILES", "PROGRAMFILES(X86)", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}
# Keywords that strict structured outputs may not accept; the Verifier still enforces the full template rules.
STRICT_UNSUPPORTED = frozenset({"minLength", "maxLength", "pattern", "minimum", "maximum", "minItems", "maxItems",
                                "format"})
JSON_TYPES = ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string"))


def strict_output_schema(schema):
    """Derive a strict JSON Schema document from the shared result schema, never a hand-written copy."""
    def convert(node):
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        converted = {key: convert(value) for key, value in node.items() if key not in STRICT_UNSUPPORTED}
        if "const" in converted:
            value = converted.pop("const")
            kind = next(name for python_type, name in JSON_TYPES if type(value) is python_type)
            converted.setdefault("type", kind)
            converted["enum"] = [value]
        return converted
    return convert(deepcopy(schema))


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


def _object_sequence_final(text, decoder):
    """The last of back-to-back JSON objects filling all of `text`, or None."""
    final = None
    try:
        while text:
            final, end = decoder.raw_decode(text)
            if not isinstance(final, dict):
                return None
            text = text[end:].lstrip()
    except ValueError:
        return None
    return final


def _contains_object(text, decoder):
    """True when any `{` in plain text starts a decodable JSON object."""
    index = text.find("{")
    while index >= 0:
        try:
            if isinstance(decoder.raw_decode(text, index)[0], dict):
                return True
        except ValueError:
            pass
        index = text.find("{", index + 1)
    return False


def _response_agrees(response, structured, schema_echo, expected_schema):
    """Allow plain narration, then complete draft objects, before the display copy of the structured result.

    The narration is never part of the result: the result comes from `structured_output`, and the narration
    may not carry a JSON object of its own. Nothing may follow the last object, which must equal the result.
    """
    text = response.strip() if response else ""
    decoder = json.JSONDecoder(object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)
    try:
        if schema_echo is not None and _bytes(schema_echo) != _bytes(expected_schema):
            return False
        if not text or text == structured["summary"].strip():
            return True
        canonical = _bytes(structured)
        start = text.find("{")
        while start >= 0:
            final = _object_sequence_final(text[start:], decoder)
            if final is not None:
                return _bytes(final) == canonical and not _contains_object(text[:start], decoder)
            start = text.find("{", start + 1)
        return False
    except (ValueError, RecursionError):
        return False
