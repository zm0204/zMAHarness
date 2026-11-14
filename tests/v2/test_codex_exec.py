"""What codex-exec provides on top of the shared CLI Worker base. No real model is called."""
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, ProxyHandler, build_opener
from uuid import uuid4

from mah.adapters import codex_probe, registry
from mah.adapters.agy_print import _response_schema
from mah.adapters.base import Adapter, AdapterPolicy
from mah.adapters.codex_exec import (DISABLED_FEATURES, FIXED_CONFIG, LAST_MESSAGE_FILE, MISSING_EXECUTABLE,
                                     SCHEMA_FILE, STRICT_UNSUPPORTED, TOOL_ITEM_OBSERVED, CodexExecAdapter,
                                     codex_environment, resolve_codex_executable, strict_output_schema)
from mah.ledger import LedgerError
from mah.verifier import VerificationEvidence, Verifier

from test_cli_worker import WorkerFixture

# Speaks `codex exec --json`: JSONL events on stdout and the final message in the -o file.
CODEX_STAND_IN = r"""
import json, re, sys, uuid
from pathlib import Path
argv = sys.argv[1:]
assert argv[0] == "exec" and argv[-1] == "-"
prompt = sys.stdin.buffer.read().decode("utf-8")
scenario = re.search(r"scenario=(\w+)", prompt)[1]
schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8"))
last = Path(argv[argv.index("-o") + 1])
run_id = schema["properties"]["run_id"]["enum"][0]
template = schema["properties"]["template"]["enum"][0]
result = dict(schema_version="2.0", run_id=run_id, template=template, status="completed", summary="synthetic review",
              blocked_reason=None, open_questions=[], template_payload={"findings": []})
text = json.dumps(result, ensure_ascii=False)
thread = str(uuid.uuid4())
events = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.started"}]
message = text if scenario != "conflict" else json.dumps(dict(result, summary="different"), ensure_ascii=False)
events.append({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": message}})
events.append({"type": "turn.completed", "usage": {"input_tokens": 9, "cached_input_tokens": 0, "output_tokens": 4}})
if scenario == "corrupt_last":
    last.write_text("not json", encoding="utf-8")
elif scenario != "missing_last":
    last.write_text(text, encoding="utf-8")
sys.stdout.write("\n".join(json.dumps(event) for event in events) + "\n")
"""


def write(path, data=b"synthetic"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class CodexPolicyTests(unittest.TestCase):
    def test_protocol_policy_and_registration(self):
        self.assertIsInstance(CodexExecAdapter(None), Adapter)
        policy = registry.policy_for("codex-exec")
        expected = {"user_configurable": True, "requires_dispatch_scope": True, "attended_approval_only": True,
                    "template_verified": True, "run_id_prefix": "codex"}
        for field in fields(AdapterPolicy):
            with self.subTest(flag=field.name):
                default = None if field.name == "run_id_prefix" else False
                self.assertEqual(getattr(policy, field.name), expected.get(field.name, default))
        self.assertEqual(registry.capture_reason("codex-exec"), "codex_capture_integrity")
        self.assertEqual(registry.capture_reason("agy-print"), "agy_capture_integrity")
        self.assertEqual(registry.capture_reason("claude-print"), "claude_capture_integrity")
        self.assertEqual(registry.executable_missing_reason("codex-exec"), MISSING_EXECUTABLE)


class CodexExecutableTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="mah-codex-exe-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def test_absolute_exe_is_used_and_shims_are_refused(self):
        native = write(self.root / "custom/codex.exe")
        self.assertEqual(resolve_codex_executable(str(native)), native.resolve())
        for name in ("codex.cmd", "codex.bat", "codex.ps1", "codex"):
            with self.subTest(name=name):
                self.assertIsNone(resolve_codex_executable(str(write(self.root / "shims" / name))))
        self.assertIsNone(resolve_codex_executable(str(self.root / "missing/codex.exe")))

    def test_path_lookup_exe_and_npm_layouts(self):
        native = write(self.root / "bin/codex.exe")
        with patch("mah.adapters.codex_exec.which", return_value=str(native)):
            self.assertEqual(resolve_codex_executable("codex"), native.resolve())
        for package, triple in (("codex-win32-x64", "x86_64-pc-windows-msvc"),
                                ("codex-win32-arm64", "aarch64-pc-windows-msvc")):
            npm = self.root / package
            exe = write(npm / "node_modules/@openai/codex/node_modules/@openai" / package / "vendor" / triple
                        / "bin/codex.exe")
            for shim in ("codex.cmd", "codex.ps1", "codex"):
                with self.subTest(package=package, shim=shim), patch(
                        "mah.adapters.codex_exec.which", return_value=str(write(npm / shim))):
                    self.assertEqual(resolve_codex_executable("codex"), exe.resolve())

    def test_unknown_or_incomplete_shims_are_not_found(self):
        for shim in ("codex.cmd", "other.cmd", "codex.bat"):
            with self.subTest(shim=shim), patch("mah.adapters.codex_exec.which",
                                                return_value=str(write(self.root / "empty" / shim))):
                self.assertIsNone(resolve_codex_executable("codex"))

    def test_relative_command_resolves_next_to_the_local_toml(self):
        native = write(self.root / "tools/codex.exe")
        config = SimpleNamespace(path=self.root / "harness.local.toml",
                                 values={"adapters": {"codex_command": "tools/codex.exe"},
                                         "limits": {"max_task_bytes": 2048}})
        adapter = CodexExecAdapter.from_config(None, config)
        self.assertEqual((adapter.resolve_executable(), adapter.max_task_bytes), (native.resolve(), 2048))
        with self.assertRaises(LedgerError) as error:
            CodexExecAdapter(None, command=str(self.root / "codex.cmd")).resolve_executable()
        self.assertEqual((error.exception.code, str(error.exception)), (14, MISSING_EXECUTABLE))


class CodexEnvironmentTests(unittest.TestCase):
    def test_openai_and_codex_session_variables_are_not_passed(self):
        extra = {"OPENAI_API_KEY": "sk-synthetic", "OPENAI_BASE_URL": "https://example.invalid",
                 "openai_org": "lower", "CODEX_API_KEY": "synthetic", "CODEX_SANDBOX": "seatbelt",
                 "CODEX_THREAD_ID": "thread", "CODEX_SANDBOX_NETWORK_DISABLED": "1",
                 "CODEX_HOME": r"C:\synthetic\codex", "PATH": "synthetic"}
        with patch.dict(os.environ, extra):
            environment = codex_environment()
        self.assertFalse([key for key in environment if key.upper().startswith("OPENAI_")])
        self.assertEqual([key for key in environment if key.upper().startswith("CODEX_")], ["CODEX_HOME"])
        self.assertEqual((environment["CODEX_HOME"], environment["PATH"]), (r"C:\synthetic\codex", "synthetic"))


class CodexProbeTests(unittest.TestCase):
    wrappers = [{"type": "namespace", "name": "functions", "tools": [
        {"type": "custom", "name": "exec"}, {"type": "function", "name": "wait"}]}]
    states = {
        "shell_tool": ("stable", False), "code_mode_host": ("experimental", False),
        "unified_exec": ("stable", True), "secret_auth_storage": ("stable", True),
    }

    def test_feature_parser_and_explained_rows(self):
        rows = {**self.states, **{name: ("removed", True) for name in (
            "item_ids", "realtime_conversation", "resize_all_images", "terminal_resize_reflow",
            "tool_search_always_defer_mcp_tools", "tui_app_server", "worktrees")}}
        text = "\n".join(f"{name}  {stage}   {str(enabled).lower()}" for name, (stage, enabled) in rows.items())
        self.assertEqual(codex_probe.parse_features(text.encode()), rows)
        codex_probe.verify_features(rows)
        for name in set(rows) - {"secret_auth_storage"}:
            stage, enabled = rows[name]
            modified = {**rows, name: ("stable", True) if stage == "removed" else (stage, not enabled)}
            if name == "unified_exec":
                modified["shell_tool"] = ("stable", True)
            with self.subTest(feature=name), self.assertRaises(LedgerError) as error:
                codex_probe.verify_features(modified)
            self.assertEqual(error.exception.code, 17)
        for raw in (b"", b"something  unknown  false", b"something  stable  true\nsomething  stable  false",
                    b"something  stable  auto", b"\xff"):
            with self.subTest(raw=raw), self.assertRaises(LedgerError):
                codex_probe.parse_features(raw)
        with self.assertRaises(LedgerError):
            codex_probe.verify_features({**rows, "future_tool": ("stable", True)})

    def test_probe_checks_both_tool_locations_and_backend(self):
        empty = {"model": "synthetic", "input": [{"type": "message"}], "tools": []}
        self.assertEqual(codex_probe.declared_tools(empty, "synthetic", True, True), [])
        for request in (dict(empty, tools=self.wrappers),
                        dict(empty, input=[{"type": "additional_tools", "tools": self.wrappers}])):
            self.assertEqual(codex_probe.declared_tools(request, "synthetic", False, False),
                             ["functions.exec", "functions.wait"])
            for host, fallback in ((True, False), (False, True), (None, False), (False, None)):
                with self.subTest(host=host, fallback=fallback), self.assertRaises(LedgerError):
                    codex_probe.declared_tools(request, "synthetic", host, fallback)
        for changed in (dict(empty, model="other"), dict(empty, tools=None), dict(empty, input="wrong"),
                        dict(empty, input=[{"type": "future_input"}]),
                        dict(empty, input=[{"type": "additional_tools"}]),
                        dict(empty, tools=[{"type": "function", "name": "exec"}]),
                        dict(empty, tools=self.wrappers, input=[{"type": "additional_tools", "tools": [
                            {"type": "web_search"}]}]),
                        dict(empty, input=[{"type": "additional_tools", "tools": [{
                            **self.wrappers[0], "tools": [*self.wrappers[0]["tools"],
                                                        {"type": "function", "name": "send_message_to_user_async"}],
                        }]}])):
            with self.subTest(changed=changed), self.assertRaises(LedgerError) as error:
                codex_probe.declared_tools(changed, "synthetic", False, False)
            self.assertEqual(error.exception.code, 17)

    def test_explained_tool_is_allowed_only_when_model_metadata_declares_it(self):
        send = {"type": "function", "name": "send_message_to_user_async"}
        with_send = {"model": "synthetic", "input": [{"type": "message"}], "tools": [
            {**self.wrappers[0], "tools": [*self.wrappers[0]["tools"], send]}]}
        declared = codex_probe.metadata_tools(
            {"experimental_supported_tools": ["clock", "send_message_to_user_async"]})
        self.assertEqual(declared, {"functions.send_message_to_user_async"})
        self.assertEqual(codex_probe.declared_tools(with_send, "synthetic", False, False, declared),
                         ["functions.exec", "functions.send_message_to_user_async", "functions.wait"])
        # The similarly named send_user_message_async (Terra and others) does not unlock it.
        for metadata in ({"experimental_supported_tools": ["send_user_message_async", "clock"]},
                         {"experimental_supported_tools": []}, {}):
            with self.subTest(metadata=metadata), self.assertRaises(LedgerError) as error:
                codex_probe.declared_tools(with_send, "synthetic", False, False, codex_probe.metadata_tools(metadata))
            self.assertEqual(error.exception.code, 17)
        for refused in ((with_send, True, False), (with_send, False, True)):
            with self.subTest(backend=refused[1:]), self.assertRaises(LedgerError):
                codex_probe.declared_tools(*refused[:1], "synthetic", *refused[1:], declared)
        wrong_kind = dict(with_send, tools=[{**self.wrappers[0], "tools": [
            *self.wrappers[0]["tools"], dict(send, type="custom")]}])
        extra = dict(with_send, tools=[{**self.wrappers[0], "tools": [
            *self.wrappers[0]["tools"], send, {"type": "function", "name": "request_user_input_async"}]}])
        for request in (wrong_kind, extra):
            with self.assertRaises(LedgerError):
                codex_probe.declared_tools(request, "synthetic", False, False, declared)
        for metadata in ({"experimental_supported_tools": "send_message_to_user_async"},
                         {"experimental_supported_tools": [1]}):
            with self.assertRaises(LedgerError):
                codex_probe.metadata_tools(metadata)
        for name, spec in codex_probe.EXPLAINED_TOOLS.items():
            self.assertTrue(spec["sources"])
            self.assertTrue(all(link.startswith(codex_probe.UPSTREAM) for link in spec["sources"]))

    def test_cached_observation_with_explained_tool_must_match_its_metadata(self):
        fields = dict(executable_sha256="exe", model="a", effort="low", profile_sha256="profile",
                      model_metadata_sha256="metadata", model_entry_sha256="entry")
        base = {"copy_sha256": "metadata", "served_cache_sha256": "metadata", "model_entry_sha256": "entry",
                "models_endpoint": "/v1/models", "endpoint": "/v1/responses"}
        tools = ["functions.exec", "functions.send_message_to_user_async", "functions.wait"]
        with TemporaryDirectory() as directory:
            accepted = codex_probe.cached_declarations(Path(directory), fields, Mock(return_value={
                **base, "tools": tools, "metadata_tools": ["functions.send_message_to_user_async"]}))
            self.assertEqual(accepted["observation"]["tools"], tools)
            with self.assertRaises(LedgerError):
                codex_probe.cached_declarations(Path(directory), dict(fields, model="b"), Mock(return_value={
                    **base, "tools": tools, "metadata_tools": []}))

    def test_feature_probe_uses_fresh_homes_and_same_config_overrides(self):
        initial = b"shell_tool stable true\ncode_mode_host experimental true\nnew_flag stable true"
        effective = initial.replace(b"true", b"false")
        homes = []

        def fake_run(argv, **kwargs):
            home = Path(kwargs["env"]["CODEX_HOME"])
            self.assertEqual(list(home.iterdir()), [])
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertFalse(kwargs["shell"])
            self.assertEqual(argv[1:3], ["-c", "agents.enabled=false"])
            self.assertEqual(argv[-2:], ["features", "list"])
            homes.append(home)
            return SimpleNamespace(returncode=0, stdout=initial if len(homes) == 1 else effective)

        with patch.object(codex_probe.subprocess, "run", side_effect=fake_run):
            disabled, observed = codex_probe.feature_profile("synthetic.exe", ["-c", "agents.enabled=false"],
                                                            ("shell_tool", "code_mode_host"),
                                                            {"OPENAI_API_KEY": "never-inherit"})
        self.assertEqual(disabled, ("shell_tool", "code_mode_host", "new_flag"))
        self.assertEqual(observed["new_flag"], ["stable", False])
        self.assertEqual(len(set(homes)), 2)

    def test_http_probe_uses_only_synthetic_input_and_a_child_only_key(self):
        schema = b'{"type":"object"}'
        cache = b'{"models":[{"slug":"synthetic"}]}'
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_real_home = root / "formal"
            write(fake_real_home / "auth.json", b"must-never-be-read")
            write(fake_real_home / "config.toml", b"must-never-be-read")
            request_json = {"model": "synthetic", "input": [{"type": "message"}], "tools": self.wrappers}

            def fake_run(argv, **kwargs):
                isolated = Path(kwargs["env"]["CODEX_HOME"])
                self.assertNotEqual(isolated, fake_real_home)
                self.assertEqual([file.name for file in isolated.iterdir()], ["models_cache.json"])
                self.assertEqual((isolated / "models_cache.json").read_bytes(), cache)
                self.assertEqual(list(kwargs["cwd"].iterdir()), [])
                self.assertEqual(Path(argv[argv.index("--output-schema") + 1]).read_bytes(), schema)
                self.assertEqual(kwargs["input"], codex_probe.PROBE_STDIN)
                self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
                self.assertNotIn(codex_probe.PROBE_KEY_ENV, codex_environment())
                self.assertNotIn("model_catalog_json", " ".join(argv))
                endpoint = next(json.loads(item.split("=", 1)[1]) for item in argv
                                if item.startswith("model_providers.zmah_probe.base_url="))
                self.assertTrue(endpoint.startswith("http://127.0.0.1:"))
                request = Request(endpoint + "/responses", data=json.dumps(request_json).encode(), headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + kwargs["env"][codex_probe.PROBE_KEY_ENV],
                })
                opener = build_opener(ProxyHandler({}))
                models = Request(endpoint + "/models?client_version=0.169.0", headers={
                    "Authorization": request.get_header("Authorization"),
                })
                with opener.open(models, timeout=5) as response:
                    self.assertEqual(response.read(), cache)
                with opener.open(request, timeout=5) as response:
                    self.assertIn(b"response.completed", response.read())
                return SimpleNamespace(returncode=0)

            argv = ("synthetic.exe", "exec", "-s", "read-only", "-C", "original-cwd", "-m", "synthetic",
                    "--output-schema", "original-schema", "-o", "original-output", "-")
            environment = {"CODEX_HOME": str(fake_real_home), "OPENAI_API_KEY": "never-inherit"}
            with patch.object(codex_probe.subprocess, "run", side_effect=fake_run):
                evidence = codex_probe.capture_declarations(argv, schema, cache, environment, "synthetic",
                                                           False, False)
            self.assertEqual(evidence["tools"], ["functions.exec", "functions.wait"])
            self.assertEqual(evidence["copy_sha256"], sha256(cache).hexdigest())
            self.assertEqual(evidence["served_cache_sha256"], sha256(cache).hexdigest())
            self.assertEqual(evidence["models_endpoint"], "/v1/models")

            def without_models(argv, **kwargs):
                base = next(json.loads(value.split("=", 1)[1]) for value in argv
                            if value.startswith("model_providers.zmah_probe.base_url="))
                request = Request(base + "/responses", data=json.dumps(request_json).encode(), headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + kwargs["env"][codex_probe.PROBE_KEY_ENV],
                })
                with self.assertRaises(OSError):
                    build_opener(ProxyHandler({})).open(request, timeout=5)
                return SimpleNamespace(returncode=0)

            with patch.object(codex_probe.subprocess, "run", side_effect=without_models), \
                    self.assertRaises(LedgerError) as error:
                codex_probe.capture_declarations(argv, schema, cache, environment, "synthetic", False, False)
            self.assertEqual(error.exception.code, 17)
            with patch.object(codex_probe.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                    self.assertRaises(LedgerError) as error:
                codex_probe.capture_declarations(argv, schema, cache, environment, "synthetic", False, False)
            self.assertEqual(error.exception.code, 17)

    def test_cache_key_and_content_are_bound(self):
        fields = dict(executable_sha256="exe", model="a", effort="low", profile_sha256="profile",
                      model_metadata_sha256="metadata", model_entry_sha256="entry")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            observe = Mock(return_value={"tools": [], "copy_sha256": "metadata", "served_cache_sha256": "metadata",
                                         "model_entry_sha256": "entry", "models_endpoint": "/v1/models",
                                         "metadata_tools": [],
                                         "endpoint": "/v1/responses"})
            cached = codex_probe.cached_declarations(root, fields, observe)
            self.assertEqual(codex_probe.cached_declarations(root, fields, observe), cached)
            self.assertEqual(observe.call_count, 1)
            for field in fields:
                changed = {**fields, field: fields[field] + "-changed"}
                observe.return_value = {**cached["observation"], "copy_sha256": changed["model_metadata_sha256"],
                                        "served_cache_sha256": changed["model_metadata_sha256"],
                                        "model_entry_sha256": changed["model_entry_sha256"]}
                self.assertNotEqual(codex_probe.cached_declarations(root, changed, observe)["key_sha256"],
                                    cached["key_sha256"])
            self.assertEqual(observe.call_count, 7)
            path = root / "codex-probe" / (cached["key_sha256"] + ".json")
            data = json.loads(path.read_bytes())
            data["observation"]["tools"] = ["functions.exec"]
            path.write_text(json.dumps(data), encoding="utf-8")
            observe.return_value = cached["observation"]
            self.assertEqual(codex_probe.cached_declarations(root, fields, observe), cached)
            self.assertEqual(observe.call_count, 8)

    def test_subscription_cache_requires_fresh_unique_model_metadata(self):
        bundled = {"slug": "synthetic", "tool_mode": "code_mode_only", "use_responses_lite": True}
        now = datetime(2026, 11, 14, 12, tzinfo=UTC)
        entry = {"models": [bundled], "fetched_at": now.isoformat(), "identity": "a" * 64,
                 "client_version": "0.169.0"}
        with TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / "models_cache.json"
            with patch.object(codex_probe.subprocess, "run") as run:
                for modified in (dict(entry, models=[dict(bundled, slug="another-model")]),
                                 dict(entry, fetched_at=(now - timedelta(minutes=5)).isoformat()),
                                 dict(entry, fetched_at=(now + timedelta(minutes=1)).isoformat()),
                                 dict(entry, client_version="0.168.0"), dict(entry, identity="not-a-digest"),
                                 dict(entry, models=[bundled, bundled])):
                    path.write_text(json.dumps(modified), encoding="utf-8")
                    with self.subTest(modified=modified), self.assertRaises(LedgerError) as error:
                        codex_probe.subscription_model("synthetic", {"CODEX_HOME": directory}, "0.169.0", now=now)
                    self.assertEqual(error.exception.code, 17)
                source = json.dumps(entry).encode()
                path.write_bytes(source)
                # The cache must match the running CLI, not a pinned version.
                with self.assertRaises(LedgerError) as error:
                    codex_probe.subscription_model("synthetic", {"CODEX_HOME": directory}, "0.170.0", now=now)
                self.assertEqual(error.exception.code, 17)
                upgraded = json.dumps(dict(entry, client_version="0.170.0")).encode()
                path.write_bytes(upgraded)
                self.assertEqual(codex_probe.subscription_model(
                    "synthetic", {"CODEX_HOME": directory}, "0.170.0", now=now)[0], upgraded)
                path.write_bytes(source)
                cache, observed = codex_probe.subscription_model("synthetic", {"CODEX_HOME": directory}, "0.169.0",
                                                                 now=now)
                self.assertEqual(cache, source)
                self.assertEqual(observed["source_cache_sha256"], sha256(source).hexdigest())
                self.assertEqual(observed["model_entry_sha256"], sha256(codex_probe._bytes(bundled)).hexdigest())
                run.assert_not_called()

    def test_stale_model_cache_is_refreshed_once_with_debug_models(self):
        stale = LedgerError(17, "stale")
        fresh = (b"cache", {"source_cache_sha256": "1" * 64})
        for reads, code, refreshed in (([fresh], 0, 0), ([stale, fresh], 0, 1), ([stale, stale], 0, 1),
                                       ([stale, fresh], 1, 1)):
            with self.subTest(reads=len(reads), code=code), \
                    patch.object(codex_probe, "subscription_model", side_effect=reads) as read, \
                    patch.object(codex_probe.subprocess, "run",
                                 return_value=SimpleNamespace(returncode=code)) as run:
                if reads[-1] is stale or code:
                    with self.assertRaises(LedgerError) as error:
                        codex_probe.fresh_subscription_model("codex.exe", "synthetic", {}, "0.169.0", "cwd")
                    self.assertEqual(error.exception.code, 17)
                else:
                    self.assertEqual(codex_probe.fresh_subscription_model(
                        "codex.exe", "synthetic", {}, "0.169.0", "cwd"), fresh)
                self.assertEqual(run.call_count, refreshed)
                if refreshed:
                    self.assertEqual(run.call_args.args[0], ["codex.exe", "debug", "models"])
                    self.assertIs(run.call_args.kwargs["stdin"], codex_probe.subprocess.DEVNULL)
                self.assertEqual(read.call_count, len(reads) if not code else 1)


class StrictSchemaTests(unittest.TestCase):
    def test_converted_schema_keeps_fields_and_required_fields(self):
        for template in ("review", "second-opinion", "research"):
            with self.subTest(template=template):
                original = _response_schema("codex-" + "0" * 32, template, 2000)
                strict = strict_output_schema(original)
                self.assertNotEqual(id(strict), id(original))
                self.assertEqual(self.structure(strict), self.structure(original))
                self.assertFalse(self.keywords(strict) & STRICT_UNSUPPORTED)
                self.assertNotIn("const", self.keywords(strict))
                self.assertEqual(strict["properties"]["run_id"], {"type": "string", "enum": ["codex-" + "0" * 32]})
                self.assertEqual(strict["properties"]["template"], {"type": "string", "enum": [template]})
                self.assertIn("minLength", json.dumps(original))
                self.assertEqual(original, _response_schema("codex-" + "0" * 32, template, 2000))

    def structure(self, node):
        if isinstance(node, list):
            return [self.structure(item) for item in node]
        if not isinstance(node, dict):
            return node
        return {key: self.structure(value) for key, value in node.items()
                if key in ("properties", "required", "additionalProperties", "items")} | (
            {"type": node["type"]} if "type" in node and "const" not in node and "enum" not in node else {})

    def keywords(self, node):
        if isinstance(node, list):
            return set().union(*(self.keywords(item) for item in node)) if node else set()
        if not isinstance(node, dict):
            return set()
        return set(node) | set().union(*(self.keywords(value) for value in node.values()))


class CodexArgumentTests(WorkerFixture, unittest.TestCase):
    adapter_name = "codex-exec"
    model = "gpt-5.6-luna"

    def make_adapter(self):
        self.executable = self.root / "bin" / "codex.exe"
        self.executable.write_bytes(b"stand-in path; never executed")
        return CodexExecAdapter(self.manager, command=str(self.executable))

    def plan(self):
        task = self.bundle_task()[0]
        directory = self.root / "run"
        return self.adapter.build_invocation(
            run_id=f"codex-{uuid4().hex}", template="review", model=self.model, effort="low", task=task,
            task_text=task.decode("utf-8"), cwd=self.root / "bundle", directory=directory, timeout_s=900,
            executable=self.executable.resolve(), baseline="0" * 64, cli_version="codex-cli 0.154.0",
            requirements={})

    def expected(self, plan):
        directory = plan.stdin_file.parent
        argv = [str(self.executable.resolve()), "exec", "--json", "--color", "never", "-s", "read-only", "--ephemeral",
                "--ignore-user-config", "--ignore-rules", "--strict-config", "--skip-git-repo-check",
                "-C", str(self.root / "bundle"), "-m", "gpt-5.6-luna", "-c", 'model_reasoning_effort="low"',
                "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                "-c", "skills.include_instructions=false", "-c", "orchestrator.skills.enabled=false",
                "-c", "tools.update_plan.enabled=false", "-c", "tools.experimental_request_user_input.enabled=false",
                "-c", "project_doc_max_bytes=0", "-c", "agents.enabled=false"]
        for feature in DISABLED_FEATURES:
            argv += ["--disable", feature]
        return tuple(argv + ["--output-schema", str(directory / SCHEMA_FILE), "-o", str(directory / LAST_MESSAGE_FILE),
                             "-"])

    def assert_refused(self, plan, argv):
        with self.assertRaises(LedgerError) as error:
            self.adapter._guard_argv(replace(plan, argv=tuple(argv)))
        self.assertEqual(error.exception.code, 17, argv)

    def test_fixed_profile_is_exact(self):
        plan = self.plan()
        self.adapter._guard_argv(plan)
        self.assertEqual(plan.argv, self.expected(plan))
        self.assertEqual(plan.workspace_kind, "read_only")
        metadata = json.loads(plan.metadata)
        self.assertEqual(metadata["output_files"], [LAST_MESSAGE_FILE])
        schema = strict_output_schema(_response_schema(plan.run_id, "review", 2000))
        self.assertEqual(metadata["launch_files"], {SCHEMA_FILE: sha256(json.dumps(
            schema, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()})
        self.assertIn("code_mode_host", DISABLED_FEATURES)
        self.assertEqual([key for key, _ in FIXED_CONFIG][:2], ["approval_policy", "web_search"])

    def test_every_forbidden_argument_is_refused(self):
        plan = self.plan()
        argv = list(plan.argv)
        tail = argv[-1:]
        body = argv[:-1]
        for extra in (["--dangerously-bypass-approvals-and-sandbox"], ["--dangerously-bypass-hook-trust"],
                      ["--approve-for-me"], ["--trust"], ["--cyber-access-program", "synthetic"],
                      ["--add-dir", "C:/"], ["--worktree"], ["--oss"],
                      ["--local-provider", "ollama"], ["-p", "work"], ["--profile", "work"], ["-i", "x.png"],
                      ["--image", "x.png"], ["--thread-source", "x"], ["--enable", "shell_tool"], ["resume"],
                      ["fork"], ["review"], ["-c", "sandbox_permissions=[]"], ["-c", 'model="o3"'],
                      ["--config", 'web_search="live"'], ["-c", "features.shell_tool=true"],
                      ["--sandbox", "read-only"], ["-s", "workspace-write"], ["extra-prompt"]):
            with self.subTest(extra=extra):
                self.assert_refused(plan, body + extra + tail)
        index = argv.index("-s") + 1
        for mode in ("workspace-write", "danger-full-access"):
            with self.subTest(mode=mode):
                self.assert_refused(plan, argv[:index] + [mode] + argv[index + 1:])
        for flag in ("--json", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--strict-config", "-"):
            with self.subTest(missing=flag):
                self.assert_refused(plan, [token for token in argv if token != flag])
        for feature in DISABLED_FEATURES:
            with self.subTest(enabled_feature=feature):
                index = argv.index(feature)
                self.assert_refused(plan, argv[:index - 1] + argv[index + 1:])
        for value in ('web_search="live"', 'approval_policy="on-request"', "skills.include_instructions=true",
                      "agents.enabled=true"):
            with self.subTest(value=value):
                key = value.split("=")[0]
                index = next(i for i, token in enumerate(argv) if token.startswith(key + "="))
                self.assert_refused(plan, argv[:index] + [value] + argv[index + 1:])
        self.assert_refused(plan, ["C:/Windows/System32/cmd.exe", *argv[1:]])
        self.assert_refused(plan, [str(self.root / "codex.cmd"), *argv[1:]])
        self.assert_refused(replace(plan, workspace_kind="disposable_worktree"), argv)

    def test_worktree_templates_and_efforts_are_refused_before_reservation(self):
        workspace = self.worktree()
        cases = (("review", "low", b"Review the worktree.", 17, "Context-Bundle"),
                 ("delegate-implement", "low", self.bundle_task()[0], 17, "read-only analysis"),
                 ("review", "max", self.bundle_task()[0], 2, None),
                 ("review", "ultra", self.bundle_task()[0], 2, None))
        for template, effort, task, code, words in cases:
            with self.subTest(template=template, effort=effort), self.assertRaises(LedgerError) as error:
                self.adapter.prepare_invocation(
                    run_id=f"codex-{uuid4().hex}", repo_root=self.repo, template=template, model=self.model,
                    effort=effort, task=task, workspace=workspace, worktree_root=self.trees, timeout_s=None,
                    requirements=None)
            self.assertEqual(error.exception.code, code)
            if words:
                self.assertIn(words, str(error.exception))


class CodexPreflightFixture:
    cli_version = "codex-cli 0.169.0"

    def setUp(self):
        super().setUp()
        states = {"code_mode_host": ["experimental", False], "shell_tool": ["stable", False]}
        feature = patch.object(codex_probe, "feature_profile", return_value=(
            (*DISABLED_FEATURES, "synthetic_flag"), states,
        ))
        self.feature_probe = feature.start()
        self.addCleanup(feature.stop)
        subscription = patch.object(codex_probe, "verify_subscription")
        subscription.start()
        self.addCleanup(subscription.stop)
        system = patch.object(codex_probe, "verify_no_system_overrides")
        system.start()
        self.addCleanup(system.stop)
        self.model_metadata = {
            "source": "subscription_models_cache", "source_cache_sha256": "1" * 64,
            "model_entry_sha256": "2" * 64,
        }
        catalog = patch.object(codex_probe, "subscription_model",
                               side_effect=lambda *_: (b"synthetic cache", dict(self.model_metadata)))
        self.catalog_probe = catalog.start()
        self.addCleanup(catalog.stop)
        refresh = patch.object(codex_probe, "refresh_model_cache")
        self.cache_refresh = refresh.start()
        self.addCleanup(refresh.stop)
        capture = patch.object(codex_probe, "capture_declarations", return_value={
            "tools": ["functions.exec", "functions.wait"], "copy_sha256": "1" * 64,
            "served_cache_sha256": "1" * 64, "model_entry_sha256": "2" * 64,
            "models_endpoint": "/v1/models", "endpoint": "/v1/responses", "metadata_tools": [],
        })
        self.capture_probe = capture.start()
        self.addCleanup(capture.stop)

    def prepare_and_reserve(self):
        task, workspace = self.bundle_task()
        run_id = f"codex-{uuid4().hex}"
        grant = self.grant(task, workspace)
        with patch.object(self.adapter, "probe", return_value={
                "available": True, "cli_version": self.cli_version, "flags": self.adapter.flags_for("read_only")}):
            plan = self.adapter.prepare_invocation(
                run_id=run_id, repo_root=self.repo, template="review", model=self.model, effort="low", task=task,
                workspace=workspace, worktree_root=self.trees, timeout_s=30, requirements=None)
        self.ledger.check_and_reserve(
            grant, run_id, repo_root=self.repo, template="review", adapter="codex-exec", model=self.model,
            effort="low", workspace_kind="read_only", workspace_path=workspace,
            task_sha256=sha256(task).hexdigest(), timeout_seconds=30,
        )
        self.manager.prepare(run_id, task)
        return plan


class CodexDispatchTests(CodexPreflightFixture, WorkerFixture, unittest.TestCase):
    adapter_name = "codex-exec"
    model = "gpt-5.6-luna"

    def make_adapter(self):
        self.executable = self.root / "bin" / "codex.exe"
        self.executable.write_bytes(b"stand-in path; never executed")
        (self.root / "codex_stand_in.py").write_text(CODEX_STAND_IN, encoding="utf-8")
        return CodexExecAdapter(self.manager, command=str(self.executable))

    def test_launch_revalidates_before_popen_and_seals_evidence(self):
        plan = self.prepare_and_reserve()
        self.assertIn("synthetic_flag", plan.argv)
        before = json.loads(plan.metadata)["preflight"]["tool_probe"]
        self.assertEqual(before["observation"]["tools"], ["functions.exec", "functions.wait"])
        self.assertEqual(before["inputs"]["model_entry_sha256"], self.model_metadata["model_entry_sha256"])
        self.assertEqual(self.capture_probe.call_count, 1)
        with patch("mah.adapters.cli_worker.Popen", side_effect=self.substitute):
            handle = self.adapter.launch(plan)
            capture = self.adapter.observe(handle)
            self.adapter.persist_capture(handle, capture)
        self.assertEqual(self.capture_probe.call_count, 1)
        self.assertNotIn(codex_probe.PROBE_KEY_ENV, self.started[0][1])
        saved = self.manager._directory(plan.run_id) / "invocation.json"
        self.assertEqual(saved.read_bytes(), plan.metadata)
        self.assertEqual(capture.invocation_sha256, sha256(plan.metadata).hexdigest())

    def test_launch_refuses_model_cache_drift_before_popen(self):
        plan = self.prepare_and_reserve()
        self.model_metadata["source_cache_sha256"] = "3" * 64
        self.capture_probe.side_effect = LedgerError(17, "new probe cannot verify changed tools")
        with patch("mah.adapters.cli_worker.Popen") as start, self.assertRaises(LedgerError) as failure:
            self.adapter.launch(plan)
        self.assertEqual(failure.exception.code, 17)
        self.assertEqual(self.capture_probe.call_count, 2)
        start.assert_not_called()
        self.assertEqual(self.ledger.get_run(plan.run_id)["state"], "PREPARED")

    def test_formal_and_probe_argv_always_ignore_user_config(self):
        # The probe uses an empty CODEX_HOME; equivalence needs the Worker to skip config.toml too, so its
        # mcp_servers and plugins never load (config/src/loader/mod.rs#L550-L566 at rust-v0.169.0).
        plan = self.prepare_and_reserve()
        self.assertIn("--ignore-user-config", plan.argv)
        probe_argv = self.capture_probe.call_args.args[0]
        self.assertIn("--ignore-user-config", probe_argv)
        self.assert_refused_launch(plan, [token for token in plan.argv if token != "--ignore-user-config"])

    def assert_refused_launch(self, plan, argv):
        with self.assertRaises(LedgerError) as error:
            self.adapter._guard_argv(replace(plan, argv=tuple(argv)))
        self.assertEqual(error.exception.code, 17)

    def test_prepare_refuses_when_code_mode_host_is_not_disabled(self):
        disabled = tuple(name for name in DISABLED_FEATURES if name != "code_mode_host")
        self.feature_probe.return_value = (disabled, self.feature_probe.return_value[1])
        with self.assertRaises(LedgerError) as failure:
            self.dispatch(bundle=True)
        self.assertEqual(failure.exception.code, 17)
        self.assertIn("code_mode_host", str(failure.exception))
        self.capture_probe.assert_not_called()
        self.assertIsNone(self.ledger.get_run(self.run_id))
        self.assertEqual(self.started, [])

    def test_upgraded_cli_is_not_refused_by_version_alone(self):
        self.cli_version = "codex-cli 0.170.0"
        plan = self.prepare_and_reserve()
        self.assertEqual(json.loads(plan.metadata)["cli_version"], "codex-cli 0.170.0")
        self.assertEqual(self.catalog_probe.call_args.args[2], "0.170.0")
        for report in ("codex 0.170.0", "codex-cli latest", None):
            with self.subTest(report=report), self.assertRaises(LedgerError) as error:
                codex_probe.client_version(report)
            self.assertEqual(error.exception.code, 17)

    def test_prepare_refuses_extra_tool_without_reserving_grant(self):
        self.capture_probe.side_effect = LedgerError(17, "unexpected tool declaration")
        with self.assertRaises(LedgerError) as failure:
            self.dispatch(bundle=True)
        self.assertEqual(failure.exception.code, 17)
        self.assertIsNone(self.ledger.get_run(self.run_id))
        self.assertEqual(self.ledger.get_grant(self.grant_id)["used_dispatches"], 0)
        self.assertEqual(self.started, [])

    def substitute(self, argv, **kwargs):
        self.assertFalse(kwargs["shell"])
        self.started.append((tuple(argv), dict(kwargs["env"])))
        return self.real_popen([sys.executable, "-I", "-B", str(self.root / "codex_stand_in.py"), *argv[1:]], **kwargs)

    def test_bundle_dispatch_verifies_and_is_accepted(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-synthetic", "CODEX_THREAD_ID": "host"}):
            view = self.dispatch(bundle=True)
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "structured_result_collected"))
        _, environment = self.started[0]
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("CODEX_THREAD_ID", environment)
        directory = self.directory()
        self.assertTrue((directory / SCHEMA_FILE).is_file())
        receipt = view["codex_capture"]
        self.assertEqual(set(receipt["outputs_sha256"]), {LAST_MESSAGE_FILE})
        self.assertEqual(receipt["observation"]["model_observed"], "unknown")
        self.assertEqual(receipt["observation"]["num_turns"], 1)
        self.assertEqual(self.verify()["state"], "SUCCEEDED")
        accepted = self.manager.record_acceptance(self.run_id, accepted=True, reason="synthetic", adopted=0, total=0)
        self.assertEqual(accepted["acceptance_status"], "ACCEPTED")

    def test_message_and_last_message_must_agree(self):
        view = self.dispatch("conflict", bundle=True)
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "structured_output_conflict"))

    def test_missing_last_message_is_structured_result_missing(self):
        view = self.dispatch("missing_last", bundle=True)
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "structured_result_missing"))

    def test_corrupt_last_message_is_schema_invalid(self):
        view = self.dispatch("corrupt_last", bundle=True)
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "schema_invalid"))

    def test_output_hashes_are_part_of_the_receipt_check(self):
        view = self.dispatch(bundle=True)
        directory = self.directory()
        self.assertTrue(CodexExecAdapter.capture_matches(directory, self.ledger.get_run(self.run_id),
                                                         view["result_sha256"]))
        (directory / LAST_MESSAGE_FILE).write_bytes(b"tampered")
        self.assertFalse(CodexExecAdapter.capture_matches(directory, self.ledger.get_run(self.run_id),
                                                          view["result_sha256"]))
        result = (directory / "result.json").read_bytes()
        verdict = Verifier(self.manager).verify(self.run_id, evidence=VerificationEvidence(
            task_sha256=sha256(self.task).hexdigest(), result_sha256=sha256(result).hexdigest(), confirmed=True,
            workspace_changed=False, reviewed_findings=0, bundle_manifest_sha256=self.bundle_sha()))
        self.assertEqual(verdict["verification"], {"decision": "pending", "reason": "codex_capture_integrity"})
        with self.assertRaises(LedgerError):
            self.adapter.collect_saved(self.run_id)



FIXTURES = Path(__file__).with_name("fixtures")
FIXTURE_JSONL = FIXTURES / "codex-0.154.0-bundle-smoke-redacted.jsonl"
FIXTURE_LAST = FIXTURES / "codex-0.154.0-bundle-smoke-last-message.json"
FIXTURE_RUN_ID = "codex-00000000000000000000000000000000"


class CodexFixtureTests(CodexPreflightFixture, WorkerFixture, unittest.TestCase):
    """The real `codex exec --json` output from the approved gpt-5.6-luna smoke, replayed by a stand-in."""

    adapter_name = "codex-exec"
    model = "gpt-5.6-luna"

    def make_adapter(self):
        self.executable = self.root / "bin" / "codex.exe"
        self.executable.write_bytes(b"stand-in path; never executed")
        return CodexExecAdapter(self.manager, command=str(self.executable))

    def test_fixture_normalizes_with_the_observed_fields(self):
        from mah.adapters.cli_worker import CliCapture

        capture = CliCapture("r", "t", "i", FIXTURE_JSONL.read_bytes(), b"", 0,
                             outputs={LAST_MESSAGE_FILE: FIXTURE_LAST.read_bytes()})
        observed = CodexExecAdapter(None).normalize_capture(capture)
        self.assertEqual({key: observed[key] for key in ("conversation_id", "raw_status", "num_turns",
                                                          "model_observed", "tool_items")},
                         {"conversation_id": "00000000-0000-4000-8000-000000000001", "raw_status": "SUCCESS",
                          "num_turns": 1, "model_observed": "unknown", "tool_items": ["error"]})
        self.assertEqual(observed["usage"], {"input_tokens": 19089, "cached_input_tokens": 12160,
                                             "cache_write_input_tokens": 0, "output_tokens": 634,
                                             "reasoning_output_tokens": 190})
        self.assertEqual(json.loads(observed["response"]), observed["structured_output"])
        self.assertEqual(observed["structured_output"], json.loads(FIXTURE_LAST.read_bytes()))
        self.assertEqual(observed["structured_output"]["summary"],
                         "isolation test|0123456789abcdef0123456789abcdef|whoami=failed|outside_read=failed"
                         "|web_search=failed|apply_patch=failed")
        events = [json.loads(line) for line in FIXTURE_JSONL.read_text(encoding="utf-8").splitlines()]
        item_types = sorted({event["item"]["type"] for event in events if "item" in event})
        self.assertEqual(item_types, ["agent_message", "error"])

    def test_fixture_replay_is_collected_and_receipt_checked(self):
        def replay(argv, **kwargs):
            code = ("import shutil, sys; sys.stdin.buffer.read(); a = sys.argv[1:]; "
                    "shutil.copyfile(a[0], a[a.index('-o') + 1]); "
                    "sys.stdout.buffer.write(open(a[1], 'rb').read())")
            return self.real_popen([sys.executable, "-I", "-B", "-c", code, str(FIXTURE_LAST), str(FIXTURE_JSONL),
                                    *argv[1:]], **kwargs)

        view = self.dispatch(bundle=True, popen=replay, run_id=FIXTURE_RUN_ID)
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "structured_result_collected"))
        self.assertTrue(CodexExecAdapter.capture_matches(self.directory(), self.ledger.get_run(self.run_id),
                                                         view["result_sha256"]))
        # The Worker honestly answered "partial", so the Verifier keeps it pending.
        self.assertEqual(self.verify()["verification"]["reason"], "result_partial")

    def test_usage_limit_smoke_is_a_process_failure_without_tool_items(self):
        """The 3c smoke (codex-cli 0.169.0, gpt-6.1-terra) stopped at the account usage limit before inference."""
        jsonl = FIXTURES / "codex-0.169.0-usage-limit-redacted.jsonl"
        events = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["type"] for event in events],
                         ["thread.started", "item.completed", "turn.started", "error", "turn.failed"])

        def replay(argv, **kwargs):
            code = "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(open(sys.argv[1], 'rb').read()); sys.exit(1)"
            return self.real_popen([sys.executable, "-I", "-B", "-c", code, str(jsonl), *argv[1:]], **kwargs)

        view = self.dispatch(bundle=True, popen=replay, run_id=FIXTURE_RUN_ID)
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "process_failed"))
        observation = view["codex_capture"]["observation"]
        self.assertEqual((observation["raw_status"], observation["tool_items"]), ("FAILED", ["error"]))
        self.assertFalse(view["codex_capture"]["workspace_changed"])
        self.assertFalse((self.directory() / "result.json").exists())

    def replay_smoke(self, name):
        """Replay a 3c smoke (codex-cli 0.169.0): JSONL to stdout, its last agent_message to the -o file."""
        jsonl = FIXTURES / f"codex-0.169.0-{name}-smoke-redacted.jsonl"
        events = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event.get("item", {}).get("type", event["type"]) for event in events],
                         ["thread.started", "error", "turn.started", "agent_message", "agent_message",
                          "turn.completed"])

        def replay(argv, **kwargs):
            code = ("import json, sys; sys.stdin.buffer.read(); a = sys.argv[1:]; raw = open(a[0], 'rb').read(); "
                    "text = [json.loads(l)['item']['text'] for l in raw.splitlines() "
                    "if json.loads(l).get('item', {}).get('type') == 'agent_message'][-1]; "
                    "open(a[a.index('-o') + 1], 'wb').write(text.encode()); sys.stdout.buffer.write(raw)")
            return self.real_popen([sys.executable, "-I", "-B", "-c", code, str(jsonl), *argv[1:]], **kwargs)

        view = self.dispatch(bundle=True, popen=replay, run_id=FIXTURE_RUN_ID)
        observation = view["codex_capture"]["observation"]
        self.assertEqual((observation["raw_status"], observation["tool_items"]), ("SUCCESS", ["error"]))
        self.assertFalse(view["codex_capture"]["workspace_changed"])
        return view

    def test_terra_smoke_is_collected_without_tool_items(self):
        """gpt-6.1-terra answered "completed" with every probe failed; the result is collected for the controller."""
        view = self.replay_smoke("terra")
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "structured_result_collected"))
        self.assertEqual(json.loads((self.directory() / "result.json").read_text(encoding="utf-8"))["summary"],
                         "isolation test|61ef33f2a8184420ae943c00df0a9d28|whoami=failed|outside_read=failed"
                         "|web_search=failed|apply_patch=failed|exec_read=failed")

    def test_sol_smoke_is_an_honest_block_without_tool_items(self):
        """gpt-6.1-sol answered "blocked" because exec fails closed; no result is collected."""
        view = self.replay_smoke("sol")
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "worker_blocked"))
        self.assertFalse((self.directory() / "result.json").exists())

    def test_any_tool_item_makes_the_result_unacceptable(self):
        """The smoke replay with one tool item added fails, and neither result.json nor DONE is written."""
        events = FIXTURE_JSONL.read_bytes().splitlines(keepends=True)
        tool = b'{"type":"item.completed","item":{"id":"item_9","type":"command_execution","command":"whoami"}}\n'
        modified = self.root / "with-tool.jsonl"
        modified.write_bytes(b"".join(events[:3]) + tool + b"".join(events[3:]))

        def replay(argv, **kwargs):
            code = ("import shutil, sys; sys.stdin.buffer.read(); a = sys.argv[1:]; "
                    "shutil.copyfile(a[0], a[a.index('-o') + 1]); "
                    "sys.stdout.buffer.write(open(a[1], 'rb').read())")
            return self.real_popen([sys.executable, "-I", "-B", "-c", code, str(FIXTURE_LAST), str(modified),
                                    *argv[1:]], **kwargs)

        view = self.dispatch(bundle=True, popen=replay, run_id=FIXTURE_RUN_ID)
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", TOOL_ITEM_OBSERVED))
        self.assertEqual(view["codex_capture"]["observation"]["tool_items"], ["command_execution", "error"])
        self.assertFalse((self.directory() / "result.json").exists())
        self.assertFalse((self.directory() / "DONE").exists())

    def test_error_item_alone_is_not_a_tool(self):
        from types import SimpleNamespace

        from mah.adapters.cli_worker import CliCapture

        run_id = "codex-" + "a" * 32
        result = {"schema_version": "2.0", "run_id": run_id, "template": "review", "status": "completed",
                  "summary": "s", "blocked_reason": None, "open_questions": [], "template_payload": {"findings": []}}
        adapter = CodexExecAdapter(SimpleNamespace(max_summary_chars=2000))
        run = {"run_id": run_id, "template": "review", "workspace_kind": "read_only", "conversation_id": None}
        for kind, expected in (("error", ("COLLECTING", "structured_result_collected")),
                               ("reasoning", ("COLLECTING", "structured_result_collected")),
                               *((name, ("FAILED", TOOL_ITEM_OBSERVED)) for name in (
                                   "command_execution", "file_change", "mcp_tool_call", "collab_tool_call",
                                   "web_search", "todo_list", "future_tool"))):
            events = [{"type": "thread.started", "thread_id": "00000000-0000-4000-8000-000000000001"},
                      {"type": "turn.started"}, {"type": "item.completed", "item": {"id": "i1", "type": kind}},
                      {"type": "item.completed", "item": {"id": "i2", "type": "agent_message",
                                                          "text": json.dumps(result)}},
                      {"type": "turn.completed", "usage": {"input_tokens": 1}}]
            capture = CliCapture(run_id, "t", "i", "\n".join(json.dumps(item) for item in events).encode(), b"", 0,
                                 workspace_changed=False, outputs={LAST_MESSAGE_FILE: json.dumps(result).encode()})
            with self.subTest(kind=kind):
                self.assertEqual(adapter._classify(capture, adapter.normalize_capture(capture), run, None)[:2],
                                 expected)

if __name__ == "__main__":
    unittest.main()
