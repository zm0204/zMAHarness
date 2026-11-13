"""Load the local Harness settings using the Python standard library."""
from dataclasses import dataclass
import os
from pathlib import Path
import tomllib

from .adapters.registry import configurable_names
from .ledger import BRAND_NAME, EFFORT_LEVELS, Limits

DEFAULTS = {
    "paths": {"state_dir": "state", "worktree_root": "worktrees", "gui_workspace_root": "gui"},
    "adapters": {"enabled": ["codex-exec", "gui-terra", "agy-print"], "codex_command": "codex", "agy_command": "agy"},
    "approval": {"mode": "codex_prompt", "grant_ttl_minutes": 240},
    "controller": {"light_models": [], "suggested_controller": "Codex Sol High"},
    "limits": {"max_dispatches_per_grant": 5, "max_concurrency": 1, "max_effort": "high",
               "max_task_bytes": 32768, "max_result_summary_chars": 2000},
    "defaults": {
        **{template: {"adapter": "agy-print", "model": "gemini-3.8-flash-low", "effort": "low"}
           for template in ("review", "second_opinion", "research")},
        "delegate_implement": {"adapter": "codex-exec", "model": "", "effort": "medium"},
    },
    "triggers": {"t1_failed_fix_rounds": 2, "t2_risky_diff_lines": 80,
                 "t2_risky_paths": ["auth", "security", "migration", "permission"],
                 "t3_read_files": 15, "t3_read_kb": 200, "t4_codex_used_percent": 70},
    "verify": {"allowed_command_prefixes": ["pytest", "npm test", "npm run test", "dotnet test"],
               "timeout_s": 900},
    "privacy": {"retain_run_dirs_days": 30},
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    path: Path
    values: dict
    warnings: tuple[str, ...]

    @property
    def database(self):
        return self.values["paths"]["state_dir"] / "ledger.sqlite3"

    @property
    def ledger_limits(self):
        limits = self.values["limits"]
        return Limits(limits["max_dispatches_per_grant"],
                      self.values["approval"]["grant_ttl_minutes"], limits["max_effort"])


def default_config_path():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    current = base / BRAND_NAME / "harness.local.toml"
    legacy = base / "MultiAgentHarness" / "harness.local.toml"
    return legacy if not current.exists() and legacy.is_file() else current


def _merge(defaults, supplied, prefix=""):
    if type(supplied) is not dict:
        raise ConfigError(f"{prefix.rstrip('.')} 必須是 TOML 表格")
    for key in supplied:
        if key not in defaults:
            raise ConfigError(f"未知的設定欄位 {prefix}{key}")
    values = {}
    for key, default in defaults.items():
        name = prefix + key
        value = supplied.get(key, default)
        if isinstance(default, dict):
            value = _merge(default, value, name + ".")
        elif type(value) is not type(default):
            raise ConfigError(f"{name} 的值類型不正確")
        elif isinstance(value, str):
            optional_model = prefix.startswith("defaults.") and key == "model"
            if (value != value.strip() or (not value and not optional_model)
                    or any(character in value for character in ("\r", "\n", "\0"))):
                raise ConfigError(f"{name} 必須是有效的單行文字")
        elif isinstance(value, list):
            if any(type(item) is not str or not item.strip() or
                   any(character in item for character in ("\r", "\n", "\0")) for item in value):
                raise ConfigError(f"{name} 必須是單行文字陣列")
            value = value.copy()
        elif isinstance(value, int):
            valid = 0 <= value <= 100 if name == "triggers.t4_codex_used_percent" else value > 0
            if not valid:
                raise ConfigError(f"{name} 超出允許範圍")
        values[key] = value
    return values


def _local_path(path, name):
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ConfigError(f"{name} 必須位於 Git 工作區外")
    synced_names = ("onedrive", "dropbox", "google drive", "googledrive", "icloud")
    if any(part.casefold().startswith(synced_names) for part in path.parts):
        raise ConfigError(f"{name} 位於已知的雲端同步路徑")
    for key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        if os.environ.get(key) and path.is_relative_to(Path(os.environ[key]).resolve()):
            raise ConfigError(f"{name} 位於已知的雲端同步路徑")
    return path


def load_config(path=None):
    config_path = (Path(path) if path is not None else default_config_path()).expanduser().resolve()
    try:
        with config_path.open("rb") as file:
            supplied = tomllib.load(file)
    except FileNotFoundError:
        if path is not None:
            raise ConfigError("找不到指定的 TOML 設定檔") from None
        supplied = {}
    except (OSError, ValueError) as exc:
        raise ConfigError("無法讀取設定檔，請檢查 UTF-8 與 TOML 語法") from exc

    values = _merge(DEFAULTS, supplied)
    if values["approval"]["mode"] not in ("manual_cli", "codex_prompt"):
        raise ConfigError("approval.mode 須為 manual_cli 或 codex_prompt")
    limits = values["limits"]
    if limits["max_effort"] not in EFFORT_LEVELS:
        raise ConfigError("limits.max_effort 無效")
    adapters = set(configurable_names())
    enabled = values["adapters"]["enabled"]
    if any(adapter not in adapters for adapter in enabled) or len(set(enabled)) != len(enabled):
        raise ConfigError("adapters.enabled 有未知或重複的 Adapter")
    for template, defaults in values["defaults"].items():
        if defaults["adapter"] not in adapters or defaults["effort"] not in EFFORT_LEVELS:
            raise ConfigError(f"defaults.{template} 的 Adapter 或思考程度無效")
        if (defaults["adapter"] != DEFAULTS["defaults"][template]["adapter"]
                and "model" not in supplied.get("defaults", {}).get(template, {})):
            defaults["model"] = ""
    for name, value in values["paths"].items():
        resolved = (config_path.parent / Path(value).expanduser()).resolve()
        values["paths"][name] = _local_path(resolved, f"paths.{name}") if name != "gui_workspace_root" else resolved
    config = Config(config_path, values, ("limits.max_concurrency 已固定為 1",)
                    if limits["max_concurrency"] > 1 else ())
    limits["max_concurrency"] = 1
    database = config.database
    if database.resolve() != database or database.is_symlink():
        raise ConfigError("Ledger 資料庫路徑不得指向其他位置")
    return config
