"""Adapter names, policies, factories and optional capability hooks.

The registry only answers queries; callers keep their own errors and exit codes.
Adapter modules are imported lazily so that importing the registry never loads a
worker implementation or creates an import cycle.
"""
import re

from .base import AdapterPolicy

DEFAULT_POLICY = AdapterPolicy()

# agy lists Orchestrator models that start their own subagents. Whether the --settings deny rules
# also bind those subagents has not been verified, so the read-only guarantee cannot be claimed for them.
# No trailing hyphen: agy 1.5.14 added the bare ID "gemini-pro-orchestrator" (Orchestrator (Carbon)).
AGY_ORCHESTRATOR_PREFIX = "gemini-pro-orchestrator"
AGY_ORCHESTRATOR_REASON = ("agy-print 不接受 Orchestrator 模型 {model}：Orchestrator 會自行派出子代理，"
                           "--settings 的 deny 規則能否約束這些子代理尚未驗證，無法保證唯讀。"
                           "請改用一般 Gemini 模型（例如 gemini-4-neon-high）。")

ADAPTER_POLICIES = {
    "codex-exec": AdapterPolicy(
        user_configurable=True, requires_dispatch_scope=True, attended_approval_only=True,
        template_verified=True, run_id_prefix="codex",
    ),
    "gui-terra": AdapterPolicy(user_configurable=True, gui_submission=True),
    "agy-print": AdapterPolicy(
        user_configurable=True, requires_dispatch_scope=True, attended_approval_only=True,
        replays_structured_output=True, refunds_unstarted_process=True, template_verified=True,
        run_id_prefix="agy",
    ),
    "claude-print": AdapterPolicy(
        user_configurable=True, requires_dispatch_scope=True, attended_approval_only=True,
        template_verified=True, run_id_prefix="claude",
    ),
    "openai-api": AdapterPolicy(
        user_configurable=True, requires_dispatch_scope=True, attended_approval_only=True,
        refunds_unstarted_process=True, closes_grant_on_refund=True, template_verified=True,
        run_id_prefix="api",
    ),
    # Internal test double only; not accepted in the local TOML.
    "fake": AdapterPolicy(window_approval=True, template_verified=True, allows_delegate_implement=True),
}


def _agy_print_factory(manager, config):
    from .agy_print import AgyPrintAdapter

    return AgyPrintAdapter.from_config(manager, config)


def _agy_print_capture_check(directory, run, result_sha256):
    from .agy_print import capture_matches

    return capture_matches(directory, run, result_sha256)


def _agy_print_executable(config):
    from .agy_print import AgyPrintAdapter

    return AgyPrintAdapter.from_config(None, config).find_executable()


def _claude_print_factory(manager, config):
    from .claude_print import ClaudePrintAdapter

    return ClaudePrintAdapter.from_config(manager, config)


def _claude_print_capture_check(directory, run, result_sha256):
    from .claude_print import ClaudePrintAdapter

    return ClaudePrintAdapter.capture_matches(directory, run, result_sha256)


def _claude_print_executable(config):
    from .claude_print import ClaudePrintAdapter

    return ClaudePrintAdapter.from_config(None, config).find_executable()


def _codex_exec_factory(manager, config):
    from .codex_exec import CodexExecAdapter

    return CodexExecAdapter.from_config(manager, config)


def _codex_exec_capture_check(directory, run, result_sha256):
    from .codex_exec import CodexExecAdapter

    return CodexExecAdapter.capture_matches(directory, run, result_sha256)


def _codex_exec_executable(config):
    from .codex_exec import CodexExecAdapter

    return CodexExecAdapter.from_config(None, config).find_executable()


def _agy_print_model_lister(config, executable):
    from ..model_detect import list_agy_models

    return list_agy_models(config, executable)


def _codex_exec_model_lister(config, executable):
    from ..model_detect import list_codex_models

    return list_codex_models(config, executable)


def _openai_api_factory(manager, config):
    from .openai_api import OpenAIApiAdapter

    return OpenAIApiAdapter.from_config(manager, config)


def _openai_api_capture_check(directory, run, result_sha256):
    from .openai_api import capture_matches

    return capture_matches(directory, run, result_sha256)


def _openai_api_preflight_check(config, *, model=None, providers=None):
    from .api_providers import preflight_check_providers

    return preflight_check_providers(config, model=model, providers=providers)


# factory(manager, config) -> an object implementing mah.adapters.base.Adapter
ADAPTER_FACTORIES = {"agy-print": _agy_print_factory, "claude-print": _claude_print_factory,
                     "codex-exec": _codex_exec_factory, "openai-api": _openai_api_factory}
# check(run_directory, run_summary, result_sha256) -> bool; optional receipt integrity capability.
CAPTURE_CHECKS = {"agy-print": _agy_print_capture_check, "claude-print": _claude_print_capture_check,
                  "codex-exec": _codex_exec_capture_check, "openai-api": _openai_api_capture_check}
# The Verifier reason when a registered check fails; each Adapter names its own.
CAPTURE_REASONS = {"agy-print": "agy_capture_integrity", "claude-print": "claude_capture_integrity",
                   "codex-exec": "codex_capture_integrity", "openai-api": "api_capture_integrity"}
DEFAULT_CAPTURE_REASON = "capture_integrity"
# (model ID prefixes compared case-insensitively, reason with {adapter} and {model}); optional model refusal
# applied by card creation, preflight --model and the Adapter's own prepare_invocation.
REFUSED_MODELS = {"agy-print": ((AGY_ORCHESTRATOR_PREFIX,), AGY_ORCHESTRATOR_REASON)}
DEFAULT_REFUSED_REASON = "{adapter} 不接受模型 {model}"
# Only these two placeholders are filled; any other brace in a reason is kept as written.
REASON_PLACEHOLDER = re.compile(r"\{(adapter|model)\}")
# lookup(config) -> resolved path or None; optional read-only Worker executable check for preflight.
EXECUTABLE_LOOKUPS = {"agy-print": _agy_print_executable, "claude-print": _claude_print_executable,
                      "codex-exec": _codex_exec_executable}
# lister(config, executable) -> ListResult(status, ids, efforts, detail); optional account model listing.
# claude-print uses server-side aliases and intentionally has no lister.
MODEL_LISTERS = {"agy-print": _agy_print_model_lister, "codex-exec": _codex_exec_model_lister}
# check(config, *, model=None, providers=None) -> ProviderChecks; optional provider-granular preflight check.
PREFLIGHT_CHECKS = {"openai-api": _openai_api_preflight_check}
# Supported workspace modes per Adapter (safety property owned by code, not the catalog).
DEFAULT_WORKSPACE_SUPPORT = frozenset({"bundle", "worktree"})
WORKSPACE_SUPPORT = {"codex-exec": frozenset({"bundle"}), "openai-api": frozenset({"bundle"})}
# The preflight reason when the lookup finds nothing; a generic sentence is used otherwise.
EXECUTABLE_MISSING_REASONS = {
    "claude-print": ("找不到 claude 的原生執行檔（.exe）。zMAHarness 不經由 .cmd、.bat、.ps1 轉呼啟動；"
                     "請在本機 TOML 把 claude_command 設成 claude.exe 的絕對路徑。"),
    "codex-exec": ("找不到 codex 的原生執行檔（.exe）。zMAHarness 不經由 .cmd、.bat、.ps1 轉呼啟動；"
                   "請在本機 TOML 把 codex_command 設成 codex.exe 的絕對路徑。"),
}


def _check_refused_models(value):
    """A (prefixes, reason) pair: prefixes a nonempty tuple of nonempty strings, reason a string or None.

    A bare string as prefixes would be matched one character at a time and an empty prefix matches every
    model, so both are refused here instead of silently blocking ordinary models later.
    """
    if not isinstance(value, tuple) or len(value) != 2:
        raise TypeError("refused_models must be a (prefixes, reason) tuple")
    prefixes, reason = value
    if not isinstance(prefixes, tuple) or not prefixes:
        raise TypeError("refused_models prefixes must be a nonempty tuple of strings, not a single string")
    if any(not isinstance(prefix, str) for prefix in prefixes):
        raise TypeError("refused_models prefixes must all be strings")
    if any(not prefix.strip() for prefix in prefixes):
        raise ValueError("refused_models prefixes must not be empty strings")
    if reason is not None and not isinstance(reason, str):
        raise TypeError("refused_models reason must be a string or None")


def register(name, policy, *, factory=None, capture_check=None, capture_reason=None, executable_lookup=None,
             executable_missing_reason=None, refused_models=None, model_lister=None, workspace_support=None,
             preflight_check=None):
    """Register an Adapter without editing core modules. Names must be new."""
    if name in ADAPTER_POLICIES:
        raise ValueError(f"Adapter is already registered: {name}")
    if not isinstance(policy, AdapterPolicy):
        raise TypeError("policy must be an AdapterPolicy")
    # Checked before any table changes, so a malformed entry leaves nothing half-registered.
    if refused_models is not None:
        _check_refused_models(refused_models)
    if workspace_support is not None:
        if not isinstance(workspace_support, (set, frozenset)) or not workspace_support <= DEFAULT_WORKSPACE_SUPPORT:
            raise ValueError("workspace_support must be a subset of {'bundle', 'worktree'}")
    ADAPTER_POLICIES[name] = policy
    if factory is not None:
        ADAPTER_FACTORIES[name] = factory
    if capture_check is not None:
        CAPTURE_CHECKS[name] = capture_check
    if capture_reason is not None:
        CAPTURE_REASONS[name] = capture_reason
    if executable_lookup is not None:
        EXECUTABLE_LOOKUPS[name] = executable_lookup
    if executable_missing_reason is not None:
        EXECUTABLE_MISSING_REASONS[name] = executable_missing_reason
    if refused_models is not None:
        REFUSED_MODELS[name] = refused_models
    if model_lister is not None:
        MODEL_LISTERS[name] = model_lister
    if workspace_support is not None:
        WORKSPACE_SUPPORT[name] = frozenset(workspace_support)
    if preflight_check is not None:
        PREFLIGHT_CHECKS[name] = preflight_check


def unregister(name):
    for table in (ADAPTER_POLICIES, ADAPTER_FACTORIES, CAPTURE_CHECKS, CAPTURE_REASONS, EXECUTABLE_LOOKUPS,
                  EXECUTABLE_MISSING_REASONS, REFUSED_MODELS, MODEL_LISTERS, WORKSPACE_SUPPORT, PREFLIGHT_CHECKS):
        table.pop(name, None)


def is_registered(name):
    return name in ADAPTER_POLICIES


def policy_for(name):
    """Return the policy, or DEFAULT_POLICY for any unregistered (e.g. historical) name."""
    return ADAPTER_POLICIES.get(name, DEFAULT_POLICY)


def refused_model_reason(name, model):
    """The user-facing reason when the Adapter refuses this model ID, otherwise None."""
    prefixes, reason = REFUSED_MODELS.get(name, ((), None))
    if isinstance(model, str) and any(model.lower().startswith(prefix.lower()) for prefix in prefixes):
        # One pass: braces in the reason or in a substituted value are never parsed, so this cannot raise.
        values = {"adapter": str(name), "model": model}
        return REASON_PLACEHOLDER.sub(lambda match: values[match.group(1)], reason or DEFAULT_REFUSED_REASON)
    return None


def configurable_names():
    return tuple(name for name, policy in ADAPTER_POLICIES.items() if policy.user_configurable)


def dispatchable_names():
    return tuple(ADAPTER_FACTORIES)


def has_factory(name):
    return name in ADAPTER_FACTORIES


def create_adapter(name, manager, config):
    return ADAPTER_FACTORIES[name](manager, config)


def capture_check(name):
    return CAPTURE_CHECKS.get(name)


def capture_reason(name):
    return CAPTURE_REASONS.get(name, DEFAULT_CAPTURE_REASON)


def executable_lookup(name):
    return EXECUTABLE_LOOKUPS.get(name)


def executable_missing_reason(name):
    return EXECUTABLE_MISSING_REASONS.get(name)


def model_lister(name):
    return MODEL_LISTERS.get(name)


def workspace_support(name):
    return WORKSPACE_SUPPORT.get(name, DEFAULT_WORKSPACE_SUPPORT)


def preflight_check(name):
    return PREFLIGHT_CHECKS.get(name)
