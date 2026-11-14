"""What claude-print provides on top of the shared CLI Worker base. No real model is called."""
from dataclasses import fields, replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from mah.adapters import registry
from mah.adapters.base import Adapter, AdapterPolicy
from mah.adapters.claude_print import (FORBIDDEN_TOOLS, MISSING_EXECUTABLE, ClaudePrintAdapter, claude_environment,
                                       resolve_claude_executable)
from mah.ledger import LedgerError
from mah.preflight import preflight

from test_cli_worker import WorkerFixture

# De-identified stdout from the approved claude 2.1.325 smoke (sonnet, effort low, one-file Context-Bundle).
FIXTURE = Path(__file__).with_name("fixtures") / "claude-2.1.325-bundle-smoke-redacted.json"
FIXTURE_RUN_ID = "claude-00000000000000000000000000000000"

CREDENTIAL_VARIABLES = {"ANTHROPIC_API_KEY": "sk-ant-synthetic", "ANTHROPIC_AUTH_TOKEN": "synthetic",
                        "ANTHROPIC_BASE_URL": "https://example.invalid", "ANTHROPIC_CUSTOM_HEADERS": "x: y",
                        "anthropic_api_key": "lowercase-synthetic"}


class ClaudePolicyTests(unittest.TestCase):
    def test_protocol_policy_and_registration(self):
        self.assertIsInstance(ClaudePrintAdapter(None), Adapter)
        policy = registry.policy_for("claude-print")
        expected = {"user_configurable": True, "requires_dispatch_scope": True, "attended_approval_only": True,
                    "template_verified": True, "run_id_prefix": "claude"}
        for field in fields(AdapterPolicy):
            with self.subTest(flag=field.name):
                self.assertEqual(getattr(policy, field.name), expected.get(field.name, False if field.name !=
                                                                            "run_id_prefix" else None))
        self.assertEqual(registry.capture_reason("claude-print"), "claude_capture_integrity")
        self.assertEqual(registry.capture_reason("agy-print"), "agy_capture_integrity")
        self.assertEqual(registry.executable_missing_reason("claude-print"), MISSING_EXECUTABLE)
        self.assertIn("claude-print", registry.dispatchable_names())


class ClaudeExecutableTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="mah-claude-exe-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def file(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
        return path

    def test_absolute_native_exe_is_used(self):
        native = self.file("custom/claude.exe")
        self.assertEqual(resolve_claude_executable(str(native)), native.resolve())

    def test_absolute_shims_and_missing_files_are_refused(self):
        for name in ("claude.cmd", "claude.bat", "claude.ps1", "claude"):
            with self.subTest(name=name):
                self.assertIsNone(resolve_claude_executable(str(self.file(f"shims/{name}"))))
        self.assertIsNone(resolve_claude_executable(str(self.root / "missing" / "claude.exe")))

    def test_path_lookup_that_finds_an_exe_uses_it(self):
        native = self.file("local/bin/claude.exe")
        with patch("mah.adapters.claude_print.which", return_value=str(native)):
            self.assertEqual(resolve_claude_executable("claude"), native.resolve())

    def test_npm_shims_map_to_the_package_native_exe(self):
        native = self.file("npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
        for shim in ("claude.cmd", "claude.ps1", "claude"):
            with self.subTest(shim=shim), patch("mah.adapters.claude_print.which",
                                                return_value=str(self.file(f"npm/{shim}"))):
                self.assertEqual(resolve_claude_executable("claude"), native.resolve())

    def test_npm_shim_without_the_native_package_file_is_not_found(self):
        for shim in ("claude.cmd", "other.cmd", "claude.bat"):
            with self.subTest(shim=shim), patch("mah.adapters.claude_print.which",
                                                return_value=str(self.file(f"empty/{shim}"))):
                self.assertIsNone(resolve_claude_executable("claude"))
        with patch("mah.adapters.claude_print.which", return_value=None):
            self.assertIsNone(resolve_claude_executable("claude"))

    def test_relative_command_resolves_next_to_the_local_toml(self):
        native = self.file("tools/claude.exe")
        config = SimpleNamespace(path=self.root / "harness.local.toml",
                                 values={"adapters": {"claude_command": "tools/claude.exe"},
                                         "limits": {"max_task_bytes": 1024}})
        adapter = ClaudePrintAdapter.from_config(None, config)
        self.assertEqual((adapter.resolve_executable(), adapter.max_task_bytes), (native.resolve(), 1024))

    def test_missing_executable_raises_14_and_preflight_explains_it(self):
        adapter = ClaudePrintAdapter(None, command=str(self.root / "claude.cmd"))
        with self.assertRaises(LedgerError) as error:
            adapter.resolve_executable()
        self.assertEqual((error.exception.code, str(error.exception)), (14, MISSING_EXECUTABLE))
        home = self.root / "home"
        home.mkdir()
        (home / "harness.local.toml").write_text(
            '[adapters]\nenabled = ["claude-print"]\nclaude_command = "C:/missing/claude.cmd"\n', encoding="utf-8")
        with patch.dict(os.environ, {"ZMAHARNESS_HOME": str(home)}):
            report = preflight(self.root, "claude-print")
        self.assertFalse(report["adapter_executable_found"])
        self.assertIn(MISSING_EXECUTABLE, report["reasons"])


class ClaudeEnvironmentTests(unittest.TestCase):
    def test_credentials_and_host_session_variables_are_not_passed(self):
        extra = CREDENTIAL_VARIABLES | {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                                        "CLAUDE_CONFIG_DIR": r"C:\synthetic\claude",
                                        "CLAUDE_CODE_GIT_BASH_PATH": r"C:\synthetic\bash.exe", "PATH": "synthetic"}
        with patch.dict(os.environ, extra):
            environment = claude_environment()
        self.assertFalse([key for key in environment if key.upper().startswith("ANTHROPIC_")])
        self.assertNotIn("CLAUDECODE", environment)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", environment)
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], r"C:\synthetic\claude")
        self.assertEqual(environment["CLAUDE_CODE_GIT_BASH_PATH"], r"C:\synthetic\bash.exe")
        self.assertEqual(environment["PATH"], "synthetic")


class ClaudeDispatchTests(WorkerFixture, unittest.TestCase):
    adapter_name = "claude-print"
    model = "sonnet"

    def make_adapter(self):
        self.executable = self.root / "bin" / "claude.exe"
        self.executable.write_bytes(b"stand-in path; never executed")
        return ClaudePrintAdapter(self.manager, command=str(self.executable))

    def schema_of(self, argv):
        return argv[argv.index("--json-schema") + 1]

    def expected_argv(self, argv, tools_and_mode):
        return (str(self.executable.resolve()), "-p", "--output-format", "json", "--json-schema", self.schema_of(argv),
                "--model", "sonnet", "--effort", "low", "--restricted", "--strict-mcp-config",
                "--permission-prompts", "none", "--no-session-persistence", *tools_and_mode)

    def test_worktree_dispatch_verifies_and_is_accepted(self):
        with patch.dict(os.environ, CREDENTIAL_VARIABLES | {"CLAUDECODE": "1"}):
            view = self.dispatch()
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "structured_result_collected"))
        argv, environment = self.started[0]
        self.assertEqual(argv, self.expected_argv(argv, ("--permission-mode", "plan", "--tools", "Read,Grep,Glob")))
        self.assertFalse([key for key in environment if key.upper().startswith("ANTHROPIC_")])
        self.assertNotIn("CLAUDECODE", environment)
        receipt = view["claude_capture"]
        self.assertEqual(receipt["observation"]["model_observed"], "claude-stand-in")
        self.assertEqual(receipt["executable"], str(self.executable.resolve()))
        invocation = json.loads((self.directory() / "invocation.json").read_bytes())
        self.assertEqual((invocation["cli_version"], invocation["stdin_file"]), ("stand-in 1.0", "claude.prompt.txt"))
        self.assertEqual(self.verify()["state"], "SUCCEEDED")
        accepted = self.manager.record_acceptance(self.run_id, accepted=True, reason="合成驗收", adopted=0, total=0)
        self.assertEqual(accepted["acceptance_status"], "ACCEPTED")

    def test_bundle_dispatch_has_no_tools(self):
        view = self.dispatch(bundle=True)
        self.assertEqual(view["state"], "COLLECTING")
        argv, _ = self.started[0]
        self.assertEqual(argv, self.expected_argv(argv, ("--tools", "")))
        self.assertEqual(self.verify()["state"], "SUCCEEDED")

    def test_tampered_receipt_uses_the_claude_reason(self):
        self.dispatch()
        receipt = self.directory() / "claude.stderr.txt"
        receipt.write_bytes(b"tampered")
        result = (self.directory() / "result.json").read_bytes()
        from hashlib import sha256
        from mah.verifier import VerificationEvidence, Verifier

        view = Verifier(self.manager).verify(self.run_id, evidence=VerificationEvidence(
            task_sha256=sha256(self.task).hexdigest(), result_sha256=sha256(result).hexdigest(), confirmed=True,
            workspace_changed=False, reviewed_findings=0))
        self.assertEqual(view["verification"], {"decision": "pending", "reason": "claude_capture_integrity"})


class ClaudeArgumentTests(WorkerFixture, unittest.TestCase):
    adapter_name = "claude-print"
    model = "sonnet"

    def make_adapter(self):
        self.executable = self.root / "bin" / "claude.exe"
        self.executable.write_bytes(b"stand-in path; never executed")
        return ClaudePrintAdapter(self.manager, command=str(self.executable))

    def plan(self, bundle):
        task = self.bundle_task()[0] if bundle else b"Review the worktree. scenario=success"
        return self.adapter.build_invocation(
            run_id=f"claude-{uuid4().hex}", template="review", model="sonnet", effort="low", task=task,
            task_text=task.decode("utf-8"), cwd=self.root, directory=self.root / "run", timeout_s=900,
            executable=self.executable.resolve(), baseline="0" * 64, cli_version="2.1.325", requirements={})

    def assert_refused(self, plan, argv):
        with self.assertRaises(LedgerError) as error:
            self.adapter._guard_argv(replace(plan, argv=tuple(argv)))
        self.assertEqual(error.exception.code, 17, argv)

    def test_canonical_profiles_pass(self):
        for bundle in (True, False):
            with self.subTest(bundle=bundle):
                plan = self.plan(bundle)
                self.adapter._guard_argv(plan)
                self.assertEqual(plan.workspace_kind, "read_only" if bundle else "disposable_worktree")
                self.assertEqual(plan.stdin_file.name, "claude.prompt.txt")

    def test_every_forbidden_flag_is_refused(self):
        plan = self.plan(False)
        argv = list(plan.argv)
        for flag in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--bare", "--continue",
                     "--chrome", "--ide", "--bg", "--fork-session", "--safe-mode"):
            with self.subTest(flag=flag):
                self.assert_refused(plan, [*argv, flag])
        for flag, value in (("--add-dir", "C:/"), ("--allowedTools", "Bash"), ("--allowed-tools", "Edit"),
                            ("--disallowedTools", "Read"), ("--mcp-config", "{}"), ("--settings", "{}"),
                            ("--setting-sources", "user"), ("--plugin-dir", "x"), ("--plugin-url", "https://x"),
                            ("--agents", "{}"), ("--resume", "id"), ("--session-id", str(uuid4())),
                            ("--system-prompt", "x"), ("--append-system-prompt", "x"), ("--file", "f:x"),
                            ("--worktree", "x"), ("--cloud", "x"), ("--fallback-model", "opus"),
                            ("--input-format", "stream-json"), ("--debug-file", "x")):
            with self.subTest(flag=flag):
                self.assert_refused(plan, [*argv, flag, value])
        self.assert_refused(plan, [*argv, "--add-dir=C:/"])
        self.assert_refused(plan, [*argv, "extra positional prompt"])

    def test_forbidden_permission_modes_are_refused(self):
        plan = self.plan(False)
        index = plan.argv.index("--permission-mode") + 1
        for mode in ("bypassPermissions", "acceptEdits", "auto", "dontAsk", "manual", "default"):
            with self.subTest(mode=mode):
                self.assert_refused(plan, [*plan.argv[:index], mode, *plan.argv[index + 1:]])
        bundle = self.plan(True)
        self.assert_refused(bundle, [*bundle.argv, "--permission-mode", "plan"])

    def test_every_forbidden_tool_is_refused(self):
        for bundle in (False, True):
            plan = self.plan(bundle)
            index = plan.argv.index("--tools") + 1
            for tools in (*sorted(FORBIDDEN_TOOLS), "Read,Grep,Glob,Bash", "Read Edit", "Bash(git *)", "default",
                          "Read" if bundle else "Read,Grep", "LS"):
                with self.subTest(bundle=bundle, tools=tools):
                    self.assert_refused(plan, [*plan.argv[:index], tools, *plan.argv[index + 1:]])

    def test_missing_or_changed_required_flags_are_refused(self):
        for bundle in (False, True):
            plan = self.plan(bundle)
            argv = list(plan.argv)
            for flag in ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
                with self.subTest(bundle=bundle, missing=flag):
                    self.assert_refused(plan, [token for token in argv if token != flag])
            for flag in ("--output-format", "--json-schema", "--model", "--effort", "--permission-prompts", "--tools",
                         *(() if bundle else ("--permission-mode",))):
                with self.subTest(bundle=bundle, missing=flag):
                    index = argv.index(flag)
                    self.assert_refused(plan, argv[:index] + argv[index + 2:])
            for flag, value in (("--output-format", "text"), ("--output-format", "stream-json"),
                                ("--permission-prompts", "host"), ("--model", "opus")):
                with self.subTest(bundle=bundle, flag=flag, value=value):
                    index = argv.index(flag) + 1
                    self.assert_refused(plan, [*argv[:index], value, *argv[index + 1:]])
            self.assert_refused(plan, [*argv, "-p"])

    def test_first_argument_must_be_a_native_exe(self):
        plan = self.plan(False)
        for first in ("claude.cmd", "claude.bat", "claude.ps1", "claude", r"C:\Windows\System32\cmd.exe",
                      str(self.root / "other" / "claude.exe")):
            with self.subTest(first=first):
                self.assert_refused(plan, [first, *plan.argv[1:]])

    def test_unsupported_effort_and_template_are_refused(self):
        workspace = self.worktree()
        for template, effort in (("delegate-implement", "low"), ("review", "ultra")):
            with self.subTest(template=template, effort=effort), self.assertRaises(LedgerError) as error:
                self.adapter.prepare_invocation(
                    run_id=f"claude-{uuid4().hex}", repo_root=self.repo, template=template, model="sonnet",
                    effort=effort, task=b"task", workspace=workspace, worktree_root=self.trees, timeout_s=None,
                    requirements=None)
            self.assertEqual(error.exception.code, 2)



class ClaudeFixtureTests(WorkerFixture, unittest.TestCase):
    """The real `claude -p --output-format json` envelope, replayed by a stand-in process."""

    adapter_name = "claude-print"
    model = "sonnet"

    def make_adapter(self):
        self.executable = self.root / "bin" / "claude.exe"
        self.executable.write_bytes(b"stand-in path; never executed")
        return ClaudePrintAdapter(self.manager, command=str(self.executable))

    def test_receipts_keep_the_phase_3a_byte_layout(self):
        """Base hooks are opt-in: Claude receipts keep exactly the keys written by phase 3a."""
        import subprocess
        import sys

        def replay(argv, **kwargs):
            code = "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())"
            return self.real_popen([sys.executable, "-I", "-B", "-c", code, str(FIXTURE)], **kwargs)

        self.dispatch(bundle=True, popen=replay, run_id=FIXTURE_RUN_ID)
        directory = self.directory()
        keys = {name: sorted(json.loads((directory / name).read_bytes())) for name in
                ("claude.capture.json", "claude.process.json", "invocation.json")}
        self.assertEqual(keys["claude.capture.json"], [
            "adapter", "classification", "cli_version", "effort_requested", "events_sha256", "executable", "exit_code",
            "invocation_sha256", "launch_detail", "launch_error", "model_requested", "observation",
            "process_receipt_sha256", "reason", "result_sha256", "run_id", "stderr_sha256", "stdout_sha256",
            "task_sha256", "termination", "wall_milliseconds", "workspace_after_sha256", "workspace_changed"])
        self.assertEqual(keys["claude.process.json"], [
            "exit_code", "invocation_sha256", "launch_detail", "launch_error", "process", "run_id", "started_at",
            "stderr", "stdout", "task_sha256", "termination", "tool_events", "wall_milliseconds",
            "workspace_after_sha256", "workspace_changed"])
        self.assertEqual(keys["invocation.json"], [
            "argv", "bundle_manifest_sha256", "cli_version", "cwd", "executable", "launch_profile",
            "permissions_observed", "prompt_sha256", "run_id", "sandbox_observed", "stdin_file", "task_sha256",
            "timeout_s", "tools_observed", "verification_requirements", "workspace_baseline_sha256", "workspace_kind"])
        stored = (directory / "claude.capture.json").read_bytes()
        # Re-collecting rebuilds the receipt and refuses any byte difference from the stored one.
        self.adapter.collect_saved(self.run_id)
        self.assertEqual((directory / "claude.capture.json").read_bytes(), stored)

    def test_redacted_fixture_normalizes_with_the_observed_fields(self):
        raw = FIXTURE.read_bytes()
        self.assertEqual(json.loads(raw)["session_id"], "00000000-0000-4000-8000-000000000001")
        observed = ClaudePrintAdapter(None).normalize_stdout(raw)
        self.assertEqual({key: observed[key] for key in ("conversation_id", "raw_status", "num_turns",
                                                          "model_observed", "effort_observed", "permission_denials")},
                         {"conversation_id": "00000000-0000-4000-8000-000000000001", "raw_status": "SUCCESS",
                          "num_turns": 2, "model_observed": "claude-sonnet-5-5", "effort_observed": "unknown",
                          "permission_denials": 0})
        self.assertEqual(observed["usage"], {"input_tokens": 2, "output_tokens": 272,
                                             "cache_creation_input_tokens": 3839, "cache_read_input_tokens": 0})
        # The result text is the same JSON as structured_output, so the agreement check stays enabled.
        self.assertEqual(json.loads(observed["response"]), observed["structured_output"])
        self.assertEqual(observed["structured_output"]["summary"], "isolation test|0123456789abcdef0123456789abcdef")

    def test_fixture_is_collected_receipt_checked_verified_and_accepted(self):
        import subprocess
        import sys

        def replay(argv, **kwargs):
            self.started.append((tuple(argv), dict(kwargs["env"])))
            code = "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())"
            return self.real_popen([sys.executable, "-I", "-B", "-c", code, str(FIXTURE)], **kwargs)

        view = self.dispatch(bundle=True, popen=replay, run_id=FIXTURE_RUN_ID)
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "structured_result_collected"))
        self.assertEqual(view["claude_capture"]["observation"]["model_observed"], "claude-sonnet-5-5")
        self.assertTrue(ClaudePrintAdapter.capture_matches(self.directory(), self.ledger.get_run(self.run_id),
                                                           view["result_sha256"]))
        self.assertEqual(self.verify()["state"], "SUCCEEDED")
        accepted = self.manager.record_acceptance(self.run_id, accepted=True, reason="fixture", adopted=0, total=0)
        self.assertEqual(accepted["acceptance_status"], "ACCEPTED")

    def test_error_envelope_with_uppercase_success_subtype_is_not_success(self):
        """is_error true with subtype "SUCCESS" must not collide with the internal success value."""
        import subprocess
        import sys

        envelope = json.loads(FIXTURE.read_bytes())
        self.assertEqual((envelope["type"], envelope["subtype"], envelope["is_error"]), ("result", "success", False))
        modified = self.root / "error-envelope.json"
        modified.write_bytes(json.dumps(envelope | {"is_error": True, "subtype": "SUCCESS"}).encode())
        self.assertEqual(ClaudePrintAdapter(None).normalize_stdout(modified.read_bytes())["raw_status"],
                         "CLAUDE:SUCCESS")

        def replay(argv, **kwargs):
            # Exit code 0, so only the envelope can mark the failure.
            code = "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())"
            return self.real_popen([sys.executable, "-I", "-B", "-c", code, str(modified)], **kwargs)

        view = self.dispatch(bundle=True, popen=replay, run_id=FIXTURE_RUN_ID)
        self.assertEqual((view["state"], view["state_reason"], view["exit_code"]),
                         ("FAILED", "worker_status_not_success", 0))
        self.assertFalse((self.directory() / "result.json").exists())
        self.assertFalse((self.directory() / "DONE").exists())


if __name__ == "__main__":
    unittest.main()
