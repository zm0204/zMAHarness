"""Checks that run before a collaboration card is offered and before a dispatch reserves a Grant."""
import os
from pathlib import Path
import subprocess
from tempfile import mkstemp

from .adapters.registry import executable_lookup
from .config import HOME_VARIABLE, ConfigError, adapter_enabled, default_config_path, load_config, \
    sandbox_redirect_detected
from .ledger import LedgerError

UNSANDBOXED_DISPATCH_REQUIRED = (
    "派工必須在沙盒外執行：目前環境無法寫入 zMAHarness 狀態目錄或啟動 Worker。"
    "請依主控平台的機制核准一次沙盒外執行後重試。"
)


def git_toplevel(path):
    """`git rev-parse --show-toplevel` for path, or None when it is not a local Git work tree."""
    try:
        result = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    top = result.stdout.strip()
    return top if result.returncode == 0 and top else None


def preflight(repo, adapter, config_path=None):
    """Read-only: no file writes, no network, no quota query and no ledger. Returns the report."""
    reasons = []
    repo_root = git_toplevel(repo)
    if repo_root is None:
        reasons.append("目標不是本機 Git 專案；雲端硬碟、網頁等資料不在協作範圍內。")

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

    enabled = adapter_enabled(config, adapter) if config is not None else False
    if config is not None and not enabled:
        reasons.append(f"{adapter} 未在本機設定啟用（adapters.enabled）")

    # None: not applicable (no executable lookup registered) or unknown (settings did not load).
    lookup = executable_lookup(adapter)
    executable_found = None if lookup is None or config is None else lookup(config) is not None
    if executable_found is False:
        reasons.append(f"找不到 {adapter} 的 Worker 執行檔，請安裝或檢查本機設定。")

    redirected = sandbox_redirect_detected()
    return {
        "repo_root": repo_root,
        "config_path": str(path),
        "config_found": config_found,
        "adapter": adapter,
        "adapter_enabled": enabled,
        "adapter_executable_found": executable_found,
        "sandbox_redirect_detected": redirected,
        # Advisory: only the Codex AppContainer redirect is recognized; dispatch also probes for writes.
        "needs_unsandboxed_dispatch": redirected,
        "ok": not reasons,
        "reasons": reasons,
    }


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
