"""Pre-card login checks for CLI Workers: subscription sign-in only, never an API key.

Each check runs one local status command of the official CLI, with the same filtered environment the
Worker gets. These commands read the CLI's stored sign-in and send no inference request (a fake key is
reported as signed in without validation). The CLI may update files in its own home folder.

- claude 2.1.325, ``claude auth status --json``: ``loggedIn``; ``authMethod`` is ``claude.ai`` for a
  Claude subscription (other values: ``api_key``, also for a key stored by ``/login``,
  ``api_key_helper``, ``oauth_token``, ``third_party``, ``none``); ``apiKeySource`` appears only when a
  key is used; ``loginExpiresAt`` is the refresh-token expiry, after which the whole sign-in has ended.
- codex 0.154.0, ``codex login status``: the last status line on stderr is ``Logged in using ChatGPT``
  for a subscription; ``Logged in using an API key - ...``, token or Bedrock variants, or
  ``Not logged in`` otherwise.
"""
from datetime import UTC, datetime
import json
import subprocess

CLAUDE_AUTH_METHODS = {"claude.ai", "api_key", "api_key_helper", "oauth_token", "third_party", "none"}
CODEX_SUBSCRIPTION = "Logged in using ChatGPT"
TIMEOUT_SECONDS = 20


def _status(argv, environment):
    return subprocess.run([str(part) for part in argv], stdin=subprocess.DEVNULL, capture_output=True,
                          shell=False, env=environment, timeout=TIMEOUT_SECONDS)


def claude_login_reasons(executable, *, now=None):
    from .adapters.claude_print import claude_environment

    try:
        report = _status([executable, "auth", "status", "--json"], claude_environment())
        data = json.loads(report.stdout.decode("utf-8-sig"))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return [f"無法確認 Claude CLI 的登入方式（{type(exc).__name__}），請執行 claude auth status 檢查。"]
    if not isinstance(data, dict):
        return ["無法確認 Claude CLI 的登入方式，請執行 claude auth status 檢查。"]
    if data.get("loggedIn") is not True:
        return ["Claude CLI 尚未登入，請執行 claude auth login 以 Claude 帳號登入。"]
    method = data.get("authMethod")
    key_source = data.get("apiKeySource")
    if method != "claude.ai" or data.get("apiProvider") != "firstParty" or key_source not in (None, "none"):
        shown = method if method in CLAUDE_AUTH_METHODS else "unknown"
        return [f"Claude CLI 目前的登入方式是 {shown}，不是 Claude 訂閱帳號；zMAHarness 不使用 API 金鑰或第三方供應商。"
                "請執行 claude auth login 改以 Claude 帳號登入。"]
    if report.returncode != 0:
        return [f"claude auth status 結束碼 {report.returncode}，無法確認登入狀態。"]
    expires = data.get("loginExpiresAt")
    if isinstance(expires, str):
        try:
            ends = datetime.fromisoformat(expires)
        except ValueError:
            return ["無法解讀 Claude CLI 的登入到期時間，請執行 claude auth status 檢查。"]
        if ends.tzinfo is None:
            return ["無法解讀 Claude CLI 的登入到期時間，請執行 claude auth status 檢查。"]
        if ends <= (now or datetime.now(UTC)):
            return [f"Claude CLI 的登入已在 {ends.astimezone(UTC).isoformat(timespec='seconds')} 到期，"
                    "請執行 claude auth login 重新登入。"]
    return []


def codex_login_reasons(executable):
    from .adapters.codex_exec import codex_environment

    try:
        report = _status([executable, "login", "status"], codex_environment())
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return [f"無法確認 Codex CLI 的登入方式（{type(exc).__name__}），請執行 codex login status 檢查。"]
    text = b"\n".join((report.stdout, report.stderr)).decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines()
             if line.strip() == "Not logged in" or line.strip().startswith("Logged in using ")]
    status = lines[-1] if lines else None
    if status == CODEX_SUBSCRIPTION:
        return [] if report.returncode == 0 else [
            f"codex login status 結束碼 {report.returncode}，無法確認登入狀態。"]
    if status is None or status == "Not logged in":
        return ["Codex CLI 尚未登入，請執行 codex login 以 ChatGPT 帳號登入。"]
    # Drop anything after " - ", where the CLI shows a masked key.
    method = status.removeprefix("Logged in using ").split(" - ", 1)[0][:60]
    return [f"Codex CLI 目前以「{method}」登入，不是 ChatGPT 訂閱帳號；zMAHarness 不使用 API 金鑰。"
            "請執行 codex logout 後再以 codex login 用 ChatGPT 帳號登入。"]


LOGIN_CHECKS = {"claude-print": claude_login_reasons, "codex-exec": codex_login_reasons}


def login_reasons(adapter, executable):
    """Reasons that block the card; empty when the Adapter has no login check or uses a subscription."""
    check = LOGIN_CHECKS.get(adapter)
    return [] if check is None else check(executable)
