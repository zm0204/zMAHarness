"""Offline feature and tool declaration checks for codex-cli (source-reviewed at 0.169.0)."""
from datetime import UTC, datetime
from hashlib import file_digest, sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
from secrets import token_hex
import subprocess
from stat import FILE_ATTRIBUTE_REPARSE_POINT, S_ISREG
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit

from ..atomic_io import atomic_write
from ..ledger import LedgerError
from ..login_status import CODEX_SUBSCRIPTION
from ..run_manager import _bytes, _read


# All source anchors below refer to openai/codex at rust-v0.169.0.
UPSTREAM = "https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/"
INFRA_FEATURES = {
    "secret_auth_storage": UPSTREAM + "core/src/config/auth_keyring.rs#L105-L117",
}
EXPLAINED_FEATURES = {
    "unified_exec": UPSTREAM + "core/src/tools/spec_plan.rs#L1289-L1319",
    "item_ids": UPSTREAM + "features/src/lib.rs#L2039-L2044",
    "realtime_conversation": UPSTREAM + "features/src/lib.rs#L2327-L2332",
    "resize_all_images": UPSTREAM + "features/src/lib.rs#L2033-L2038",
    "terminal_resize_reflow": UPSTREAM + "features/src/lib.rs#L1389-L1394",
    "tool_search_always_defer_mcp_tools": UPSTREAM + "features/src/lib.rs#L1799-L1804",
    "tui_app_server": UPSTREAM + "features/src/lib.rs#L2345-L2350",
    "worktrees": UPSTREAM + "features/src/lib.rs#L1627-L1632",
}
FEATURE_NAME = r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*"
FEATURE_ROW = re.compile(rf"({FEATURE_NAME})\s+(under development|experimental|stable|deprecated|removed)\s+(true|false)")
WRAPPERS = {"functions.exec", "functions.wait"}
WRAPPER_KINDS = {"exec": "custom", "wait": "function"}
# Tools a model's own metadata can add, allowed only when that model's experimental_supported_tools
# lists them (spec_plan.rs#L1399-L1407 registers them from metadata, past any --disable).
# send_message_to_user_async: the handler only emits an AgentMessage item and returns
# {"accepted":true} (no file, process or network). Its control-tool telemetry carries metadata only.
# On this item's path the only extension hook overriding on_item_completed is the goal extension,
# gated by the Goals feature (always disabled here) and limited to in-memory turn accounting.
EXPLAINED_TOOLS = {
    "functions.send_message_to_user_async": {
        "kind": "function", "metadata": "send_message_to_user_async",
        "sources": [
            UPSTREAM + "core/src/tools/handlers/send_message_to_user_async.rs#L59-L103",
            UPSTREAM + "core/src/tools/control_tool_analytics.rs#L29-L58",
            UPSTREAM + "core/src/session/mod.rs#L3102-L3145",
            UPSTREAM + "app-server/src/extensions.rs#L52-L142",
            UPSTREAM + "ext/extension-api/src/contributors.rs#L249-L270",
            UPSTREAM + "ext/goal/src/extension.rs#L297-L312",
            UPSTREAM + "ext/goal/src/accounting.rs#L175-L215",
        ],
    },
}
CHILD_KINDS = {**WRAPPER_KINDS, **{name.split(".", 1)[1]: spec["kind"] for name, spec in EXPLAINED_TOOLS.items()}}
FEATURES_TIMEOUT = 15
PROBE_TIMEOUT = 25
MODEL_REFRESH_TIMEOUT = 30
MODEL_CACHE_FILE = "models_cache.json"
PROBE_KEY_ENV = "ZMAH_CODEX_PROBE_KEY"
PROBE_STDIN = b"Local protocol probe using synthetic data. Reply with OK. Do not call tools."
PROBE_REVISION = 2
# The CLI keeps its model cache only when client_version equals its own package version.
# https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/models-manager/src/manager.rs#L1006-L1057
CLI_VERSION = re.compile(r"codex-cli (\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)")
MODEL_CACHE_TTL_SECONDS = 300
MODEL_CACHE_MIN_REMAINING_SECONDS = 30
MAX_MODEL_CACHE_BYTES = 8 * 1024 * 1024
_FEATURE_KEYS = frozenset({"code_mode_host", "shell_tool"})


def refuse(reason):
    raise LedgerError(17, f"codex-exec safety preflight: {reason}")


def client_version(cli_version):
    """`codex-cli 0.169.0` -> `0.169.0`. Any other report is refused instead of guessed."""
    match = CLI_VERSION.fullmatch(cli_version) if isinstance(cli_version, str) else None
    if match is None:
        refuse("unrecognized codex --version report")
    return match[1]


def parse_features(raw):
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        refuse("features list is not UTF-8")
    rows = {}
    for line in lines:
        match = FEATURE_ROW.fullmatch(line)
        if match is None or match[1] in rows:
            refuse("unrecognized or duplicate features list row")
        rows[match[1]] = (match[2], match[3] == "true")
    if not _FEATURE_KEYS <= rows.keys():
        refuse("features list is missing shell_tool or code_mode_host")
    return rows


def verify_features(rows):
    if any(rows.get(name, (None, None))[1] is not False for name in ("shell_tool", "code_mode_host")):
        refuse("shell_tool and code_mode_host must both be observed disabled")
    for name, (stage, enabled) in rows.items():
        if enabled is False or name in INFRA_FEATURES:
            continue
        if name == "unified_exec":  # shell_tool was independently checked above.
            continue
        if name not in EXPLAINED_FEATURES or stage != "removed":
            refuse(f"unexpected active feature: {name}")


def isolated_environment(environment, home):
    """Ignore the real profile, proxy variables, and credential variables for diagnostics."""
    allowed = {"COMSPEC", "PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR"}
    env = {name: value for name, value in environment.items() if name.upper() in allowed}
    env["CODEX_HOME"] = str(home)
    for name in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP"):
        env[name] = str(home)
    return env


def feature_profile(executable, config_args, required, environment):
    """Apply the same configuration and disable flags to an isolated features list."""
    with TemporaryDirectory(prefix="zmah-codex-features-") as root:
        cwd = Path(root) / "cwd"
        cwd.mkdir()

        def read_state(disabled):
            with TemporaryDirectory(prefix="home-", dir=root) as home:
                options = [flag for name in disabled for flag in ("--disable", name)]
                argv = [str(executable), *config_args, *options, "features", "list"]
                result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, shell=False,
                                        env=isolated_environment(environment, home), cwd=cwd,
                                        timeout=FEATURES_TIMEOUT)
            if result.returncode != 0:
                refuse("features list failed")
            return parse_features(result.stdout)

        try:
            initial = read_state(())
            if not set(required) <= initial.keys():
                refuse("CLI feature catalogue is missing required feature flags")
            disabled = tuple(dict.fromkeys((*required, *sorted(
                name for name, (_, enabled) in initial.items()
                if enabled and name not in INFRA_FEATURES
            ))))
            rows = read_state(disabled)
        except (OSError, subprocess.TimeoutExpired):
            refuse("features list unavailable")
    if {name: stage for name, (stage, _) in rows.items()} != {
            name: stage for name, (stage, _) in initial.items()}:
        refuse("features list changed between discovery and validation")
    verify_features(rows)
    return disabled, {name: [stage, enabled] for name, (stage, enabled) in sorted(rows.items())}


def verify_subscription(executable, config_args, environment, cwd):
    """Check the formal CLI login locally so a cache alone cannot stand in for a subscription."""
    try:
        result = subprocess.run([str(executable), *config_args, "login", "status"], stdin=subprocess.DEVNULL,
                                capture_output=True, shell=False, env=environment, cwd=cwd,
                                timeout=FEATURES_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        refuse("could not check formal CLI authentication")
    lines = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace").splitlines()
    reports = [line.strip() for line in lines
               if line.strip() == "Not logged in" or line.strip().startswith("Logged in using ")]
    if result.returncode != 0 or reports != [CODEX_SUBSCRIPTION]:
        refuse("formal CLI is not confirmed to be using a ChatGPT subscription")


def verify_no_system_overrides():
    """Managed files can override the catalog/provider beyond --ignore-user-config."""
    # https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/config/src/loader/mod.rs#L843-L874
    if os.name != "nt":
        refuse("this probe profile is verified only on Windows")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\OpenAI\Codex",
                            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            winreg.QueryValueEx(key, "RequirementsPath")
    except FileNotFoundError:
        pass
    except OSError:
        refuse("could not exclude a Windows Codex policy override")
    else:
        refuse("Windows Codex policy may override the model provider or catalog")
    # Follow Windows' registered common application data location as well as
    # Codex's fallback. The environment variable alone need not match it.
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
                            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            common_appdata, _ = winreg.QueryValueEx(key, "Common AppData")
    except FileNotFoundError:
        common_appdata = None
    except OSError:
        refuse("could not resolve Windows Codex's system settings location")
    roots = {Path(value).resolve() for value in (
        common_appdata, os.environ.get("ProgramData"), r"C:\ProgramData") if value}
    for root in roots:
        for name in ("requirements.toml", "config.toml"):
            try:
                (root / "OpenAI" / "Codex" / name).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                refuse("could not inspect Windows Codex's system settings location")
            refuse("system Codex configuration may override the model provider or catalog")


def metadata_tools(entry):
    tools = entry.get("experimental_supported_tools", [])
    if not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools):
        refuse("model metadata has an unrecognized experimental_supported_tools list")
    return {name for name, spec in EXPLAINED_TOOLS.items() if spec["metadata"] in tools}


def approved_tool_set(names, explained, host_enabled, disable_in_process_fallback):
    """Empty or exec/wait (backend disabled), plus explained tools the model's metadata declares."""
    base = set(names) - set(explained)
    if base and (base != WRAPPERS or host_enabled is not False or disable_in_process_fallback is not False):
        return False
    return set(names) - WRAPPERS <= set(explained)


def declared_tools(request, model, host_enabled, disable_in_process_fallback, explained=frozenset()):
    if (not isinstance(request, dict) or request.get("model") != model
            or "additional_tools" in request):
        refuse("probe request model is missing or does not match")
    items = request.get("input")
    if not isinstance(items, list) or not items:
        refuse("probe request input is missing or unrecognized")
    declarations = [request.get("tools", [])]
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            refuse("unrecognized probe request input item")
        if item["type"] == "additional_tools" and "additional_tools" not in item:
            declarations.append(item.get("tools"))
        elif item["type"] != "message" or "tools" in item or "additional_tools" in item:
            refuse("unrecognized probe request input item")
    names = set()
    for group in declarations:
        if not isinstance(group, list):
            refuse("unrecognized tool declaration list")
        for tool in group:
            if not isinstance(tool, dict):
                refuse("unrecognized tool declaration")
            kind, name = tool.get("type"), tool.get("name")
            if kind == "namespace" and name == "functions":
                functions = tool.get("tools")
                if not isinstance(functions, list) or not functions:
                    refuse("unrecognized functions namespace")
                for function in functions:
                    if not isinstance(function, dict):
                        refuse("unrecognized declaration in functions namespace")
                    child = function.get("name")
                    if (not isinstance(child, str) or child not in CHILD_KINDS
                            or function.get("type") != CHILD_KINDS[child] or "tools" in function):
                        extra = " " + child if isinstance(child, str) and re.fullmatch(FEATURE_NAME, child) else ""
                        refuse("unexpected declaration in functions namespace" + extra)
                    names.add("functions." + child)
            elif (isinstance(name, str) and (name in WRAPPERS or name in EXPLAINED_TOOLS)
                  and kind == CHILD_KINDS[name.split(".")[1]] and "tools" not in tool):
                names.add(name)
            else:
                refuse("unexpected tool declaration")
    # ThreadManager chooses the disabled provider only when both inputs are false.
    # https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/thread_manager.rs#L674-L684
    if not approved_tool_set(names, explained, host_enabled, disable_in_process_fallback):
        refuse("tool set or code-mode backend differs from the approved profile")
    return sorted(names)


def file_sha256(path):
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest()


def strict_json(data):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON key")
        return result

    def invalid_number(_):
        raise ValueError("non-finite JSON number")

    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid_number)


def model_entry(catalog, model):
    models = catalog["models"]
    if not isinstance(models, list) or any(not isinstance(item, dict) for item in models):
        raise ValueError("invalid model list")
    matches = [entry for entry in models if entry.get("slug") == model]
    if len(matches) != 1:
        raise ValueError("missing or duplicate requested model")
    return matches[0]


def read_model_cache(home, model, version, now):
    # Codex reuses the subscription cache only with matching identity and version,
    # and may refresh once it expires. Bind a fresh snapshot, with room for startup.
    # https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/models-manager/src/manager.rs#L1006-L1057
    path = home / MODEL_CACHE_FILE
    info = path.lstat()
    if (not S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
            or info.st_size > MAX_MODEL_CACHE_BYTES):
        raise ValueError("model cache is not a bounded regular file")
    with path.open("rb") as file:
        raw = file.read(MAX_MODEL_CACHE_BYTES + 1)
    if len(raw) > MAX_MODEL_CACHE_BYTES:
        raise ValueError("model cache exceeds size limit")
    cache = strict_json(raw)
    if (not isinstance(cache, dict) or set(cache) - {"models", "client_version", "identity", "etag", "fetched_at"}
            or cache.get("client_version") != version
            or re.fullmatch(r"[0-9a-f]{64}", cache["identity"]) is None):
        raise ValueError("unrecognized model cache")
    age = (now - datetime.fromisoformat(cache["fetched_at"])).total_seconds()
    if not 0 <= age < MODEL_CACHE_TTL_SECONDS - MODEL_CACHE_MIN_REMAINING_SECONDS:
        raise ValueError("model cache too old or future dated")
    return raw, model_entry(cache, model)


def subscription_model(model, environment, version, *, now=None):
    hint = next((value for key, value in environment.items() if key.upper() == "CODEX_HOME" and value.strip()), None)
    home = Path(hint) if hint is not None else Path.home() / ".codex"
    try:
        raw, cached = read_model_cache(home, model, version, now or datetime.now(UTC))
    except (KeyError, TypeError, ValueError, OverflowError, OSError, RecursionError):
        refuse("cannot verify a fresh model cache for this Codex version and model")
    return raw, {"source": "subscription_models_cache", "source_cache_sha256": sha256(raw).hexdigest(),
                 "model_entry_sha256": sha256(_bytes(cached)).hexdigest()}


def refresh_model_cache(executable, environment, cwd):
    """Refresh CODEX_HOME/models_cache.json with `codex debug models` (network, no inference).

    Verified on 0.169.0: the command rewrites the cache (new fetched_at). It reads the formal profile;
    it does not accept --ignore-user-config, so a non-default provider in config.toml can make the
    formal run refetch, which is a documented residual risk.
    https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/cli/src/main.rs#L2235-L2273
    """
    try:
        result = subprocess.run([str(executable), "debug", "models"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
                                env=environment, cwd=cwd, timeout=MODEL_REFRESH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        refuse("could not refresh the model cache")
    if result.returncode != 0:
        refuse("could not refresh the model cache")


def fresh_subscription_model(executable, model, environment, version, cwd):
    """Use the formal cache when it is fresh; otherwise refresh it once and read it again.

    Launch runs this again seconds after prepare. A fresh cache is not refreshed, so its bytes, and
    the hash recorded at prepare, stay the same unless another Codex process rewrites it meanwhile.
    """
    try:
        return subscription_model(model, environment, version)
    except LedgerError:
        refresh_model_cache(executable, environment, cwd)
    return subscription_model(model, environment, version)


def capture_declarations(argv, schema, cache, environment, model, host_enabled, fallback):
    """Run the approved vector with probe-only paths, prompt, key and a local provider."""
    requests = []
    model_requests = []
    violations = []
    fake_key = token_hex(32)
    try:
        entry = model_entry(strict_json(cache), model)
        model_digest = sha256(_bytes(entry)).hexdigest()
    except (KeyError, TypeError, ValueError, RecursionError):
        refuse("probe model metadata is invalid")
    explained = metadata_tools(entry)
    with TemporaryDirectory(prefix="zmah-codex-tools-") as directory:
        root = Path(directory)
        home, cwd = root / "home", root / "cwd"
        home.mkdir()
        cwd.mkdir()
        cache_file = home / MODEL_CACHE_FILE
        cache_file.write_bytes(cache)
        copied_digest = file_sha256(cache_file)
        if copied_digest != sha256(cache).hexdigest():
            refuse("probe model cache copy does not match the source")
        schema_file = root / "schema.json"
        schema_file.write_bytes(schema)

        class Stub(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *_):
                pass

            def _reply(self, status, media_type, payload):
                self.send_response(status)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                url = urlsplit(self.path)
                if (url.path != "/v1/models" or url.netloc or url.scheme or url.fragment
                        or self.headers.get("Authorization") != "Bearer " + fake_key):
                    violations.append("unexpected GET")
                    self._reply(404, "text/plain", b"not found")
                    return
                self._reply(200, "application/json", cache)
                model_requests.append(("/v1/models", sha256(cache).hexdigest()))

            def do_POST(self):
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if (model_requests != [("/v1/models", copied_digest)]
                            or self.path not in ("/v1/responses", "/v1/responses/lite")
                            or self.headers.get("Authorization") != "Bearer " + fake_key
                            or self.headers.get_content_type() != "application/json"
                            or not 0 < size <= 2_000_000):
                        raise ValueError("unexpected request envelope")
                    data = self.rfile.read(size)
                    if len(data) != size:
                        raise ValueError("incomplete request")
                    requests.append((self.path, strict_json(data)))
                except (OSError, ValueError, RecursionError):
                    violations.append("unrecognized POST")
                    self._reply(400, "text/plain", b"invalid probe request")
                    return
                completed = _bytes({"type": "response.completed", "response": {
                    "id": "offline-probe", "status": "completed",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                }})
                self._reply(200, "text/event-stream", b"event: response.completed\ndata: " + completed + b"\n\n")

        try:
            with HTTPServer(("127.0.0.1", 0), Stub) as server:
                env = isolated_environment(environment, home)
                env.update({PROBE_KEY_ENV: fake_key, "NO_PROXY": "127.0.0.1"})
                provider = "model_providers.zmah_probe"
                base = f"http://127.0.0.1:{server.server_port}/v1"
                # CLI 0.169.0 skips /models for API-key auth when api_key_model_discovery
                # is disabled. Command auth is provider-local and keeps the same feature
                # flags as the Worker. Its helper reads only the probe child's fake key.
                credential = ["-I", "-S", "-B", "-c", f"import os; print(os.environ[{PROBE_KEY_ENV!r}])"]
                changes = [
                    'model_provider="zmah_probe"', f'{provider}.name="OpenAI"',
                    f'{provider}.base_url={json.dumps(base)}',
                    f'{provider}.model_catalog_url={json.dumps(base + "/models")}',
                    f'{provider}.wire_api="responses"',
                    f'{provider}.auth.command={json.dumps(sys.executable)}',
                    f'{provider}.auth.args={json.dumps(credential)}',
                    f'{provider}.auth.cwd={json.dumps(str(cwd))}',
                    f"{provider}.request_max_retries=0", f"{provider}.stream_max_retries=0",
                ]
                command = list(argv)
                for flag, value in (("-C", cwd), ("--output-schema", schema_file), ("-o", root / "last.json")):
                    command[command.index(flag) + 1] = str(value)
                command[-1:-1] = [value for setting in changes for value in ("-c", setting)]
                thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
                thread.start()
                try:
                    result = subprocess.run(command, input=PROBE_STDIN, capture_output=True, shell=False,
                                            env=env, cwd=cwd, timeout=PROBE_TIMEOUT)
                finally:
                    server.shutdown()
                    thread.join()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            refuse("could not complete localhost probe")
        if (result.returncode != 0 or violations or len(requests) != 1
                or model_requests != [("/v1/models", copied_digest)]):
            refuse("expected a localhost /models fetch followed by one successful inference-shaped request")
        endpoint, request = requests[0]
        return {"tools": declared_tools(request, model, host_enabled, fallback, explained),
                "metadata_tools": sorted(explained), "endpoint": endpoint,
                "models_endpoint": model_requests[0][0], "copy_sha256": copied_digest,
                "served_cache_sha256": model_requests[0][1], "model_entry_sha256": model_digest}


def cached_declarations(state, inputs, observe):
    # Use a stable key and seal the observation as well as the key fields. Per-run metadata
    # also embeds this record so later edits to the cache cannot change recorded evidence.
    inputs = {"probe_revision": PROBE_REVISION, **inputs}
    key = sha256(_bytes(inputs)).hexdigest()
    directory = state / "codex-probe"
    directory.mkdir(exist_ok=True)
    if directory.is_symlink() or directory.resolve().parent != state.resolve():
        refuse("tool probe cache must stay in state")
    path = directory / f"{key}.json"

    def valid_observation(observation):
        explained = observation["metadata_tools"]
        return (isinstance(explained, list) and set(explained) <= EXPLAINED_TOOLS.keys()
                and isinstance(observation["tools"], list) and observation["tools"] == sorted(observation["tools"])
                and approved_tool_set(observation["tools"], explained, False, False)
                and observation["copy_sha256"] == observation["served_cache_sha256"]
                == inputs["model_metadata_sha256"]
                and observation["model_entry_sha256"] == inputs["model_entry_sha256"]
                and observation["models_endpoint"] == "/v1/models"
                and observation["endpoint"] in ("/v1/responses", "/v1/responses/lite"))

    try:
        if path.lstat().st_size > 16_384:
            raise ValueError("probe record too large")
        saved = strict_json(_read(path))
        observation = saved["observation"]
        if (saved["key_sha256"] == key and saved["inputs"] == inputs
                and valid_observation(observation)
                and saved["observation_sha256"] == sha256(_bytes(observation)).hexdigest()
                and set(saved) == {"key_sha256", "inputs", "observation", "observation_sha256"}):
            return saved
    except (KeyError, TypeError, ValueError, OSError, RecursionError):
        pass
    observation = observe()
    try:
        if not valid_observation(observation):
            raise ValueError("mismatched proof")
    except (KeyError, TypeError, ValueError):
        refuse("model and tool evidence does not match the probe cache key")
    result = {"key_sha256": key, "inputs": inputs, "observation": observation,
              "observation_sha256": sha256(_bytes(observation)).hexdigest()}
    atomic_write(path, _bytes(result))
    return result
