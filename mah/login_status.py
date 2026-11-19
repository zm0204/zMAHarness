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
- codex 0.169.0, after a ChatGPT sign-in: ``account/rateLimits/read`` through ``codex app-server``
  (read-only). Only ``ordinaryUsageAllowed: false`` blocks the card; failures never block.
"""
from datetime import UTC, datetime
import json
import subprocess

from . import process_tree

CLAUDE_AUTH_METHODS = {"claude.ai", "api_key", "api_key_helper", "oauth_token", "third_party", "none"}
CODEX_SUBSCRIPTION = "Logged in using ChatGPT"
TIMEOUT_SECONDS = 10
_REAL_SUBPROCESS_RUN = subprocess.run


def _status(argv, environment):
    if subprocess.run is not _REAL_SUBPROCESS_RUN:
        return subprocess.run([str(part) for part in argv], stdin=subprocess.DEVNULL, capture_output=True,
                              shell=False, env=environment, timeout=TIMEOUT_SECONDS)
    return process_tree.run_contained(argv, env=environment, timeout=TIMEOUT_SECONDS, popen=subprocess.Popen)


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


# ChatGPT quota through `codex app-server` JSON-RPC over stdio (codex 0.169.0). Only these three messages are
# ever sent; the request is read-only and makes no inference. Reset credits are never used.
# https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server-protocol/src/protocol/common.rs#L1437-L1441
QUOTA_MESSAGES = (
    {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "zmaharness-preflight", "title": None,
                                                                 "version": "1"}}},
    {"method": "initialized"},
    {"id": 2, "method": "account/rateLimits/read"},
)
QUOTA_TIMEOUT_SECONDS = 10


def _quota_reply(executable, environment, timeout, *, cwd=None):
    """The JSON-RPC response to request 2, or None on any failure. Never raises."""
    import threading

    try:
        process = subprocess.Popen([str(executable), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, shell=False, env=environment, cwd=cwd)
    except (OSError, ValueError):
        return None
    job, _ = process_tree.contain(process)
    replies = []
    finished = threading.Event()

    def read():
        try:
            for raw in process.stdout:
                message = json.loads(raw)
                if isinstance(message, dict) and message.get("id") == 2:
                    replies.append(message)
                    break
        except (OSError, ValueError):
            pass
        finally:
            try:
                process.stdout.close()
            except OSError:
                pass
            finished.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        for message in QUOTA_MESSAGES:
            process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        process.stdin.flush()
        finished.wait(timeout)
    except (OSError, ValueError):
        pass
    finally:
        process_tree.terminate_tree(process, job)
        reader.join(timeout=0.3)
        try:
            process.stdin.close()
        except OSError:
            pass
    return replies[0] if replies else None


def _reset_time(result):
    """Latest reset among exhausted windows, else the upsell reset time; None when unknown."""
    times = []
    for limits in (result.get("rateLimits"), *(result.get("rateLimitsByLimitId") or {}).values()):
        if not isinstance(limits, dict):
            continue
        for window in (limits.get("primary"), limits.get("secondary")):
            if (isinstance(window, dict) and isinstance(window.get("resetsAt"), int)
                    and isinstance(window.get("usedPercent"), (int, float)) and window["usedPercent"] >= 100):
                times.append(window["resetsAt"])
    upsell = result.get("rateLimitUpsell")
    if not times and isinstance(upsell, dict) and isinstance(upsell.get("reset_at"), int):
        times.append(upsell["reset_at"])
    return max(times) if times else None


def _quota_reset_epoch(until):
    if until is None:
        return None
    try:
        return int(datetime.fromisoformat(until).timestamp())
    except ValueError:
        return None


def codex_quota_status(executable, *, timeout=QUOTA_TIMEOUT_SECONDS, cwd=None):
    """("exhausted", reset epoch or None), ("available", None) or ("unknown", detail). Never raises."""
    from .quota import query_chatgpt_quota

    state = query_chatgpt_quota(executable, cwd=cwd, timeout=timeout)
    if state.state == "exhausted":
        return "exhausted", _quota_reset_epoch(state.until)
    if state.state in ("ok", "tight"):
        return "available", None
    return "unknown", state.detail or "無法查詢"


def local_time(epoch):
    if epoch is None:
        return "未知"
    try:
        moment = datetime.fromtimestamp(epoch, UTC).astimezone()
    except (OverflowError, OSError, ValueError):
        return "未知"
    return moment.strftime("%Y-%m-%d %H:%M（UTC%z）")


def codex_quota_reasons(executable, *, cwd=None, quota_state=None):
    """Block only when the quota is confirmed exhausted; a failed or unclear query never blocks."""
    if quota_state is not None:
        if quota_state.state == "exhausted":
            return [f"ChatGPT 額度已用完，重設時間 {local_time(_quota_reset_epoch(quota_state.until))}"]
        return []
    state, value = codex_quota_status(executable, cwd=cwd)
    return [f"ChatGPT 額度已用完，重設時間 {local_time(value)}"] if state == "exhausted" else []


def codex_reasons(executable, *, cwd=None, quota_state=None):
    return codex_login_reasons(executable) or codex_quota_reasons(executable, cwd=cwd, quota_state=quota_state)


LOGIN_CHECKS = {"claude-print": claude_login_reasons, "codex-exec": codex_reasons}


def login_reasons(adapter, executable, *, cwd=None, quota_state=None):
    """Reasons that block the card; empty when the Adapter has no login check or uses a subscription."""
    check = LOGIN_CHECKS.get(adapter)
    if check is None:
        return []
    if check is codex_reasons:
        return check(executable, cwd=cwd, quota_state=quota_state)
    return check(executable)
