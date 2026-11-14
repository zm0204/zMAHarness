"""Adapter names, policies, factories and optional capability hooks.

The registry only answers queries; callers keep their own errors and exit codes.
Adapter modules are imported lazily so that importing the registry never loads a
worker implementation or creates an import cycle.
"""
from .base import AdapterPolicy

DEFAULT_POLICY = AdapterPolicy()

ADAPTER_POLICIES = {
    "codex-exec": AdapterPolicy(user_configurable=True),
    "gui-terra": AdapterPolicy(user_configurable=True, gui_submission=True),
    "agy-print": AdapterPolicy(
        user_configurable=True, requires_dispatch_scope=True, attended_approval_only=True,
        replays_structured_output=True, refunds_unstarted_process=True, template_verified=True,
        run_id_prefix="agy",
    ),
    # Internal test double only; not accepted in the local TOML.
    "fake": AdapterPolicy(window_approval=True, template_verified=True),
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


# factory(manager, config) -> an object implementing mah.adapters.base.Adapter
ADAPTER_FACTORIES = {"agy-print": _agy_print_factory}
# check(run_directory, run_summary, result_sha256) -> bool; optional receipt integrity capability.
CAPTURE_CHECKS = {"agy-print": _agy_print_capture_check}
# lookup(config) -> resolved path or None; optional read-only Worker executable check for preflight.
EXECUTABLE_LOOKUPS = {"agy-print": _agy_print_executable}


def register(name, policy, *, factory=None, capture_check=None, executable_lookup=None):
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
    if executable_lookup is not None:
        EXECUTABLE_LOOKUPS[name] = executable_lookup


def unregister(name):
    for table in (ADAPTER_POLICIES, ADAPTER_FACTORIES, CAPTURE_CHECKS, EXECUTABLE_LOOKUPS):
        table.pop(name, None)


def is_registered(name):
    return name in ADAPTER_POLICIES


def policy_for(name):
    """Return the policy, or DEFAULT_POLICY for any unregistered (e.g. historical) name."""
    return ADAPTER_POLICIES.get(name, DEFAULT_POLICY)


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


def executable_lookup(name):
    return EXECUTABLE_LOOKUPS.get(name)
