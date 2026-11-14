"""Read-only Claude Worker through the official Claude Code CLI in print mode (`claude -p`)."""
import os
from pathlib import Path
from shutil import which

from ..ledger import LedgerError
from ..run_manager import _bytes
from .agy_print import WORKER_ENV, _count, _json, _response_schema, _text, _uuid
from .cli_worker import CliWorkerAdapter

CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
COMMON_FLAGS = frozenset({"--print", "--output-format", "--json-schema", "--model", "--effort", "--restricted",
                          "--strict-mcp-config", "--permission-prompts", "--no-session-persistence", "--tools"})
WORKTREE_TOOLS = ("Read", "Grep", "Glob")
# Flags that widen access, change authentication, resume or persist state, or load outside configuration.
FORBIDDEN_FLAGS = frozenset({
    "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--bare", "--add-dir",
    "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools", "--mcp-config", "--settings",
    "--setting-sources", "--plugin-dir", "--plugin-url", "--agents", "--agent", "--chrome", "--ide", "--file",
    "--cloud", "--bg", "--background", "--worktree", "-w", "--resume", "-r", "--continue", "-c", "--fork-session",
    "--session-id", "--teleport", "--from-pr", "--remote-control", "--environment", "--system-prompt",
    "--append-system-prompt", "--debug-file", "--betas", "--fallback-model", "--input-format", "--safe-mode",
})
FORBIDDEN_WORDS = ("dangerously", "bypass", "skip-permissions")
FORBIDDEN_PERMISSION_MODES = frozenset({"bypassPermissions", "acceptEdits", "auto", "dontAsk", "manual"})
# Any tool that can write, run commands or reach the network; only Read, Grep and Glob are ever allowed.
FORBIDDEN_TOOLS = frozenset({"Bash", "PowerShell", "Edit", "MultiEdit", "Write", "NotebookEdit", "WebFetch",
                             "WebSearch", "Task", "Agent", "KillShell", "BashOutput", "SlashCommand", "Skill"})
CLAUDE_ENV = frozenset(WORKER_ENV | {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_GIT_BASH_PATH"})
NPM_SHIMS = ("claude.cmd", "claude.ps1", "claude")
NPM_NATIVE = Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
MISSING_EXECUTABLE = ("找不到 claude 的原生執行檔（.exe）。zMAHarness 不經由 .cmd、.bat、.ps1 轉呼啟動；"
                      "請在本機 TOML 把 claude_command 設成 claude.exe 的絕對路徑。")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _native_file(path):
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_file() and resolved.suffix.lower() == ".exe" else None


def resolve_claude_executable(command):
    """Return the native claude.exe for claude_command, or None. Never runs or parses a shim."""
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
        # Only the fixed npm package layout is trusted; the shim's contents are never read.
        return _native_file(found.parent / NPM_NATIVE)
    return None


def claude_environment():
    """Allow-listed environment without any ANTHROPIC_* variable, so only the subscription login is used."""
    return {key: value for key, value in os.environ.items()
            if key.upper() in CLAUDE_ENV and not key.upper().startswith("ANTHROPIC_")}


def _flag_name(token):
    return token.split("=", 1)[0]


def _single_value(options, flag):
    positions = [index for index, token in enumerate(options) if _flag_name(token) == flag]
    if len(positions) != 1 or "=" in options[positions[0]] or positions[0] + 1 >= len(options):
        raise LedgerError(17, f"claude requires exactly one {flag} value")
    return options[positions[0] + 1]


class ClaudePrintAdapter(CliWorkerAdapter):
    """Read-only `claude -p` Worker. Context-Bundle has no tools; Disposable Worktree reads only."""

    id = "claude-print"
    prefix = "claude"
    capture_reason = "claude_capture_integrity"
    efforts = CLAUDE_EFFORTS

    @classmethod
    def from_config(cls, manager, config):
        command = config.values["adapters"]["claude_command"]
        # A single executable path relative to the local TOML, or a command on PATH.
        if Path(command).is_absolute() or "/" in command or chr(92) in command:
            command = str((config.path.parent / Path(command)).resolve())
        return cls(manager, command=command, max_task_bytes=config.values["limits"]["max_task_bytes"])

    def resolve_executable(self):
        executable = resolve_claude_executable(self.command)
        if executable is None:
            raise LedgerError(14, MISSING_EXECUTABLE)
        return executable

    def flags_for(self, kind):
        return COMMON_FLAGS | ({"--permission-mode"} if kind == "disposable_worktree" else set())

    def worker_environment(self):
        return claude_environment()

    def build_argv(self, executable, *, model, effort, kind, schema):
        argv = [str(executable), "-p", "--output-format", "json", "--json-schema", _bytes(schema).decode(),
                "--model", model, "--effort", effort, "--restricted", "--strict-mcp-config",
                "--permission-prompts", "none", "--no-session-persistence"]
        if kind == "disposable_worktree":
            argv += ["--permission-mode", "plan", "--tools", ",".join(WORKTREE_TOOLS)]
        else:
            argv += ["--tools", ""]
        return tuple(argv)

    def check_readonly(self, plan):
        if plan.workspace_kind not in ("read_only", "disposable_worktree"):
            raise LedgerError(17, "claude-print only launches read-only workspaces")
        options = [str(token) for token in plan.argv[1:]]
        for token in options:
            # Values too: a forbidden word anywhere in the vector (for example a permission mode) is refused.
            if any(word in token.lower() for word in FORBIDDEN_WORDS):
                raise LedgerError(17, f"Forbidden claude argument: {_flag_name(token)[:40]}")
            if token.startswith("-") and _flag_name(token) in FORBIDDEN_FLAGS:
                raise LedgerError(17, f"Forbidden claude flag: {_flag_name(token)}")
        names = [_flag_name(token) for token in options if token.startswith("-")]
        if names.count("-p") != 1 or "--print" in names:
            raise LedgerError(17, "claude requires exactly one -p")
        for flag in self.flags_for(plan.workspace_kind) - {"--print"}:
            if names.count(flag) != 1:
                raise LedgerError(17, f"claude read-only launch requires {flag}")
        if "--permission-mode" in names:
            mode = _single_value(options, "--permission-mode")
            if plan.workspace_kind != "disposable_worktree" or mode != "plan":
                raise LedgerError(17, f"Forbidden claude permission mode: {mode}")
        tools = [name for name in _single_value(options, "--tools").replace(",", " ").split() if name]
        if any(name.split("(", 1)[0] in FORBIDDEN_TOOLS or name not in WORKTREE_TOOLS for name in tools):
            raise LedgerError(17, "claude tools must be limited to Read, Grep and Glob")
        expected_tools = list(WORKTREE_TOOLS) if plan.workspace_kind == "disposable_worktree" else []
        if tools != expected_tools:
            raise LedgerError(17, "claude tools differ from the fixed read-only profile")
        if (_single_value(options, "--output-format") != "json"
                or _single_value(options, "--permission-prompts") != "none"):
            raise LedgerError(17, "claude requires --output-format json and --permission-prompts none")
        schema = _response_schema(plan.run_id, plan.template, self.manager.max_summary_chars)
        expected = self.build_argv(plan.argv[0], model=plan.model, effort=plan.effort, kind=plan.workspace_kind,
                                   schema=schema)
        if tuple(plan.argv) != expected:
            raise LedgerError(17, "claude arguments differ from the fixed read-only profile")

    def normalize_stdout(self, raw):
        """Read the `--output-format json` result envelope. Raw bytes remain the authoritative record."""
        try:
            data = _json(raw.decode("utf-8-sig"))
        except (ValueError, RecursionError, UnicodeError):
            return None
        if not isinstance(data, dict):
            return None
        usage = data.get("usage")
        tokens = {}
        if isinstance(usage, dict):
            tokens = {key: usage[key] for key in TOKEN_FIELDS if _count(usage.get(key)) is not None}
        models = data.get("modelUsage")
        model_observed = "unknown"
        if isinstance(models, dict) and len(models) == 1 and _text(next(iter(models))):
            model_observed = next(iter(models))
        subtype = _text(data.get("subtype"))
        if data.get("type") == "result" and subtype == "success" and data.get("is_error") is False:
            raw_status = "SUCCESS"
        else:
            raw_status = subtype or ("ERROR" if data.get("type") == "result" else None)
        optional = {"structured_output": data["structured_output"]} if "structured_output" in data else {}
        response = _text(data.get("result"))
        if "result" in data and response is None:
            optional["response_invalid"] = True
        denials = data.get("permission_denials")
        return {
            "conversation_id": _uuid(data.get("session_id")),
            "raw_status": raw_status,
            "response": response,
            "num_turns": _count(data.get("num_turns")),
            "duration_ms": _count(data.get("duration_ms")),
            "usage": tokens or None,
            "permission_denials": len(denials) if isinstance(denials, list) else None,
            "model_observed": model_observed,
            "effort_observed": "unknown",
            "rate_limits": None,
        } | optional
