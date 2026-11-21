"""OpenAI-compatible HTTP API Worker Adapter (openai-api)."""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import os
from pathlib import Path
from time import monotonic
from typing import Any

from ..context_bundle import bundle_manifest
from ..db import transaction
from ..ledger import (
    READ_ONLY_TEMPLATES,
    TEMPLATE_TIMEOUTS,
    TERMINAL_STATES,
    LedgerError,
    event,
    require_bound_workspace,
    timestamp,
)
from ..run_manager import _bytes, _publish, _read, _result_document, record_observation
from ..verifier import _template_valid, requirements_for
from .api_providers import (
    ApiProviderConfig,
    check_api_key_env,
    is_model_not_found_404,
    parse_base_url,
    split_provider_model,
)
from .api_transport import ALLOWED_RESPONSE_HEADERS, TransportResult, execute_request
from .cli_common import (
    CAPTURE_STATES,
    _bundle_workspace_snapshot,
    _count,
    _json,
    _require_bundle_directory,
    _response_schema,
    _text,
    strict_output_schema,
)
from .cli_worker import CliWorkerAdapter, _neutral

ALLOWED_REQUEST_KEYS = frozenset({
    "model", "messages", "response_format", "max_tokens", "stream", "temperature", "reasoning_effort", "provider",
})
FORBIDDEN_REQUEST_KEYS = frozenset({
    "tools", "tool_choice", "functions", "function_call", "plugins", "web_search_options",
})
TERMINATIONS = ("exited", "timeout", "interrupted", "not_started")
STAGES = ("pre_connect", "connect", "send", "recv", "done")
USAGE_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens", "cached_tokens")


@dataclass(frozen=True)
class ApiInvocationPlan:
    run_id: str
    provider: ApiProviderConfig
    raw_model: str
    cwd: Path
    workspace_kind: str
    timeout_s: int
    metadata: bytes
    request_bytes: bytes
    request_sha256: str
    baseline: str
    task_sha256: str
    model: str
    effort: str
    template: str


@dataclass(frozen=True)
class ApiLaunchHandle:
    plan: ApiInvocationPlan
    started_at: str
    clock_started: float
    not_sent_reason: str | None = None
    execute: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class ApiCapture:
    run_id: str
    task_sha256: str
    invocation_sha256: str
    request_sha256: str
    response_body: bytes
    stage: str
    http_status: int | None
    headers: dict
    termination: str = "exited"
    launch_error: str | None = None
    error_detail: dict | None = None
    response_too_large: bool = False
    redacted: bool = False
    wall_milliseconds: int | None = None
    workspace_changed: bool | None = None
    workspace_after_sha256: str | None = None


def _minimal_example(run_id: str, template: str) -> dict:
    if template == "second-opinion":
        payload = {
            "hypotheses": [{"hypothesis": "替代假設", "evidence": "觀察證據", "experiment": "驗證步驟"}],
            "recommended_next_step": "建議下一步",
        }
    elif template == "research":
        payload = {
            "answers": [{
                "question": "問題",
                "answer": "回答",
                "citations": [{"path": "file.py", "lines": "L1", "claim": "依據"}],
            }]
        }
    else:
        payload = {"findings": []}
    return {
        "schema_version": "2.0",
        "run_id": run_id,
        "template": template,
        "status": "completed",
        "summary": "摘要說明",
        "blocked_reason": None,
        "open_questions": [],
        "template_payload": payload,
    }


def _check_request_payload(payload: dict) -> None:
    if (not isinstance(payload, dict)
            or not set(payload) <= ALLOWED_REQUEST_KEYS
            or bool(set(payload) & FORBIDDEN_REQUEST_KEYS)):
        raise LedgerError(17, "openai-api request body contains forbidden or non-allowlisted keys")


def _check_capture(capture: ApiCapture) -> None:
    if (not isinstance(capture, ApiCapture)
            or not isinstance(capture.response_body, bytes)
            or capture.stage not in STAGES
            or capture.termination not in TERMINATIONS
            or (capture.http_status is not None and type(capture.http_status) is not int)
            or not isinstance(capture.headers, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) or k not in ALLOWED_RESPONSE_HEADERS
                   for k, v in capture.headers.items())
            or (capture.wall_milliseconds is not None and _count(capture.wall_milliseconds) is None)
            or type(capture.response_too_large) is not bool
            or type(capture.redacted) is not bool
            or (capture.error_detail is not None and not isinstance(capture.error_detail, dict))):
        raise LedgerError(30, "Invalid openai-api process capture")
    if capture.launch_error is not None:
        if (capture.launch_error not in ("request_not_sent", "auth_missing")
                or capture.stage not in ("pre_connect", "connect")
                or capture.http_status is not None
                or capture.response_body):
            raise LedgerError(30, "A pre-submit API receipt must prove no request bytes were sent")
    elif capture.termination == "not_started":
        raise LedgerError(30, "A not_started API receipt requires a launch_error")


def _strip_single_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_nl = stripped.find("\n")
        if first_nl > 0:
            header = stripped[3:first_nl].strip().lower()
            inner = stripped[first_nl + 1:-3]
            if header in ("", "json") and "```" not in inner:
                return inner.strip()
    return stripped


def normalize_response(run_id: str, raw: bytes) -> dict:
    """Inspect a completed HTTP 200 body and extract observation metadata without trusting content yet."""
    base = {
        "conversation_id": f"api:{run_id}",
        "response_id": None,
        "usage": None,
        "model_observed": "unknown",
        "finish_reason": None,
        "reasoning_field_present": False,
        "envelope": None,
    }
    try:
        data = _json(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return base
    if not isinstance(data, dict):
        return base

    response_id = _text(data.get("id"))
    model_obs = _text(data.get("model"))
    usage_raw = data.get("usage")
    tokens = {}
    if isinstance(usage_raw, dict):
        for key in USAGE_TOKEN_FIELDS:
            if _count(usage_raw.get(key)) is not None:
                tokens[key] = usage_raw[key]

    finish_reason = None
    reasoning_present = False
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice0 = choices[0]
        finish_reason = _text(choice0.get("finish_reason"))
        msg = choice0.get("message")
        if isinstance(msg, dict):
            for r_key in ("reasoning_content", "reasoning"):
                r_val = msg.get(r_key)
                if r_val is not None and r_val != "" and r_val != []:
                    reasoning_present = True
        for r_key in ("reasoning_content", "reasoning"):
            r_val = choice0.get(r_key)
            if r_val is not None and r_val != "" and r_val != []:
                reasoning_present = True

    return {
        "conversation_id": f"api:{run_id}",
        "response_id": response_id if response_id and response_id.strip() else None,
        "usage": tokens or None,
        "model_observed": model_obs.strip() if model_obs and model_obs.strip() else "unknown",
        "finish_reason": finish_reason,
        "reasoning_field_present": reasoning_present,
        "envelope": data,
    }


def capture_matches(directory: Path, run, result_sha256: str) -> bool:
    """Verify that Verifier is reading the result and receipt chain from the same sealed API capture."""
    try:
        raw_capture = _read(directory / "api.capture.json")
        receipt = _json(raw_capture)
        if (receipt["adapter"] != "openai-api"
                or receipt["run_id"] != run["run_id"]
                or receipt["task_sha256"] != run["task_sha256"]
                or receipt["exit_code"] is not None
                or run["exit_code"] is not None
                or receipt["classification"] != "COLLECTING"
                or receipt["result_sha256"] != result_sha256):
            return False
        observed_id = receipt["observation"]["conversation_id"]
        if observed_id != f"api:{run['run_id']}" or observed_id != run["conversation_id"]:
            return False
        process_sha = receipt.get("process_receipt_sha256")
        raw_process = _read(directory / "api.process.json")
        if process_sha is None or sha256(raw_process).hexdigest() != process_sha:
            return False
        process_doc = _json(raw_process)
        if (process_doc["run_id"] != run["run_id"]
                or process_doc["invocation_sha256"] != receipt["invocation_sha256"]
                or process_doc["request_sha256"] != receipt["request_sha256"]
                or process_doc["response_sha256"] != receipt["response_sha256"]):
            return False
        files = {
            "invocation.json": "invocation_sha256",
            "api.request.json": "request_sha256",
            "api.response.raw": "response_sha256",
            "events.jsonl": "events_sha256",
        }
        if not all(sha256(_read(directory / name)).hexdigest() == receipt[field] for name, field in files.items()):
            return False
        invocation_doc = _json(_read(directory / "invocation.json"))
        return invocation_doc.get("request_sha256") == receipt["request_sha256"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return False


class OpenAIApiAdapter:
    """Synchronous OpenAI-compatible HTTP API Worker (Context-Bundle & read-only templates only).

    Never calls RunManager.mark_running: runs.pid, runs.exit_code, and runs.cli_version stay NULL,
    and all transitions after LAUNCHING happen atomically inside collect_saved's SQLite transaction.
    """

    id = "openai-api"
    prefix = "api"
    capture_reason = "api_capture_integrity"
    replays_output = False
    evidence_key = "capture_sha256"
    conversation_reason = "conversation_observed"

    def __init__(self, manager, *, providers=None, max_task_bytes=32768, connection_factory=None):
        self.manager = manager
        self.providers: dict[str, ApiProviderConfig] = dict(providers or {})
        self.max_task_bytes = max_task_bytes
        self.connection_factory = connection_factory

    @classmethod
    def from_config(cls, manager, config):
        providers = config.values.get("api", {}).get("providers", {}) if config is not None else {}
        max_task_bytes = config.values["limits"]["max_task_bytes"] if config is not None else 32768
        return cls(manager, providers=providers, max_task_bytes=max_task_bytes)

    def _clock(self) -> float:
        return monotonic()

    def _snapshot(self, plan: ApiInvocationPlan):
        return _bundle_workspace_snapshot(plan.cwd)

    def _bundle_directory(self, cwd: Path, repo: Path) -> None:
        try:
            _require_bundle_directory(cwd, repo, self.manager.database.parent)
        except LedgerError as exc:
            raise _neutral(exc) from None

    def _transport(self, plan: ApiInvocationPlan, secret: str | None, clock_started: float) -> TransportResult:
        remaining = max(0.1, clock_started + plan.timeout_s - self._clock())
        return execute_request(
            plan.provider,
            "POST",
            plan.provider.chat_completions_path(),
            body=plan.request_bytes,
            secret=secret,
            total_timeout_s=remaining,
            clock=self._clock,
            connection_factory=self.connection_factory,
        )

    def prepare_invocation(
        self,
        *,
        run_id,
        repo_root,
        template,
        model,
        effort,
        task,
        workspace,
        worktree_root,
        timeout_s,
        requirements,
    ) -> ApiInvocationPlan:
        if template not in READ_ONLY_TEMPLATES:
            raise LedgerError(
                17,
                f"openai-api only runs read-only analysis (review, second-opinion, research); {template} is refused",
            )
        provider_name, raw_model = split_provider_model(model)
        provider = self.providers.get(provider_name)
        if provider is None:
            raise LedgerError(14, f"找不到已設定的 API provider：{provider_name}（[api.providers.{provider_name}]）")
        if effort not in provider.allowed_efforts():
            raise LedgerError(2, f"Invalid openai-api effort {effort!r} for provider {provider_name}")
        try:
            parse_base_url(provider.base_url, label=f"api.providers.{provider_name}.base_url")
        except ValueError as exc:
            raise LedgerError(17, str(exc)) from None

        # Pre-reserve API key existence and header character check (F2-1): no Run created, no Grant consumed.
        key_ok, key_reason = check_api_key_env(provider.api_key_env)
        if not key_ok:
            raise LedgerError(14, key_reason or f"找不到 API 金鑰環境變數 {provider.api_key_env}")

        if not isinstance(task, bytes) or not 0 < len(task) <= self.max_task_bytes:
            raise LedgerError(16, "Task exceeds the configured limit or is empty")
        task_text = task.decode("utf-8-sig")
        if "\0" in task_text or not task_text.strip():
            raise LedgerError(2, "Task must be nonempty UTF-8 without NUL")

        bundle = bundle_manifest(task)
        if bundle is None or bundle[0]["version"] != 2:
            raise LedgerError(17, "openai-api only supports a sealed Manifest v2 Context-Bundle")

        max_timeout = (
            min(TEMPLATE_TIMEOUTS[template], provider.timeout_s)
            if provider.timeout_s is not None
            else TEMPLATE_TIMEOUTS[template]
        )
        timeout_s = max_timeout if timeout_s is None else timeout_s
        if type(timeout_s) is not int or not 1 <= timeout_s <= max_timeout:
            raise LedgerError(2, "openai-api timeout exceeds template or provider limits")

        requirements = requirements_for({} if requirements is None else requirements)
        directory = self.manager._directory(run_id)
        cwd, repo, root = Path(workspace).resolve(), Path(repo_root).resolve(), Path(worktree_root).resolve()
        self._bundle_directory(cwd, repo)
        if cwd == root or not cwd.is_relative_to(root):
            raise LedgerError(20, "Bundle workspace must be a directory under worktree_root")
        baseline, nonempty = _bundle_workspace_snapshot(cwd)
        if nonempty:
            raise LedgerError(20, "Bundle workspace must start empty")
        if directory.is_relative_to(cwd):
            raise LedgerError(20, "Worker evidence directory must stay outside the worker worktree")

        return self.build_invocation(
            run_id=run_id,
            template=template,
            model=model,
            raw_model=raw_model,
            provider=provider,
            effort=effort,
            task=task,
            task_text=task_text,
            cwd=cwd,
            timeout_s=timeout_s,
            baseline=baseline,
            bundle_sha256=bundle[1],
            requirements=requirements,
        )

    def build_invocation(
        self,
        *,
        run_id,
        template,
        model,
        raw_model,
        provider: ApiProviderConfig,
        effort,
        task,
        task_text,
        cwd,
        timeout_s,
        baseline,
        bundle_sha256,
        requirements,
    ) -> ApiInvocationPlan:
        schema = _response_schema(run_id, template, self.manager.max_summary_chars)
        user_prompt = CliWorkerAdapter.prompt_bytes(run_id, template, task_text).decode("utf-8")
        if provider.structured_output == "json_schema_strict":
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "zmaharness_result",
                    "strict": True,
                    "schema": strict_output_schema(schema),
                },
            }
            system_prompt = (
                "你是 zMAHarness 的唯讀分析 Worker。請勿使用或呼叫任何工具，只輸出符合指定 JSON Schema 的單一 JSON 物件。"
            )
        else:
            response_format = {"type": "json_object"}
            example = _minimal_example(run_id, template)
            system_prompt = (
                "你是 zMAHarness 的唯讀分析 Worker。請勿使用或呼叫任何工具，只以 json 格式輸出單一 JSON 物件，"
                "不得包含任何前後文字或思考過程。\n"
                f"JSON Schema:\n{_bytes(schema).decode('utf-8')}\n"
                f"最小 json 範例:\n{_bytes(example).decode('utf-8')}"
            )

        request_payload = {
            "model": raw_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": response_format,
            "max_tokens": provider.max_output_tokens,
            "stream": False,
        }
        if provider.reasoning_effort:
            request_payload["reasoning_effort"] = effort
        _check_request_payload(request_payload)

        request_bytes = _bytes(request_payload)
        request_sha256 = sha256(request_bytes).hexdigest()
        task_sha256 = sha256(task).hexdigest()
        metadata = {
            "run_id": run_id,
            "task_sha256": task_sha256,
            "cwd": str(cwd),
            "workspace_kind": "read_only",
            "workspace_baseline_sha256": baseline,
            "timeout_s": timeout_s,
            "verification_requirements": requirements,
            "provider": provider.name,
            "provider_kind": provider.kind,
            "base_url": provider.base_url,
            "endpoint": provider.chat_completions_path(),
            "host": provider.host,
            "port": provider.port,
            "local": provider.local,
            "structured_output": provider.structured_output,
            "request_sha256": request_sha256,
            "api_key_env": provider.api_key_env,
            "auth": "bearer" if provider.api_key_env else "none",
            "max_tokens": provider.max_output_tokens,
            "launch_profile": "context-bundle",
            "bundle_manifest_sha256": bundle_sha256,
        }
        return ApiInvocationPlan(
            run_id=run_id,
            provider=provider,
            raw_model=raw_model,
            cwd=cwd,
            workspace_kind="read_only",
            timeout_s=timeout_s,
            metadata=_bytes(metadata),
            request_bytes=request_bytes,
            request_sha256=request_sha256,
            baseline=baseline,
            task_sha256=task_sha256,
            model=model,
            effort=effort,
            template=template,
        )

    def launch(self, plan: ApiInvocationPlan) -> ApiLaunchHandle:
        _check_request_payload(_json(plan.request_bytes))
        manager = self.manager
        run = manager.ledger.get_run(plan.run_id)
        if run is None or (
            run["adapter"],
            run["template"],
            run["model_requested"],
            run["effort_requested"],
            run["task_sha256"],
            Path(run["workspace_path"]),
            run["workspace_kind"],
        ) != (
            self.id,
            plan.template,
            plan.model,
            plan.effort,
            plan.task_sha256,
            plan.cwd,
            plan.workspace_kind,
        ):
            raise LedgerError(10, "openai-api invocation does not match its reservation")

        directory = manager._directory(plan.run_id)
        metadata = _json(plan.metadata)
        task = _read(directory / "task.md")
        max_timeout = (
            min(TEMPLATE_TIMEOUTS[plan.template], plan.provider.timeout_s)
            if plan.provider.timeout_s is not None
            else TEMPLATE_TIMEOUTS[plan.template]
        )
        if not 1 <= plan.timeout_s <= max_timeout:
            raise LedgerError(2, "openai-api timeout exceeds template or provider limits")

        self._bundle_directory(plan.cwd, Path(run["repo_root"]))
        baseline, nonempty = self._snapshot(plan)
        if nonempty or baseline != plan.baseline:
            raise LedgerError(20, "Worker directory changed before launch")

        bundle = bundle_manifest(task)
        if bundle is None or bundle[0]["version"] != 2:
            raise LedgerError(17, "openai-api only supports a sealed Manifest v2 Context-Bundle")
        expected = self.build_invocation(
            run_id=plan.run_id,
            template=run["template"],
            model=run["model_requested"],
            raw_model=plan.raw_model,
            provider=plan.provider,
            effort=run["effort_requested"],
            task=task,
            task_text=task.decode("utf-8-sig"),
            cwd=plan.cwd,
            timeout_s=plan.timeout_s,
            baseline=baseline,
            bundle_sha256=bundle[1],
            requirements=requirements_for(metadata["verification_requirements"]),
        )
        if expected != plan:
            raise LedgerError(20, "openai-api launch plan does not match the prepared invocation")

        grant = manager.ledger.get_grant(run["grant_id"])
        if grant is None or grant["max_dispatches"] != 1:
            raise LedgerError(10, "Context-Bundle requires a grant limited to one dispatch")

        _publish(directory / "api.request.json", plan.request_bytes)
        scope = manager.claim_launch(plan.run_id, invocation=plan.metadata)
        started_at = timestamp(manager.ledger.clock())
        clock_started = self._clock()
        try:
            require_bound_workspace(plan.cwd, scope)
            self._bundle_directory(plan.cwd, Path(run["repo_root"]))
            if self._snapshot(plan) != (plan.baseline, False):
                raise LedgerError(20, "Bundle workspace changed after launch claim")
        except (LedgerError, OSError) as exc:
            manager.ledger.transition(plan.run_id, "LAUNCHING", "SUBMISSION_UNKNOWN")
            raise LedgerError(21, "Worker directory changed after launch claim; preserve the run for review") from exc

        secret = None
        if plan.provider.api_key_env is not None:
            key_ok, _ = check_api_key_env(plan.provider.api_key_env)
            if not key_ok:
                return ApiLaunchHandle(
                    plan=plan,
                    started_at=started_at,
                    clock_started=clock_started,
                    not_sent_reason="auth_missing",
                    execute=None,
                )
            secret = os.environ.get(plan.provider.api_key_env)

        def _execute_once() -> TransportResult:
            return self._transport(plan, secret, clock_started)

        return ApiLaunchHandle(
            plan=plan,
            started_at=started_at,
            clock_started=clock_started,
            not_sent_reason=None,
            execute=_execute_once,
        )

    def observe(self, handle: ApiLaunchHandle) -> ApiCapture:
        """Execute and seal the three-phase request, absorbing transport errors and KeyboardInterrupt."""
        if handle.not_sent_reason == "auth_missing" or handle.execute is None:
            outcome = TransportResult(
                stage="pre_connect",
                termination="not_started",
                launch_error="auth_missing",
                http_status=None,
                headers={},
                body=b"",
                response_too_large=False,
                redacted=False,
                error_detail={
                    "stage": "pre_connect",
                    "type": "AuthMissing",
                    "errno": None,
                    "winerror": None,
                    "message": "api_key_env missing or invalid after claim_launch",
                },
                wall_milliseconds=max(0, round((self._clock() - handle.clock_started) * 1000)),
            )
        else:
            try:
                outcome = handle.execute()
            except KeyboardInterrupt:
                outcome = TransportResult(
                    stage="connect",
                    termination="interrupted",
                    launch_error="request_not_sent",
                    http_status=None,
                    headers={},
                    body=b"",
                    response_too_large=False,
                    redacted=False,
                    error_detail={
                        "stage": "connect",
                        "type": "KeyboardInterrupt",
                        "errno": None,
                        "winerror": None,
                        "message": "interrupted before connect",
                    },
                    wall_milliseconds=max(0, round((self._clock() - handle.clock_started) * 1000)),
                )

        after = None
        if outcome.launch_error is not None:
            after = handle.plan.baseline
        else:
            try:
                after, _ = self._snapshot(handle.plan)
            except (OSError, ValueError):
                after = None

        return ApiCapture(
            run_id=handle.plan.run_id,
            task_sha256=handle.plan.task_sha256,
            invocation_sha256=sha256(handle.plan.metadata).hexdigest(),
            request_sha256=handle.plan.request_sha256,
            response_body=outcome.body,
            stage=outcome.stage,
            http_status=outcome.http_status,
            headers=dict(outcome.headers),
            termination=outcome.termination,
            launch_error=outcome.launch_error,
            error_detail=outcome.error_detail,
            response_too_large=outcome.response_too_large,
            redacted=outcome.redacted,
            wall_milliseconds=outcome.wall_milliseconds,
            workspace_changed=None if after is None else after != handle.plan.baseline,
            workspace_after_sha256=after,
        )

    def process_record(self, handle: ApiLaunchHandle, capture: ApiCapture) -> dict:
        record = asdict(capture)
        record.pop("response_body")
        record["response_sha256"] = sha256(capture.response_body).hexdigest()
        record["started_at"] = handle.started_at
        return record

    def persist_capture(self, handle: ApiLaunchHandle, capture: ApiCapture) -> None:
        directory = self.manager._directory(capture.run_id)
        _publish(directory / "api.request.json", handle.plan.request_bytes)
        _publish(directory / "api.response.raw", capture.response_body)
        _publish(directory / "api.process.json", _bytes(self.process_record(handle, capture)))

    def collect_saved(self, run_id: str, *, replay_output: bool = False):
        """Re-read sealed API observation and drive the run to its collected state without sending requests."""
        if replay_output:
            raise LedgerError(20, "Output replay is not supported for openai-api")
        directory = self.manager._directory(run_id)
        try:
            raw_receipt = _read(directory / "api.process.json")
        except FileNotFoundError:
            return self.manager.collect(run_id)
        except OSError as exc:
            raise LedgerError(30, f"Sealed API process receipt cannot be read: {exc}") from exc

        try:
            record = _json(raw_receipt)
            if not isinstance(record, dict) or record.get("run_id") != run_id:
                raise LedgerError(30, "API process receipt belongs to a different run")
            raw_request = _read(directory / "api.request.json")
            raw_response = _read(directory / "api.response.raw")
            if sha256(raw_request).hexdigest() != record.get("request_sha256"):
                raise LedgerError(30, "API process receipt does not match api.request.json")
            if sha256(raw_response).hexdigest() != record.get("response_sha256"):
                raise LedgerError(30, "API process receipt does not match api.response.raw")
            capture_fields = dict(record)
            capture_fields.pop("response_sha256", None)
            capture_fields.pop("started_at", None)
            capture_fields["response_body"] = raw_response
            capture = ApiCapture(**capture_fields)
            self._check_sealed(directory, capture)
        except LedgerError as exc:
            if exc.code in (2, 20):
                raise LedgerError(30, f"Sealed API evidence is damaged: {exc}") from exc
            raise
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
            raise LedgerError(30, f"Sealed API evidence is damaged: {exc}") from exc

        return self.collect(run_id, capture, process_receipt_sha256=sha256(raw_receipt).hexdigest())

    def _check_sealed(self, directory: Path, capture: ApiCapture) -> None:
        _check_capture(capture)
        raw_inv = _read(directory / "invocation.json")
        if sha256(raw_inv).hexdigest() != capture.invocation_sha256:
            raise LedgerError(30, "API capture does not match invocation.json")
        metadata = _json(raw_inv.decode("utf-8"))
        if not isinstance(metadata, dict) or metadata.get("request_sha256") != capture.request_sha256:
            raise LedgerError(30, "API invocation request hash mismatch")
        requirements_for(metadata["verification_requirements"])

    def _classify(self, capture: ApiCapture, observed: dict | None, run, provider_kind: str):
        if capture.launch_error == "request_not_sent":
            return "FAILED_PRE_SUBMIT", "process_not_started", None
        if capture.launch_error == "auth_missing":
            return "FAILED_PRE_SUBMIT", "auth_missing", None
        if capture.stage in ("pre_connect", "connect"):
            return "FAILED_PRE_SUBMIT", "process_not_started", None
        if capture.stage in ("send", "recv") or capture.termination in ("timeout", "interrupted") or capture.http_status is None:
            reason = "request_timed_out" if capture.termination == "timeout" else "response_unknown"
            return "SUBMISSION_UNKNOWN", reason, None
        if capture.response_too_large:
            return "FAILED", "api_response_too_large", None

        status = capture.http_status
        if status in (401, 403):
            return "FAILED_PRE_SUBMIT", "auth_missing", None
        if status == 404:
            if is_model_not_found_404(provider_kind, capture.response_body, run["model_requested"]):
                return "FAILED_PRE_SUBMIT", "model_not_found", None
            return "FAILED", "api_request_invalid", None
        if status == 402:
            return "FAILED", "api_insufficient_balance", None
        if status == 429:
            return "FAILED", "api_rate_limited", None
        if 300 <= status <= 399:
            return "FAILED", "api_redirect_refused", None
        if 400 <= status <= 499:
            return "FAILED", "api_request_invalid", None
        if status != 200:
            return "FAILED", "api_server_error", None

        if capture.workspace_changed is True:
            return "FAILED", "readonly_violation", None

        envelope = (observed or {}).get("envelope")
        if not isinstance(envelope, dict):
            return "FAILED", "schema_invalid", None
        if envelope.get("error") is not None:
            return "FAILED", "api_error_in_body", None
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return "FAILED", "api_error_in_body", None
        choice0 = choices[0]
        message = choice0.get("message")
        if not isinstance(message, dict):
            return "FAILED", "api_error_in_body", None

        finish_reason = _text(choice0.get("finish_reason"))
        tool_calls = message.get("tool_calls")
        function_call = message.get("function_call")
        has_tool_calls = tool_calls is not None and (not isinstance(tool_calls, list) or len(tool_calls) > 0)
        has_function_call = function_call is not None and (not isinstance(function_call, dict) or len(function_call) > 0)
        if has_tool_calls or has_function_call or finish_reason in ("tool_calls", "function_call"):
            return "FAILED", "api_tool_call_observed", None

        if finish_reason == "length":
            return "FAILED", "output_truncated", None

        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return "FAILED", "schema_invalid", None
        cleaned = _strip_single_code_fence(content)
        if not cleaned.startswith("{") or not cleaned.endswith("}"):
            return "FAILED", "schema_invalid", None
        try:
            parsed = _json(cleaned)
            valid = _result_document(_bytes(parsed), run, self.manager.max_summary_chars)
            if valid is not None and not _template_valid(run["template"], valid["template_payload"]):
                valid = None
        except (ValueError, RecursionError):
            valid = None
        if valid is None:
            return "FAILED", "schema_invalid", None
        if valid["status"] in ("failed", "blocked"):
            return "FAILED", "worker_" + valid["status"], None
        if capture.workspace_changed is None:
            return "COLLECTING", "bundle_workspace_evidence_missing", None
        return "COLLECTING", "structured_result_collected", valid

    def collect(self, run_id: str, capture: ApiCapture, *, process_receipt_sha256: str | None = None):
        _check_capture(capture)
        if capture.run_id != run_id:
            raise LedgerError(30, "API capture belongs to a different run")
        manager = self.manager
        directory = manager._directory(run_id)
        with transaction(manager.database) as db:
            run = manager._run(db, run_id)
            if (run["adapter"] != self.id
                    or run["task_sha256"] != capture.task_sha256
                    or manager._task_evidence(run) != "verified"):
                raise LedgerError(30, "API capture does not match the reserved task")
            try:
                invocation = _read(directory / "invocation.json")
                metadata = _json(invocation.decode("utf-8"))
            except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
                raise LedgerError(30, f"API invocation receipt is missing or damaged: {exc}") from exc
            if sha256(invocation).hexdigest() != capture.invocation_sha256:
                raise LedgerError(30, "API capture does not match invocation.json")
            if (metadata.get("run_id"), metadata.get("task_sha256"), metadata.get("request_sha256")) != (
                run_id,
                run["task_sha256"],
                capture.request_sha256,
            ):
                raise LedgerError(30, "API invocation metadata belongs to a different task or request")

            complete_200 = (
                capture.stage == "done"
                and capture.termination == "exited"
                and capture.http_status == 200
                and not capture.response_too_large
            )
            observed = (
                normalize_response(run_id, capture.response_body)
                if complete_200
                else {
                    "conversation_id": None,
                    "response_id": None,
                    "usage": None,
                    "model_observed": "unknown",
                    "finish_reason": None,
                    "reasoning_field_present": False,
                    "envelope": None,
                }
            )
            conversation = observed["conversation_id"]
            if conversation is not None and run["conversation_id"] not in (None, conversation):
                raise LedgerError(30, "API conversation_id conflicts with the stored run record")
            if run["exit_code"] is not None:
                raise LedgerError(30, "API run must never carry a process exit_code")

            receipt_path = directory / "api.capture.json"
            try:
                previous = _read(receipt_path)
            except FileNotFoundError:
                previous = None
            terminal = run["state"] in TERMINAL_STATES
            if terminal and previous is None:
                raise LedgerError(20, "Terminal API run has no original capture")
            if terminal:
                original = _json(previous)
                state, reason, result_hash = (original[k] for k in ("classification", "reason", "result_sha256"))
                result = None
            else:
                provider_kind = str(metadata.get("provider_kind") or "")
                state, reason, result = self._classify(capture, observed, run, provider_kind)
                result_hash = sha256(_bytes(result)).hexdigest() if result is not None else None

            observation = {k: v for k, v in observed.items() if k != "envelope"}
            receipt = {
                "adapter": self.id,
                "run_id": run_id,
                "task_sha256": run["task_sha256"],
                "invocation_sha256": capture.invocation_sha256,
                "process_receipt_sha256": process_receipt_sha256,
                "request_sha256": capture.request_sha256,
                "response_sha256": sha256(capture.response_body).hexdigest(),
                "http_status": capture.http_status,
                "exit_code": None,
                "termination": capture.termination,
                "launch_error": capture.launch_error,
                "error_detail": capture.error_detail,
                "redacted": capture.redacted,
                "wall_milliseconds": capture.wall_milliseconds,
                "workspace_changed": capture.workspace_changed,
                "workspace_after_sha256": capture.workspace_after_sha256,
                "model_requested": run["model_requested"],
                "effort_requested": run["effort_requested"],
                "observation": observation,
                "classification": state,
                "reason": reason,
                "result_sha256": result_hash,
            }
            events = ({"type": "worker_exit", **receipt},)
            journal = b"".join(_bytes(item | {"run_id": run_id}) + b"\n" for item in events)
            receipt["events_sha256"] = sha256(journal).hexdigest()
            serialized = _bytes(receipt)
            if previous is not None and previous != serialized:
                raise LedgerError(30, "API capture differs from the recorded receipt")

            files = {
                "api.response.raw": capture.response_body,
                "events.jsonl": journal,
            }
            if terminal:
                if any(_read(directory / name) != content for name, content in files.items()):
                    raise LedgerError(30, "Stored API evidence differs from the original capture")
                return manager._summary(db, run) | {"api_capture": receipt}
            if run["state"] not in CAPTURE_STATES:
                raise LedgerError(20, "API capture requires a claimed run")
            if run["state"] == "COLLECTING" and previous is None:
                raise LedgerError(30, "An existing collection needs its original API capture")

            _publish(receipt_path, serialized)
            for name, content in files.items():
                _publish(directory / name, content)
            record_observation(db, run_id, observation)
            run = self._apply(db, manager._run(db, run_id), state, reason, result, conversation, receipt)
            return manager._summary(db, run) | {"api_capture": receipt}

    def _apply(self, db, run, state: str, reason: str, result, conversation: str | None, receipt: dict):
        manager = self.manager
        current = run["state"]
        if current in TERMINAL_STATES:
            return run
        evidence = {self.evidence_key: sha256(_bytes(receipt)).hexdigest()}
        if state in ("FAILED_PRE_SUBMIT", "SUBMISSION_UNKNOWN"):
            if current in ("LAUNCHING", "RUNNING"):
                return manager._change(db, run, state, reason=reason, evidence=evidence)
            return run
        if current == "LAUNCHING":
            if conversation is None:
                run = manager._change(db, run, "SUBMISSION_UNKNOWN", reason="missing_launch_receipt")
            else:
                run = manager._change(
                    db, run, "RUNNING", reason=self.conversation_reason, evidence=evidence, conversation_id=conversation
                )
            current = run["state"]
        if state == "FAILED" and current in ("SUBMISSION_UNKNOWN", "TIMED_OUT"):
            return manager._change(db, run, "FAILED", reason=reason, evidence=evidence, conversation_id=conversation)
        if current != "COLLECTING":
            run = manager._change(
                db,
                run,
                "COLLECTING",
                reason=reason,
                evidence=evidence,
                conversation_id=conversation,
                result_sha256=receipt["result_sha256"],
            )
        elif result is not None and run["conversation_id"] is None and conversation is not None:
            db.execute(
                "UPDATE runs SET conversation_id = COALESCE(conversation_id, ?) WHERE run_id = ?",
                (conversation, run["run_id"]),
            )
            event(db, run["run_id"], timestamp(manager.ledger.clock()), current, current, evidence, actor="adapter")
            run = manager._run(db, run["run_id"])
        if state == "FAILED":
            return manager._change(db, run, "FAILED", reason=reason, evidence=evidence)
        if result is not None:
            manager._publish_result(run, result)
        return run
