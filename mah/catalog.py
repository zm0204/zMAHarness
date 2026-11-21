"""Model catalog loading, validation, local override merging and ID expansion (spec 3d)."""
from dataclasses import dataclass, replace
from pathlib import Path
import re
import tomllib

from .adapters import registry
from .ledger import EFFORT_LEVELS, TARGET_ID, valid_id

REPO_CATALOG_PATH = Path(__file__).parent / "model_catalog.toml"
LOCAL_CATALOG_NAME = "models.local.toml"
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

MODEL_STATUSES = frozenset({"recommended", "selectable", "refused"})
MODEL_COSTS = frozenset({"economy", "premium"})
MODEL_BILLINGS = frozenset({"subscription", "cloud_api", "local"})
TASKS = frozenset({"review", "second_opinion", "research"})
LEVELS = ("light", "standard", "deep")
OPTION3_KEYS = frozenset({"save", "upgrade"})

TOP_LEVEL_KEYS = frozenset({"schema_version", "updated", "tested_cli", "routing", "models", "cells", "option3"})
ROUTING_KEYS = frozenset({
    "tight_percent", "detect_ttl_hours", "quota_ttl_minutes",
    "agy_unknown_cooldown_minutes", "agy_cooldown_horizon_days",
})
MODEL_KEYS = frozenset({
    "key", "display", "family", "status", "cost", "billing",
    "efforts", "aliases", "alias_effort", "routes", "note",
})
ROUTE_KEYS = frozenset({"adapter", "id", "prefix", "quota", "provider_kind"})
CELL_KEYS = frozenset({"task", "level", "picks"})
PICK_KEYS = frozenset({"model", "effort", "option3"})
OPTION3_PICK_KEYS = frozenset({"model", "effort"})

FAMILY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
EFFORT_RANK = {name: index for index, name in enumerate(EFFORT_LEVELS)}


class CatalogError(ValueError):
    """Raised when a model catalog fails parsing or schema validation."""


@dataclass(frozen=True)
class RoutingSettings:
    tight_percent: int = 90
    detect_ttl_hours: int = 12
    quota_ttl_minutes: int = 10
    agy_unknown_cooldown_minutes: int = 60
    agy_cooldown_horizon_days: int = 8


@dataclass(frozen=True)
class RouteSpec:
    adapter: str
    id_template: str | None
    prefix: str | None
    quota: str
    provider_kind: str | None = None

    @property
    def uses_effort_in_id(self):
        return self.id_template is not None and "{effort}" in self.id_template

    def expand_id(self, effort):
        if self.id_template is None:
            raise CatalogError(f"route on {self.adapter} has no id template")
        if "{effort}" in self.id_template:
            return self.id_template.replace("{effort}", effort)
        return self.id_template


@dataclass(frozen=True)
class ModelSpec:
    key: str
    display: str
    family: str
    status: str
    cost: str | None
    billing: str
    efforts: tuple[str, ...]
    aliases: tuple[str, ...]
    alias_effort: str
    routes: tuple[RouteSpec, ...]
    note: str | None = None


@dataclass(frozen=True)
class PickSpec:
    model: str
    effort: str
    option3: tuple["PickSpec", ...] = ()


@dataclass(frozen=True)
class CellSpec:
    task: str
    level: str
    picks: tuple[PickSpec, ...]


@dataclass(frozen=True)
class Catalog:
    schema_version: int
    updated: str
    tested_cli: dict[str, str]
    routing: RoutingSettings
    models: tuple[ModelSpec, ...]
    cells: tuple[CellSpec, ...]
    option3: dict[str, tuple[PickSpec, ...]]
    local_overrides: bool = False

    def model_by_key(self, key):
        for model in self.models:
            if model.key == key:
                return model
        return None

    def model_by_alias(self, alias):
        needle = alias.casefold()
        for model in self.models:
            if any(item.casefold() == needle for item in model.aliases):
                return model
        return None

    def find_cell(self, task, level):
        if task not in TASKS or level not in LEVELS:
            return None
        by_level = {cell.level: cell for cell in self.cells if cell.task == task}
        if level in by_level:
            return by_level[level]
        index = LEVELS.index(level)
        # Missing cells fall back to higher levels first, then lower levels (spec §2.3).
        for candidate in (*LEVELS[index + 1:], *reversed(LEVELS[:index])):
            if candidate in by_level:
                return by_level[candidate]
        return None

    def adapter_id_map(self, adapter):
        """Map expanded concrete model ID -> (ModelSpec, effort_or_None, RouteSpec) for non-refused models."""
        mapping = {}
        for model in self.models:
            if model.status == "refused":
                continue
            for route in model.routes:
                if route.adapter != adapter or route.id_template is None:
                    continue
                if route.uses_effort_in_id:
                    for effort in model.efforts:
                        mapping[route.expand_id(effort)] = (model, effort, route)
                else:
                    mapping[route.id_template] = (model, None, route)
        return mapping

    def adapter_refused_prefixes(self, adapter):
        """Return tuple of (prefix, ModelSpec, RouteSpec) for refused models on this adapter."""
        prefixes = []
        for model in self.models:
            if model.status != "refused":
                continue
            for route in model.routes:
                if route.adapter == adapter and route.prefix is not None:
                    prefixes.append((route.prefix, model, route))
        return tuple(prefixes)

    def classify_adapter_id(self, adapter, model_id):
        """Classify a detected ID for `adapter` as ('refused'|'known'|'unclassified', model, effort, route)."""
        lowered = model_id.lower()
        for prefix, model, route in self.adapter_refused_prefixes(adapter):
            if lowered.startswith(prefix.lower()):
                return "refused", model, None, route
        known = self.adapter_id_map(adapter).get(model_id)
        if known is not None:
            model, effort, route = known
            return "known", model, effort, route
        return "unclassified", None, None, None


def adapter_efforts(adapter):
    """Efforts supported by the given Adapter (lazy-imported to avoid loading worker modules early)."""
    if adapter == "agy-print":
        from .adapters.agy_print import AGY_EFFORTS
        return frozenset(AGY_EFFORTS)
    if adapter == "claude-print":
        from .adapters.claude_print import CLAUDE_EFFORTS
        return frozenset(CLAUDE_EFFORTS)
    if adapter == "codex-exec":
        from .adapters.codex_exec import CODEX_EFFORTS
        return frozenset(CODEX_EFFORTS)
    return frozenset(EFFORT_LEVELS)


def effort_within_limit(effort, max_effort):
    if effort not in EFFORT_RANK or max_effort not in EFFORT_RANK:
        return False
    return EFFORT_RANK[effort] <= EFFORT_RANK[max_effort]


def effective_efforts(model, route, *, detected_efforts=None, max_effort=None):
    """model.efforts ∩ adapter efforts ∩ detected efforts (if given) ∩ <= max_effort (if given)."""
    supported = adapter_efforts(route.adapter)
    detected = set(detected_efforts) if detected_efforts is not None else None
    result = []
    for effort in model.efforts:
        if effort not in supported:
            continue
        if detected is not None and effort not in detected:
            continue
        if max_effort is not None and not effort_within_limit(effort, max_effort):
            continue
        result.append(effort)
    return tuple(result)


def local_catalog_path(config):
    return config.path.parent / LOCAL_CATALOG_NAME


def _single_line(value, label, *, allow_empty=False):
    if type(value) is not str:
        raise CatalogError(f"{label} 必須是字串")
    if value != value.strip() or any(ch in value for ch in ("\r", "\n", "\0")):
        raise CatalogError(f"{label} 必須是有效的單行文字")
    if not allow_empty and not value:
        raise CatalogError(f"{label} 不可為空")
    return value


def _check_keys(table, allowed, label):
    if type(table) is not dict:
        raise CatalogError(f"{label} 必須是 TOML 表格")
    unknown = set(table) - allowed
    if unknown:
        raise CatalogError(f"{label} 包含未知欄位：{', '.join(sorted(unknown))}")


def _parse_routing(raw, base=None):
    if raw is None:
        return base or RoutingSettings()
    _check_keys(raw, ROUTING_KEYS, "routing")
    current = base or RoutingSettings()
    updates = {}
    for key, value in raw.items():
        if type(value) is not int:
            raise CatalogError(f"routing.{key} 必須是整數")
        if key == "tight_percent":
            if not (0 <= value <= 100):
                raise CatalogError("routing.tight_percent 必須介於 0 到 100")
        elif value <= 0:
            raise CatalogError(f"routing.{key} 必須是正整數")
        updates[key] = value
    return replace(current, **updates)


def _parse_route(raw, *, model_key, status, efforts):
    _check_keys(raw, ROUTE_KEYS, f"models[{model_key}].routes")
    adapter = _single_line(raw.get("adapter"), f"models[{model_key}].routes.adapter")
    if adapter == "openai-api":
        raise CatalogError("openai-api 的目錄路由在 4a-2 才支援")
    if not registry.is_registered(adapter):
        raise CatalogError(f"models[{model_key}] 的 Adapter 尚未註冊：{adapter}")
    quota = _single_line(raw.get("quota"), f"models[{model_key}].routes.quota")
    if any(ch.isspace() for ch in quota):
        raise CatalogError(f"models[{model_key}].routes.quota 不可含空白")
    provider_kind = None
    if "provider_kind" in raw:
        provider_kind = _single_line(raw["provider_kind"], f"models[{model_key}].routes.provider_kind")

    has_id = "id" in raw
    has_prefix = "prefix" in raw
    if status == "refused":
        if has_id or not has_prefix:
            raise CatalogError(f"refused 模型 {model_key} 的 route 只能使用 prefix，不可使用 id")
        prefix = _single_line(raw["prefix"], f"models[{model_key}].routes.prefix")
        if "{" in prefix or "}" in prefix:
            raise CatalogError(f"models[{model_key}].routes.prefix 不可含大括號")
        return RouteSpec(adapter=adapter, id_template=None, prefix=prefix, quota=quota, provider_kind=provider_kind)

    if has_prefix or not has_id:
        raise CatalogError(f"非 refused 模型 {model_key} 的 route 只能使用 id，不可使用 prefix")
    id_template = _single_line(raw["id"], f"models[{model_key}].routes.id")
    stripped = id_template.replace("{effort}", "")
    if "{" in stripped or "}" in stripped:
        raise CatalogError(f"models[{model_key}].routes.id 除了 {{effort}} 以外不可有其他大括號")
    route = RouteSpec(adapter=adapter, id_template=id_template, prefix=None, quota=quota, provider_kind=provider_kind)
    for effort in efforts:
        expanded = route.expand_id(effort)
        if not valid_id(TARGET_ID, expanded):
            raise CatalogError(f"models[{model_key}] 展開後的模型 ID 無效：{expanded}")
        if registry.refused_model_reason(adapter, expanded) is not None:
            raise CatalogError(f"models[{model_key}] 展開後的模型 ID 被 registry 拒絕：{expanded}")
    return route


def _parse_model(raw):
    _check_keys(raw, MODEL_KEYS, "models")
    key = _single_line(raw.get("key"), "models.key")
    if not valid_id(TARGET_ID, key):
        raise CatalogError(f"models.key 格式不符：{key}")
    display = _single_line(raw.get("display"), f"models[{key}].display")
    family = _single_line(raw.get("family"), f"models[{key}].family")
    if not valid_id(FAMILY_RE, family):
        raise CatalogError(f"models[{key}].family 無效：{family}")
    status = _single_line(raw.get("status"), f"models[{key}].status")
    if status not in MODEL_STATUSES:
        raise CatalogError(f"models[{key}].status 無效：{status}")
    billing = _single_line(raw.get("billing"), f"models[{key}].billing")
    if billing not in MODEL_BILLINGS:
        raise CatalogError(f"models[{key}].billing 無效：{billing}")

    cost = None
    if "cost" in raw:
        cost = _single_line(raw["cost"], f"models[{key}].cost")
        if cost not in MODEL_COSTS:
            raise CatalogError(f"models[{key}].cost 無效：{cost}")
    elif status == "recommended":
        raise CatalogError(f"recommended 模型 {key} 必須填寫 cost")

    raw_efforts = raw.get("efforts")
    if raw_efforts is None:
        if status != "refused":
            raise CatalogError(f"models[{key}] 必須填寫 efforts")
        efforts = ()
    else:
        if type(raw_efforts) is not list:
            raise CatalogError(f"models[{key}].efforts 必須是陣列")
        efforts_list = []
        seen = set()
        for item in raw_efforts:
            effort = _single_line(item, f"models[{key}].efforts")
            if effort not in EFFORT_RANK:
                raise CatalogError(f"models[{key}].efforts 包含無效值：{effort}")
            if effort in seen:
                raise CatalogError(f"models[{key}].efforts 包含重複值：{effort}")
            seen.add(effort)
            efforts_list.append(effort)
        if status != "refused" and not efforts_list:
            raise CatalogError(f"models[{key}].efforts 不可為空")
        efforts = tuple(efforts_list)

    raw_aliases = raw.get("aliases", [])
    if type(raw_aliases) is not list:
        raise CatalogError(f"models[{key}].aliases 必須是陣列")
    aliases = tuple(_single_line(item, f"models[{key}].aliases") for item in raw_aliases)

    alias_effort = _single_line(raw.get("alias_effort", "card"), f"models[{key}].alias_effort")
    if alias_effort != "card" and alias_effort not in efforts:
        raise CatalogError(f"models[{key}].alias_effort 無效：{alias_effort}")

    note = _single_line(raw["note"], f"models[{key}].note") if "note" in raw else None

    raw_routes = raw.get("routes")
    if type(raw_routes) is not list or not raw_routes:
        raise CatalogError(f"models[{key}].routes 必須是非空陣列")
    routes = tuple(_parse_route(item, model_key=key, status=status, efforts=efforts) for item in raw_routes)

    return ModelSpec(
        key=key, display=display, family=family, status=status, cost=cost, billing=billing,
        efforts=efforts, aliases=aliases, alias_effort=alias_effort, routes=routes, note=note,
    )


def _parse_pick_structure(raw, *, allow_option3, label):
    _check_keys(raw, PICK_KEYS if allow_option3 else OPTION3_PICK_KEYS, label)
    model = _single_line(raw.get("model"), f"{label}.model")
    effort = _single_line(raw.get("effort"), f"{label}.effort")
    if effort not in EFFORT_RANK:
        raise CatalogError(f"{label}.effort 無效：{effort}")
    nested = ()
    if allow_option3 and "option3" in raw:
        raw_nested = raw["option3"]
        if type(raw_nested) is not list:
            raise CatalogError(f"{label}.option3 必須是陣列")
        nested = tuple(_parse_pick_structure(item, allow_option3=False, label=f"{label}.option3")
                       for item in raw_nested)
    return PickSpec(model=model, effort=effort, option3=nested)


def _parse_cell_structure(raw):
    _check_keys(raw, CELL_KEYS, "cells")
    task = _single_line(raw.get("task"), "cells.task")
    if task not in TASKS:
        raise CatalogError(f"cells.task 無效：{task}")
    level = _single_line(raw.get("level"), "cells.level")
    if level not in LEVELS:
        raise CatalogError(f"cells.level 無效：{level}")
    raw_picks = raw.get("picks")
    if type(raw_picks) is not list:
        raise CatalogError(f"cells[{task}.{level}].picks 必須是陣列")
    picks = tuple(_parse_pick_structure(item, allow_option3=True, label=f"cells[{task}.{level}].picks")
                  for item in raw_picks)
    return CellSpec(task=task, level=level, picks=picks)


def _parse_option3_structure(raw):
    if raw is None:
        return {}
    _check_keys(raw, OPTION3_KEYS, "option3")
    parsed = {}
    for key, value in raw.items():
        if type(value) is not list:
            raise CatalogError(f"option3.{key} 必須是陣列")
        parsed[key] = tuple(_parse_pick_structure(item, allow_option3=False, label=f"option3.{key}")
                            for item in value)
    return parsed


def _check_unique_aliases(models):
    seen = {}
    for model in models:
        for alias in model.aliases:
            folded = alias.casefold()
            if folded in seen:
                raise CatalogError(f"別名重複（不分大小寫）：{alias}（{seen[folded]} 與 {model.key}）")
            seen[folded] = model.key


def _validate_picks(picks, models_by_key, *, label, strict, warnings=None):
    valid = []
    for pick in picks:
        model = models_by_key.get(pick.model)
        problem = None
        if model is None:
            problem = f"{label} 引用了不存在的模型 {pick.model}"
        elif model.status != "recommended":
            problem = f"{label} 引用的模型 {pick.model} 狀態為 {model.status}，不是 recommended"
        elif pick.effort not in model.efforts:
            problem = f"{label} 的 effort {pick.effort} 不在模型 {pick.model} 的允許範圍內"
        if problem is not None:
            if strict:
                raise CatalogError(problem)
            if warnings is not None:
                warnings.append(f"本機模型覆寫略過一筆候選：{problem}")
            continue
        nested = _validate_picks(pick.option3, models_by_key, label=f"{label}.option3",
                                 strict=strict, warnings=warnings) if pick.option3 else ()
        valid.append(PickSpec(model=pick.model, effort=pick.effort, option3=nested))
    return tuple(valid)


def _parse_document(path, *, is_local):
    try:
        with Path(path).open("rb") as file:
            data = tomllib.load(file)
    except (OSError, ValueError) as exc:
        raise CatalogError(f"無法讀取 TOML：{exc}") from exc
    _check_keys(data, TOP_LEVEL_KEYS, "catalog")
    version = data.get("schema_version")
    if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
        raise CatalogError(f"不支援的 schema_version：{version!r}")

    updated = ""
    if "updated" in data:
        updated = _single_line(data["updated"], "updated")
    elif not is_local:
        raise CatalogError("缺少 updated 欄位")

    tested_cli = {}
    if "tested_cli" in data:
        raw_cli = data["tested_cli"]
        if type(raw_cli) is not dict:
            raise CatalogError("tested_cli 必須是表格")
        for name, ver in raw_cli.items():
            tested_cli[_single_line(name, "tested_cli key")] = _single_line(ver, f"tested_cli.{name}")

    return data, version, updated, tested_cli


def load_repo_catalog(repo_path=None):
    """Load and strictly validate the maintainer-managed repository catalog. Raises CatalogError on error."""
    path = Path(repo_path) if repo_path is not None else REPO_CATALOG_PATH
    data, version, updated, tested_cli = _parse_document(path, is_local=False)
    routing = _parse_routing(data.get("routing"))

    raw_models = data.get("models")
    if type(raw_models) is not list or not raw_models:
        raise CatalogError("models 必須是非空陣列")
    models = []
    models_by_key = {}
    for item in raw_models:
        model = _parse_model(item)
        if model.key in models_by_key:
            raise CatalogError(f"models.key 重複：{model.key}")
        models_by_key[model.key] = model
        models.append(model)
    _check_unique_aliases(models)

    raw_cells = data.get("cells", [])
    if type(raw_cells) is not list:
        raise CatalogError("cells 必須是陣列")
    cells = []
    seen_cells = set()
    for item in raw_cells:
        cell = _parse_cell_structure(item)
        pair = (cell.task, cell.level)
        if pair in seen_cells:
            raise CatalogError(f"cells 重複定義：{cell.task}.{cell.level}")
        seen_cells.add(pair)
        picks = _validate_picks(cell.picks, models_by_key, label=f"cells[{cell.task}.{cell.level}]", strict=True)
        cells.append(CellSpec(task=cell.task, level=cell.level, picks=picks))

    raw_option3 = _parse_option3_structure(data.get("option3"))
    option3 = {
        key: _validate_picks(raw_option3.get(key, ()), models_by_key, label=f"option3.{key}", strict=True)
        for key in ("save", "upgrade")
    }

    return Catalog(
        schema_version=version, updated=updated, tested_cli=tested_cli, routing=routing,
        models=tuple(models), cells=tuple(cells), option3=option3, local_overrides=False,
    )


def _merge_local_catalog(repo, local_path):
    """Merge an existing local override file onto `repo`. Raises CatalogError if the whole file must be ignored."""
    data, _version, _updated, _tested_cli = _parse_document(local_path, is_local=True)
    routing = _parse_routing(data.get("routing"), base=repo.routing)
    warnings = []

    merged_models = {model.key: model for model in repo.models}
    model_order = [model.key for model in repo.models]
    repo_refused_keys = {model.key for model in repo.models if model.status == "refused"}

    raw_models = data.get("models", [])
    if type(raw_models) is not list:
        raise CatalogError("models 必須是陣列")
    seen_local_keys = set()
    for item in raw_models:
        if type(item) is not dict:
            raise CatalogError("models 項目必須是表格")
        raw_key = item.get("key")
        if type(raw_key) is str and raw_key.strip() in repo_refused_keys:
            key = raw_key.strip()
            if key in seen_local_keys:
                raise CatalogError(f"本機 models.key 重複：{key}")
            seen_local_keys.add(key)
            warnings.append(f"本機模型覆寫已忽略 {key}：不可取代內建目錄中的 refused 模型")
            continue
        model = _parse_model(item)
        if model.key in seen_local_keys:
            raise CatalogError(f"本機 models.key 重複：{model.key}")
        seen_local_keys.add(model.key)
        if model.key not in merged_models:
            model_order.append(model.key)
        merged_models[model.key] = model

    models = tuple(merged_models[key] for key in model_order)
    _check_unique_aliases(models)

    raw_cells = data.get("cells", [])
    if type(raw_cells) is not list:
        raise CatalogError("cells 必須是陣列")
    local_cells = {}
    for item in raw_cells:
        cell = _parse_cell_structure(item)
        pair = (cell.task, cell.level)
        if pair in local_cells:
            raise CatalogError(f"本機 cells 重複定義：{cell.task}.{cell.level}")
        local_cells[pair] = cell

    local_option3 = _parse_option3_structure(data.get("option3"))

    merged_cells = []
    seen_pairs = set()
    for repo_cell in repo.cells:
        pair = (repo_cell.task, repo_cell.level)
        seen_pairs.add(pair)
        if pair in local_cells:
            source = local_cells[pair]
            picks = _validate_picks(source.picks, merged_models,
                                    label=f"cells[{source.task}.{source.level}]", strict=False, warnings=warnings)
        else:
            # Untouched repo cell: silently prune picks whose model was downgraded/refused by local override.
            picks = _validate_picks(repo_cell.picks, merged_models,
                                    label=f"cells[{repo_cell.task}.{repo_cell.level}]", strict=False, warnings=None)
        merged_cells.append(CellSpec(task=pair[0], level=pair[1], picks=picks))
    for pair, source in local_cells.items():
        if pair in seen_pairs:
            continue
        picks = _validate_picks(source.picks, merged_models,
                                label=f"cells[{source.task}.{source.level}]", strict=False, warnings=warnings)
        merged_cells.append(CellSpec(task=pair[0], level=pair[1], picks=picks))

    merged_option3 = {}
    for key in ("save", "upgrade"):
        if key in local_option3:
            merged_option3[key] = _validate_picks(local_option3[key], merged_models,
                                                  label=f"option3.{key}", strict=False, warnings=warnings)
        else:
            merged_option3[key] = _validate_picks(repo.option3.get(key, ()), merged_models,
                                                  label=f"option3.{key}", strict=False, warnings=None)

    catalog = Catalog(
        schema_version=repo.schema_version, updated=repo.updated, tested_cli=dict(repo.tested_cli),
        routing=routing, models=models, cells=tuple(merged_cells), option3=merged_option3,
        local_overrides=True,
    )
    return catalog, warnings


def load_catalog(*, local_path=None, repo_path=None):
    """Load the repo catalog and optional local overrides. Never raises; returns (Catalog|None, warnings)."""
    try:
        repo = load_repo_catalog(repo_path)
    except CatalogError as exc:
        return None, [f"內建模型目錄無法載入：{exc}"]

    if local_path is None:
        return repo, []
    local = Path(local_path)
    if not local.exists():
        return repo, []
    try:
        return _merge_local_catalog(repo, local)
    except CatalogError as exc:
        return repo, [f"本機模型覆寫檔 {local.name} 無效，已整份忽略並改用內建目錄：{exc}"]
