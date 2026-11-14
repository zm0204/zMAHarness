"""Offline Grant validation, reservation, and launch bookkeeping."""
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
import json
from os.path import normcase
from pathlib import Path
import re
from stat import S_ISDIR
from uuid import uuid4

from .adapters.registry import policy_for
from .db import connect, initialize, transaction

BRAND_NAME = "zMAHarness"
SUGGESTION_CARD_TITLE = f"【{BRAND_NAME} 協作建議】"
TEMPLATE_TIMEOUTS = {"review": 900, "second-opinion": 900, "research": 1200, "delegate-implement": 2700}
TEMPLATES = set(TEMPLATE_TIMEOUTS)
READ_ONLY_TEMPLATES = frozenset({"review", "second-opinion", "research"})
DESKTOP_APPROVAL_EVIDENCE = "codex_desktop_decision"
DISPATCH_SCOPE_FIELDS = {"timeout_seconds", "workspace_path", "task_sha256"}
BOUND_SCOPE_FIELDS = DISPATCH_SCOPE_FIELDS | {"workspace_identity"}
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")
TERMINAL_STATES = ("SUCCEEDED", "FAILED", "FAILED_PRE_SUBMIT", "CANCELLED", "ABANDONED")
REFUND_STATES = {"CANCELLED", "FAILED_PRE_SUBMIT"}
PRE_SUBMIT_REASONS = {"auth_missing", "unknown_flag", "model_not_found", "config_parse_error", "process_not_started"}
AGY_OUTPUT_CONFLICT = "structured_output_conflict"
DELEGATE_TEMPLATE = "delegate-implement"
DELEGATE_DEFERRED = ("delegate-implement 屬於延後階段的功能，目前所有 Worker 只做唯讀分析"
                     "（review、second-opinion、research）")
TRANSITIONS = {
    "RESERVED": {"PREPARED", "CANCELLED"},
    "PREPARED": {"LAUNCHING", "CANCELLED"},
    "LAUNCHING": {"RUNNING", "SUBMISSION_UNKNOWN", "FAILED_PRE_SUBMIT", "CANCELLED"},
    "RUNNING": {"COLLECTING", "TIMED_OUT", "SUBMISSION_UNKNOWN"},
    "COLLECTING": {"SUCCEEDED", "FAILED"},
    "TIMED_OUT": {"COLLECTING", "FAILED", "RUNNING"},
    "SUBMISSION_UNKNOWN": {"COLLECTING", "ABANDONED", "FAILED", "RUNNING"},
}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
TARGET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}-[0-9a-f]{32}")
SHA256 = re.compile(r"[0-9a-f]{64}")
BUSY_QUERY = "SELECT 1 FROM runs WHERE state NOT IN (?, ?, ?, ?, ?)"
GRANTS_WITH_CARD_HASH = "SELECT grants.*, cards.content_sha256 FROM grants JOIN cards USING (card_id)"


class LedgerError(ValueError):
    """A domain error carrying the exit code defined by the V2 plan."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Limits:
    max_dispatches: int = 5
    ttl_minutes: int = 240
    max_effort: str = "high"

    def __post_init__(self):
        if (type(self.max_dispatches) is not int or self.max_dispatches < 1
                or type(self.ttl_minutes) is not int or self.ttl_minutes < 1
                or self.max_effort not in EFFORT_LEVELS):
            raise ValueError("Invalid Ledger limits")


def utc_now():
    return datetime.now(UTC)


def timestamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def path_identity(value):
    if not value:
        raise LedgerError(2, "A local path is required")
    return normcase(str(Path(value).resolve()))


def workspace_binding(value):
    """Resolve aliases and identify the physical directory that will be passed to the OS."""
    try:
        path = Path(value).resolve(strict=True)
        info = path.stat()
        if S_ISDIR(info.st_mode) and info.st_ino:
            return {"workspace_path": normcase(str(path)), "workspace_identity": [info.st_dev, info.st_ino]}
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    raise LedgerError(2, "Worktree must be an identifiable local directory")


def require_bound_workspace(value, scope):
    try:
        binding = workspace_binding(value)
    except LedgerError:
        binding = {}
    if any(binding.get(key) != scope[key] for key in ("workspace_path", "workspace_identity")):
        raise LedgerError(10, "Worktree path or directory identity differs from the approved scope")


def require_dispatch_scope(db, grant, adapter, *, workspace_path, task_sha256, timeout_seconds):
    via, evidence = grant["approved_via"], grant["approval_evidence"]
    policy = policy_for(adapter)
    if via == "codex_prompt":
        if evidence != DESKTOP_APPROVAL_EVIDENCE or grant["max_dispatches"] != 1:
            raise LedgerError(10, "Invalid Desktop approval source or dispatch limit")
    elif policy.attended_approval_only and (
            via != "manual_cli" or evidence not in (None, "interactive_terminal_confirmation")):
        raise LedgerError(10, f"Unsupported {adapter} approval source; local window interaction is synthetic-only")
    saved = grant["dispatch_scope_json"]
    if saved is None:
        if policy.requires_dispatch_scope or via == "codex_prompt":
            raise LedgerError(10, "This grant lacks dispatch scope; create and approve a new card")
        return None
    card = db.execute("SELECT status, content_json, content_sha256 FROM cards WHERE card_id = ?",
                      (grant["card_id"],)).fetchone()
    try:
        scope = json.loads(saved)
        content = json.loads(card["content_json"])
        expected_scope = {key: content[key] for key in BOUND_SCOPE_FIELDS} | {"card_sha256": card["content_sha256"]}
        if (card["status"] != "APPROVED" or digest(card["content_json"]) != card["content_sha256"]
                or scope != expected_scope
                or grant["repo_root"] != content["repo_root"]
                or grant["max_dispatches"] != content["max_dispatches"]
                or json.loads(grant["allowed_templates"]) != content["allowed_templates"]
                or json.loads(grant["allowed_targets"]) != content["allowed_targets"]):
            raise ValueError("Grant differs from the approved card")
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerError(10, "Grant scope no longer matches its approved card") from exc
    if type(timeout_seconds) is not int or timeout_seconds != scope["timeout_seconds"]:
        raise LedgerError(10, "Timeout differs from the approved scope")
    if not valid_id(SHA256, task_sha256) or not compare_digest(task_sha256, scope["task_sha256"]):
        raise LedgerError(10, "Task SHA-256 differs from the approved scope")
    require_bound_workspace(workspace_path, scope)
    return scope


def valid_id(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def delegate_deferred(templates, adapters):
    """True when delegate-implement is requested for any Adapter that is not allowed to run it."""
    return DELEGATE_TEMPLATE in templates and any(
        not policy_for(adapter).allows_delegate_implement for adapter in adapters)


def expire_grants(db, now):
    db.execute("UPDATE grants SET status = 'EXPIRED' WHERE status = 'ACTIVE' AND expires_at <= ?", (now,))


def event(db, run_id, now, previous, state, detail=None, *, actor="mah"):
    db.execute(
        """INSERT INTO run_events (run_id, ts, from_state, to_state, actor, detail_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (run_id, now, previous, state, actor, dump(detail) if detail is not None else None),
    )


class GrantLedger:
    """Persist the core authorization checks before any worker may be started."""

    def __init__(self, database, *, limits=Limits(), clock=utc_now):
        self.database = Path(database).resolve()
        self.limits = limits
        self.clock = clock
        initialize(self.database)

    def create_card(self, card_id, *, repo_root, task_key, trigger_ids, prepare_mode,
                    allowed_templates, allowed_targets, max_dispatches, ttl_minutes,
                    override_suggestion_limit=False, enabled_adapters=None,
                    timeout_seconds=None, workspace_path=None, task_sha256=None):
        if not valid_id(IDENTIFIER, card_id) or not valid_id(IDENTIFIER, task_key):
            raise LedgerError(2, "Invalid card or task identifier")
        if prepare_mode not in ("card", "prep_then_card", "explicit"):
            raise LedgerError(2, "Invalid card preparation mode")
        if (not isinstance(trigger_ids, (list, tuple)) or not trigger_ids
                or any(item not in ("T1", "T2", "T3", "T4", "T5", "T6") for item in trigger_ids)):
            raise LedgerError(2, "A valid trigger is required")
        if (not isinstance(allowed_templates, (list, tuple)) or not allowed_templates
                or any(not isinstance(item, str) or item not in TEMPLATES for item in allowed_templates)):
            raise LedgerError(2, "A valid template scope is required")
        if not isinstance(allowed_targets, (list, tuple)) or not allowed_targets:
            raise LedgerError(2, "A model target is required")
        for target in allowed_targets:
            if (not isinstance(target, dict) or target.keys() != {"adapter", "model", "effort_max"}
                    or not valid_id(TARGET_ID, target["adapter"]) or not valid_id(TARGET_ID, target["model"])
                    or target["effort_max"] not in EFFORT_LEVELS):
                raise LedgerError(2, "Invalid model target")
            if enabled_adapters is not None and target["adapter"] not in enabled_adapters:
                raise LedgerError(14, "Adapter is not enabled in local settings")
            if EFFORT_LEVELS.index(target["effort_max"]) > EFFORT_LEVELS.index(self.limits.max_effort):
                raise LedgerError(16, "Requested effort exceeds the hard limit")
        if delegate_deferred(allowed_templates, [target["adapter"] for target in allowed_targets]):
            raise LedgerError(2, DELEGATE_DEFERRED)
        if type(max_dispatches) is not int or type(ttl_minutes) is not int or min(max_dispatches, ttl_minutes) < 1:
            raise LedgerError(2, "Dispatch limit and duration must be positive integers")
        if max_dispatches > self.limits.max_dispatches or ttl_minutes > self.limits.ttl_minutes:
            raise LedgerError(16, "Card exceeds the dispatch or duration hard limit")
        repo = path_identity(repo_root)
        scope = {}
        if any(policy_for(target["adapter"]).requires_dispatch_scope for target in allowed_targets) or any(
            value is not None for value in (timeout_seconds, workspace_path, task_sha256)
        ):
            if (type(timeout_seconds) is not int
                    or not 1 <= timeout_seconds <= min(TEMPLATE_TIMEOUTS[template] for template in allowed_templates)
                    or not workspace_path or not valid_id(SHA256, task_sha256)):
                raise LedgerError(2, "Card requires a valid timeout_seconds, workspace_path and task_sha256")
            scope = {"timeout_seconds": timeout_seconds, "task_sha256": task_sha256} | workspace_binding(workspace_path)
        content = dump({
            "card_id": card_id, "repo_root": repo, "task_key": task_key, "trigger_ids": trigger_ids,
            "prepare_mode": prepare_mode, "allowed_templates": allowed_templates,
            "allowed_targets": allowed_targets, "max_dispatches": max_dispatches, "ttl_minutes": ttl_minutes,
        } | scope)
        content_hash = digest(content)
        with transaction(self.database) as db:
            if db.execute("SELECT 1 FROM cards WHERE card_id = ?", (card_id,)).fetchone():
                raise LedgerError(2, "Card ID already exists")
            prior_cards = db.execute(
                "SELECT count(*) FROM cards WHERE repo_root = ? AND task_key = ?", (repo, task_key),
            ).fetchone()[0]
            if prior_cards >= 2 and not override_suggestion_limit:
                raise LedgerError(2, "Task already has two proposals; --override-suggestion-limit is required")
            db.execute(
                """INSERT INTO cards (card_id, created_at, repo_root, task_key, trigger_ids, prepare_mode,
                                      content_json, content_sha256, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSED')""",
                (card_id, timestamp(self.clock()), repo, task_key, dump(trigger_ids), prepare_mode,
                 content, content_hash),
            )
        return content_hash

    def get_card(self, card_id):
        with closing(connect(self.database)) as db:
            return db.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()

    def reject_card(self, card_id):
        with transaction(self.database) as db:
            if not db.execute(
                "UPDATE cards SET status = 'REJECTED' WHERE card_id = ? AND status = 'PROPOSED'",
                (card_id,),
            ).rowcount:
                raise LedgerError(2, "Card is not pending")

    @staticmethod
    def _approval_card(db, card_id, content_sha256, via, *, window=False):
        allowed = ("windows_gui",) if window else ("manual_cli", "codex_prompt")
        if via not in allowed:
            raise LedgerError(10, "Unsupported approval source")
        if not valid_id(SHA256, content_sha256):
            raise LedgerError(10, "Card hash is missing or invalid")
        card = db.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        if card is None or card["status"] != "PROPOSED":
            raise LedgerError(10, "Card is not pending approval")
        if not (compare_digest(card["content_sha256"], content_sha256)
                and compare_digest(digest(card["content_json"]), content_sha256)):
            raise LedgerError(10, "Card hash does not match")
        content = json.loads(card["content_json"])
        # A card stored before delegate-implement was deferred is refused at approval.
        if delegate_deferred(content["allowed_templates"], [target["adapter"] for target in content["allowed_targets"]]):
            raise LedgerError(10, DELEGATE_DEFERRED)
        if BOUND_SCOPE_FIELDS <= content.keys():
            require_bound_workspace(content["workspace_path"], content)
        elif BOUND_SCOPE_FIELDS & content.keys() or any(
            policy_for(target["adapter"]).requires_dispatch_scope for target in content["allowed_targets"]
        ):
            raise LedgerError(10, "Card lacks a complete dispatch scope; create a new card")
        if via == "codex_prompt" and (content["max_dispatches"] != 1 or not BOUND_SCOPE_FIELDS <= content.keys()):
            raise LedgerError(10, "Desktop approval requires a fully scoped single-dispatch card")
        if window and (content["max_dispatches"] != 1 or not BOUND_SCOPE_FIELDS <= content.keys()
                       or any(not policy_for(target["adapter"]).window_approval
                              for target in content["allowed_targets"])):
            raise LedgerError(10, "核准視窗僅接受已綁定範圍的單次 Fake Adapter 卡片")
        return card

    def approval_candidate(self, card_id, content_sha256, *, via):
        with closing(connect(self.database)) as db:
            return self._approval_card(db, card_id, content_sha256, via)

    def approve(self, card_id, content_sha256, *, via, evidence=None):
        """Record a trusted Controller decision or the existing terminal confirmation."""
        expected = DESKTOP_APPROVAL_EVIDENCE if via == "codex_prompt" else "interactive_terminal_confirmation"
        if evidence not in (None, expected):
            raise LedgerError(2, "Invalid approval evidence")
        if via == "codex_prompt":
            evidence = DESKTOP_APPROVAL_EVIDENCE
        return self._record_approval(card_id, content_sha256, via, evidence)

    def _check_window_scope(self, card):
        content = json.loads(card["content_json"])
        state = self.database.parent
        repo = Path(content["repo_root"])
        workspace = Path(content["workspace_path"])
        runs = state / "runs"
        if (not repo.is_relative_to(state) or repo == state or repo.is_relative_to(runs)
                or any((parent / ".git").exists() for parent in (repo, *repo.parents))
                or workspace.parent.parent != runs or workspace.name != "workspace"
                or not valid_id(RUN_ID, workspace.parent.name)):
            raise LedgerError(10, "核准視窗須使用獨立的合成來源及 Fake Adapter 工作目錄")

    def _window_candidate(self, card_id, content_sha256):
        with closing(connect(self.database)) as db:
            card = self._approval_card(db, card_id, content_sha256, "windows_gui", window=True)
            self._check_window_scope(card)
            return card

    def _record_window_decision(self, card_id, content_sha256, *, reject, check_deadline):
        return self._record_approval(card_id, content_sha256, "windows_gui", "local_window_interaction",
                                     window=True, reject=reject, check_deadline=check_deadline)

    def _record_approval(self, card_id, content_sha256, via, evidence, *,
                         window=False, reject=False, check_deadline=None):
        with transaction(self.database) as db:
            card = self._approval_card(db, card_id, content_sha256, via, window=window)
            if window:
                self._check_window_scope(card)
                check_deadline()
            if reject:
                db.execute("UPDATE cards SET status = 'REJECTED' WHERE card_id = ?", (card_id,))
                return None
            content = json.loads(card["content_json"])
            scope = None
            if BOUND_SCOPE_FIELDS <= content.keys():
                scope = {key: content[key] for key in BOUND_SCOPE_FIELDS} | {"card_sha256": content_sha256}
            now = self.clock()
            approved = timestamp(now)
            expires = timestamp(now + timedelta(minutes=content["ttl_minutes"]))
            grant_id = uuid4().hex
            expire_grants(db, approved)
            db.execute(
                """UPDATE grants SET status = 'SUPERSEDED', superseded_by = ?
                   WHERE repo_root = ? AND status = 'ACTIVE'""",
                (grant_id, content["repo_root"]),
            )
            db.execute(
                """INSERT INTO grants (grant_id, card_id, repo_root, status, approved_at, approved_via,
                                       approval_evidence, expires_at, allowed_templates, allowed_targets,
                                       max_dispatches, dispatch_scope_json)
                   VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (grant_id, card_id, content["repo_root"], approved, via, evidence, expires,
                 dump(content["allowed_templates"]), dump(content["allowed_targets"]), content["max_dispatches"],
                 dump(scope) if scope else None),
            )
            db.execute("UPDATE cards SET status = 'APPROVED' WHERE card_id = ?", (card_id,))
        return grant_id

    def get_grant(self, grant_id):
        with transaction(self.database) as db:
            expire_grants(db, timestamp(self.clock()))
            return db.execute(GRANTS_WITH_CARD_HASH + " WHERE grant_id = ?", (grant_id,)).fetchone()

    def list_grants(self):
        with transaction(self.database) as db:
            expire_grants(db, timestamp(self.clock()))
            return db.execute(GRANTS_WITH_CARD_HASH + " ORDER BY approved_at, grant_id").fetchall()

    def close(self, grant_id):
        with transaction(self.database) as db:
            if not db.execute(
                "UPDATE grants SET status = 'CLOSED' WHERE grant_id = ? AND status = 'ACTIVE' AND expires_at > ?",
                (grant_id, timestamp(self.clock())),
            ).rowcount:
                raise LedgerError(12, "Grant is not active")

    def get_run(self, run_id):
        with closing(connect(self.database)) as db:
            return db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def check_and_reserve(self, grant_id, run_id, *, repo_root, template, adapter, model, effort,
                          workspace_kind, workspace_path, task_sha256, timeout_seconds=None):
        if not valid_id(RUN_ID, run_id) or not valid_id(SHA256, task_sha256):
            raise LedgerError(2, "Invalid run ID or task hash")
        if workspace_kind not in ("read_only", "worktree", "disposable_worktree", "gui_fixed"):
            raise LedgerError(2, "Invalid workspace kind")
        repo, workspace = path_identity(repo_root), path_identity(workspace_path)
        with transaction(self.database) as db:
            now = timestamp(self.clock())
            expire_grants(db, now)
            grant = db.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            failure = self._reservation_failure(db, grant, now, run_id, repo, template, adapter, model, effort)
            if failure is None:
                try:
                    require_dispatch_scope(db, grant, adapter, workspace_path=workspace_path,
                                           task_sha256=task_sha256, timeout_seconds=timeout_seconds)
                except LedgerError as exc:
                    failure = exc
            if failure is None:
                db.execute("UPDATE grants SET used_dispatches = used_dispatches + 1 WHERE grant_id = ?", (grant_id,))
                db.execute(
                    """INSERT INTO runs (run_id, grant_id, template, adapter, model_requested, effort_requested,
                                         state, repo_root, workspace_kind, workspace_path, task_sha256, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?, ?, ?, ?)""",
                    (run_id, grant_id, template, adapter, model, effort, repo,
                     workspace_kind, workspace, task_sha256, now),
                )
                event(db, run_id, now, None, "RESERVED")
        if failure is not None:
            raise failure
        return run_id

    @staticmethod
    def _reservation_failure(db, grant, now, run_id, repo, template, adapter, model, effort):
        if grant is None or grant["repo_root"] != repo or grant["status"] == "SUPERSEDED":
            return LedgerError(10, "An active grant for this repository is required")
        if grant["status"] in ("CLOSED", "EXPIRED") or grant["expires_at"] <= now:
            return LedgerError(12, "Grant has expired or closed")
        templates = json.loads(grant["allowed_templates"])
        targets = json.loads(grant["allowed_targets"])
        # A Grant approved before delegate-implement was deferred cannot reserve it.
        if delegate_deferred([template], [adapter]):
            return LedgerError(10, DELEGATE_DEFERRED)
        if template not in templates or effort not in EFFORT_LEVELS or not any(
            target["adapter"] == adapter and target["model"] == model
            and EFFORT_LEVELS.index(effort) <= EFFORT_LEVELS.index(target["effort_max"])
            for target in targets
        ):
            return LedgerError(10, "Request is outside the grant scope")
        if db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone():
            return LedgerError(2, "Run ID has already been reserved")
        if grant["status"] == "EXHAUSTED" or grant["used_dispatches"] >= grant["max_dispatches"]:
            return LedgerError(11, "Grant has no remaining dispatches")
        if db.execute(BUSY_QUERY + " LIMIT 1", TERMINAL_STATES).fetchone():
            return LedgerError(13, "Another run is still active")
        return None

    def transition(self, run_id, expected, state, *, reason=None):
        """Transition existing offline callers; evidence-bearing steps use RunManager."""
        if state in ("RUNNING", "COLLECTING", "SUCCEEDED"):
            raise LedgerError(20, "Run Manager evidence is required")
        if reason is not None and state != "FAILED_PRE_SUBMIT":
            raise LedgerError(2, "This transition does not accept a reason")
        with transaction(self.database) as db:
            run = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None or run["state"] != expected:
                raise LedgerError(20, "Run is missing or has already changed state")
            self._transition(db, run, state, reason=reason)

    def _transition(self, db, run, state, *, reason=None, evidence=None, pid=None,
                    conversation_id=None, exit_code=None, result_sha256=None):
        expected = run["state"]
        replaying_output = (
            expected == "FAILED" and state == "COLLECTING" and reason == "agy_output_recollected"
            and policy_for(run["adapter"]).replays_structured_output and run["state_reason"] == AGY_OUTPUT_CONFLICT
            and run["acceptance_status"] == "PENDING" and run["result_sha256"] is None
            and run["exit_code"] == 0 and run["dispatch_consumed"] == 1
            and valid_id(SHA256, result_sha256)
            and valid_id(SHA256, (evidence or {}).get("agy_replay_sha256"))
        )
        if state not in TRANSITIONS.get(expected, ()) and not replaying_output:
            raise LedgerError(20, "Invalid run transition")
        if state == "FAILED_PRE_SUBMIT" and reason not in PRE_SUBMIT_REASONS:
            raise LedgerError(20, "Known pre-submit evidence is required for a refund")
        if state == "FAILED_PRE_SUBMIT" and reason == "process_not_started" and (
            not policy_for(run["adapter"]).refunds_unstarted_process or not (evidence or {}).get("agy_capture_sha256")
        ):
            raise LedgerError(20, "Local process failure requires a bound agy capture")
        if (state == "CANCELLED" and expected == "LAUNCHING"
                and (not policy_for(run["adapter"]).gui_submission or reason != "gui_not_submitted")):
            raise LedgerError(20, "A GUI launch needs explicit not-submitted evidence")
        refund = state in REFUND_STATES
        if refund and not run["dispatch_consumed"]:
            raise LedgerError(20, "Run debit was already returned")
        now = timestamp(self.clock())
        run_id = run["run_id"]
        changed = db.execute(
            """UPDATE runs SET state = ?, state_reason = ?,
               launched_at = COALESCE(?, launched_at),
               finished_at = CASE WHEN ? THEN NULL ELSE COALESCE(?, finished_at) END,
               dispatch_consumed = ?, pid = COALESCE(?, pid), conversation_id = COALESCE(?, conversation_id),
               exit_code = COALESCE(?, exit_code), result_sha256 = COALESCE(?, result_sha256)
               WHERE run_id = ? AND state = ?""",
            (state, reason, now if state == "LAUNCHING" else None, replaying_output,
             now if state in TERMINAL_STATES else None, 0 if refund else run["dispatch_consumed"],
             pid, conversation_id, exit_code, result_sha256, run_id, expected),
        ).rowcount
        if not changed:
            raise LedgerError(20, "Run has already changed state")
        if refund and not db.execute(
            "UPDATE grants SET used_dispatches = used_dispatches - 1 WHERE grant_id = ? AND used_dispatches > 0",
            (run["grant_id"],),
        ).rowcount:
            raise LedgerError(20, "Run debit could not be returned")
        detail = {"refund": refund, "reason": reason} | (evidence or {})
        if exit_code is not None:
            detail["exit_code"] = exit_code
        if result_sha256 is not None:
            detail["result_sha256"] = result_sha256
        event(db, run_id, now, expected, state, detail)
        expire_grants(db, now)
        if state in TERMINAL_STATES and not refund:
            db.execute(
                """UPDATE grants SET status = 'EXHAUSTED'
                   WHERE grant_id = ? AND status = 'ACTIVE' AND used_dispatches = max_dispatches""",
                (run["grant_id"],),
            )
