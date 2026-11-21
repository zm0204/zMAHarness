"""Checks that run before a collaboration card is offered and before a dispatch reserves a Grant."""
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from tempfile import mkstemp

from typing import Any

from .adapters.registry import executable_lookup, executable_missing_reason, preflight_check, refused_model_reason
from .config import HOME_VARIABLE, Config, ConfigError, adapter_enabled, default_config_path, load_config, \
    sandbox_redirect_detected
from .executable_path import EXECUTABLE_INSIDE_PROJECT, inside_roots
from .ledger import TARGET_ID, LedgerError, valid_id
from .login_status import codex_login_reasons, login_reasons
from .model_detect import query_cwd

INVALID_MODEL_ID = ("模型 ID {model!r} 格式不符：須以英文字母或數字開頭，只能包含英文字母、數字與 _ . : / -，"
                    "長度 1～128 字元。card create 會以結束碼 2 拒絕這個 ID。")

UNSANDBOXED_DISPATCH_REQUIRED = (
    "派工必須在沙盒外執行：目前環境無法寫入 zMAHarness 狀態目錄或啟動 Worker。"
    "請依主控平台的機制核准一次沙盒外執行後重試。"
)


NOT_A_GIT_REPOSITORY = "目標不是本機 Git 專案；雲端硬碟、網頁等資料不在協作範圍內。"
GIT_NOT_INSTALLED = "找不到 git 執行檔，請安裝 Git。"
OUTSIDE_WORKSPACE_HINT = "如果目標不在主控目前的工作資料夾內，請在該專案開啟主控後再試。"


def _git_failed(detail):
    return f"無法檢查目標的 Git 專案：{detail}。{OUTSIDE_WORKSPACE_HINT}"


def git_toplevel(path):
    """Return (root, None) from `git rev-parse --show-toplevel`, or (None, reason).

    A cause is named only when it is certain; anything else is reported as a Git failure with
    the original error, so a sandbox denial is never mistaken for "not a repository".
    """
    try:
        # English messages in every locale, so "not a git repository" can be matched reliably.
        environment = os.environ | {"LC_ALL": "C", "LANGUAGE": "C"}
        result = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                                env=environment)
    except FileNotFoundError:
        return None, GIT_NOT_INSTALLED
    except PermissionError as exc:
        return None, f"無法啟動 git：{exc}。可能是被主控的沙盒限制；{OUTSIDE_WORKSPACE_HINT}"
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return None, _git_failed(f"{type(exc).__name__}: {exc}")
    top = result.stdout.strip()
    if result.returncode == 0 and top:
        return top, None
    if result.returncode != 0 and "not a git repository" in result.stderr.lower():
        return None, NOT_A_GIT_REPOSITORY
    first_line = next((line.strip() for line in result.stderr.splitlines() if line.strip()), None)
    return None, _git_failed(first_line or f"git 結束碼 {result.returncode}，沒有錯誤訊息")


@dataclass(frozen=True)
class CommonCheckReport:
    repo_root: str | None
    config_path: Path
    config_found: bool
    config: Config | None
    sandbox_redirect_detected: bool
    needs_unsandboxed_dispatch: bool
    reasons: tuple[str, ...]

    @property
    def ok(self):
        return not self.reasons


@dataclass(frozen=True)
class AdapterCheckReport:
    adapter: str
    enabled: bool
    executable: Path | None
    executable_found: bool | None
    reasons: tuple[str, ...]
    provider_checks: Any = None

    @property
    def ok(self):
        return not self.reasons


def common_checks(repo, config_path=None):
    """Read-only checks shared by preflight and route: Git project, local config, and sandbox detection."""
    reasons = []
    repo_root, git_reason = git_toplevel(repo)
    if git_reason is not None:
        reasons.append(git_reason)

    # A missing default file still loads the built-in defaults, so the remaining checks stay informative.
    path = Path(config_path).expanduser().resolve() if config_path is not None else default_config_path()
    config_found = path.is_file()
    config = None
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        if config_found:
            reasons.append(f"本機設定檔無法載入：{exc}")
    if not config_found:
        if config_path is None and os.environ.get(HOME_VARIABLE) is not None:
            reasons.append(f"ZMAHARNESS_HOME 指定的位置沒有設定檔：{path}")
        else:
            reasons.append(f"找不到本機設定檔 {path}，請先執行 setup.ps1。")

    redirected = sandbox_redirect_detected()
    return CommonCheckReport(
        repo_root=repo_root,
        config_path=path,
        config_found=config_found,
        config=config,
        sandbox_redirect_detected=redirected,
        # Advisory: only the Codex AppContainer redirect is recognized; dispatch also probes for writes.
        needs_unsandboxed_dispatch=redirected,
        reasons=tuple(reasons),
    )


def adapter_checks(config, adapter, repo_root=None, *, quota_state=None, check_codex_quota=True,
                   model=None, providers=None):
    """Read-only per-adapter checks: enabled, executable lookup & location, subscription login, and codex quota."""
    reasons = []
    enabled = adapter_enabled(config, adapter) if config is not None else False
    if config is not None and not enabled:
        reasons.append(f"{adapter} 未在本機設定啟用（adapters.enabled）")

    # None: not applicable (no executable lookup registered) or unknown (settings did not load).
    lookup = executable_lookup(adapter)
    executable = None if lookup is None or config is None else lookup(config)
    executable_found = None if lookup is None or config is None else executable is not None
    if executable_found is False:
        reasons.append(executable_missing_reason(adapter) or f"找不到 {adapter} 的 Worker 執行檔，請安裝或檢查本機設定。")
    elif executable is not None:
        roots = (repo_root, config.values["paths"]["worktree_root"])
        if inside_roots(executable, roots):
            reasons.append(EXECUTABLE_INSIDE_PROJECT.format(path=Path(executable).resolve()))
        elif adapter == "codex-exec" and not check_codex_quota:
            reasons.extend(codex_login_reasons(executable))
        else:
            reasons.extend(login_reasons(adapter, executable, cwd=query_cwd(config), quota_state=quota_state))

    provider_checks = None
    pcheck = preflight_check(adapter)
    if pcheck is not None and config is not None and enabled:
        provider_checks = pcheck(config, model=model, providers=providers)
        reasons.extend(provider_checks.reasons)

    return AdapterCheckReport(
        adapter=adapter,
        enabled=enabled,
        executable=executable,
        executable_found=executable_found,
        reasons=tuple(reasons),
        provider_checks=provider_checks,
    )


def preflight(repo, adapter, config_path=None, model=None):
    """No ledger and no writes under zMAHarness or the project. Returns the report.

    For claude-print and codex-exec it runs the CLI's own login status command (no inference request);
    that CLI may refresh files in its own home folder. For codex-exec it also reads the ChatGPT quota
    (read-only, no inference) and blocks only when the quota is confirmed exhausted.
    With a model ID it also reports an ID card creation would reject (malformed or refused by the Adapter),
    so the card is never proposed.
    """
    reasons = []
    valid_model = False
    if model is not None:
        # The same full match card creation applies, so a padded or malformed ID is never reported ok.
        if not valid_id(TARGET_ID, model):
            reasons.append(INVALID_MODEL_ID.format(model=model))
        elif (refused := refused_model_reason(adapter, model)) is not None:
            reasons.append(refused)
        else:
            valid_model = True

    common = common_checks(repo, config_path)
    reasons.extend(common.reasons)
    checked = adapter_checks(
        common.config,
        adapter,
        common.repo_root,
        model=model if valid_model else None,
    )
    reasons.extend(checked.reasons)
    billing = None
    billing_notice = None
    if model is not None and adapter != "openai-api":
        billing = "subscription"
    if checked.provider_checks is not None and (model is None or valid_model):
        for p_report in checked.provider_checks.by_provider.values():
            reasons.extend(p_report.reasons)
        if valid_model and len(checked.provider_checks.by_provider) == 1:
            p_report = next(iter(checked.provider_checks.by_provider.values()))
            billing = p_report.billing
            billing_notice = p_report.billing_notice

    report = {
        "repo_root": common.repo_root,
        "config_path": str(common.config_path),
        "config_found": common.config_found,
        "adapter": adapter,
        "adapter_enabled": checked.enabled,
        "adapter_executable_found": checked.executable_found,
        "sandbox_redirect_detected": common.sandbox_redirect_detected,
        "needs_unsandboxed_dispatch": common.needs_unsandboxed_dispatch,
        "ok": not reasons,
        "reasons": reasons,
    }
    if adapter == "openai-api" or model is not None:
        report["billing"] = billing
        report["billing_notice"] = billing_notice
    return report


def require_unsandboxed_dispatch(state_dir):
    """Refuse before any Run or Grant change when the worker cannot be started from here."""
    if sandbox_redirect_detected():
        raise LedgerError(14, UNSANDBOXED_DISPATCH_REQUIRED)
    # preflight may miss other sandboxes; a real create-and-delete probe settles it before reserving.
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        handle, probe = mkstemp(prefix=".mah-write-probe-", suffix=".tmp", dir=state_dir)
        try:
            os.close(handle)
        finally:
            os.unlink(probe)
    except OSError as exc:
        raise LedgerError(14, UNSANDBOXED_DISPATCH_REQUIRED) from exc
