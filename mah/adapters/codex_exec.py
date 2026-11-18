"""Read-only GPT Worker through the official Codex CLI (`codex exec`), Context-Bundle only."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from ..executable_path import which
import subprocess

from ..context_bundle import bundle_manifest
from ..ledger import READ_ONLY_TEMPLATES, LedgerError
from ..run_manager import _bytes
from .cli_common import WORKER_ENV, _count, _json, _response_schema, _text, _uuid
from .cli_worker import STRUCTURED_OUTPUT_CONFLICT, CliWorkerAdapter
from . import codex_probe

CODEX_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
# Required disables even if the installed CLI reports that they default to false.
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot", "computer_use", "browser_use",
    "browser_use_external", "browser_use_full_cdp_access", "in_app_browser", "apps", "plugins", "remote_plugin",
    "hooks", "multi_agent", "multi_agent_v2", "memories", "image_generation", "view_image",
    "skill_mcp_dependency_install", "tool_suggest", "skill_search", "sleep_tool", "goals", "code_mode",
    "code_mode_only", "code_mode_host", "standalone_web_search", "request_permissions_tool",
)
# Fixed `-c key=value` overrides (values are TOML). model_reasoning_effort is added per run.
FIXED_CONFIG = (
    ("approval_policy", '"never"'),
    ("web_search", '"disabled"'),
    ("skills.include_instructions", "false"),
    ("orchestrator.skills.enabled", "false"),
    ("tools.update_plan.enabled", "false"),
    ("tools.experimental_request_user_input.enabled", "false"),
    ("project_doc_max_bytes", "0"),
    ("agents.enabled", "false"),
)
ALLOWED_CONFIG_KEYS = frozenset({"model_reasoning_effort", *(key for key, _ in FIXED_CONFIG)})
FORBIDDEN_WORDS = ("dangerously", "bypass", "approve-for-me")
FORBIDDEN_FLAGS = frozenset({"--add-dir", "--worktree", "--oss", "--local-provider", "-p", "--profile", "-i",
                             "--image", "--thread-source", "--enable", "--approve-for-me", "--trust",
                             "--cyber-access-program"})
FORBIDDEN_SUBCOMMANDS = frozenset({"resume", "fork", "review"})
SCHEMA_FILE = "codex.output-schema.json"
LAST_MESSAGE_FILE = "codex.last-message.json"
CODEX_ENV = frozenset(WORKER_ENV | {"CODEX_HOME"})
NPM_SHIMS = ("codex.cmd", "codex.ps1", "codex")
NPM_NATIVE = tuple(Path("node_modules") / "@openai" / "codex" / "node_modules" / "@openai" / package / "vendor"
                   / triple / "bin" / "codex.exe"
                   for package, triple in (("codex-win32-x64", "x86_64-pc-windows-msvc"),
                                           ("codex-win32-arm64", "aarch64-pc-windows-msvc")))
MISSING_EXECUTABLE = ("找不到 codex 的原生執行檔（.exe）。zMAHarness 不經由 .cmd、.bat、.ps1 轉呼啟動；"
                      "請在本機 TOML 把 codex_command 設成 codex.exe 的絕對路徑。")
WORKTREE_REFUSED = ("codex-exec only supports Context-Bundle: Codex reads files only through its shell or code "
                    "tools, and its read-only sandbox can read the whole disk, so the reading scope cannot be bound "
                    "to a Disposable Worktree. Use Context-Bundle or another Worker.")
TEMPLATE_REFUSED = ("codex-exec only runs read-only analysis (review, second-opinion, research); "
                    "{template} is refused because every Codex tool is disabled and the Worker never writes files.")
# Keywords that strict structured outputs may not accept; the Verifier still enforces the full template rules.
STRICT_UNSUPPORTED = frozenset({"minLength", "maxLength", "pattern", "minimum", "maximum", "minItems", "maxItems",
                                "format"})
JSON_TYPES = ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string"))
# `codex exec --json` item types that are not tool calls. `error` is a CLI notice (the smoke shows
# "Code Mode is unavailable"). Tool items in 0.154.0: command_execution, file_change, mcp_tool_call,
# collab_tool_call, web_search, todo_list.
NON_TOOL_ITEMS = frozenset({"agent_message", "reasoning", "error"})
TOOL_ITEM_OBSERVED = "codex_tool_item_observed"


def strict_output_schema(schema):
    """Derive the --output-schema document from the shared result schema, never a hand-written copy."""
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


def _native_file(path):
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_file() and resolved.suffix.lower() == ".exe" else None


def resolve_codex_executable(command):
    """Return the native codex.exe for codex_command, or None. Never runs or parses a shim."""
    if not command:
        return None
    if Path(command).is_absolute():
        return _native_file(command)
    found = which(str(command))
    if found is None:
        return None
    found = Path(found)
    if found.suffix.lower() == ".exe":
        return _native_file(found)
    if found.name.lower() in NPM_SHIMS:
        # Only the fixed npm package layouts are trusted; the shim's contents are never read.
        return next((native for native in (_native_file(found.parent / layout) for layout in NPM_NATIVE) if native),
                    None)
    return None


def codex_environment():
    """Allow-listed environment: no OPENAI_* and no CODEX_* except CODEX_HOME (where the login lives)."""
    environment = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper not in CODEX_ENV or upper.startswith("OPENAI_"):
            continue
        if upper.startswith("CODEX_") and upper != "CODEX_HOME":
            continue
        environment[key] = value
    return environment


def _flag_name(token):
    return token.split("=", 1)[0]


def _values(options, flag):
    return [options[index + 1] for index, token in enumerate(options[:-1]) if token == flag]


class CodexExecAdapter(CliWorkerAdapter):
    """Read-only `codex exec` Worker gated by the CLI's actual tool declarations."""

    id = "codex-exec"
    prefix = "codex"
    capture_reason = "codex_capture_integrity"
    efforts = CODEX_EFFORTS
    command_setting = "codex_command"
    # The -o file is the structured result; agreement with the JSONL message is checked in _classify.
    check_response_agreement = False
    output_files = (LAST_MESSAGE_FILE,)

    def resolve_executable(self):
        executable = resolve_codex_executable(self.command)
        if executable is None:
            raise LedgerError(14, MISSING_EXECUTABLE)
        return executable

    def flags_for(self, kind):
        return {"--json", "--color", "--sandbox", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--strict-config", "--skip-git-repo-check", "--cd", "--model", "--config", "--disable",
                "--output-schema", "--output-last-message"}

    def worker_environment(self):
        return codex_environment()

    def probe(self, *, cwd=None):
        """Run only --version and `exec --help`; the exec flags are listed only there."""
        try:
            exe = self.resolve_executable()
            reports = [subprocess.run([str(exe), *option], stdin=subprocess.DEVNULL, capture_output=True,
                                      shell=False, cwd=cwd, env=self.worker_environment(), timeout=20)
                       for option in (("--version",), ("exec", "--help"))]
        except (OSError, ValueError, LedgerError, subprocess.TimeoutExpired):
            return {"available": False, "cli_version": None, "flags": set()}
        version = reports[0].stdout.decode("utf-8", errors="replace").strip()
        help_text = b"\n".join((reports[1].stdout, reports[1].stderr)).decode("utf-8", errors="replace")
        return {"available": bool(version) and all(report.returncode == 0 for report in reports),
                "cli_version": version or None, "flags": set(self.flag_pattern.findall(help_text))}

    def prepare_invocation(self, *, run_id, repo_root, template, model, effort, task, workspace,
                           worktree_root, timeout_s, requirements):
        if template not in READ_ONLY_TEMPLATES:
            raise LedgerError(17, TEMPLATE_REFUSED.format(template=template))
        if isinstance(task, bytes) and bundle_manifest(task) is None:
            raise LedgerError(17, WORKTREE_REFUSED)
        return super().prepare_invocation(
            run_id=run_id, repo_root=repo_root, template=template, model=model, effort=effort, task=task,
            workspace=workspace, worktree_root=worktree_root, timeout_s=timeout_s, requirements=requirements)

    def invocation_profile(self, plan):
        executable = Path(plan.argv[0])
        # No version pin: an upgraded CLI is accepted only if the feature and tool probes still pass.
        version = codex_probe.client_version(_json(plan.metadata)["cli_version"])
        environment = self.worker_environment()
        configs = [token for value in _values(plan.argv, "-c") for token in ("-c", value)]
        try:
            codex_probe.verify_no_system_overrides()
            executable_digest = codex_probe.file_sha256(executable)
            disabled, states = codex_probe.feature_profile(executable, configs, DISABLED_FEATURES, environment)
            if "code_mode_host" not in disabled:
                # Without the CLI boolean override, a nested disable_in_process_fallback could survive.
                codex_probe.refuse("code_mode_host must be in the dynamic disable list")
            model_cache, provenance = codex_probe.fresh_subscription_model(
                executable, plan.model, environment, version, plan.cwd)
            codex_probe.verify_subscription(executable, configs, environment, plan.cwd)
            profile_inputs = {
                "disabled_features": list(disabled), "effective_features": states,
                "code_mode_host": states["code_mode_host"][1],
                # CLI --disable passes features.code_mode_host=false (a boolean);
                # the resolver ignores the nested field for the boolean variant.
                # https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/config/mod.rs#L2844-L2875
                "disable_in_process_fallback": "code_mode_host" not in disabled,
            }
            profile = {**profile_inputs, "model_metadata": provenance}
            canonical_argv = self.build_argv(
                "<codex.exe>", model=plan.model, effort=plan.effort, kind=plan.workspace_kind,
                schema=None, directory="<run>", cwd="<workspace>", disabled_features=disabled)
            key = {
                "executable_sha256": executable_digest, "model": plan.model, "effort": plan.effort,
                "profile_sha256": sha256(_bytes([canonical_argv, profile_inputs])).hexdigest(),
                "model_metadata_sha256": provenance["source_cache_sha256"],
                "model_entry_sha256": provenance["model_entry_sha256"],
            }
            argv = self.build_argv(executable, model=plan.model, effort=plan.effort, kind=plan.workspace_kind,
                                   schema=None, directory=plan.stdin_file.parent, cwd=plan.cwd,
                                   disabled_features=disabled)
            schema = self.invocation_files(run_id=plan.run_id, template=plan.template)[SCHEMA_FILE]
            observation = codex_probe.cached_declarations(
                self.manager.database.parent, key,
                lambda: codex_probe.capture_declarations(
                    argv, schema, model_cache, environment, plan.model,
                    profile["code_mode_host"], profile["disable_in_process_fallback"]),
            )
            _, current = codex_probe.subscription_model(plan.model, self.worker_environment(), version)
            if current != provenance or codex_probe.file_sha256(executable) != executable_digest:
                codex_probe.refuse("executable or model metadata changed during preflight")
            return {**profile, "tool_probe": observation}
        except OSError:
            codex_probe.refuse("could not read or seal Codex preflight evidence")

    def invocation_mismatch(self):
        return LedgerError(17, "codex-exec launch evidence differs from the prepared invocation")

    def argv_context(self, *, run_id, directory, cwd, profile=None):
        return {"directory": directory, "cwd": cwd,
                "disabled_features": DISABLED_FEATURES if profile is None else profile["disabled_features"]}

    def invocation_files(self, *, run_id, template):
        schema = strict_output_schema(_response_schema(run_id, template, self.manager.max_summary_chars))
        return {SCHEMA_FILE: _bytes(schema)}

    def build_argv(self, executable, *, model, effort, kind, schema, directory, cwd,
                   disabled_features=DISABLED_FEATURES):
        if kind != "read_only":
            raise LedgerError(17, WORKTREE_REFUSED)
        argv = [str(executable), "exec", "--json", "--color", "never", "-s", "read-only", "--ephemeral",
                "--ignore-user-config", "--ignore-rules", "--strict-config", "--skip-git-repo-check",
                "-C", str(cwd), "-m", model, "-c", f'model_reasoning_effort="{effort}"']
        for key, value in FIXED_CONFIG:
            argv += ["-c", f"{key}={value}"]
        for feature in disabled_features:
            argv += ["--disable", feature]
        argv += ["--output-schema", str(Path(directory) / SCHEMA_FILE),
                 "-o", str(Path(directory) / LAST_MESSAGE_FILE), "-"]
        return tuple(argv)

    def check_readonly(self, plan):
        if plan.workspace_kind != "read_only":
            raise LedgerError(17, WORKTREE_REFUSED)
        options = [str(token) for token in plan.argv[1:]]
        if not options or options[0] != "exec":
            raise LedgerError(17, "codex-exec must run the exec subcommand")
        for token in options:
            if any(word in token.lower() for word in FORBIDDEN_WORDS):
                raise LedgerError(17, f"Forbidden codex argument: {_flag_name(token)[:40]}")
            if token.startswith("-") and _flag_name(token) in FORBIDDEN_FLAGS:
                raise LedgerError(17, f"Forbidden codex flag: {_flag_name(token)}")
            if token in FORBIDDEN_SUBCOMMANDS:
                raise LedgerError(17, f"Forbidden codex subcommand: {token}")
        sandboxes = _values(options, "-s") + _values(options, "--sandbox")
        if sandboxes != ["read-only"] or any(_flag_name(token) == "--sandbox" for token in options):
            raise LedgerError(17, "codex sandbox must be exactly read-only")
        configs = _values(options, "-c") + _values(options, "--config")
        if any(_flag_name(token) == "--config" for token in options) or any(
                value.split("=", 1)[0] not in ALLOWED_CONFIG_KEYS for value in configs):
            raise LedgerError(17, "codex -c only accepts the fixed allow-listed keys")
        metadata = _json(plan.metadata)
        try:
            profile = metadata.get("preflight")
            disabled = DISABLED_FEATURES if profile is None else profile["disabled_features"]
            if (not isinstance(disabled, (list, tuple)) or not set(DISABLED_FEATURES) <= set(disabled)
                    or len(set(disabled)) != len(disabled)
                    or any(re.fullmatch(codex_probe.FEATURE_NAME, name) is None for name in disabled)):
                raise ValueError("invalid disable list")
        except (AttributeError, KeyError, TypeError, ValueError):
            codex_probe.refuse("invocation feature evidence is invalid")
        expected = self.build_argv(plan.argv[0], model=plan.model, effort=plan.effort, kind=plan.workspace_kind,
                                   schema=None, directory=plan.stdin_file.parent, cwd=plan.cwd,
                                   disabled_features=disabled)
        if tuple(plan.argv) != expected:
            raise LedgerError(17, "codex arguments differ from the fixed read-only profile")

    def normalize_stdout(self, raw):
        return self._normalize(raw, None)

    def normalize_capture(self, capture):
        return self._normalize(capture.stdout, capture.outputs.get(LAST_MESSAGE_FILE))

    def _normalize(self, raw, last_message):
        """Read the `--json` JSONL events; the -o file, when present, is the structured result."""
        try:
            lines = [line for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
            events = [_json(line) for line in lines]
        except (ValueError, RecursionError, UnicodeError):
            return None
        if not events or any(not isinstance(item, dict) for item in events):
            return None
        thread = next((item.get("thread_id") for item in events if item.get("type") == "thread.started"), None)
        turns = sum(1 for item in events if item.get("type") == "turn.started")
        failed = any(item.get("type") in ("turn.failed", "error") for item in events)
        completed = any(item.get("type") == "turn.completed" for item in events)
        messages = [item["item"].get("text") for item in events if item.get("type") == "item.completed"
                    and isinstance(item.get("item"), dict) and item["item"].get("type") == "agent_message"]
        usage = next((item.get("usage") for item in reversed(events) if item.get("type") == "turn.completed"), None)
        tokens = {key: value for key, value in usage.items() if _count(value) is not None} if isinstance(
            usage, dict) else {}
        tool_items = sorted({item["item"].get("type") for item in events if isinstance(item.get("item"), dict)
                             and item["item"].get("type") not in (None, "agent_message", "reasoning")})
        observed = {
            "conversation_id": _uuid(thread) or (_text(thread) if isinstance(thread, str) and thread.strip() else None),
            "raw_status": "SUCCESS" if completed and not failed else ("FAILED" if failed else "INCOMPLETE"),
            "response": _text(messages[-1]) if messages else None,
            "num_turns": turns,
            "usage": tokens or None,
            "tool_items": tool_items,
            "model_observed": "unknown",
            "effort_observed": "unknown",
            "rate_limits": None,
        }
        if last_message is not None:
            try:
                observed["structured_output"] = _json(last_message.decode("utf-8-sig"))
            except (ValueError, RecursionError, UnicodeError):
                # Present but not JSON: keep it so the result schema check fails it as schema_invalid.
                observed["structured_output"] = last_message.decode("utf-8", errors="replace")
        return observed

    def _classify(self, capture, observed, run, tool_reason):
        state, reason, result = super()._classify(capture, observed, run, tool_reason)
        # Every Codex tool is disabled, so any other item type (including a future one) means the
        # read-only profile did not hold and the result cannot be accepted.
        if state == "COLLECTING" and observed and any(kind not in NON_TOOL_ITEMS for kind in observed["tool_items"]):
            return "FAILED", TOOL_ITEM_OBSERVED, None
        if result is not None:
            # The last JSONL agent message must carry the same result as the -o file.
            try:
                agrees = observed["response"] is not None and _json(observed["response"]) == result
            except (ValueError, RecursionError):
                agrees = False
            if not agrees:
                return "FAILED", STRUCTURED_OUTPUT_CONFLICT, None
        return state, reason, result
