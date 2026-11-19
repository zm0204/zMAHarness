"""Adapter names, policies, factories and optional capability hooks.

The registry only answers queries; callers keep their own errors and exit codes.
Adapter modules are imported lazily so that importing the registry never loads a
worker implementation or creates an import cycle.
"""
from .base import AdapterPolicy

DEFAULT_POLICY = AdapterPolicy()

# agy 1.5.12 lists Orchestrator models that start their own subagents. Whether the --settings deny rules
# also bind those subagents has not been verified, so the read-only guarantee cannot be claimed for them.
AGY_ORCHESTRATOR_PREFIX = "gemini-pro-orchestrator-"
AGY_ORCHESTRATOR_REASON = ("agy-print 不接受 Orchestrator 模型 {model}：Orchestrator 會自行派出子代理，"
                           "--settings 的 deny 規則能否約束這些子代理尚未驗證，無法保證唯讀。"
                           "請改用一般 Gemini 模型（例如 gemini-4-argon-high）。")

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


# factory(manager, config) -> an object implementing mah.adapters.base.Adapter
ADAPTER_FACTORIES = {"agy-print": _agy_print_factory, "claude-print": _claude_print_factory,
                     "codex-exec": _codex_exec_factory}
# check(run_directory, run_summary, result_sha256) -> bool; optional receipt integrity capability.
CAPTURE_CHECKS = {"agy-print": _agy_print_capture_check, "claude-print": _claude_print_capture_check,
                  "codex-exec": _codex_exec_capture_check}
# The Verifier reason when a registered check fails; each Adapter names its own.
CAPTURE_REASONS = {"agy-print": "agy_capture_integrity", "claude-print": "claude_capture_integrity",
                   "codex-exec": "codex_capture_integrity"}
DEFAULT_CAPTURE_REASON = "capture_integrity"
# (model ID prefixes compared case-insensitively, reason with {adapter} and {model}); optional model refusal
# applied by card creation, preflight --model and the Adapter's own prepare_invocation.
REFUSED_MODELS = {"agy-print": ((AGY_ORCHESTRATOR_PREFIX,), AGY_ORCHESTRATOR_REASON)}
# lookup(config) -> resolved path or None; optional read-only Worker executable check for preflight.
EXECUTABLE_LOOKUPS = {"agy-print": _agy_print_executable, "claude-print": _claude_print_executable,
                      "codex-exec": _codex_exec_executable}
# The preflight reason when the lookup finds nothing; a generic sentence is used otherwise.
EXECUTABLE_MISSING_REASONS = {
    "claude-print": ("找不到 claude 的原生執行檔（.exe）。zMAHarness 不經由 .cmd、.bat、.ps1 轉呼啟動；"
                     "請在本機 TOML 把 claude_command 設成 claude.exe 的絕對路徑。"),
    "codex-exec": ("找不到 codex 的原生執行檔（.exe）。zMAHarness 不經由 .cmd、.bat、.ps1 轉呼啟動；"
                   "請在本機 TOML 把 codex_command 設成 codex.exe 的絕對路徑。"),
}


def register(name, policy, *, factory=None, capture_check=None, capture_reason=None, executable_lookup=None,
             executable_missing_reason=None, refused_models=None):
    """Register an Adapter without editing core modules. Names must be new."""
    if name in ADAPTER_POLICIES:
        raise ValueError(f"Adapter is already registered: {name}")
    if not isinstance(policy, AdapterPolicy):
        raise TypeError("policy must be an AdapterPolicy")
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


def unregister(name):
    for table in (ADAPTER_POLICIES, ADAPTER_FACTORIES, CAPTURE_CHECKS, CAPTURE_REASONS, EXECUTABLE_LOOKUPS,
                  EXECUTABLE_MISSING_REASONS, REFUSED_MODELS):
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
        return (reason or "{adapter} 不接受模型 {model}").format(adapter=name, model=model)
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
