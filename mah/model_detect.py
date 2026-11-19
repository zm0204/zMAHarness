"""Read-only account model detection, routing cache I/O, and `mah models` report (spec 3d)."""
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading

from . import process_tree
from .adapters import registry
from .atomic_io import atomic_write
from .catalog import effective_efforts, load_catalog, local_catalog_path
from .config import adapter_enabled
from .db import connect_read_only
from .executable_path import EXECUTABLE_INSIDE_PROJECT, inside_roots
from .ledger import TARGET_ID, valid_id
from .login_status import claude_login_reasons

DETECT_TIMEOUT_SECONDS = 10
OVERALL_TIMEOUT_SECONDS = 25
PARTIAL_TIMEOUT_WARNING = "部分查詢逾時"
CACHE_FILENAME = "routing_cache.json"
CACHE_VERSION = 1
FUTURE_SKEW_TOLERANCE = timedelta(minutes=5)
DEFAULT_INSPECT_ADAPTERS = ("agy-print", "codex-exec", "claude-print")
_REAL_SUBPROCESS_RUN = subprocess.run
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ListResult:
    """Return shape of registry.MODEL_LISTERS entries (spec §6.4)."""

    status: str  # "ok" | "unknown"
    ids: tuple[str, ...] = ()
    efforts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    detail: str | None = None


@dataclass(frozen=True)
class DetectionRecord:
    adapter: str
    status: str  # "ok" | "unknown" | "alias" | "disabled"
    source: str  # "cache" | "live" | "alias" | "none"
    fetched_at: str | None = None
    ids: tuple[str, ...] = ()
    efforts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    executable_found: bool | None = None
    detail: str | None = None

    def route_model_status(self, route, effort):
        """Return 'available', 'missing', or 'unknown' for a concrete (route, effort) pair."""
        if self.status == "alias":
            return "available"
        if self.status != "ok":
            return "unknown"
        if route.uses_effort_in_id:
            return "available" if route.expand_id(effort) in self.ids else "missing"
        if route.id_template not in self.ids:
            return "missing"
        supported = self.efforts.get(route.id_template)
        if supported is not None and effort not in supported:
            return "missing"
        return "available"


def cache_path(config):
    return config.values["paths"]["state_dir"] / CACHE_FILENAME


def query_cwd(config):
    """Working directory for read-only CLI queries: zMAHarness config dir, never the target project."""
    if config is None:
        return None
    directory = Path(config.path).parent
    return str(directory) if directory.is_dir() else tempfile.gettempdir()


def executable_key(executable):
    """Stable identity for a CLI binary: resolved path + mtime_ns + byte size (spec §3.5)."""
    try:
        resolved = Path(executable).resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError):
        return None
    return f"{resolved}|{info.st_mtime_ns}|{info.st_size}"


def _empty_cache():
    return {"version": CACHE_VERSION, "detect": {}, "quota": {}, "breakers": {"claude_usage": None}}


def read_routing_cache(path):
    """Read `<state_dir>/routing_cache.json`; any error or corruption yields an empty cache."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return _empty_cache()
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return _empty_cache()
    detect = data.get("detect")
    quota = data.get("quota")
    breakers = data.get("breakers")
    return {
        "version": CACHE_VERSION,
        "detect": dict(detect) if isinstance(detect, dict) else {},
        "quota": dict(quota) if isinstance(quota, dict) else {},
        "breakers": dict(breakers) if isinstance(breakers, dict) else {"claude_usage": None},
    }


def write_routing_cache(path, data):
    """Best-effort atomic write of `routing_cache.json`. Returns True on success, never raises."""
    target = Path(path)
    payload = {
        "version": CACHE_VERSION,
        "detect": data.get("detect", {}),
        "quota": data.get("quota", {}),
        "breakers": data.get("breakers", {"claude_usage": None}),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        atomic_write(target, content)
        return True
    except OSError:
        return False


def update_routing_cache(path, mutator):
    """Thread-safe read-modify-write of `routing_cache.json`. Never raises."""
    with _CACHE_LOCK:
        latest = read_routing_cache(path)
        mutator(latest)
        return write_routing_cache(path, latest)


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


def _run_lister_command(argv, *, env, cwd, timeout):
    if subprocess.run is not _REAL_SUBPROCESS_RUN:
        return subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, shell=False,
            env=env, cwd=cwd, timeout=timeout,
        )
    return process_tree.run_contained(argv, env=env, cwd=cwd, timeout=timeout, popen=subprocess.Popen)


def list_agy_models(config, executable, *, timeout=DETECT_TIMEOUT_SECONDS):
    """Run `agy.exe models` (read-only, no inference) and parse `<ID>\\t<display>` lines (spec §3.2)."""
    from .adapters.agy_print import _worker_environment

    try:
        result = _run_lister_command(
            [str(executable), "models"],
            env=_worker_environment(), cwd=query_cwd(config), timeout=timeout,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return ListResult("unknown", detail=f"{type(exc).__name__}: {exc}")
    if result.returncode != 0:
        return ListResult("unknown", detail=f"agy models 結束碼 {result.returncode}")
    text = result.stdout.decode("utf-8", errors="replace")
    ids = []
    seen = set()
    for line in text.splitlines():
        if "\t" not in line:
            continue
        candidate = line.split("\t", 1)[0].strip()
        if valid_id(TARGET_ID, candidate) and candidate not in seen:
            seen.add(candidate)
            ids.append(candidate)
    if not ids:
        return ListResult("unknown", detail="agy models 未回報任何有效模型 ID")
    return ListResult("ok", ids=tuple(ids), efforts={})


def list_codex_models(config, executable, *, timeout=DETECT_TIMEOUT_SECONDS):
    """Run `codex.exe debug models` (read-only, no inference) and parse listable models (spec §3.3)."""
    from .adapters.codex_exec import codex_environment

    try:
        result = _run_lister_command(
            [str(executable), "debug", "models"],
            env=codex_environment(), cwd=query_cwd(config), timeout=timeout,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return ListResult("unknown", detail=f"{type(exc).__name__}: {exc}")
    if result.returncode != 0:
        return ListResult("unknown", detail=f"codex debug models 結束碼 {result.returncode}")
    try:
        data = json.loads(result.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        return ListResult("unknown", detail=f"codex debug models JSON 解析失敗：{exc}")
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return ListResult("unknown", detail="codex debug models 缺少 models 陣列")

    ids = []
    efforts = {}
    seen = set()
    for item in data["models"]:
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not valid_id(TARGET_ID, slug) or slug in seen:
            continue
        raw_levels = item.get("supported_reasoning_levels")
        parsed_efforts = []
        seen_efforts = set()
        if isinstance(raw_levels, list):
            for level in raw_levels:
                if isinstance(level, dict):
                    effort = level.get("effort")
                    if isinstance(effort, str) and effort and effort not in seen_efforts:
                        seen_efforts.add(effort)
                        parsed_efforts.append(effort)
        seen.add(slug)
        ids.append(slug)
        efforts[slug] = tuple(parsed_efforts)

    if not ids:
        return ListResult("unknown", detail="codex debug models 未回報任何 list 模型")
    return ListResult("ok", ids=tuple(ids), efforts=efforts)


def _fresh_cached_detection(adapter, entry, exe_key, *, now, ttl_hours):
    if not isinstance(entry, dict) or not exe_key or entry.get("exe_key") != exe_key:
        return None
    fetched = _parse_iso(entry.get("fetched_at"))
    if fetched is None:
        return None
    age = now - fetched
    if age > timedelta(hours=ttl_hours) or fetched > now + FUTURE_SKEW_TOLERANCE:
        return None
    if isinstance(entry.get("models"), dict) and entry["models"]:
        ids = []
        efforts = {}
        for slug, raw_efforts in entry["models"].items():
            if not isinstance(slug, str) or not valid_id(TARGET_ID, slug) or not isinstance(raw_efforts, list):
                return None
            if any(not isinstance(item, str) or not item for item in raw_efforts):
                return None
            ids.append(slug)
            efforts[slug] = tuple(raw_efforts)
        return DetectionRecord(
            adapter=adapter, status="ok", source="cache", fetched_at=entry["fetched_at"],
            ids=tuple(ids), efforts=efforts, executable_found=True,
        )
    if isinstance(entry.get("ids"), list) and entry["ids"]:
        if any(not isinstance(item, str) or not valid_id(TARGET_ID, item) for item in entry["ids"]):
            return None
        return DetectionRecord(
            adapter=adapter, status="ok", source="cache", fetched_at=entry["fetched_at"],
            ids=tuple(entry["ids"]), efforts={}, executable_found=True,
        )
    return None


def _encode_cache_entry(exe_key, fetched_at, result):
    if result.efforts:
        return {
            "exe_key": exe_key,
            "fetched_at": fetched_at,
            "models": {slug: list(efforts) for slug, efforts in result.efforts.items()},
        }
    return {
        "exe_key": exe_key,
        "fetched_at": fetched_at,
        "ids": list(result.ids),
    }


def _resolve_executable(config, adapter, exclude_roots):
    lookup = registry.executable_lookup(adapter)
    if lookup is None:
        return None, None
    executable = lookup(config)
    if executable is None:
        return None, "executable_missing"
    roots = (config.values["paths"]["worktree_root"], *exclude_roots)
    if inside_roots(executable, roots):
        return None, EXECUTABLE_INSIDE_PROJECT.format(path=Path(executable).resolve())
    return executable, None


def detect_adapters(config, adapters, *, ttl_hours=12, refresh=False, now=None, exclude_roots=(),
                    overall_timeout=OVERALL_TIMEOUT_SECONDS):
    """Detect available account models for `adapters` in parallel using registry.MODEL_LISTERS."""
    moment = now or datetime.now(UTC)
    cpath = cache_path(config)
    cached_doc = read_routing_cache(cpath)
    detect_cache = cached_doc.get("detect", {})
    unique_adapters = tuple(dict.fromkeys(adapters))
    results = {}
    pending_updates = {}

    def probe(adapter):
        if not adapter_enabled(config, adapter):
            return DetectionRecord(adapter=adapter, status="disabled", source="none",
                                   executable_found=None, detail="adapter_disabled"), None
        lister = registry.model_lister(adapter)
        executable, problem = _resolve_executable(config, adapter, exclude_roots)
        if lister is None:
            return DetectionRecord(adapter=adapter, status="alias", source="alias",
                                   executable_found=executable is not None, detail=problem), None
        if executable is None:
            return DetectionRecord(adapter=adapter, status="unknown", source="none",
                                   executable_found=False, detail=problem), None
        exe_key = executable_key(executable)
        if not refresh:
            hit = _fresh_cached_detection(adapter, detect_cache.get(adapter), exe_key, now=moment, ttl_hours=ttl_hours)
            if hit is not None:
                return hit, None
        listed = lister(config, executable)
        if listed.status == "ok" and listed.ids:
            fetched_at = _iso_utc(moment)
            cache_entry = _encode_cache_entry(exe_key, fetched_at, listed) if exe_key else None
            return DetectionRecord(
                adapter=adapter, status="ok", source="live", fetched_at=fetched_at,
                ids=tuple(listed.ids), efforts=dict(listed.efforts), executable_found=True,
            ), cache_entry
        return DetectionRecord(
            adapter=adapter, status="unknown", source="live", fetched_at=None,
            ids=(), efforts={}, executable_found=True, detail=listed.detail,
        ), None

    done, errors, timed_out = process_tree.run_parallel_with_deadline(
        {adapter: (lambda a=adapter: probe(a)) for adapter in unique_adapters},
        timeout=overall_timeout,
    )
    for adapter in unique_adapters:
        if adapter in done:
            record, update = done[adapter]
            results[adapter] = record
            if update is not None:
                pending_updates[adapter] = update
        elif adapter in timed_out:
            results[adapter] = DetectionRecord(
                adapter=adapter, status="unknown", source="live", detail="timeout",
            )
        else:
            exc = errors.get(adapter)
            results[adapter] = DetectionRecord(
                adapter=adapter, status="unknown", source="live",
                detail=f"{type(exc).__name__}: {exc}" if exc else "error",
            )

    if pending_updates:
        # Serialized read-modify-write so parallel detect/quota threads never overwrite each other.
        update_routing_cache(cpath, lambda latest: latest.setdefault("detect", {}).update(pending_updates))

    return results


def claude_observed_models(database_path):
    """Read-only lookup of the latest `model_observed` per requested alias for `claude-print` (spec §3.4)."""
    path = Path(database_path)
    if not path.is_file():
        return {}
    try:
        with closing(connect_read_only(path)) as db:
            rows = db.execute(
                """SELECT model_requested, model_observed
                   FROM runs
                   WHERE adapter = 'claude-print'
                     AND model_observed IS NOT NULL AND model_observed != ''
                   ORDER BY COALESCE(finished_at, launched_at, created_at) DESC, rowid DESC"""
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}
    observed = {}
    for row in rows:
        requested = row["model_requested"]
        if requested not in observed:
            observed[requested] = row["model_observed"]
    return observed


def _claude_alias_status(config, exclude_roots=()):
    """Check claude-print subscription login and latest observed model from ledger (read-only)."""
    observed = claude_observed_models(config.database)
    if not adapter_enabled(config, "claude-print"):
        return {"status": "alias", "login": "disabled", "observed": observed}
    executable, problem = _resolve_executable(config, "claude-print", exclude_roots)
    if executable is None:
        entry = {"status": "alias", "login": "missing", "observed": observed}
        if problem and problem != "executable_missing":
            entry["detail"] = problem
        return entry
    reasons = claude_login_reasons(executable)
    if reasons:
        return {"status": "alias", "login": "error", "detail": reasons[0], "observed": observed}
    return {"status": "alias", "login": "ok", "observed": observed}


def _unclassified_ids(catalog, adapter, ids):
    if catalog is None:
        return list(ids)
    return [model_id for model_id in ids if catalog.classify_adapter_id(adapter, model_id)[0] == "unclassified"]


def _format_detection_section(catalog, records, claude_info, *, include_all):
    section = {}
    for adapter, record in records.items():
        if adapter == "claude-print":
            section[adapter] = claude_info
            continue
        if record.status == "alias":
            section[adapter] = {"status": "alias"}
            continue
        entry = {"status": record.status, "source": record.source}
        if record.fetched_at is not None:
            entry["fetched_at"] = record.fetched_at
        if record.status == "ok":
            unclassified = _unclassified_ids(catalog, adapter, record.ids)
            entry["detected_count"] = len(record.ids)
            entry["unclassified_count"] = len(unclassified)
            if include_all:
                entry["unclassified"] = unclassified
        elif record.detail:
            entry["detail"] = record.detail
        section[adapter] = entry
    return section


def _format_models_section(catalog, records, claude_info, *, max_effort):
    if catalog is None:
        return []
    entries = []
    for model in catalog.models:
        if model.status == "refused":
            routes = []
            for route in model.routes:
                record = records.get(route.adapter)
                route_entry = {"adapter": route.adapter, "available": False, "prefix": route.prefix}
                if record is not None and record.status == "ok" and route.prefix:
                    matched = [mid for mid in record.ids if mid.lower().startswith(route.prefix.lower())]
                    if matched:
                        route_entry["matched_ids"] = matched
                routes.append(route_entry)
            item = {
                "key": model.key, "display": model.display, "family": model.family,
                "status": model.status, "billing": model.billing, "available": False, "routes": routes,
            }
            if model.note is not None:
                item["note"] = model.note
            entries.append(item)
            continue

        route_entries = []
        for route in model.routes:
            record = records.get(route.adapter)
            if route.adapter == "claude-print":
                effs = effective_efforts(model, route, max_effort=max_effort)
                avail = claude_info.get("login") == "ok" and bool(effs)
                route_entries.append({
                    "adapter": route.adapter,
                    "available": avail,
                    "ids": [route.id_template] if avail else [],
                    "efforts": list(effs) if avail else [],
                })
                continue

            if record is None or record.status == "disabled" or record.executable_found is False:
                route_entries.append({"adapter": route.adapter, "available": False, "ids": [], "efforts": []})
                continue

            if route.uses_effort_in_id:
                effs = effective_efforts(model, route, max_effort=max_effort)
                if record.status == "ok":
                    effs = tuple(e for e in effs if route.expand_id(e) in record.ids)
                ids = [route.expand_id(e) for e in effs]
                route_entries.append({
                    "adapter": route.adapter,
                    "available": bool(ids),
                    "ids": ids,
                    "efforts": list(effs),
                })
            else:
                if record.status == "ok":
                    if route.id_template not in record.ids:
                        effs = ()
                    else:
                        effs = effective_efforts(
                            model, route,
                            detected_efforts=record.efforts.get(route.id_template),
                            max_effort=max_effort,
                        )
                else:
                    effs = effective_efforts(model, route, max_effort=max_effort)
                avail = bool(effs)
                route_entries.append({
                    "adapter": route.adapter,
                    "available": avail,
                    "ids": [route.id_template] if avail else [],
                    "efforts": list(effs),
                })

        item = {
            "key": model.key, "display": model.display, "family": model.family,
            "status": model.status, "billing": model.billing,
            "available": any(r["available"] for r in route_entries),
            "routes": route_entries,
        }
        if model.cost is not None:
            item["cost"] = model.cost
        if model.aliases:
            item["aliases"] = list(model.aliases)
        if model.note is not None:
            item["note"] = model.note
        entries.append(item)
    return entries


def models_report(config, *, refresh=False, include_all=False, repo_catalog_path=None, now=None,
                  overall_timeout=OVERALL_TIMEOUT_SECONDS):
    """Build the JSON report for `mah models` (catalog + detection + quota) within `overall_timeout`."""
    from .quota import QuotaState, cached_quota_fallback, get_quota_states

    moment = now or datetime.now(UTC)
    catalog, warnings = load_catalog(local_path=local_catalog_path(config), repo_path=repo_catalog_path)
    ttl_hours = catalog.routing.detect_ttl_hours if catalog is not None else 12

    adapters = list(DEFAULT_INSPECT_ADAPTERS)
    target_pools = ["chatgpt", "claude", "agy:gemini", "agy:claude"]
    if catalog is not None:
        for model in catalog.models:
            for route in model.routes:
                if route.adapter not in adapters:
                    adapters.append(route.adapter)
                if route.quota not in target_pools:
                    target_pools.append(route.quota)

    lister_adapters = [a for a in adapters if a != "claude-print"]
    tasks = {}
    for adapter in lister_adapters:
        tasks[f"detect:{adapter}"] = (
            lambda a=adapter: detect_adapters(
                config, [a], ttl_hours=ttl_hours, refresh=refresh, now=moment, overall_timeout=overall_timeout,
            )[a]
        )
    tasks["claude_alias"] = lambda: _claude_alias_status(config)
    for pool_name in ("chatgpt", "claude"):
        if pool_name in target_pools:
            tasks[f"quota:{pool_name}"] = (
                lambda p=pool_name: get_quota_states(
                    config, catalog, [p], refresh=refresh, now=moment, overall_timeout=overall_timeout,
                )
            )
    agy_pools = [p for p in target_pools if p.startswith("agy:")]
    tasks["quota:agy"] = lambda: get_quota_states(
        config, catalog, agy_pools, refresh=refresh, now=moment, overall_timeout=overall_timeout,
    )

    done, _errors, timed_out = process_tree.run_parallel_with_deadline(tasks, timeout=overall_timeout)
    partial_timeout = bool(timed_out)

    records = {}
    for adapter in lister_adapters:
        key = f"detect:{adapter}"
        if key in done:
            records[adapter] = done[key]
        else:
            records[adapter] = DetectionRecord(adapter=adapter, status="unknown", source="live", detail="timeout")

    if "claude_alias" in done:
        claude_info = done["claude_alias"]
    else:
        claude_info = {"status": "alias", "login": "unknown", "detail": "timeout",
                       "observed": claude_observed_models(config.database)}

    quota_states = {}
    quota_warnings = []
    for pool_name in ("chatgpt", "claude"):
        key = f"quota:{pool_name}"
        if key in done:
            st_map, w_list = done[key]
            quota_states.update(st_map)
            quota_warnings.extend(w_list)
        elif pool_name in target_pools:
            quota_states[pool_name] = cached_quota_fallback(config, catalog, pool_name, now=moment)
    if "quota:agy" in done:
        st_map, w_list = done["quota:agy"]
        quota_states.update(st_map)
        quota_warnings.extend(w_list)
    for pool_name in target_pools:
        quota_states.setdefault(pool_name, QuotaState(pool=pool_name, state="unknown", source="none"))

    # Keep a consistent ordering across adapters.
    ordered_records = {}
    for adapter in adapters:
        if adapter == "claude-print":
            ordered_records[adapter] = DetectionRecord(adapter="claude-print", status="alias", source="alias")
        else:
            ordered_records[adapter] = records[adapter]

    max_effort = config.values["limits"]["max_effort"]
    all_warnings = list(dict.fromkeys([
        *warnings, *quota_warnings, *([PARTIAL_TIMEOUT_WARNING] if partial_timeout else ()),
    ]))
    return {
        "catalog": {
            "updated": catalog.updated if catalog is not None else None,
            "local_overrides": catalog.local_overrides if catalog is not None else False,
            "warnings": warnings,
        },
        "warnings": all_warnings,
        "detection": _format_detection_section(catalog, ordered_records, claude_info, include_all=include_all),
        "quota": {pool_name: state.to_dict() for pool_name, state in quota_states.items()},
        "models": _format_models_section(catalog, ordered_records, claude_info, max_effort=max_effort),
    }
