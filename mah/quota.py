"""Quota status queries (ChatGPT app-server, Claude /usage, passive agy 429) and cache rules (spec 3d)."""
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from math import isfinite
from pathlib import Path
import re
import sqlite3
import subprocess

from . import login_status, process_tree
from .adapters import registry
from .catalog import load_catalog, local_catalog_path
from .config import adapter_enabled
from .db import connect_read_only
from .executable_path import inside_roots
from .model_detect import (
    OVERALL_TIMEOUT_SECONDS,
    PARTIAL_TIMEOUT_WARNING,
    cache_path,
    detect_adapters,
    executable_key,
    query_cwd,
    read_routing_cache,
    update_routing_cache,
    write_routing_cache,
)

QUOTA_TIMEOUT_SECONDS = 10
STALE_SNAPSHOT_MAX_AGE = timedelta(hours=24)
AGY_MAX_COOLDOWN = timedelta(days=8)
AGY_STDERR_MAX_BYTES = 65536
FUTURE_SKEW_TOLERANCE = timedelta(minutes=5)

# Verified in PowerShell (D12): --no-session-persistence keeps local_command="usage", num_turns=0, total_cost_usd=0.
CLAUDE_USAGE_ARGS = ("-p", "/usage", "--output-format", "json", "--no-session-persistence")
CLAUDE_BREAKER_WARNING = "Claude /usage 沒有在本機處理，已停用 Claude 額度查詢，直到 Claude CLI 更新。"

CLAUDE_SESSION_RE = re.compile(r"^Current session:\s*(\d{1,3})% used(?:\s*·\s*resets\s+(.+))?$")
CLAUDE_WEEK_RE = re.compile(r"^Current week \(([^)]+)\):\s*(\d{1,3})% used(?:\s*·\s*resets\s+(.+))?$")
CLAUDE_RESET_RE = re.compile(r"^([A-Z][a-z]{2}) (\d{1,2}), (\d{1,2})(?::(\d\d))?(am|pm) \(([^)]*)\)$")
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

AGY_ERROR_LINE_RE = re.compile(r"^AGY_ERROR:\s*(\{.*\})\s*$")
AGY_429_FALLBACK_RE = re.compile(r"RESOURCE_EXHAUSTED\s*\(code 429\)")
AGY_RESETS_IN_RE = re.compile(r"Resets in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


@dataclass(frozen=True)
class QuotaWindow:
    name: str
    used_percent: int | float
    resets_at: str | None = None
    applies_to: str | None = None  # None = all models in pool; "sonnet" / "opus" for Claude-scoped windows

    def to_dict(self):
        data = {"name": self.name, "used_percent": self.used_percent}
        if self.resets_at is not None:
            data["resets_at"] = self.resets_at
        if self.applies_to is not None:
            data["applies_to"] = self.applies_to
        return data


@dataclass(frozen=True)
class QuotaState:
    pool: str
    state: str  # "ok" | "tight" | "exhausted" | "cooling" | "unknown"
    windows: tuple[QuotaWindow, ...] = ()
    until: str | None = None
    source: str = "none"  # "app_server" | "claude_usage" | "agy_429" | "none"
    fetched_at: str | None = None
    stale: bool = False
    age_seconds: int | None = None
    ordinary_usage_allowed: bool | None = None
    detail: str | None = None

    def to_dict(self):
        data = {"state": self.state, "source": self.source}
        if self.fetched_at is not None:
            data["fetched_at"] = self.fetched_at
        if self.windows:
            data["windows"] = [w.to_dict() for w in self.windows]
        if self.until is not None:
            data["until"] = self.until
        if self.stale:
            data["stale"] = True
            if self.age_seconds is not None:
                data["age_seconds"] = self.age_seconds
        return data

    def evaluate(self, *, model_hint=None, tight_percent=90, now=None):
        """Return (effective_state, until, peak_percent) for a specific model (or pool-wide when model_hint is None)."""
        moment = now or datetime.now(UTC)
        if self.state == "cooling":
            until_dt = _parse_iso(self.until)
            if until_dt is not None and until_dt <= moment:
                return "unknown", None, None
            return "cooling", self.until, None
        if self.state == "unknown":
            return "unknown", None, None

        if self.ordinary_usage_allowed is False:
            if not self.stale:
                return "exhausted", self.until, 100
            until_dt = _parse_iso(self.until)
            if until_dt is not None and until_dt > moment:
                return "exhausted", self.until, 100
            # Stale snapshot whose reset time passed or is missing cannot stay exhausted.
            return "unknown", None, None

        applicable = _active_windows(self.windows, model_hint=model_hint, now=moment, require_future_reset=self.stale)
        exhausted = [w for w in applicable if w.used_percent >= 100]
        if exhausted:
            return "exhausted", _latest_iso(w.resets_at for w in exhausted) or self.until, max(
                w.used_percent for w in exhausted)
        tight = [w for w in applicable if w.used_percent >= tight_percent]
        if tight:
            return "tight", None, max(w.used_percent for w in tight)
        if self.stale:
            return "unknown", None, None
        if self.ordinary_usage_allowed is True or applicable:
            return "ok", None, max((w.used_percent for w in applicable), default=None)
        return "unknown", None, None


def _parse_iso(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _iso_utc(moment):
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _epoch_to_iso(epoch):
    if type(epoch) is not int:
        return None
    try:
        return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _latest_iso(values):
    best_dt = None
    best_str = None
    for value in values:
        dt = _parse_iso(value)
        if dt is not None and (best_dt is None or dt > best_dt):
            best_dt = dt
            best_str = value
    return best_str


def _window_matches_model(window, model_hint):
    if window.applies_to is None:
        return True
    if not model_hint:
        return False
    hint = str(model_hint).lower()
    return window.applies_to in hint


def _active_windows(windows, *, model_hint, now, require_future_reset=False):
    result = []
    for window in windows:
        if not _window_matches_model(window, model_hint):
            continue
        reset_dt = _parse_iso(window.resets_at)
        if reset_dt is not None and reset_dt <= now:
            continue
        if require_future_reset and reset_dt is None:
            continue
        result.append(window)
    return result


def _window_name(minutes):
    if minutes == 300:
        return "five_hour"
    if minutes == 10080:
        return "weekly"
    if type(minutes) is int and minutes > 0:
        return f"{minutes}m"
    return "window"


def _valid_percent(value):
    return type(value) in (int, float) and isfinite(value) and value >= 0


def parse_chatgpt_quota(reply, *, now=None, tight_percent=90):
    """Parse a codex app-server `account/rateLimits/read` JSON-RPC reply into QuotaState.

    Never reads or stores accountId, credits, planType, or reset-credit fields (spec §4.3).
    """
    moment = now or datetime.now(UTC)
    fetched = _iso_utc(moment)
    result = reply.get("result") if isinstance(reply, dict) else None
    if not isinstance(result, dict):
        return QuotaState(pool="chatgpt", state="unknown", source="none",
                          detail="無法查詢" if reply is None else "回應格式無法解讀")

    allowed = result.get("ordinaryUsageAllowed")
    if allowed is not None and type(allowed) is not bool:
        return QuotaState(pool="chatgpt", state="unknown", source="none", detail="回應格式無法解讀")

    windows = []
    limits = result.get("rateLimits")
    if isinstance(limits, dict):
        for slot in ("primary", "secondary"):
            raw = limits.get(slot)
            if not isinstance(raw, dict):
                continue
            used = raw.get("usedPercent")
            if not _valid_percent(used):
                continue
            name = _window_name(raw.get("windowDurationMins"))
            resets_at = _epoch_to_iso(raw.get("resetsAt"))
            windows.append(QuotaWindow(name=name, used_percent=used, resets_at=resets_at))

    if allowed is None and not windows:
        return QuotaState(pool="chatgpt", state="unknown", source="none", detail="回應格式無法解讀")

    if allowed is False:
        reset_epoch = login_status._reset_time(result)
        until = _epoch_to_iso(reset_epoch)
        return QuotaState(
            pool="chatgpt", state="exhausted", windows=tuple(windows), until=until,
            source="app_server", fetched_at=fetched, ordinary_usage_allowed=False,
        )

    active = _active_windows(windows, model_hint=None, now=moment)
    exhausted = [w for w in active if w.used_percent >= 100]
    if exhausted:
        until = _latest_iso(w.resets_at for w in exhausted)
        return QuotaState(
            pool="chatgpt", state="exhausted", windows=tuple(windows), until=until,
            source="app_server", fetched_at=fetched, ordinary_usage_allowed=allowed,
        )
    tight = [w for w in active if w.used_percent >= tight_percent]
    if tight:
        return QuotaState(
            pool="chatgpt", state="tight", windows=tuple(windows), until=None,
            source="app_server", fetched_at=fetched, ordinary_usage_allowed=allowed,
        )
    return QuotaState(
        pool="chatgpt", state="ok", windows=tuple(windows), until=None,
        source="app_server", fetched_at=fetched, ordinary_usage_allowed=allowed,
    )


def query_chatgpt_quota(executable, *, cwd=None, timeout=QUOTA_TIMEOUT_SECONDS, now=None, tight_percent=90):
    """Run read-only `codex app-server` quota query via login_status._quota_reply (shared by preflight/models/route)."""
    from .adapters.codex_exec import codex_environment

    reply = login_status._quota_reply(executable, codex_environment(), timeout, cwd=cwd)
    return parse_chatgpt_quota(reply, now=now, tight_percent=tight_percent)


def parse_claude_reset(text, *, now=None):
    """Parse `Nov 19, 3:10pm (Asia/Taipei)` or `Nov 22, 8am (...)` as local time (spec §4.4)."""
    if not isinstance(text, str):
        return None
    match = CLAUDE_RESET_RE.match(text.strip())
    if not match:
        return None
    month_name, day_s, hour_s, minute_s, ampm, _tz = match.groups()
    month = MONTHS.get(month_name)
    if month is None:
        return None
    day = int(day_s)
    hour = int(hour_s)
    minute = int(minute_s) if minute_s is not None else 0
    if not (1 <= hour <= 12) or not (0 <= minute <= 59):
        return None
    if ampm == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12

    local_now = (now or datetime.now(UTC)).astimezone()
    tz = local_now.tzinfo
    for year in (local_now.year, local_now.year + 1):
        try:
            candidate = datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError:
            continue
        if year == local_now.year and candidate < local_now - timedelta(days=1):
            continue
        return candidate.isoformat(timespec="seconds")
    return None


def parse_claude_usage_text(text, *, now=None, tight_percent=90):
    """Parse the human-readable `result` string from `claude -p /usage --output-format json`."""
    if not isinstance(text, str):
        return "unknown", (), None
    moment = now or datetime.now(UTC)
    windows = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        session_match = CLAUDE_SESSION_RE.match(line)
        if session_match:
            pct = int(session_match.group(1))
            resets = parse_claude_reset(session_match.group(2), now=moment) if session_match.group(2) else None
            windows.append(QuotaWindow(name="five_hour", used_percent=pct, resets_at=resets, applies_to=None))
            continue
        week_match = CLAUDE_WEEK_RE.match(line)
        if week_match:
            label = week_match.group(1).strip().lower()
            if label == "all models":
                applies_to = None
            elif "sonnet" in label:
                applies_to = "sonnet"
            elif "opus" in label:
                applies_to = "opus"
            else:
                continue
            pct = int(week_match.group(2))
            resets = parse_claude_reset(week_match.group(3), now=moment) if week_match.group(3) else None
            windows.append(QuotaWindow(name="weekly", used_percent=pct, resets_at=resets, applies_to=applies_to))

    if not windows:
        return "unknown", (), None

    # Pool-level summary evaluates general (applies_to is None) windows; model-scoped windows are evaluated per model.
    general = _active_windows(windows, model_hint=None, now=moment)
    exhausted = [w for w in general if w.used_percent >= 100]
    if exhausted:
        return "exhausted", tuple(windows), _latest_iso(w.resets_at for w in exhausted)
    tight = [w for w in general if w.used_percent >= tight_percent]
    if tight:
        return "tight", tuple(windows), None
    return "ok", tuple(windows), None


def _passes_claude_anti_billing(data):
    return (
        isinstance(data, dict)
        and data.get("type") == "result"
        and data.get("subtype") == "success"
        and data.get("is_error") is False
        and data.get("local_command") == "usage"
        and type(data.get("num_turns")) is int and data["num_turns"] == 0
        and type(data.get("total_cost_usd")) in (int, float) and data["total_cost_usd"] == 0
    )


def query_claude_quota(executable, *, cwd=None, timeout=QUOTA_TIMEOUT_SECONDS, now=None, tight_percent=90,
                       breaker_exe_key=None):
    """Run `claude -p /usage --output-format json --no-session-persistence` with anti-billing guard & circuit breaker.

    Returns (QuotaState, breaker_update_key_or_None, warning_or_None, clear_breaker_bool).
    """
    from .adapters.claude_print import claude_environment

    moment = now or datetime.now(UTC)
    current_key = executable_key(executable)
    if breaker_exe_key is not None and current_key is not None and breaker_exe_key == current_key:
        return QuotaState(pool="claude", state="unknown", source="none", detail="breaker_active"), None, None, False
    clear_breaker = breaker_exe_key is not None and current_key is not None and breaker_exe_key != current_key

    argv = [str(executable), *CLAUDE_USAGE_ARGS]
    try:
        completed = process_tree.run_contained(
            argv, env=claude_environment(), cwd=cwd, timeout=timeout, popen=subprocess.Popen,
        )
    except subprocess.TimeoutExpired:
        return QuotaState(pool="claude", state="unknown", source="none", detail="timeout"), None, None, clear_breaker
    except (OSError, ValueError) as exc:
        return QuotaState(pool="claude", state="unknown", source="none", detail=str(exc)), None, None, clear_breaker

    stdout = completed.stdout or b""
    try:
        data = json.loads(stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        return QuotaState(pool="claude", state="unknown", source="none", detail="invalid_json"), None, None, clear_breaker

    if not isinstance(data, dict):
        return QuotaState(pool="claude", state="unknown", source="none", detail="invalid_json"), None, None, clear_breaker

    if not _passes_claude_anti_billing(data):
        return (
            QuotaState(pool="claude", state="unknown", source="none", detail="anti_billing_tripped"),
            current_key,
            CLAUDE_BREAKER_WARNING,
            False,
        )

    if completed.returncode != 0:
        return QuotaState(pool="claude", state="unknown", source="none",
                          detail=f"exit_{completed.returncode}"), None, None, clear_breaker

    state, windows, until = parse_claude_usage_text(data.get("result"), now=moment, tight_percent=tight_percent)
    if state == "unknown":
        return QuotaState(pool="claude", state="unknown", source="none",
                          detail="unparseable_usage_text"), None, None, clear_breaker

    return (
        QuotaState(pool="claude", state=state, windows=windows, until=until,
                   source="claude_usage", fetched_at=_iso_utc(moment)),
        None,
        None,
        clear_breaker,
    )


def _parse_agy_429_cooldown(stderr_text, finished_at, *, unknown_minutes):
    """Return cooldown_until datetime if stderr indicates agy 429 / RESOURCE_EXHAUSTED, else None."""
    is_429 = False
    search_targets = []
    for line in stderr_text.splitlines():
        match = AGY_ERROR_LINE_RE.match(line.strip())
        if match:
            try:
                payload = json.loads(match.group(1))
            except ValueError:
                payload = None
            if isinstance(payload, dict) and (
                payload.get("error_code") == 429 or payload.get("status") == "RESOURCE_EXHAUSTED"
            ):
                is_429 = True
                short = payload.get("short_error")
                if isinstance(short, str) and short:
                    search_targets.append(short)
                break
    if not is_429:
        if AGY_429_FALLBACK_RE.search(stderr_text):
            is_429 = True
        else:
            return None

    search_targets.append(stderr_text)
    delta = None
    for target in search_targets:
        match = AGY_RESETS_IN_RE.search(target)
        if match and any(group is not None for group in match.groups()):
            hours = int(match.group(1) or 0)
            minutes = int(match.group(2) or 0)
            seconds = int(match.group(3) or 0)
            delta = timedelta(hours=hours, minutes=minutes, seconds=seconds)
            if delta > AGY_MAX_COOLDOWN:
                delta = AGY_MAX_COOLDOWN
            break
    if delta is None:
        delta = timedelta(minutes=unknown_minutes)
    return finished_at + delta


def derive_agy_cooldowns(config, catalog, *, now=None):
    """Read-only derivation of active agy 429 cooldowns from ledger + run stderr receipts (spec §4.5)."""
    moment = now or datetime.now(UTC)
    horizon_days = catalog.routing.agy_cooldown_horizon_days if catalog is not None else 8
    unknown_minutes = catalog.routing.agy_unknown_cooldown_minutes if catalog is not None else 60
    cutoff = moment - timedelta(days=horizon_days)

    database = Path(config.database)
    if not database.is_file():
        return {}
    try:
        with closing(connect_read_only(database)) as db:
            rows = db.execute(
                """SELECT run_id, model_requested, finished_at
                   FROM runs
                   WHERE adapter = 'agy-print' AND state = 'FAILED' AND finished_at IS NOT NULL
                   ORDER BY finished_at DESC"""
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}

    runs_root = config.values["paths"]["state_dir"] / "runs"
    latest_until = {}
    for row in rows:
        finished_at = _parse_iso(row["finished_at"])
        if finished_at is None or finished_at < cutoff:
            continue
        stderr_file = runs_root / row["run_id"] / "agy.stderr.txt"
        try:
            with stderr_file.open("rb") as handle:
                raw = handle.read(AGY_STDERR_MAX_BYTES)
        except OSError:
            continue
        cooldown_until = _parse_agy_429_cooldown(
            raw.decode("utf-8", errors="replace"), finished_at, unknown_minutes=unknown_minutes,
        )
        if cooldown_until is None or cooldown_until <= moment:
            continue

        model_requested = row["model_requested"]
        pool = None
        if catalog is not None:
            kind, _model, _effort, route = catalog.classify_adapter_id("agy-print", model_requested)
            if kind in ("known", "refused") and route is not None:
                pool = route.quota
        if pool is None:
            pool = f"agy:id:{model_requested}"

        prev = latest_until.get(pool)
        if prev is None or cooldown_until > prev:
            latest_until[pool] = cooldown_until

    fetched = _iso_utc(moment)
    return {
        pool: QuotaState(pool=pool, state="cooling", until=_iso_utc(until), source="agy_429", fetched_at=fetched)
        for pool, until in latest_until.items()
    }


def _decode_cached_quota(pool, entry, *, now, ttl_minutes, tight_percent):
    """Return (QuotaState, is_fresh) or (None, False)."""
    if not isinstance(entry, dict):
        return None, False
    fetched = _parse_iso(entry.get("fetched_at"))
    if fetched is None or fetched > now + FUTURE_SKEW_TOLERANCE:
        return None, False
    age = now - fetched
    raw_windows = entry.get("windows", [])
    if not isinstance(raw_windows, list):
        return None, False
    windows = []
    for item in raw_windows:
        if not isinstance(item, dict):
            return None, False
        name = item.get("name")
        pct = item.get("used_percent")
        resets_at = item.get("resets_at")
        applies_to = item.get("applies_to")
        if not isinstance(name, str) or not _valid_percent(pct):
            return None, False
        if resets_at is not None and not isinstance(resets_at, str):
            return None, False
        if applies_to is not None and not isinstance(applies_to, str):
            return None, False
        windows.append(QuotaWindow(name=name, used_percent=pct, resets_at=resets_at, applies_to=applies_to))

    source = entry.get("source") if isinstance(entry.get("source"), str) else (
        "app_server" if pool == "chatgpt" else "claude_usage"
    )
    until = entry.get("until") if isinstance(entry.get("until"), str) else None
    ordinary = entry.get("ordinary_usage_allowed") if type(entry.get("ordinary_usage_allowed")) is bool else None

    is_fresh = age <= timedelta(minutes=ttl_minutes)
    if is_fresh:
        state_obj = QuotaState(
            pool=pool, state=entry.get("state", "ok"), windows=tuple(windows), until=until,
            source=source, fetched_at=entry["fetched_at"], stale=False, ordinary_usage_allowed=ordinary,
        )
        eff_state, eff_until, _ = state_obj.evaluate(model_hint=None, tight_percent=tight_percent, now=now)
        return QuotaState(
            pool=pool, state=eff_state, windows=tuple(windows), until=eff_until,
            source=source, fetched_at=entry["fetched_at"], stale=False, ordinary_usage_allowed=ordinary,
        ), True

    # Appendix C D13: expired snapshot (<= 24h) preserves tight/exhausted when resets_at has not passed.
    if age > STALE_SNAPSHOT_MAX_AGE:
        return None, False
    stale_candidate = QuotaState(
        pool=pool, state=entry.get("state", "unknown"), windows=tuple(windows), until=until,
        source=source, fetched_at=entry["fetched_at"], stale=True,
        age_seconds=max(0, int(age.total_seconds())), ordinary_usage_allowed=ordinary,
    )
    eff_state, eff_until, _ = stale_candidate.evaluate(model_hint=None, tight_percent=tight_percent, now=now)
    # Also check model-scoped tight/exhausted windows (e.g. Claude Sonnet-only) so route can still see them.
    scoped_signal = any(
        w.applies_to is not None
        and _parse_iso(w.resets_at) is not None and _parse_iso(w.resets_at) > now
        and w.used_percent >= tight_percent
        for w in windows
    )
    if eff_state in ("tight", "exhausted") or scoped_signal:
        kept_state = eff_state if eff_state in ("tight", "exhausted") else "ok"
        return QuotaState(
            pool=pool, state=kept_state, windows=tuple(windows), until=eff_until,
            source=source, fetched_at=entry["fetched_at"], stale=True,
            age_seconds=max(0, int(age.total_seconds())), ordinary_usage_allowed=ordinary,
        ), False
    return None, False


def _encode_cached_quota(state):
    entry = {
        "state": state.state,
        "source": state.source,
        "fetched_at": state.fetched_at,
        "windows": [w.to_dict() for w in state.windows],
    }
    if state.until is not None:
        entry["until"] = state.until
    if state.ordinary_usage_allowed is not None:
        entry["ordinary_usage_allowed"] = state.ordinary_usage_allowed
    return entry


def _resolve_adapter_executable(config, adapter, exclude_roots):
    if not adapter_enabled(config, adapter):
        return None
    lookup = registry.executable_lookup(adapter)
    if lookup is None:
        return None
    executable = lookup(config)
    if executable is None:
        return None
    roots = (config.values["paths"]["worktree_root"], *exclude_roots)
    if inside_roots(executable, roots):
        return None
    return executable


def cached_quota_fallback(config, catalog, pool_name, *, now=None):
    """Return usable cached snapshot (fresh or D13 stale tight/exhausted) or unknown when a query times out."""
    moment = now or datetime.now(UTC)
    routing = catalog.routing if catalog is not None else None
    tight_percent = routing.tight_percent if routing is not None else 90
    ttl_minutes = routing.quota_ttl_minutes if routing is not None else 10
    quota_cache = read_routing_cache(cache_path(config)).get("quota", {})
    cached_state, _ = _decode_cached_quota(
        pool_name, quota_cache.get(pool_name), now=moment, ttl_minutes=ttl_minutes, tight_percent=tight_percent,
    )
    return cached_state or QuotaState(pool=pool_name, state="unknown", source="none", detail="timeout")


def get_quota_states(config, catalog=None, pools=None, *, refresh=False, now=None, exclude_roots=(),
                     overall_timeout=OVERALL_TIMEOUT_SECONDS):
    """Query or derive quota states for requested pools (or all known pools when pools is None).

    Returns (dict[pool -> QuotaState], list[str] warnings). Never raises.
    """
    moment = now or datetime.now(UTC)
    routing = catalog.routing if catalog is not None else None
    tight_percent = routing.tight_percent if routing is not None else 90
    ttl_minutes = routing.quota_ttl_minutes if routing is not None else 10
    cwd = query_cwd(config)

    if pools is None:
        target_pools = ["chatgpt", "claude", "agy:gemini", "agy:claude"]
        if catalog is not None:
            for model in catalog.models:
                for route in model.routes:
                    if route.quota not in target_pools:
                        target_pools.append(route.quota)
    else:
        target_pools = list(dict.fromkeys(pools))

    cpath = cache_path(config)
    cached_doc = read_routing_cache(cpath)
    quota_cache = cached_doc.get("quota", {})
    breakers = cached_doc.get("breakers", {})
    claude_breaker = breakers.get("claude_usage") if isinstance(breakers, dict) else None

    states = {}
    warnings = []
    cache_quota_updates = {}
    breaker_update = None
    clear_claude_breaker = False

    def fetch_chatgpt():
        cached_state, fresh = (None, False)
        if not refresh:
            cached_state, fresh = _decode_cached_quota(
                "chatgpt", quota_cache.get("chatgpt"), now=moment, ttl_minutes=ttl_minutes, tight_percent=tight_percent,
            )
            if fresh and cached_state is not None:
                return cached_state, None
        else:
            cached_state, _ = _decode_cached_quota(
                "chatgpt", quota_cache.get("chatgpt"), now=moment, ttl_minutes=ttl_minutes, tight_percent=tight_percent,
            )
        executable = _resolve_adapter_executable(config, "codex-exec", exclude_roots)
        if executable is None:
            return cached_state or QuotaState(pool="chatgpt", state="unknown", source="none"), None
        live = query_chatgpt_quota(executable, cwd=cwd, timeout=QUOTA_TIMEOUT_SECONDS, now=moment,
                                   tight_percent=tight_percent)
        if live.state != "unknown":
            return live, _encode_cached_quota(live)
        # Live failed -> Appendix C D13 stale fallback if tight/exhausted & unreset.
        return cached_state or live, None

    def fetch_claude():
        cached_state, fresh = (None, False)
        if not refresh:
            cached_state, fresh = _decode_cached_quota(
                "claude", quota_cache.get("claude"), now=moment, ttl_minutes=ttl_minutes, tight_percent=tight_percent,
            )
            if fresh and cached_state is not None:
                return cached_state, None, None, None, False
        else:
            cached_state, _ = _decode_cached_quota(
                "claude", quota_cache.get("claude"), now=moment, ttl_minutes=ttl_minutes, tight_percent=tight_percent,
            )
        executable = _resolve_adapter_executable(config, "claude-print", exclude_roots)
        if executable is None:
            return cached_state or QuotaState(pool="claude", state="unknown", source="none"), None, None, None, False
        live, tripped, warning, cleared = query_claude_quota(
            executable, cwd=cwd, timeout=QUOTA_TIMEOUT_SECONDS, now=moment,
            tight_percent=tight_percent, breaker_exe_key=claude_breaker,
        )
        if live.state != "unknown":
            return live, _encode_cached_quota(live), tripped, warning, cleared
        return cached_state or live, None, tripped, warning, cleared

    need_chatgpt = "chatgpt" in target_pools
    need_claude = "claude" in target_pools
    need_agy = any(p.startswith("agy:") for p in target_pools) or pools is None

    tasks = {}
    if need_chatgpt:
        tasks["chatgpt"] = fetch_chatgpt
    if need_claude:
        tasks["claude"] = fetch_claude
    if need_agy:
        tasks["agy"] = lambda: derive_agy_cooldowns(config, catalog, now=moment)

    done, _errors, timed_out = process_tree.run_parallel_with_deadline(tasks, timeout=overall_timeout)
    if timed_out:
        warnings.append(PARTIAL_TIMEOUT_WARNING)

    if need_chatgpt:
        if "chatgpt" in done:
            state, update = done["chatgpt"]
            states["chatgpt"] = state
            if update is not None:
                cache_quota_updates["chatgpt"] = update
        else:
            states["chatgpt"] = cached_quota_fallback(config, catalog, "chatgpt", now=moment)
    if need_claude:
        if "claude" in done:
            state, update, tripped, warning, cleared = done["claude"]
            states["claude"] = state
            if update is not None:
                cache_quota_updates["claude"] = update
            if tripped is not None:
                breaker_update = tripped
            if warning is not None:
                warnings.append(warning)
            if cleared:
                clear_claude_breaker = True
        else:
            states["claude"] = cached_quota_fallback(config, catalog, "claude", now=moment)
    if need_agy and "agy" in done:
        for p, state in done["agy"].items():
            if pools is None or p in target_pools:
                states[p] = state

    for pool_name in target_pools:
        if pool_name not in states:
            states[pool_name] = QuotaState(pool=pool_name, state="unknown", source="none")

    if cache_quota_updates or breaker_update is not None or clear_claude_breaker:
        def _mutate(latest):
            latest.setdefault("quota", {}).update(cache_quota_updates)
            if breaker_update is not None:
                latest.setdefault("breakers", {})["claude_usage"] = breaker_update
            elif clear_claude_breaker:
                latest.setdefault("breakers", {})["claude_usage"] = None

        update_routing_cache(cpath, _mutate)

    return states, warnings


def refresh_routing_cache_best_effort(config, *, now=None):
    """Appendix C D13: best-effort parallel refresh of detection and quota after an unsandboxed dispatch."""
    try:
        moment = now or datetime.now(UTC)
        catalog, _ = load_catalog(local_path=local_catalog_path(config))
        ttl_hours = catalog.routing.detect_ttl_hours if catalog is not None else 12
        detect_targets = [a for a in ("agy-print", "codex-exec") if adapter_enabled(config, a)]
        quota_pools = []
        if adapter_enabled(config, "codex-exec"):
            quota_pools.append("chatgpt")
        if adapter_enabled(config, "claude-print"):
            quota_pools.append("claude")
        if not detect_targets and not quota_pools:
            return
        tasks = {}
        if detect_targets:
            tasks["detect"] = lambda: detect_adapters(
                config, detect_targets, ttl_hours=ttl_hours, refresh=False, now=moment, overall_timeout=15,
            )
        if quota_pools:
            tasks["quota"] = lambda: get_quota_states(
                config, catalog, quota_pools, refresh=True, now=moment, overall_timeout=15,
            )
        process_tree.run_parallel_with_deadline(tasks, timeout=15)
    except Exception:
        pass
