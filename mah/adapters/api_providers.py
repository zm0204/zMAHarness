"""Provider configuration parsing, loopback validation, 404 matchers, and preflight GET checks for openai-api."""
from dataclasses import asdict, dataclass
import os
import re
from urllib.parse import urlsplit

from ..ledger import IDENTIFIER, TARGET_ID, TEMPLATE_TIMEOUTS, LedgerError, valid_id
from .cli_common import _json

PROVIDER_KINDS_4A = frozenset({"lmstudio", "ollama"})
CLOUD_KINDS_4B = frozenset({"deepseek", "openrouter", "custom"})
PROVIDER_KEYS = frozenset({
    "kind", "base_url", "api_key_env", "timeout_s", "max_output_tokens", "reasoning_effort", "structured_output",
})
FORBIDDEN_SECRET_KEYS = frozenset({
    "api_key", "key", "token", "authorization", "secret", "password", "bearer",
})
API_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
API_KEY_VALUE_RE = re.compile(r"^[\x21-\x7e]+$")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MAX_TEMPLATE_TIMEOUT = max(TEMPLATE_TIMEOUTS.values())


@dataclass(frozen=True)
class ApiProviderConfig:
    name: str
    kind: str
    base_url: str
    scheme: str
    host: str
    port: int
    base_path: str
    api_key_env: str | None = None
    timeout_s: int | None = None
    max_output_tokens: int = 8192
    connect_timeout_s: int = 10
    reasoning_effort: bool = False
    structured_output: str = "json_schema_strict"
    local: bool = True

    def chat_completions_path(self) -> str:
        return f"{self.base_path}/chat/completions" if self.base_path else "/chat/completions"

    def models_path(self) -> str:
        return f"{self.base_path}/models" if self.base_path else "/models"

    def billing(self) -> str:
        return "local" if self.local else "cloud_api"

    def billing_notice(self) -> str | None:
        return None if self.local else f"會使用你的 API 額度計費（{self.name}）"

    def allowed_efforts(self) -> frozenset[str]:
        return frozenset({"low", "medium", "high"}) if self.reasoning_effort else frozenset({"medium"})

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def get(self, key, default=None):
        return getattr(self, key, default)

    def keys(self):
        return asdict(self).keys()


@dataclass(frozen=True)
class ProviderCheck:
    ok: bool
    reasons: tuple[str, ...]
    local: bool
    billing: str
    billing_notice: str | None


@dataclass(frozen=True)
class ProviderChecks:
    reasons: tuple[str, ...]
    by_provider: dict[str, ProviderCheck]


def _config_error(message: str):
    from ..config import ConfigError

    return ConfigError(message)


def parse_base_url(raw_url, *, label: str) -> tuple[str, str, str, int, str]:
    """Validate a 4a-1 loopback base_url and return (normalized_url, scheme, host, port, base_path)."""
    if (type(raw_url) is not str or not raw_url or raw_url != raw_url.strip()
            or any(ch.isspace() or ch in ("\r", "\n", "\0") for ch in raw_url)):
        raise _config_error(f"{label} 必須是不含空白的單行 URL 字串")
    try:
        parts = urlsplit(raw_url)
    except ValueError as exc:
        raise _config_error(f"{label} URL 格式無效") from exc
    if parts.scheme not in ("http", "https"):
        raise _config_error(f"{label} 協定須為 http 或 https")
    if (parts.username is not None or parts.password is not None or "@" in parts.netloc
            or parts.query or parts.fragment or not parts.netloc):
        raise _config_error(f"{label} 不得包含使用者資訊、查詢字串或片段")

    netloc = parts.netloc
    if netloc.startswith("["):
        if "]" not in netloc:
            raise _config_error(f"{label} 的 IPv6 主機格式無效")
        raw_host, _, rest = netloc[1:].partition("]")
        if rest and not rest.startswith(":"):
            raise _config_error(f"{label} 的主機與埠號格式無效")
        port_str = rest[1:] if rest.startswith(":") else None
        if raw_host.lower() != "::1":
            raise _config_error(f"{label} 在 4a 僅允許本機 loopback 字面值（127.0.0.1、[::1]、localhost）")
        host = "::1"
        host_for_url = "[::1]"
    else:
        if ":" in netloc:
            raw_host, _, port_str = netloc.partition(":")
        else:
            raw_host, port_str = netloc, None
        lowered = raw_host.lower()
        if lowered not in ("127.0.0.1", "localhost"):
            raise _config_error(f"{label} 在 4a 僅允許本機 loopback 字面值（127.0.0.1、[::1]、localhost）")
        host = "127.0.0.1"
        host_for_url = "127.0.0.1"

    if port_str is not None:
        if not port_str or not port_str.isascii() or not port_str.isdigit():
            raise _config_error(f"{label} 的埠號格式無效")
        port = int(port_str)
        if not 1 <= port <= 65535:
            raise _config_error(f"{label} 的埠號超出範圍（1..65535）")
    else:
        port = 443 if parts.scheme == "https" else 80

    if parts.path and not parts.path.startswith("/"):
        raise _config_error(f"{label} 的路徑格式無效")
    base_path = parts.path.rstrip("/")
    if "//" in base_path or ".." in base_path.split("/"):
        raise _config_error(f"{label} 的路徑不得包含 // 或 ..")

    normalized = (
        f"{parts.scheme}://{host_for_url}:{port}{base_path}"
        if port_str is not None
        else f"{parts.scheme}://{host_for_url}{base_path}"
    )
    return normalized, parts.scheme, host, port, base_path


def parse_providers(raw_providers, *, api_defaults: dict) -> dict[str, ApiProviderConfig]:
    """Parse and validate [api.providers.*] from harness.local.toml."""
    if raw_providers is None:
        return {}
    if type(raw_providers) is not dict:
        raise _config_error("api.providers 必須是 TOML 表格")
    default_max_tokens = api_defaults.get("max_output_tokens", 8192)
    default_connect_timeout = api_defaults.get("connect_timeout_s", 10)
    if type(default_connect_timeout) is not int or not 1 <= default_connect_timeout <= 60:
        raise _config_error("api.connect_timeout_s 必須介於 1 到 60 秒")
    if type(default_max_tokens) is not int or not 1 <= default_max_tokens <= 65536:
        raise _config_error("api.max_output_tokens 超出允許範圍")

    providers: dict[str, ApiProviderConfig] = {}
    for name, table in raw_providers.items():
        if not isinstance(name, str) or ":" in name or not valid_id(IDENTIFIER, name):
            raise _config_error(f"api.providers 名稱無效：{name}")
        if type(table) is not dict:
            raise _config_error(f"api.providers.{name} 必須是 TOML 表格")
        for key in table:
            if str(key).lower() in FORBIDDEN_SECRET_KEYS:
                raise _config_error(
                    f"api.providers.{name}.{key} 禁止在設定檔直接填寫金鑰；請改用 api_key_env 指定環境變數名稱"
                )
        unknown = set(table) - PROVIDER_KEYS
        if unknown:
            raise _config_error(f"未知的設定欄位 api.providers.{name}.{sorted(unknown)[0]}")

        kind = table.get("kind")
        if type(kind) is not str or not kind or kind != kind.strip():
            raise _config_error(f"api.providers.{name}.kind 必須是非空字串")
        if kind in CLOUD_KINDS_4B:
            raise _config_error(f"api.providers.{name}.kind 為 {kind}，4b 才支援雲端與 custom provider")
        if kind not in PROVIDER_KINDS_4A:
            raise _config_error(f"api.providers.{name}.kind 無效：{kind}（4a 僅支援 lmstudio、ollama，其他類型 4b 才支援）")

        if "structured_output" in table:
            raise _config_error(f"api.providers.{name}.structured_output 只限 4b 的 custom provider 使用")
        structured_output = "json_schema_strict" if kind == "lmstudio" else "json_object"

        if "base_url" not in table:
            raise _config_error(f"api.providers.{name}.base_url 為必填欄位")
        normalized_url, scheme, host, port, base_path = parse_base_url(
            table["base_url"], label=f"api.providers.{name}.base_url"
        )

        api_key_env = table.get("api_key_env")
        if api_key_env is not None:
            if (type(api_key_env) is not str or "sk-" in api_key_env.lower()
                    or "bearer" in api_key_env.lower() or not API_KEY_ENV_RE.match(api_key_env)):
                raise _config_error(
                    f"api.providers.{name}.api_key_env 必須符合 [A-Z][A-Z0-9_]{{2,63}}，不得直接填入 API 金鑰"
                )

        timeout_s = table.get("timeout_s")
        if timeout_s is not None:
            if type(timeout_s) is not int or not 1 <= timeout_s <= MAX_TEMPLATE_TIMEOUT:
                raise _config_error(f"api.providers.{name}.timeout_s 必須是 1 到 {MAX_TEMPLATE_TIMEOUT} 的整數")

        max_output_tokens = table.get("max_output_tokens", default_max_tokens)
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 65536:
            raise _config_error(f"api.providers.{name}.max_output_tokens 超出允許範圍")

        reasoning_effort = table.get("reasoning_effort", False)
        if type(reasoning_effort) is not bool:
            raise _config_error(f"api.providers.{name}.reasoning_effort 必須是布林值")

        providers[name] = ApiProviderConfig(
            name=name,
            kind=kind,
            base_url=normalized_url,
            scheme=scheme,
            host=host,
            port=port,
            base_path=base_path,
            api_key_env=api_key_env,
            timeout_s=timeout_s,
            max_output_tokens=max_output_tokens,
            connect_timeout_s=default_connect_timeout,
            reasoning_effort=reasoning_effort,
            structured_output=structured_output,
            local=True,
        )
    return providers


def split_provider_model(model: str) -> tuple[str, str]:
    """Split '<provider>:<model_id>' on the first colon and validate both sides."""
    if not isinstance(model, str) or ":" not in model or not valid_id(TARGET_ID, model):
        raise LedgerError(2, "Invalid openai-api model target; expected <provider>:<model>")
    provider_name, raw_model = model.split(":", 1)
    if not valid_id(IDENTIFIER, provider_name) or not valid_id(TARGET_ID, raw_model):
        raise LedgerError(2, "Invalid openai-api model target; expected <provider>:<model>")
    return provider_name, raw_model


def check_api_key_env(api_key_env: str | None) -> tuple[bool, str | None]:
    """Validate that api_key_env exists in os.environ and has only visible ASCII header chars.
    Never returns or embeds the key value in the reason string.
    """
    if api_key_env is None:
        return True, None
    raw = os.environ.get(api_key_env)
    if raw is None or not raw.strip():
        return False, f"找不到 API 金鑰環境變數 {api_key_env} 或其值為空"
    if raw != raw.strip() or not API_KEY_VALUE_RE.match(raw):
        return False, f"API 金鑰環境變數 {api_key_env} 格式無效（不得含空白、換行或非可見 ASCII 字元）"
    return True, None


def is_model_not_found_404(kind: str, raw_body: bytes, requested_model: str | None = None) -> bool:
    """Provider-specific 404 body matcher distinguishing 'model not found' from a bad endpoint URL."""
    if kind not in PROVIDER_KINDS_4A or not isinstance(raw_body, (bytes, bytearray)) or not raw_body:
        return False
    try:
        data = _json(bytes(raw_body).decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    if not isinstance(data, dict):
        return False
    error = data.get("error")
    phrases = ("not found", "does not exist", "not loaded", "no model", "unknown model", "failed to load")
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip().lower()
        err_type = str(error.get("type") or "").strip().lower()
        message = str(error.get("message") or "").strip().lower()
        if code == "model_not_found" or err_type == "model_not_found":
            return True
        if "model" in message and any(phrase in message for phrase in phrases):
            return True
    elif isinstance(error, str):
        message = error.strip().lower()
        if "model" in message and any(phrase in message for phrase in phrases):
            return True
    return False


def _probe_single_provider(
    provider: ApiProviderConfig,
    *,
    expected_model: str | None,
    connect_probe: bool,
    connection_factory=None,
) -> ProviderCheck:
    from ..config import ConfigError, sandbox_redirect_detected
    from .api_transport import classify_connect_error, execute_request

    reasons: list[str] = []
    try:
        parse_base_url(provider.base_url, label=f"api.providers.{provider.name}.base_url")
    except ConfigError as exc:
        reasons.append(str(exc))

    key_ok, key_reason = check_api_key_env(provider.api_key_env)
    if not key_ok and key_reason is not None:
        reasons.append(key_reason)

    if connect_probe and not reasons:
        secret = os.environ.get(provider.api_key_env) if provider.api_key_env else None
        res = execute_request(
            provider,
            "GET",
            provider.models_path(),
            body=None,
            secret=secret,
            total_timeout_s=5.0,
            connection_factory=connection_factory,
        )
        if res.stage != "done" or res.http_status is None:
            category = classify_connect_error(res.error_detail)
            if category == "connection_refused":
                server_label = "LM Studio" if provider.kind == "lmstudio" else provider.name
                reasons.append(
                    f"找不到本機模型伺服器（{provider.host}:{provider.port}），請先在 {server_label} 開啟本機伺服器"
                )
            elif category == "permission_denied":
                if not sandbox_redirect_detected():
                    reasons.append(f"連線本機模型伺服器（{provider.host}:{provider.port}）被拒絕（存取權限不足）")
            else:
                reasons.append(f"連線本機模型伺服器（{provider.host}:{provider.port}）逾時或失敗")
        elif res.response_too_large:
            reasons.append(f"provider {provider.name} 的 /models 回應超過大小上限")
        elif res.http_status in (401, 403):
            reasons.append(f"provider {provider.name} 拒絕授權（HTTP {res.http_status}）")
        elif res.http_status != 200:
            reasons.append(f"provider {provider.name} 的 /models 回應錯誤（HTTP {res.http_status}）")
        else:
            try:
                payload = _json(res.body.decode("utf-8-sig"))
                items = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(items, list):
                    raise ValueError("missing data list")
                ids = {
                    item["id"]
                    for item in items
                    if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
                }
            except (ValueError, UnicodeDecodeError, RecursionError):
                reasons.append(f"provider {provider.name} 的 /models 回應不是有效的模型清單 JSON")
            else:
                if expected_model is not None and expected_model not in ids:
                    reasons.append(f"provider {provider.name} 的 /models 清單找不到模型 {expected_model}")

    return ProviderCheck(
        ok=not reasons,
        reasons=tuple(reasons),
        local=provider.local,
        billing=provider.billing(),
        billing_notice=provider.billing_notice(),
    )


def preflight_check_providers(config, *, model=None, providers=None, connection_factory=None) -> ProviderChecks:
    """Provider-granular preflight check registered in PREFLIGHT_CHECKS['openai-api']."""
    configured: dict[str, ApiProviderConfig] = config.values.get("api", {}).get("providers", {}) if config else {}
    if not configured:
        return ProviderChecks(reasons=("尚未設定任何 provider（[api.providers.*]）",), by_provider={})

    by_provider: dict[str, ProviderCheck] = {}
    if model is not None:
        try:
            prov_name, raw_model = split_provider_model(model)
        except LedgerError:
            return ProviderChecks(
                reasons=(f"openai-api 模型名稱格式無效：{model}（須為 <provider>:<model>）",),
                by_provider={},
            )
        provider = configured.get(prov_name)
        if provider is None:
            return ProviderChecks(
                reasons=(f"找不到已設定的 API provider：{prov_name}（[api.providers.{prov_name}]）",),
                by_provider={},
            )
        by_provider[prov_name] = _probe_single_provider(
            provider, expected_model=raw_model, connect_probe=True, connection_factory=connection_factory
        )
        return ProviderChecks(reasons=(), by_provider=by_provider)

    if providers is not None:
        for prov_name in providers:
            provider = configured.get(prov_name)
            if provider is None:
                by_provider[prov_name] = ProviderCheck(
                    ok=False,
                    reasons=(f"找不到已設定的 API provider：{prov_name}（[api.providers.{prov_name}]）",),
                    local=True,
                    billing="local",
                    billing_notice=None,
                )
            else:
                by_provider[prov_name] = _probe_single_provider(
                    provider, expected_model=None, connect_probe=True, connection_factory=connection_factory
                )
        return ProviderChecks(reasons=(), by_provider=by_provider)

    for prov_name, provider in configured.items():
        by_provider[prov_name] = _probe_single_provider(
            provider, expected_model=None, connect_probe=False, connection_factory=connection_factory
        )
    return ProviderChecks(reasons=(), by_provider=by_provider)
