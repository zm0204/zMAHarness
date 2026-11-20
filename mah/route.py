"""Model routing, heterogeneous selection, quota-aware fallback, and manual overrides (spec 3d §5)."""
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import process_tree
from .adapters import registry
from .catalog import (
    EFFORT_RANK,
    Catalog,
    PickSpec,
    adapter_efforts,
    effective_efforts,
    effort_within_limit,
    load_catalog,
    local_catalog_path,
)
from .config import adapter_enabled
from .ledger import EFFORT_LEVELS, TARGET_ID, valid_id
from .model_detect import (
    OVERALL_TIMEOUT_SECONDS,
    PARTIAL_TIMEOUT_WARNING,
    DetectionRecord,
    cached_cli_version_warnings,
    detect_adapters,
)
from .preflight import INVALID_MODEL_ID, AdapterCheckReport, adapter_checks, common_checks
from .quota import QuotaState, cached_quota_fallback, get_quota_states

CONTROLLER_FAMILIES = {"codex": "gpt", "claude": "claude", "other": None}
POOL_LABELS = {
    "agy:gemini": "Gemini",
    "agy:claude": "agy Claude",
    "claude": "Claude",
    "chatgpt": "ChatGPT",
}


@dataclass(frozen=True)
class _Candidate:
    pick_index: int
    pick: PickSpec
    model: object  # ModelSpec
    effort: str
    route: object  # RouteSpec
    model_id: str

    @property
    def display(self):
        return f"{self.model_id} · {self.effort}"


def _pool_label(pool):
    return POOL_LABELS.get(pool, pool)


def _format_until(until_iso, now):
    if not until_iso:
        return None
    try:
        moment = datetime.fromisoformat(until_iso)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    seconds = (moment - now).total_seconds()
    if seconds <= 0:
        return "即將重設"
    if seconds >= 3600:
        return f"約 {max(1, round(seconds / 3600))} 小時後重設"
    if seconds >= 60:
        return f"約 {max(1, round(seconds / 60))} 分鐘後重設"
    return "即將重設"


def _stale_phrase(qstate):
    if qstate is None or not qstate.stale or qstate.age_seconds is None:
        return None
    if qstate.age_seconds >= 3600:
        return f"約 {max(1, round(qstate.age_seconds / 3600))} 小時前的資料"
    return f"約 {max(1, round(qstate.age_seconds / 60))} 分鐘前的資料"


def _eval_quota(quota_states, pool, *, model_hint, tight_percent, now):
    qstate = quota_states.get(pool)
    if qstate is None:
        return "unknown", None, None, None
    state, until, pct = qstate.evaluate(model_hint=model_hint, tight_percent=tight_percent, now=now)
    return state, until, pct, qstate


def _quota_summary_text(pools, quota_states, *, tight_percent, now):
    parts = []
    for pool in dict.fromkeys(pools):
        state, until, _pct, qstate = _eval_quota(quota_states, pool, model_hint=None,
                                                 tight_percent=tight_percent, now=now)
        label = _pool_label(pool)
        if state == "cooling":
            u = _format_until(until, now)
            parts.append(f"{label} 冷卻中（{u}）" if u else f"{label} 冷卻中")
        elif state == "exhausted":
            u = _format_until(until, now)
            stale = _stale_phrase(qstate)
            extra = "、".join(p for p in (u, stale) if p)
            parts.append(f"{label} 額度已用完（{extra}）" if extra else f"{label} 額度已用完")
    return "；".join(parts) if parts else "可用池額度皆不足"


def _build_note(option2, top_unconstrained, quota_states, *, tight_percent, now):
    """Build the single-sentence card note and structured note_data (spec §5.4 + Appendix C D13)."""
    if top_unconstrained is not None and (
        option2.model.key != top_unconstrained.model.key
        or option2.route.adapter != top_unconstrained.route.adapter
    ):
        top_pool = top_unconstrained.route.quota
        q_state, until, pct, qstate = _eval_quota(
            quota_states, top_pool, model_hint=top_unconstrained.model.key,
            tight_percent=tight_percent, now=now,
        )
        label = _pool_label(top_pool)
        stale = _stale_phrase(qstate)
        if q_state == "cooling":
            u = _format_until(until, now)
            detail = f"（{u}）" if u else ""
            note = f"{label} 額度冷卻中{detail}，改推 {option2.display}。"
        elif q_state == "exhausted":
            extra = "、".join(p for p in (_format_until(until, now), stale) if p)
            detail = f"（{extra}）" if extra else ""
            note = f"{label} 額度已用完{detail}，改推 {option2.display}。"
        elif q_state == "tight":
            bits = []
            if pct is not None:
                bits.append(f"已用 {pct}%")
            if stale:
                bits.append(stale)
            detail = f"（{'、'.join(bits)}）" if bits else ""
            note = f"{label} 額度剩不多{detail}，改推 {option2.display}。"
        elif option2.model.billing != "subscription":
            note = f"其他 Worker 目前不可用，改推 {option2.display}。"
            return note, {"kind": "non_subscription", "pool": None, "until": None, "used_percent": None}
        else:
            return None, None

        data = {"kind": "rerouted", "pool": top_pool, "state": q_state, "until": until, "used_percent": pct}
        if qstate is not None and qstate.stale:
            data["stale"] = True
            data["age_seconds"] = qstate.age_seconds
        return note, data

    # No reroute, or rerouted to a still-tight option 2 -> warn if option 2 itself is tight.
    opt_pool = option2.route.quota
    q_state, until, pct, qstate = _eval_quota(
        quota_states, opt_pool, model_hint=option2.model.key,
        tight_percent=tight_percent, now=now,
    )
    if q_state == "tight":
        label = _pool_label(opt_pool)
        stale = _stale_phrase(qstate)
        bits = []
        if pct is not None:
            bits.append(f"已用 {pct}%")
        if stale:
            bits.append(stale)
        detail = f"（{'、'.join(bits)}）" if bits else ""
        note = f"{label} 額度剩不多{detail}，仍建議 {option2.display}。"
        data = {"kind": "tight", "pool": opt_pool, "state": "tight", "until": until, "used_percent": pct}
        if qstate is not None and qstate.stale:
            data["stale"] = True
            data["age_seconds"] = qstate.age_seconds
        return note, data

    if option2.model.billing != "subscription":
        return f"其他 Worker 目前不可用，改推 {option2.display}。", {
            "kind": "non_subscription", "pool": None, "until": None, "used_percent": None,
        }
    return None, None


def _override_quota_note(model_id, pool, model_hint, quota_states, *, tight_percent, now):
    if not pool:
        return None, None
    q_state, until, pct, qstate = _eval_quota(
        quota_states, pool, model_hint=model_hint, tight_percent=tight_percent, now=now,
    )
    stale = _stale_phrase(qstate)
    if q_state == "cooling":
        u = _format_until(until, now)
        detail = f"（{u}）" if u else ""
        note = f"你指定的 {model_id} 目前在冷卻中{detail}，派工可能失敗。"
    elif q_state == "exhausted":
        extra = "、".join(p for p in (_format_until(until, now), stale) if p)
        detail = f"（{extra}）" if extra else ""
        note = f"你指定的 {model_id} 額度已用完{detail}，派工可能失敗。"
    elif q_state == "tight":
        bits = []
        if pct is not None:
            bits.append(f"已用 {pct}%")
        if stale:
            bits.append(stale)
        detail = f"（{'、'.join(bits)}）" if bits else ""
        note = f"你指定的 {model_id} 額度剩不多{detail}，請留意額度。"
    else:
        return None, None
    data = {"kind": "override_quota", "pool": pool, "state": q_state, "until": until, "used_percent": pct}
    if qstate is not None and qstate.stale:
        data["stale"] = True
        data["age_seconds"] = qstate.age_seconds
    return note, data


def _expand_picks(catalog, picks):
    expanded = []
    for index, pick in enumerate(picks):
        model = catalog.model_by_key(pick.model)
        if model is None:
            continue
        for route in model.routes:
            if route.id_template is None:
                continue
            model_id = route.expand_id(pick.effort)
            expanded.append(_Candidate(index, pick, model, pick.effort, route, model_id))
    return expanded


def _passes_static(candidate, *, config, mode, excluded_family, max_effort, skipped=None):
    def reject(reason):
        if skipped is not None:
            skipped.append({"model": candidate.model.key, "adapter": candidate.route.adapter,
                            "effort": candidate.effort, "reason": reason})
        return False

    if candidate.model.status != "recommended":
        return reject(f"status={candidate.model.status}")
    if excluded_family is not None and candidate.model.family == excluded_family:
        return reject(f"same_family:{excluded_family}")
    if not adapter_enabled(config, candidate.route.adapter):
        return reject("adapter_disabled")
    if mode not in registry.workspace_support(candidate.route.adapter):
        return reject(f"mode_unsupported:{mode}")
    if candidate.effort not in effective_efforts(candidate.model, candidate.route, max_effort=max_effort):
        return reject(f"effort_not_allowed:{candidate.effort}")
    if registry.refused_model_reason(candidate.route.adapter, candidate.model_id) is not None:
        return reject("registry_refused")
    return True


def _parallel_probe(config, catalog, adapters, pools, *, repo_root, refresh, now,
                    overall_timeout=OVERALL_TIMEOUT_SECONDS):
    """Run only needed adapter checks, model detections, and quota queries in parallel within `overall_timeout`."""
    unique_adapters = tuple(dict.fromkeys(adapters))
    unique_pools = tuple(dict.fromkeys(pools))
    ttl_hours = catalog.routing.detect_ttl_hours if catalog is not None else 12
    exclude_roots = (repo_root,) if repo_root else ()

    tasks = {}
    for adapter in unique_adapters:
        # check_codex_quota=False avoids a duplicate app-server call; quota_states handles ChatGPT quota.
        tasks[f"check:{adapter}"] = (
            lambda a=adapter: adapter_checks(config, a, repo_root, check_codex_quota=False)
        )
        tasks[f"detect:{adapter}"] = (
            lambda a=adapter: detect_adapters(
                config, [a], ttl_hours=ttl_hours, refresh=refresh, now=now,
                exclude_roots=exclude_roots, overall_timeout=overall_timeout,
            )[a]
        )
    agy_pools = [p for p in unique_pools if p.startswith("agy:")]
    for pool_name in unique_pools:
        if pool_name.startswith("agy:"):
            continue
        tasks[f"quota:{pool_name}"] = (
            lambda p=pool_name: get_quota_states(
                config, catalog, [p], refresh=refresh, now=now,
                exclude_roots=exclude_roots, overall_timeout=overall_timeout,
            )
        )
    if agy_pools:
        tasks["quota:agy"] = lambda: get_quota_states(
            config, catalog, agy_pools, refresh=refresh, now=now,
            exclude_roots=exclude_roots, overall_timeout=overall_timeout,
        )

    done, _errors, timed_out = process_tree.run_parallel_with_deadline(tasks, timeout=overall_timeout)
    warnings = [PARTIAL_TIMEOUT_WARNING] if timed_out else []

    adapter_reports = {}
    detected = {}
    for adapter in unique_adapters:
        if f"check:{adapter}" in done:
            adapter_reports[adapter] = done[f"check:{adapter}"]
        else:
            adapter_reports[adapter] = AdapterCheckReport(
                adapter=adapter, enabled=True, executable=None, executable_found=None,
                reasons=(PARTIAL_TIMEOUT_WARNING,),
            )
        if f"detect:{adapter}" in done:
            detected[adapter] = done[f"detect:{adapter}"]
        else:
            detected[adapter] = DetectionRecord(adapter=adapter, status="unknown", source="live", detail="timeout")

    quota_states = {}
    for pool_name in unique_pools:
        if pool_name.startswith("agy:"):
            continue
        key = f"quota:{pool_name}"
        if key in done:
            st_map, w_list = done[key]
            quota_states.update(st_map)
            warnings.extend(w_list)
        else:
            quota_states[pool_name] = cached_quota_fallback(config, catalog, pool_name, now=now)
    if agy_pools and "quota:agy" in done:
        st_map, w_list = done["quota:agy"]
        quota_states.update(st_map)
        warnings.extend(w_list)
    for pool_name in unique_pools:
        quota_states.setdefault(pool_name, QuotaState(pool=pool_name, state="unknown", source="none"))

    return adapter_reports, detected, quota_states, list(dict.fromkeys(warnings))


def _legacy_defaults(common, task, mode, *, warnings):
    """Fallback when the repo catalog cannot be loaded or an unexpected routing error occurs (spec §7.3)."""
    config = common.config
    default = config.values["defaults"].get(task)
    if not default:
        return {
            "ok": False, "card": False, "reasons": [f"本機預設設定缺少任務模板 {task}"],
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "needs_unsandboxed_dispatch": common.needs_unsandboxed_dispatch,
            "fallback": "defaults", "warnings": list(warnings),
            "catalog": {"updated": None, "local_overrides": False},
        }
    adapter = default["adapter"]
    model_id = default["model"]
    effort = default["effort"]
    reasons = []
    if mode not in registry.workspace_support(adapter):
        reasons.append(f"{adapter} 不支援 {mode} 模式")
    if model_id and (refused := registry.refused_model_reason(adapter, model_id)) is not None:
        reasons.append(refused)
    checked = adapter_checks(config, adapter, common.repo_root)
    reasons.extend(checked.reasons)
    if reasons or not model_id:
        if not model_id and not reasons:
            reasons.append(f"本機預設設定 defaults.{task}.model 為空")
        return {
            "ok": False, "card": False, "reasons": reasons,
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "needs_unsandboxed_dispatch": common.needs_unsandboxed_dispatch,
            "fallback": "defaults", "warnings": list(warnings),
            "catalog": {"updated": None, "local_overrides": False},
        }
    return {
        "ok": True, "card": True, "reasons": [],
        "recommended": {
            "adapter": adapter, "model": model_id, "effort": effort,
            "display": f"{model_id} · {effort}", "family": None,
            "billing": "subscription", "status": "default",
        },
        "alternate": None, "note": None, "note_data": None,
        "needs_unsandboxed_dispatch": common.needs_unsandboxed_dispatch,
        "fallback": "defaults", "warnings": list(warnings),
        "catalog": {"updated": None, "local_overrides": False},
    }


def _recommended_dict(candidate):
    return {
        "adapter": candidate.route.adapter,
        "model": candidate.model_id,
        "effort": candidate.effort,
        "display": candidate.display,
        "family": candidate.model.family,
        "billing": candidate.model.billing,
        "status": candidate.model.status,
    }


def _alternate_dict(candidate, purpose):
    return {
        "adapter": candidate.route.adapter,
        "model": candidate.model_id,
        "effort": candidate.effort,
        "display": candidate.display,
        "purpose": purpose,
    }


def _verbose_payload(detected, quota_states, skipped):
    return {
        "detection": {
            adapter: {"status": rec.status, "source": rec.source, "fetched_at": rec.fetched_at}
            for adapter, rec in detected.items()
        },
        "quota": {pool: state.to_dict() for pool, state in quota_states.items()},
        "skipped": skipped,
    }


def _refused_override_reason(catalog, text, adapter_hint=None):
    """Return registry refusal reason if `text` matches a refused adapter prefix or catalog refused model."""
    adapters = (adapter_hint,) if adapter_hint else tuple(registry.REFUSED_MODELS)
    for adapter in adapters:
        reason = registry.refused_model_reason(adapter, text)
        if reason is not None:
            return reason
    lowered = text.casefold()
    for model in catalog.models:
        if model.status != "refused":
            continue
        if model.key.casefold() == lowered or any(a.casefold() == lowered for a in model.aliases):
            for route in model.routes:
                if adapter_hint and route.adapter != adapter_hint:
                    continue
                sample = route.prefix + "model" if route.prefix else text
                reason = registry.refused_model_reason(route.adapter, sample)
                return reason or f"{route.adapter} 不接受模型 {model.key}"
        for route in model.routes:
            if adapter_hint and route.adapter != adapter_hint:
                continue
            if route.prefix and lowered.startswith(route.prefix.lower()):
                reason = registry.refused_model_reason(route.adapter, text)
                return reason or f"{route.adapter} 不接受模型 {text}"
    return None


def _match_catalog_override(catalog, text, adapter_hint=None):
    """Return list of (ModelSpec, implied_effort_or_None, route_or_None) matching `text`."""
    # 1. Exact full expanded model ID (case-sensitive, then case-insensitive).
    for case_insensitive in (False, True):
        matches = []
        seen_keys = set()
        for model in catalog.models:
            if model.status == "refused":
                continue
            for route in model.routes:
                if adapter_hint and route.adapter != adapter_hint:
                    continue
                if route.id_template is None:
                    continue
                if route.uses_effort_in_id:
                    for effort in model.efforts:
                        expanded = route.expand_id(effort)
                        eq = expanded.casefold() == text.casefold() if case_insensitive else expanded == text
                        if eq and (model.key, effort) not in seen_keys:
                            seen_keys.add((model.key, effort))
                            matches.append((model, effort, route))
                else:
                    eq = (route.id_template.casefold() == text.casefold()
                          if case_insensitive else route.id_template == text)
                    if eq and (model.key, None) not in seen_keys:
                        seen_keys.add((model.key, None))
                        matches.append((model, None, route))
        if matches:
            return matches

    # 2. Catalog key (case-insensitive).
    needle = text.casefold()
    for model in catalog.models:
        if model.key.casefold() == needle:
            return [(model, None, None)]

    # 3. Catalog aliases (case-insensitive).
    alias_matches = [
        (model, None, None) for model in catalog.models
        if any(alias.casefold() == needle for alias in model.aliases)
    ]
    if alias_matches:
        return alias_matches

    # 4. Ambiguous partial matches (only triggers when >= 2 non-refused models match by key/display token).
    partial = [
        (model, None, None) for model in catalog.models
        if model.status != "refused"
        and (needle in model.key.casefold() or needle in model.display.casefold())
        and (not adapter_hint or any(r.adapter == adapter_hint for r in model.routes))
    ]
    if len(partial) >= 2:
        return partial
    return []


def _infer_effort_from_id(model_id):
    lowered = model_id.lower()
    for effort in EFFORT_LEVELS:
        if lowered.endswith("-" + effort):
            return effort
    return None


def _route_override(common, catalog, *, task, level, mode, controller, override, effort, card_effort,
                    adapter_hint, refresh, verbose, now, warnings, overall_timeout=OVERALL_TIMEOUT_SECONDS):
    config = common.config
    max_effort = config.values["limits"]["max_effort"]
    tight_percent = catalog.routing.tight_percent
    text = (override or "").strip()
    base_meta = {
        "needs_unsandboxed_dispatch": common.needs_unsandboxed_dispatch,
        "fallback": None,
        "catalog": {"updated": catalog.updated, "local_overrides": catalog.local_overrides},
    }

    if not text:
        return {
            **base_meta, "ok": False, "card": False, "resolved": False,
            "reasons": ["--override 不可為空"], "recommended": None, "alternate": None,
            "note": None, "note_data": None, "warnings": list(warnings),
        }

    # Special case: user only specified an effort level (e.g. --override "high") -> apply to cell's top model.
    if text.lower() in EFFORT_RANK and effort is None and adapter_hint is None:
        requested_effort = text.lower()
        if not effort_within_limit(requested_effort, max_effort):
            return {
                **base_meta, "ok": False, "card": False, "resolved": True,
                "reasons": [f"思考程度 {requested_effort} 超過本機上限 limits.max_effort={max_effort}"],
                "recommended": None, "alternate": None, "note": None, "note_data": None,
                "warnings": list(warnings),
            }
        normal = _route_normal(
            common, catalog, task=task, level=level, mode=mode, controller=controller,
            refresh=refresh, verbose=verbose, now=now, warnings=warnings, force_effort=requested_effort,
            overall_timeout=overall_timeout,
        )
        normal["resolved"] = True
        return normal

    # Step 1: check registry / catalog refusal first.
    refused = _refused_override_reason(catalog, text, adapter_hint=adapter_hint)
    if refused is not None:
        return {
            **base_meta, "ok": False, "card": False, "resolved": True,
            "reasons": [refused], "recommended": None, "alternate": None,
            "note": None, "note_data": None, "warnings": list(warnings),
        }

    # Step 2: match in catalog.
    matches = _match_catalog_override(catalog, text, adapter_hint=adapter_hint)
    distinct_models = list({m.key: (m, imp, rt) for m, imp, rt in matches}.values())
    if len(distinct_models) > 1:
        choices = [{"key": m.key, "display": m.display, "aliases": list(m.aliases)} for m, _, _ in distinct_models]
        return {
            **base_meta, "ok": True, "card": False, "resolved": False, "choices": choices,
            "reasons": ["指定的模型名稱對應到多個候選，請選擇要使用的模型。"],
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "warnings": list(warnings),
        }

    if len(distinct_models) == 1:
        model, implied_effort, matched_route = distinct_models[0]
        chosen_effort = effort or implied_effort or (
            model.alias_effort if model.alias_effort != "card" else None
        ) or card_effort or (model.efforts[0] if model.efforts else "medium")

        if chosen_effort not in EFFORT_RANK:
            return {
                **base_meta, "ok": False, "card": False, "resolved": True,
                "reasons": [f"無效的思考程度：{chosen_effort}"],
                "recommended": None, "alternate": None, "note": None, "note_data": None,
                "warnings": list(warnings),
            }
        if not effort_within_limit(chosen_effort, max_effort):
            return {
                **base_meta, "ok": False, "card": False, "resolved": True,
                "reasons": [f"思考程度 {chosen_effort} 超過本機上限 limits.max_effort={max_effort}"],
                "recommended": None, "alternate": None, "note": None, "note_data": None,
                "warnings": list(warnings),
            }
        if chosen_effort not in model.efforts:
            return {
                **base_meta, "ok": False, "card": False, "resolved": True,
                "reasons": [f"模型 {model.key} 不支援思考程度 {chosen_effort}"],
                "recommended": None, "alternate": None, "note": None, "note_data": None,
                "warnings": list(warnings),
            }

        routes = [matched_route] if matched_route is not None else [
            r for r in model.routes if not adapter_hint or r.adapter == adapter_hint
        ]
        if not routes:
            return {
                **base_meta, "ok": False, "card": False, "resolved": True,
                "reasons": [f"模型 {model.key} 沒有支援 Adapter {adapter_hint} 的路線"],
                "recommended": None, "alternate": None, "note": None, "note_data": None,
                "warnings": list(warnings),
            }

        probe_adapters = [r.adapter for r in routes]
        probe_pools = [r.quota for r in routes]
        adapter_reports, detected, quota_states, quota_warnings = _parallel_probe(
            config, catalog, probe_adapters, probe_pools,
            repo_root=common.repo_root, refresh=refresh, now=now, overall_timeout=overall_timeout,
        )
        all_warnings = [*warnings, *quota_warnings]

        selected = None
        failure_reasons = []
        skipped = []
        for route in routes:
            if mode not in registry.workspace_support(route.adapter):
                msg = f"{route.adapter} 不支援 {mode} 模式"
                failure_reasons.append(msg)
                skipped.append({"model": model.key, "adapter": route.adapter, "reason": msg})
                continue
            if chosen_effort not in adapter_efforts(route.adapter):
                msg = f"{route.adapter} 不支援思考程度 {chosen_effort}"
                failure_reasons.append(msg)
                skipped.append({"model": model.key, "adapter": route.adapter, "reason": msg})
                continue
            report = adapter_reports[route.adapter]
            if not report.ok:
                failure_reasons.extend(report.reasons)
                skipped.append({"model": model.key, "adapter": route.adapter, "reason": "; ".join(report.reasons)})
                continue
            rec = detected.get(route.adapter)
            if rec is not None and rec.route_model_status(route, chosen_effort) == "missing":
                msg = f"帳號在 {route.adapter} 未回報 {route.expand_id(chosen_effort)}"
                failure_reasons.append(msg)
                skipped.append({"model": model.key, "adapter": route.adapter, "reason": msg})
                continue
            selected = route
            break

        if selected is None:
            out = {
                **base_meta, "ok": False, "card": False, "resolved": True,
                "reasons": failure_reasons or [f"找不到可用的路線執行 {model.key}"],
                "recommended": None, "alternate": None, "note": None, "note_data": None,
                "warnings": all_warnings,
            }
            if verbose:
                out.update(_verbose_payload(detected, quota_states, skipped))
            return out

        model_id = selected.expand_id(chosen_effort)
        if (refused_expanded := registry.refused_model_reason(selected.adapter, model_id)) is not None:
            return {
                **base_meta, "ok": False, "card": False, "resolved": True,
                "reasons": [refused_expanded], "recommended": None, "alternate": None,
                "note": None, "note_data": None, "warnings": all_warnings,
            }
        # Manual override does NOT exclude on quota; only attaches reminder note (spec §5.4 rule 7).
        note, note_data = _override_quota_note(
            model_id, selected.quota, model.key, quota_states, tight_percent=tight_percent, now=now,
        )
        out = {
            **base_meta, "ok": True, "card": True, "resolved": True, "reasons": [],
            "recommended": {
                "adapter": selected.adapter, "model": model_id, "effort": chosen_effort,
                "display": f"{model_id} · {chosen_effort}", "family": model.family,
                "billing": model.billing, "status": model.status,
            },
            "alternate": None, "note": note, "note_data": note_data, "warnings": all_warnings,
        }
        if verbose:
            out.update(_verbose_payload(detected, quota_states, skipped))
        return out

    # Step 3: Not in catalog -> must at least match TARGET_ID.
    if not valid_id(TARGET_ID, text):
        return {
            **base_meta, "ok": False, "card": False, "resolved": False,
            "reasons": [INVALID_MODEL_ID.format(model=text)],
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "warnings": list(warnings),
        }

    # Check detected lists of enabled lister adapters.
    lister_adapters = [
        a for a in registry.MODEL_LISTERS
        if adapter_enabled(config, a) and (not adapter_hint or a == adapter_hint)
    ]
    exclude_roots = (common.repo_root,) if common.repo_root else ()
    detected = detect_adapters(
        config, lister_adapters, ttl_hours=catalog.routing.detect_ttl_hours,
        refresh=refresh, now=now, exclude_roots=exclude_roots, overall_timeout=overall_timeout,
    ) if lister_adapters else {}
    detected_hits = [
        adapter for adapter, rec in detected.items()
        if rec.status == "ok" and text in rec.ids
    ]

    if len(detected_hits) > 1:
        return {
            **base_meta, "ok": True, "card": False, "resolved": False,
            "choices": [{"adapter": a, "model": text} for a in detected_hits],
            "reasons": ["多個 Adapter 都偵測到這個未分級模型，請指定 --adapter。"],
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "warnings": list(warnings),
        }

    target_adapter = detected_hits[0] if len(detected_hits) == 1 else adapter_hint
    if target_adapter is None:
        choices = [
            {"adapter": a, "model": text}
            for a in registry.dispatchable_names()
            if adapter_enabled(config, a)
        ]
        return {
            **base_meta, "ok": True, "card": False, "resolved": False, "choices": choices,
            "reasons": [f"目錄與偵測清單都找不到 {text}，請一併指定 --adapter。"],
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "warnings": list(warnings),
        }

    if not registry.is_registered(target_adapter):
        return {
            **base_meta, "ok": False, "card": False, "resolved": False,
            "reasons": [f"未知的 Adapter：{target_adapter}"],
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "warnings": list(warnings),
        }
    if (refused := registry.refused_model_reason(target_adapter, text)) is not None:
        return {
            **base_meta, "ok": False, "card": False, "resolved": True,
            "reasons": [refused], "recommended": None, "alternate": None,
            "note": None, "note_data": None, "warnings": list(warnings),
        }
    if mode not in registry.workspace_support(target_adapter):
        return {
            **base_meta, "ok": False, "card": False, "resolved": True,
            "reasons": [f"{target_adapter} 不支援 {mode} 模式"],
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "warnings": list(warnings),
        }

    chosen_effort = effort or (
        _infer_effort_from_id(text) if target_adapter == "agy-print" else None
    ) or card_effort or "medium"
    if chosen_effort not in EFFORT_RANK or chosen_effort not in adapter_efforts(target_adapter):
        return {
            **base_meta, "ok": False, "card": False, "resolved": True,
            "reasons": [f"{target_adapter} 不支援思考程度 {chosen_effort}"],
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "warnings": list(warnings),
        }
    if not effort_within_limit(chosen_effort, max_effort):
        return {
            **base_meta, "ok": False, "card": False, "resolved": True,
            "reasons": [f"思考程度 {chosen_effort} 超過本機上限 limits.max_effort={max_effort}"],
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "warnings": list(warnings),
        }

    pool_name = (
        f"agy:id:{text}" if target_adapter == "agy-print"
        else ("chatgpt" if target_adapter == "codex-exec"
              else ("claude" if target_adapter == "claude-print" else None))
    )
    checked = adapter_checks(config, target_adapter, common.repo_root, check_codex_quota=False)
    if not checked.ok:
        return {
            **base_meta, "ok": False, "card": False, "resolved": True,
            "reasons": list(checked.reasons), "recommended": None, "alternate": None,
            "note": None, "note_data": None, "warnings": list(warnings),
        }
    quota_states, quota_warnings = get_quota_states(
        config, catalog, [pool_name] if pool_name else [], refresh=refresh, now=now,
        exclude_roots=exclude_roots, overall_timeout=overall_timeout,
    ) if pool_name else ({}, [])
    note, note_data = _override_quota_note(
        text, pool_name, None, quota_states, tight_percent=tight_percent, now=now,
    )
    out = {
        **base_meta, "ok": True, "card": True, "resolved": True, "reasons": [],
        "recommended": {
            "adapter": target_adapter, "model": text, "effort": chosen_effort,
            "display": f"{text} · {chosen_effort}", "family": None,
            "billing": "subscription", "status": "unclassified",
        },
        "alternate": None, "note": note, "note_data": note_data,
        "warnings": [*warnings, *quota_warnings],
    }
    if verbose:
        out.update(_verbose_payload(detected, quota_states, []))
    return out


def _route_normal(common, catalog, *, task, level, mode, controller, refresh, verbose, now, warnings,
                  force_effort=None, overall_timeout=OVERALL_TIMEOUT_SECONDS):
    config = common.config
    max_effort = config.values["limits"]["max_effort"]
    tight_percent = catalog.routing.tight_percent
    excluded_family = CONTROLLER_FAMILIES[controller]
    base_meta = {
        "needs_unsandboxed_dispatch": common.needs_unsandboxed_dispatch,
        "fallback": None,
        "catalog": {"updated": catalog.updated, "local_overrides": catalog.local_overrides},
    }

    cell = catalog.find_cell(task, level)
    if cell is None:
        return _legacy_defaults(common, task, mode, warnings=[*warnings, f"目錄缺少 {task}.{level} 規則，已退回預設"])

    cell_picks = cell.picks
    if force_effort is not None:
        cell_picks = tuple(PickSpec(model=p.model, effort=force_effort, option3=p.option3) for p in cell.picks)

    skipped = []
    static_cell = [
        c for c in _expand_picks(catalog, cell_picks)
        if _passes_static(c, config=config, mode=mode, excluded_family=excluded_family,
                          max_effort=max_effort, skipped=skipped)
    ]

    # Gather all option3 picks that could be considered so queries happen in one minimal parallel batch.
    possible_opt3_picks = []
    for pick in cell_picks:
        possible_opt3_picks.extend(pick.option3)
    possible_opt3_picks.extend(catalog.option3.get("save", ()))
    possible_opt3_picks.extend(catalog.option3.get("upgrade", ()))
    static_opt3 = [
        c for c in _expand_picks(catalog, possible_opt3_picks)
        if _passes_static(c, config=config, mode=mode, excluded_family=excluded_family, max_effort=max_effort)
    ]

    if not static_cell:
        out = {
            **base_meta, "ok": False, "card": False,
            "reasons": [f"沒有符合條件的 Worker 候選（task={task}, level={level}, mode={mode}, controller={controller}）"],
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "warnings": list(warnings),
        }
        if verbose:
            out.update(_verbose_payload({}, {}, skipped))
        return out

    # Only query adapters and quota pools actually needed after static filtering (spec §5.2 & §9.1).
    needed_adapters = [c.route.adapter for c in (*static_cell, *static_opt3)]
    needed_pools = [c.route.quota for c in (*static_cell, *static_opt3)]
    adapter_reports, detected, quota_states, quota_warnings = _parallel_probe(
        config, catalog, needed_adapters, needed_pools,
        repo_root=common.repo_root, refresh=refresh, now=now, overall_timeout=overall_timeout,
    )
    all_warnings = list(dict.fromkeys([*warnings, *quota_warnings]))

    def route_ready(candidate):
        if not adapter_reports[candidate.route.adapter].ok:
            skipped.append({"model": candidate.model.key, "adapter": candidate.route.adapter,
                            "effort": candidate.effort,
                            "reason": "; ".join(adapter_reports[candidate.route.adapter].reasons)})
            return False
        rec = detected.get(candidate.route.adapter)
        if rec is not None and rec.route_model_status(candidate.route, candidate.effort) == "missing":
            skipped.append({"model": candidate.model.key, "adapter": candidate.route.adapter,
                            "effort": candidate.effort, "reason": "not_in_account_models"})
            return False
        return True

    def q_state_of(candidate):
        return _eval_quota(
            quota_states, candidate.route.quota, model_hint=candidate.model.key,
            tight_percent=tight_percent, now=now,
        )[0]

    def usable(candidate):
        if not adapter_reports[candidate.route.adapter].ok:
            return False
        rec = detected.get(candidate.route.adapter)
        if rec is not None and rec.route_model_status(candidate.route, candidate.effort) == "missing":
            return False
        return q_state_of(candidate) not in ("exhausted", "cooling")

    ready_cell = [c for c in static_cell if route_ready(c)]
    top_unconstrained = ready_cell[0] if ready_cell else None

    # First usable route per pick (e.g. Claude: claude-print before agy-print).
    ranked = []
    seen_pick = set()
    for candidate in ready_cell:
        if candidate.pick_index in seen_pick:
            continue
        qstate = q_state_of(candidate)
        if qstate in ("exhausted", "cooling"):
            skipped.append({"model": candidate.model.key, "adapter": candidate.route.adapter,
                            "effort": candidate.effort, "reason": f"quota:{qstate}"})
            continue
        seen_pick.add(candidate.pick_index)
        ranked.append(candidate)

    # Rule 3 (non-tight before tight) and Rule 6 (subscription before non-subscription).
    def rank_key(candidate):
        non_sub = 0 if candidate.model.billing == "subscription" else 1
        is_tight = 1 if q_state_of(candidate) == "tight" else 0
        return (non_sub, is_tight)

    ranked_ordered = sorted(ranked, key=rank_key)
    option2 = ranked_ordered[0] if ranked_ordered else None

    if option2 is None:
        if ready_cell:
            summary = _quota_summary_text([c.route.quota for c in ready_cell], quota_states,
                                          tight_percent=tight_percent, now=now)
            out = {
                **base_meta, "ok": True, "card": False,
                "reasons": [f"目前可用的 Worker 都在冷卻或額度已用完：{summary}"],
                "recommended": None, "alternate": None, "note": None, "note_data": None,
                "warnings": all_warnings,
            }
        else:
            top_adapter = static_cell[0].route.adapter
            top_reasons = list(adapter_reports[top_adapter].reasons)
            if not top_reasons:
                top_reasons = ["帳號可用的模型清單中沒有符合這個任務的候選模型"]
            out = {
                **base_meta, "ok": False, "card": False, "reasons": top_reasons,
                "recommended": None, "alternate": None, "note": None, "note_data": None,
                "warnings": all_warnings,
            }
        if verbose:
            out.update(_verbose_payload(detected, quota_states, skipped))
        return out

    # Option 3 selection (§5.2 & §5.3 & Appendix C D7).
    option3 = None
    option3_purpose = None
    lists = []
    if option2.pick.option3:
        lists.append(("other_family", option2.pick.option3))
    purpose = "save" if option2.model.cost == "premium" else "upgrade"
    lists.append((purpose, catalog.option3.get(purpose, ())))

    for cand_purpose, picks in lists:
        pool_candidates = [
            c for c in _expand_picks(catalog, picks)
            if _passes_static(c, config=config, mode=mode, excluded_family=excluded_family, max_effort=max_effort)
            and usable(c)
            and not (c.model.key == option2.model.key and c.effort == option2.effort)
        ]
        # First usable route per pick, preferring non-tight over tight.
        deduped = []
        seen_p = set()
        for c in pool_candidates:
            if c.pick_index not in seen_p:
                seen_p.add(c.pick_index)
                deduped.append(c)
        deduped.sort(key=lambda c: 1 if q_state_of(c) == "tight" else 0)
        if deduped:
            option3 = deduped[0]
            option3_purpose = cand_purpose
            break

    note, note_data = _build_note(option2, top_unconstrained, quota_states,
                                  tight_percent=tight_percent, now=now)
    out = {
        **base_meta, "ok": True, "card": True, "reasons": [],
        "recommended": _recommended_dict(option2),
        "alternate": _alternate_dict(option3, option3_purpose) if option3 is not None else None,
        "note": note, "note_data": note_data, "warnings": all_warnings,
    }
    if verbose:
        out.update(_verbose_payload(detected, quota_states, skipped))
    return out


def route(repo, *, task, level, mode, controller, config_path=None, override=None, effort=None,
          card_effort=None, adapter=None, refresh=False, verbose=False, now=None, repo_catalog_path=None,
          overall_timeout=OVERALL_TIMEOUT_SECONDS):
    """Run pre-card feasibility checks, model detection, quota lookup, and option 2/3 selection (spec §5-6)."""
    moment = now or datetime.now(UTC)
    common = common_checks(repo, config_path)
    if not common.ok:
        return {
            "ok": False, "card": False, "reasons": list(common.reasons),
            "recommended": None, "alternate": None, "note": None, "note_data": None,
            "needs_unsandboxed_dispatch": common.needs_unsandboxed_dispatch,
            "fallback": None, "warnings": [],
            "catalog": {"updated": None, "local_overrides": False},
        }

    try:
        catalog, warnings = load_catalog(local_path=local_catalog_path(common.config), repo_path=repo_catalog_path)
        if catalog is None:
            return _legacy_defaults(common, task, mode, warnings=warnings)
        exclude_roots = (common.repo_root,) if common.repo_root else ()
        drift_warnings = cached_cli_version_warnings(common.config, catalog, exclude_roots=exclude_roots)
        warnings = list(dict.fromkeys([*warnings, *drift_warnings]))
        if override is not None:
            return _route_override(
                common, catalog, task=task, level=level, mode=mode, controller=controller,
                override=override, effort=effort, card_effort=card_effort, adapter_hint=adapter,
                refresh=refresh, verbose=verbose, now=moment, warnings=warnings,
                overall_timeout=overall_timeout,
            )
        return _route_normal(
            common, catalog, task=task, level=level, mode=mode, controller=controller,
            refresh=refresh, verbose=verbose, now=moment, warnings=warnings,
            overall_timeout=overall_timeout,
        )
    except Exception as exc:
        return _legacy_defaults(
            common, task, mode,
            warnings=[f"路由發生非預期錯誤，已退回本機預設設定：{type(exc).__name__}: {exc}"],
        )
