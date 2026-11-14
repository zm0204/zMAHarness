"""What codex-exec provides on top of the shared CLI Worker base. No real model is called."""
from dataclasses import fields, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from mah.adapters import registry
from mah.adapters.agy_print import _response_schema
from mah.adapters.base import Adapter, AdapterPolicy
from mah.adapters.codex_exec import (DISABLED_FEATURES, FIXED_CONFIG, LAST_MESSAGE_FILE, MISSING_EXECUTABLE,
                                     SCHEMA_FILE, STRICT_UNSUPPORTED, CodexExecAdapter, codex_environment,
                                     resolve_codex_executable, strict_output_schema)
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
                "-c", "project_doc_max_bytes=0"]
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
        self.assertEqual(len(DISABLED_FEATURES), 28)
        self.assertEqual([key for key, _ in FIXED_CONFIG][:2], ["approval_policy", "web_search"])

    def test_every_forbidden_argument_is_refused(self):
        plan = self.plan()
        argv = list(plan.argv)
        tail = argv[-1:]
        body = argv[:-1]
        for extra in (["--dangerously-bypass-approvals-and-sandbox"], ["--dangerously-bypass-hook-trust"],
                      ["--approve-for-me"], ["--add-dir", "C:/"], ["--worktree"], ["--oss"],
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
        for value in ('web_search="live"', 'approval_policy="on-request"', "skills.include_instructions=true"):
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


class CodexDispatchTests(WorkerFixture, unittest.TestCase):
    adapter_name = "codex-exec"
    model = "gpt-5.6-luna"

    def make_adapter(self):
        self.executable = self.root / "bin" / "codex.exe"
        self.executable.write_bytes(b"stand-in path; never executed")
        (self.root / "codex_stand_in.py").write_text(CODEX_STAND_IN, encoding="utf-8")
        return CodexExecAdapter(self.manager, command=str(self.executable))

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


class CodexFixtureTests(WorkerFixture, unittest.TestCase):
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

if __name__ == "__main__":
    unittest.main()
