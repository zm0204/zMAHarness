from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
from hashlib import sha256
from io import StringIO
import json
import os
import shutil
import sqlite3
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from mah import cli
from mah.adapters.agy_print import AgyPrintAdapter
from mah.adapters.registry import REFUSED_MODELS
from mah.context_bundle import bundle_manifest, prepare_bundle
from mah.db import SCHEMA
from mah.ledger import GrantLedger, LedgerError, dump
from mah.run_manager import RunManager
from mah.verifier import VerificationEvidence, Verifier


ROOT = Path(__file__).resolve().parents[2]
MODEL = "gemini-3.8-flash-low"
SMOKE_FILE = "驗證文件.txt"
SMOKE_INSTRUCTIONS = ("只分析 Context-Bundle 內的驗證文件.txt。請勿使用工具、讀寫檔案、執行命令或連線。"
                      "以「<標題>｜<驗證字串>」作為 summary，findings 回傳空陣列。")
FAKE_HELP = "--print --output-format --model --effort --mode --sandbox --settings --print-timeout --json-schema --disable-slash-commands --log-file"
WORKER = r"""
import argparse
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

parser = argparse.ArgumentParser()
for flag in ("model", "effort", "settings", "output-format", "print-timeout", "json-schema", "log-file"):
    parser.add_argument("--" + flag, required=True)
parser.add_argument("--mode")
parser.add_argument("--sandbox", action="store_true")
parser.add_argument("--disable-slash-commands", action="store_true")
parser.add_argument("-p", required=True)
args = parser.parse_args()
is_bundle = "# MAH Context-Bundle v1" in args.p
assert args.sandbox and args.model == "gemini-3.8-flash-low" and args.effort == "low"
settings = json.loads(args.settings)
if is_bundle:
    assert args.disable_slash_commands and args.mode is None
    assert settings == {"toolPermission": "request-review", "enableTerminalSandbox": True,
                        "disableSlashCommands": True, "allowNonWorkspaceAccess": False, "useG1Credits": False,
                        "permissions": {"allow": [], "ask": [], "deny": [
                            "read_file(*)", "write_file(*)", "command(*)", "mcp(*)", "read_url(*)", "execute_url(*)"]}}
    assert not list(Path.cwd().iterdir()) and not any((p / ".git").exists() for p in Path.cwd().parents)
    assert "中文 Bundle" in str(Path.cwd())
    bundle = json.loads(args.p.split("# MAH Context-Bundle v1\n", 1)[1].split("\n", 1)[1])
    bundle_path, = bundle["files"]
    text = bundle["files"][bundle_path]
    token_match = re.search(r"驗證字串=([0-9a-f]{32})", text)
    token = token_match[1] if token_match else None
    scenario = "dirty" if token and token.endswith("d") else "success"
else:
    assert not args.disable_slash_commands and args.mode == "plan"
    assert args.p.startswith("MultiAgentHarness V2 ")
    assert settings == {"toolPermission": "request-review", "enableTerminalSandbox": True,
                        "disableSlashCommands": False, "allowNonWorkspaceAccess": False, "useG1Credits": False,
                        "permissions": {"allow": ["read_file(*)"], "ask": [], "deny": [
                            "write_file(*)", "command(*)", "mcp(*)", "read_url(*)", "execute_url(*)"]}}
    assert "中文工作樹" in str(Path.cwd())
    assert "中文任務" in args.p
    scenario = re.search(r"scenario=(\w+)", args.p)[1]
assert args.output_format == "json" and args.print_timeout.endswith("s")
assert sys.stdin.buffer.read() == b""
schema_text = args.json_schema if args.json_schema.startswith("{") else Path(args.json_schema).read_text(encoding="utf-8")
schema = json.loads(schema_text)
run_id = schema["properties"]["run_id"]["const"]
template = schema["properties"]["template"]["const"]
time.sleep(0.1)
if scenario in ("timeout", "logged_timeout", "created_only", "stream_timeout"):
    if scenario != "timeout":
        log = "I1112 22:46:56.253018       1 server.go:1233] Created conversation 12345678-1234-4234-8234-123456789abc\n"
        if scenario == "logged_timeout":
            log += "I1112 22:46:56.254706       1 server.go:1817] Sending user message\n"
        elif scenario == "stream_timeout":
            log += "I1112 22:47:03.887409     990 http_helpers.go:654] URL: https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse\n"
        Path(args.log_file).write_text(log, encoding="utf-8")
    sys.stdout.buffer.write(b"partial")
    sys.stdout.buffer.flush()
    time.sleep(60)
if scenario == "interrupted":
    raise SystemExit(73)
if scenario == "corrupt":
    sys.stdout.buffer.write(b"\xff{")
    sys.stderr.buffer.write(b"\xff")
    raise SystemExit(0)
if scenario == "dirty":
    Path("測試副作用.txt").write_text("synthetic", encoding="utf-8")
payload = {"findings": []}
if is_bundle and template == "review" and ("outside-finding" in text or "distant-finding" in text):
    outside = "outside-finding" in text
    payload["findings"] = [{"file": "unselected.txt" if outside else bundle_path,
                            "line": 0 if outside else 1000, "severity": "minor", "title": "合成發現",
                            "rationale": "合成證據", "suggestion": "獨立核對"}]
if template == "second-opinion":
    payload = {"hypotheses": [{"hypothesis": "替代解釋", "evidence": "觀察", "experiment": "重跑合成測試"}],
               "recommended_next_step": "比較輸出"}
elif template == "research":
    citation_path = "unselected.txt" if "outside-citation" in text else bundle_path
    citation_lines = "L1000-L1001" if "distant-citation" in text else "L1"
    payload = {"answers": [{"question": "合成問題？", "answer": "合成答案",
                            "citations": [{"path": citation_path, "lines": citation_lines, "claim": "合成證據"}]}]}
result = dict(schema_version="2.0", run_id=run_id, template=template, status="completed",
              summary=f"隔離測試｜{token}" if is_bundle and token else "繁體中文\n第二行",
              blocked_reason=None, open_questions=[], template_payload=payload)
envelope = dict(conversation_id=str(uuid4()), status="SUCCESS", response=json.dumps(result, ensure_ascii=False),
                num_turns=1, duration_seconds=0.1, usage={"input_tokens": 1, "output_tokens": 1})
if is_bundle:
    envelope["structured_output"] = result
sys.stderr.buffer.write("合成警告\r\n".encode("utf-8"))
sys.stdout.buffer.write(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
"""


@unittest.skipUnless(os.name == "nt", "Windows agy installation layout")
class AgyExecutableTests(unittest.TestCase):
    def test_default_command_falls_back_to_localappdata_when_path_lookup_fails(self):
        with TemporaryDirectory(dir=ROOT.parent) as tmp:
            installed = Path(tmp) / "agy" / "bin" / "agy.exe"
            installed.parent.mkdir(parents=True)
            installed.touch()
            adapter = AgyPrintAdapter(None, command="agy")
            with patch.dict(os.environ, {"LOCALAPPDATA": tmp}), patch("mah.adapters.agy_print.which") as lookup:
                lookup.return_value = sys.executable
                self.assertEqual(adapter._executable(), Path(sys.executable).resolve())
                lookup.return_value = None
                self.assertEqual(adapter._executable(), installed.resolve())
                lookup.assert_called_with("agy")
                with self.assertRaisesRegex(LedgerError, "executable is unavailable"):
                    AgyPrintAdapter(None, command="missing-custom-agy")._executable()
                with patch.dict(os.environ, {"LOCALAPPDATA": ""}), self.assertRaisesRegex(
                        LedgerError, "executable is unavailable"):
                    adapter._executable()


class AgyProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = TemporaryDirectory(prefix="agy-offline-", dir=ROOT.parent)
        cls.addClassCleanup(scratch.cleanup)
        cls.scratch = Path(scratch.name)
        cls.repo = cls.scratch / "clone"
        subprocess.run(["git", "init", "--quiet", str(cls.repo)], check=True, timeout=15)
        objects = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute",
                                           "--git-path", "objects"], timeout=15).decode("utf-8").strip()
        (cls.repo / ".git" / "objects" / "info" / "alternates").write_bytes((objects + "\n").encode("utf-8"))
        head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], timeout=15).decode().strip()
        subprocess.run(["git", "-C", str(cls.repo), "update-ref", "HEAD", head], check=True, timeout=15)
        cls.trees = cls.scratch / "trees"
        cls.trees.mkdir()
        cls.worker = cls.scratch / "fake_agy.py"
        cls.worker.write_text(WORKER, encoding="utf-8")

    def setUp(self):
        state = TemporaryDirectory(prefix="state-", dir=self.scratch)
        self.addCleanup(state.cleanup)
        self.state = Path(state.name)
        self.workspace = self.trees / f"中文工作樹-{uuid4().hex}"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--quiet", "--detach", str(self.workspace), "HEAD"],
                       check=True, capture_output=True, timeout=15)
        self.ledger = GrantLedger(self.state / "ledger.sqlite3")
        self.manager = RunManager(self.ledger)
        self.adapter = AgyPrintAdapter(self.manager, command=sys.executable)
        self.grant = self.make_grant()
        self.run = f"agy-{uuid4().hex}"
        self.directory = self.state / "runs" / self.run
        self.started = []
        self.real_popen = subprocess.Popen

    @staticmethod
    def task_bytes(scenario="success"):
        return f"中文任務 scenario={scenario}".encode()

    def make_grant(self, scenario="success", *, timeout=5, workspace=None, task_sha=None, max_dispatches=1,
                   template="review", via="manual_cli"):
        card_id = f"card-{uuid4().hex}"
        card = self.ledger.create_card(
            card_id, repo_root=self.repo, task_key=card_id, trigger_ids=["T5"], prepare_mode="explicit",
            allowed_templates=[template], allowed_targets=[{"adapter": "agy-print", "model": MODEL, "effort_max": "low"}],
            max_dispatches=max_dispatches, ttl_minutes=10, timeout_seconds=timeout,
            workspace_path=workspace or self.workspace,
            task_sha256=task_sha or sha256(self.task_bytes(scenario)).hexdigest(),
        )
        # Populate an isolated test Ledger through its API; no interactive confirmation.
        return self.ledger.approve(card_id, card, via=via)

    def another_worktree(self):
        other = self.trees / f"另一工作樹-{uuid4().hex}"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--quiet", "--detach", str(other), "HEAD"],
                       check=True, capture_output=True, timeout=15)
        return other

    def assert_dispatch_denied(self, **changes):
        with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("must not start a model")):
            with self.assertRaises(LedgerError) as error:
                self.run_dispatch(**changes)
        self.assertEqual(error.exception.code, 10)
        self.assertIsNone(self.ledger.get_run(self.run))
        self.assertEqual(self.ledger.get_grant(changes.get("grant_id", self.grant))["used_dispatches"], 0)

    def substitute(self, argv, **kwargs):
        self.assertFalse(kwargs["shell"])
        self.assertEqual(Path(kwargs["cwd"]), self.workspace)
        for key in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT"):
            self.assertNotIn(key, kwargs["env"])
        self.started.append(argv)
        return self.real_popen([sys.executable, "-I", "-B", str(self.worker), *argv[1:]], **kwargs)

    def run_dispatch(self, scenario="success", *, reported_flags=FAKE_HELP, **options):
        supplied = dict(grant_id=self.grant, run_id=self.run, repo_root=self.repo, template="review", model=MODEL,
                        effort="low", task=self.task_bytes(scenario), workspace=self.workspace,
                        worktree_root=self.trees, timeout_s=5)
        with patch.object(self.adapter, "probe", return_value={
                "available": True, "cli_version": "fake-agy", "flags": dict.fromkeys(reported_flags.split(), True)}):
            return self.manager.dispatch(self.adapter, **(supplied | options))

    def test_plan_slash_conflict_is_rejected_before_reservation_or_launch(self):
        build = self.adapter.build_invocation

        def conflicting_plan(**options):
            plan = build(**options)
            args = tuple(arg for arg in plan.argv if arg != "--disable-slash-commands")
            return replace(plan, argv=(*args[:-2], "--disable-slash-commands", *args[-2:]))

        with patch.object(self.adapter, "build_invocation", side_effect=conflicting_plan), patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(LedgerError, r"--mode plan.*--disable-slash-commands"):
                self.run_dispatch()
        self.assertIsNone(self.ledger.get_run(self.run))
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_conflicting_flags_inserted_after_reservation_are_rejected(self):
        launch = self.adapter.launch

        def reinsert(plan):
            return launch(replace(plan, argv=(*plan.argv[:-2], "--disable-slash-commands", *plan.argv[-2:])))

        with patch.object(self.adapter, "launch", side_effect=reinsert), patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(LedgerError, r"--mode plan.*--disable-slash-commands"):
                self.run_dispatch()
        self.assertEqual(self.ledger.get_run(self.run)["state"], "CANCELLED")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_readonly_launch_without_sandbox_is_rejected(self):
        launch = self.adapter.launch

        def drop_sandbox(plan):
            return launch(replace(plan, argv=tuple(arg for arg in plan.argv if arg != "--sandbox")))

        with patch.object(self.adapter, "launch", side_effect=drop_sandbox), patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(LedgerError, r"requires explicit --mode plan and --sandbox"):
                self.run_dispatch()
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_disposable_launch_rejects_changed_permissions_after_reservation(self):
        launch = self.adapter.launch

        def permit_writes(plan):
            settings_index = plan.argv.index("--settings") + 1
            settings = json.loads(plan.argv[settings_index])
            settings["permissions"]["deny"].remove("write_file(*)")
            return launch(replace(plan, argv=(*plan.argv[:settings_index], json.dumps(settings),
                                              *plan.argv[settings_index + 1:])))

        with patch.object(self.adapter, "launch", side_effect=permit_writes), patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(LedgerError, "Disposable Worktree session settings differ"):
                self.run_dispatch()
        self.assertEqual(self.ledger.get_run(self.run)["state"], "CANCELLED")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_end_to_end_preserves_utf8_and_requires_independent_verifier(self):
        self.grant = self.make_grant(timeout=60)
        with patch.dict(os.environ, {"AGY_CLI_DISABLE_AUTO_UPDATE": "1", "UNLISTED_VAR": "secret"}), patch(
                "mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.run_dispatch(timeout_s=60, reported_flags=FAKE_HELP.replace("--disable-slash-commands", ""))
            self.assertEqual(self.adapter.worker_environment().get("AGY_CLI_DISABLE_AUTO_UPDATE"), "1")
            self.assertNotIn("UNLISTED_VAR", self.adapter.worker_environment())
        self.assertEqual(len(self.started), 1)
        if os.name == "nt":
            schema_file = self.directory / "agy.schema.json"
            self.assertTrue(schema_file.is_file())
            invocation = json.loads((self.directory / "invocation.json").read_bytes())
            self.assertEqual(invocation["launch_files"],
                             {"agy.schema.json": sha256(schema_file.read_bytes()).hexdigest()})
            self.assertEqual(invocation["argv"][invocation["argv"].index("--json-schema") + 1], str(schema_file))
        self.assertEqual((view["state"], view["completion_evidence"], view["exit_code"]), ("COLLECTING", "verified", 0))
        self.assertEqual(view["agy_capture"]["workspace_changed"], False)
        self.assertEqual((self.directory / "agy.stderr.txt").read_bytes(), "合成警告\r\n".encode())
        process = json.loads((self.directory / "agy.process.json").read_bytes())
        self.assertEqual(process["process"]["pid"], view["pid"])
        self.assertTrue(process["process"]["identity"])
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)
        verifier = Verifier(self.manager)
        self.assertEqual(verifier.verify(self.run)["state"], "COLLECTING")
        result = (self.directory / "result.json").read_bytes()
        self.assertEqual(json.loads(result)["summary"], "繁體中文\n第二行")
        evidence = VerificationEvidence(view["task_sha256"], sha256(result).hexdigest(),
                                        confirmed=True, workspace_changed=False, reviewed_findings=0)
        accepted = verifier.verify(self.run, evidence=evidence)
        self.assertEqual((accepted["state"], accepted["acceptance_status"]), ("SUCCEEDED", "PENDING"))

    def prepare_bundle_case(self, scenario="success", *, instructions_text=SMOKE_INSTRUCTIONS,
                            payload=None, max_dispatches=1, file_name=SMOKE_FILE, template="review", via="manual_cli"):
        self.smoke_token = "d" * 32 if scenario == "dirty" else "a" * 32
        content = f"標題=隔離測試\n驗證字串={self.smoke_token}\n"
        (self.workspace / file_name).write_text(content if payload is None else payload, encoding="utf-8")
        with (self.workspace / ".gitignore").open("ab") as ignore:
            ignore.write(f"\n!/{file_name}\n".encode("utf-8"))
        self.smoke_instructions = self.state / "bundle-instructions.md"
        self.smoke_instructions.write_text(instructions_text, encoding="utf-8")
        self.smoke_task = self.state / "bundle" / "task.md"
        prepare_bundle(self.workspace, [file_name], self.smoke_instructions, self.smoke_task.parent, max_bytes=32768)
        task = self.smoke_task.read_bytes()
        scratch = TemporaryDirectory(prefix="mah-bundle-worker-", dir=ROOT.parent)
        self.addCleanup(scratch.cleanup)
        self.bundle_root = Path(scratch.name)
        self.workspace = self.bundle_root / f"中文 Bundle-{uuid4().hex}"
        self.workspace.mkdir()
        self.grant = self.make_grant(timeout=60, workspace=self.workspace, task_sha=sha256(task).hexdigest(),
                                     max_dispatches=max_dispatches, template=template, via=via)
        return task

    def dispatch_bundle(self, task, **options):
        return self.run_dispatch(task=task, timeout_s=60,
                                 **({"worktree_root": self.bundle_root} | options))

    def test_bundle_uses_inline_context_without_plan_or_read_tool_and_requires_verifier(self):
        task = self.prepare_bundle_case()
        with patch(
                "mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.dispatch_bundle(task)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(view["state"], "COLLECTING")
        self.assertEqual(view["completion_evidence"], "verified")
        self.assertEqual(view["workspace_kind"], "read_only")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)
        invocation = json.loads((self.directory / "invocation.json").read_bytes())
        self.assertEqual(invocation["bundle_manifest_sha256"], bundle_manifest(task)[1])
        self.assertEqual(invocation["launch_profile"], "context-bundle")
        for field in ("permissions_observed", "sandbox_observed", "tools_observed"):
            self.assertEqual(invocation[field], "unknown")
        self.assertEqual(json.loads((self.directory / "result.json").read_bytes())["summary"],
                         f"隔離測試｜{self.smoke_token}")
        self.assertEqual(invocation["verification_requirements"]["required_tools"], [])
        self.assertNotIn("--mode", invocation["argv"])
        self.assertIn("--disable-slash-commands", invocation["argv"])
        self.assertEqual(view["agy_capture"]["invocation_sha256"],
                         sha256((self.directory / "invocation.json").read_bytes()).hexdigest())
        self.assertEqual(view["agy_capture"]["workspace_after_sha256"], invocation["workspace_baseline_sha256"])
        self.assertEqual(view["agy_capture"]["process_receipt_sha256"],
                         sha256((self.directory / "agy.process.json").read_bytes()).hexdigest())
        verifier = Verifier(self.manager)
        self.assertEqual(verifier.verify(self.run)["verification"]["reason"], "controller_evidence_missing")
        evidence = VerificationEvidence(view["task_sha256"], view["result_sha256"], confirmed=True,
            workspace_changed=False, reviewed_findings=0, bundle_manifest_sha256=bundle_manifest(task)[1])
        verified = verifier.verify(self.run, evidence=evidence)
        self.assertEqual((verified["state"], verified["acceptance_status"]), ("SUCCEEDED", "PENDING"))

    def test_bundle_accepts_ordinary_project_content_without_smoke_flag(self):
        task = self.prepare_bundle_case(file_name="測試模組.py", payload="def add(a, b): return a + b",
                                        instructions_text="獨立審查選定程式碼", via="codex_prompt")
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.dispatch_bundle(task)
        self.assertEqual(view["state"], "COLLECTING")
        self.assertEqual(len(self.started), 1)
        grant = self.ledger.get_grant(self.grant)
        self.assertEqual((grant["approved_via"], grant["approval_evidence"], grant["used_dispatches"]),
                         ("codex_prompt", "codex_desktop_decision", 1))

    def test_bundle_rejects_plain_task_in_empty_directory_and_write_template(self):
        task = self.prepare_bundle_case()
        with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(LedgerError, "linked worktree"):
                self.dispatch_bundle(self.task_bytes())
            with self.assertRaisesRegex(LedgerError, "read-only analysis template"):
                self.dispatch_bundle(task, template="delegate-implement")
        self.assertIsNone(self.ledger.get_run(self.run))

    def verify_analysis_cli(self, template, task, extra, *, requirements=None, expected_state="SUCCEEDED"):
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.dispatch_bundle(task, template=template, requirements=requirements)
        self.assertEqual(view["state"], "COLLECTING")
        self.assertEqual(view["completion_evidence"], "verified")
        config = self.state / "analysis.toml"
        config.write_text('[paths]\nstate_dir = "."\n', encoding="utf-8")
        # Controller evidence sits outside the repo, worker root, and run artifacts.
        evidence = self.state / "controller-proof.json"
        evidence.write_text(json.dumps({
            "run_id": self.run, "task_sha256": view["task_sha256"], "result_sha256": view["result_sha256"],
            "bundle_manifest_sha256": bundle_manifest(task)[1],
            "confirmed": True, "workspace_changed": False, **extra,
        }, ensure_ascii=False), encoding="utf-8")
        args = ["--config", str(config), "verify", self.run, "--evidence-file", str(evidence)]
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = cli.main(args)
        self.assertEqual(code, 0 if expected_state == "SUCCEEDED" else 20)
        verified = json.loads(stdout.getvalue())
        self.assertEqual((verified["state"], verified["acceptance_status"]), (expected_state, "PENDING"))
        return verified

    def test_bundle_second_opinion_accepts_experiment_evidence(self):
        task = self.prepare_bundle_case(template="second-opinion", via="codex_prompt")
        self.verify_analysis_cli("second-opinion", task, {"experiments": ["重跑合成測試"]})

    def test_bundle_research_accepts_manifest_citation_evidence(self):
        task = self.prepare_bundle_case(template="research", file_name="研究來源.md", payload="合成證據",
                                        instructions_text="回答合成問題？", via="codex_prompt")
        self.verify_analysis_cli("research", task, {"checked_citations": [["研究來源.md", "L1", "合成證據"]]},
                                 requirements={"questions": ["合成問題？"]})

    def test_bundle_research_leaves_line_accuracy_to_controller(self):
        task = self.prepare_bundle_case(template="research", file_name="研究來源.md", payload="distant-citation",
                                        instructions_text="回答合成問題？", via="codex_prompt")
        self.verify_analysis_cli("research", task, {"checked_citations": [["研究來源.md", "L1000-L1001", "合成證據"]]},
                                 requirements={"questions": ["合成問題？"]})

    def test_bundle_review_leaves_line_accuracy_to_controller(self):
        task = self.prepare_bundle_case(payload="distant-finding", via="codex_prompt")
        self.verify_analysis_cli("review", task, {"reviewed_findings": 1})

    def test_bundle_review_rejects_unselected_path_even_at_line_zero(self):
        task = self.prepare_bundle_case(payload="outside-finding", via="codex_prompt")
        view = self.verify_analysis_cli("review", task, {"reviewed_findings": 1}, expected_state="FAILED")
        self.assertEqual(view["verification"]["reason"], "bundle_finding_out_of_scope")

    def test_bundle_research_rejects_a_checked_citation_outside_manifest(self):
        task = self.prepare_bundle_case(template="research", file_name="研究來源.md", payload="outside-citation",
                                        instructions_text="回答合成問題？", via="codex_prompt")
        view = self.verify_analysis_cli("research", task, {"checked_citations": [["unselected.txt", "L1", "合成證據"]]},
                                        requirements={"questions": ["合成問題？"]}, expected_state="FAILED")
        self.assertEqual(view["verification"]["reason"], "bundle_citation_out_of_scope")

    def test_bundle_smoke_rejects_a_multi_use_grant(self):
        task = self.prepare_bundle_case(max_dispatches=2)
        with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(LedgerError, "one dispatch"):
                self.dispatch_bundle(task)
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_bundle_cli_runs_once_and_excludes_billing_environment(self):
        self.prepare_bundle_case()
        settings = self.state / "smoke.toml"
        settings.write_text(
            '[paths]\nstate_dir = "."\nworktree_root = ' + json.dumps(self.bundle_root.as_posix()) +
            '\n[adapters]\nenabled = ["agy-print"]\nagy_command = ' + json.dumps(Path(sys.executable).as_posix()) + '\n',
            encoding="utf-8",
        )
        args = ["--config", str(settings), "dispatch", "--grant", self.grant, "--run-id", self.run, "--template", "review",
                "--adapter", "agy-print", "--model", MODEL, "--effort", "low", "--task-file", str(self.smoke_task),
                "--workspace", str(self.workspace), "--timeout", "60"]
        inherited = dict.fromkeys(("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                                   "GOOGLE_CLOUD_PROJECT"), "synthetic")
        with patch.dict(os.environ, inherited), patch("mah.adapters.agy_print.Popen", side_effect=self.substitute), patch.object(
                AgyPrintAdapter, "probe", return_value={"available": True, "cli_version": "fake-agy",
                                                       "flags": dict.fromkeys(FAKE_HELP.split(), True)}), redirect_stdout(
                StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(cli.main(args), 30)
            self.assertNotEqual(cli.main(args), 0)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_smoke_requires_the_reported_safety_flags(self):
        task = self.prepare_bundle_case()
        with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            for flag in ("--sandbox", "--settings", "--disable-slash-commands"):
                with self.subTest(flag=flag), self.assertRaisesRegex(LedgerError, "required print flags"):
                    self.dispatch_bundle(task, reported_flags=FAKE_HELP.replace(flag, ""))
        self.assertIsNone(self.ledger.get_run(self.run))
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_bundle_rejects_unverified_window_receipt_before_reservation(self):
        task = self.prepare_bundle_case()
        for via in ("windows_gui", "manual_cli"):
            with self.subTest(via=via):
                with closing(sqlite3.connect(self.ledger.database)) as db, db:
                    db.execute("UPDATE grants SET approved_via = ?, approval_evidence = ? WHERE grant_id = ?",
                               (via, "local_window_interaction", self.grant))
                with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
                    with self.assertRaisesRegex(LedgerError, "approval source"):
                        self.dispatch_bundle(task)
                self.assertIsNone(self.ledger.get_run(self.run))
                self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_bundle_rechecks_approval_source_at_launch_claim(self):
        task = self.prepare_bundle_case()
        launch = self.adapter.launch

        def replace_receipt(plan):
            with closing(sqlite3.connect(self.ledger.database)) as db, db:
                db.execute("UPDATE grants SET approved_via = 'windows_gui', "
                           "approval_evidence = 'local_window_interaction' WHERE grant_id = ?", (self.grant,))
            return launch(plan)

        with patch.object(self.adapter, "launch", side_effect=replace_receipt), patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(LedgerError, "approval source"):
                self.dispatch_bundle(task)
        self.assertEqual(self.ledger.get_run(self.run)["state"], "CANCELLED")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)
        self.assertFalse((self.directory / "invocation.json").exists())

    def test_bundle_scope_snapshot_must_match_approved_card(self):
        task = self.prepare_bundle_case()
        grant = self.ledger.get_grant(self.grant)
        changes = {"allowed_templates": dump(["review", "research"]),
                   "allowed_targets": dump([{"adapter": "agy-print", "model": MODEL, "effort_max": "medium"}]),
                   "max_dispatches": 2}
        for field, replacement in changes.items():
            with self.subTest(field=field):
                with closing(sqlite3.connect(self.ledger.database)) as db, db:
                    db.execute(f"UPDATE grants SET {field} = ? WHERE grant_id = ?", (replacement, self.grant))
                try:
                    with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
                        with self.assertRaisesRegex(LedgerError, "approved card"):
                            self.dispatch_bundle(task)
                    self.assertIsNone(self.ledger.get_run(self.run))
                    self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)
                finally:
                    with closing(sqlite3.connect(self.ledger.database)) as db, db:
                        db.execute(f"UPDATE grants SET {field} = ? WHERE grant_id = ?", (grant[field], self.grant))

    def test_bundle_rejects_nonempty_workspace_before_reservation(self):
        task = self.prepare_bundle_case()
        (self.workspace / "unexpected.txt").write_text("synthetic", encoding="utf-8")
        with patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(LedgerError, "empty"):
                self.dispatch_bundle(task)
        self.assertIsNone(self.ledger.get_run(self.run))

    def test_bundle_rejects_git_worktree_even_if_grant_matches(self):
        git_tree = self.workspace
        task = self.prepare_bundle_case()
        self.grant = self.make_grant(timeout=60, workspace=git_tree, task_sha=sha256(task).hexdigest())
        with patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(LedgerError, "outside.*Git"):
                self.dispatch_bundle(task, workspace=git_tree, worktree_root=self.trees)
        self.assertIsNone(self.ledger.get_run(self.run))

    def test_bundle_workspace_write_is_a_readonly_violation(self):
        task = self.prepare_bundle_case("dirty")
        with patch(
                "mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.dispatch_bundle(task)
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "readonly_violation"))
        self.assertTrue(view["agy_capture"]["workspace_changed"])

    def test_bundle_tampered_session_settings_never_reach_popen(self):
        task = self.prepare_bundle_case()
        launch = self.adapter.launch

        def loosen(plan):
            argv = list(plan.argv)
            i = argv.index("--settings") + 1
            settings = json.loads(argv[i])
            if enable_credits:
                settings["useG1Credits"] = True
            else:
                settings["permissions"]["deny"].remove("command(*)")
            argv[i] = json.dumps(settings)
            return launch(replace(plan, argv=tuple(argv)))

        for enable_credits in (False, True):
            with self.subTest(enable_credits=enable_credits):
                self.grant = self.make_grant(timeout=60, workspace=self.workspace, task_sha=sha256(task).hexdigest())
                self.run = f"agy-{uuid4().hex}"
                with patch.object(self.adapter, "launch", side_effect=loosen), patch(
                        "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
                    with self.assertRaisesRegex(LedgerError, "session settings"):
                        self.dispatch_bundle(task)
                self.assertEqual(self.ledger.get_run(self.run)["state"], "CANCELLED")
                grant = self.ledger.get_grant(self.grant)
                self.assertEqual((grant["used_dispatches"], grant["status"]), (0, "CLOSED"))

    def test_bundle_does_not_remove_explicit_tool_evidence_requirements(self):
        task = self.prepare_bundle_case()
        with patch(
                "mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.dispatch_bundle(task, requirements={"required_tools": ["read_file"]})
        evidence = VerificationEvidence(view["task_sha256"], view["result_sha256"], confirmed=True,
            workspace_changed=False, reviewed_findings=0, bundle_manifest_sha256=bundle_manifest(task)[1])
        verified = Verifier(self.manager).verify(self.run, evidence=evidence)
        self.assertEqual(verified["state"], "COLLECTING")
        self.assertEqual(verified["verification"]["reason"], "required_tool_missing")

    def test_bundle_task_change_cannot_reuse_grant(self):
        source = self.workspace
        task = self.prepare_bundle_case()
        (source / SMOKE_FILE).write_text(f"標題=隔離測試\n驗證字串={'b' * 32}\n", encoding="utf-8")
        second = self.state / "different-bundle"
        prepare_bundle(source, [SMOKE_FILE], self.smoke_instructions, second, max_bytes=32768)
        changed = (second / "task.md").read_bytes()
        first_manifest, first_digest = bundle_manifest(task)
        second_manifest, second_digest = bundle_manifest(changed)
        self.assertEqual(first_manifest["files"][0]["path"], second_manifest["files"][0]["path"])
        self.assertNotEqual(first_digest, second_digest)
        with patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaises(LedgerError) as error:
                self.dispatch_bundle(changed)
        self.assertEqual(error.exception.code, 10)
        self.assertIsNone(self.ledger.get_run(self.run))
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_bundle_workspace_changes_after_claim_preserve_unknown(self):
        task = self.prepare_bundle_case()
        claim = self.manager.claim_launch

        def change_directory(*args, **kwargs):
            scope = claim(*args, **kwargs)
            (self.workspace / "unexpected.txt").touch()
            return scope

        with patch.object(
                self.manager, "claim_launch", side_effect=change_directory), patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(LedgerError, "changed after launch claim"):
                self.dispatch_bundle(task)
        self.assertEqual(self.ledger.get_run(self.run)["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)
        self.assertTrue((self.directory / "invocation.json").is_file())

    def test_disposable_worktree_changes_after_claim_prevent_launch(self):
        claim = self.manager.claim_launch

        def change_worktree(*args, **kwargs):
            scope = claim(*args, **kwargs)
            (self.workspace / "README.md").write_bytes(b"Changed after claim\n")
            return scope

        with patch.object(self.manager, "claim_launch", side_effect=change_worktree), patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(LedgerError, "changed after launch claim"):
                self.run_dispatch()
        self.assertEqual(self.ledger.get_run(self.run)["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_bundle_with_missing_workspace_evidence_stays_pending(self):
        task = self.prepare_bundle_case()
        observe = self.adapter.observe

        def missing_snapshot(handle):
            with patch("mah.adapters.agy_print._bundle_workspace_snapshot", side_effect=OSError("unreadable")):
                return observe(handle)

        with patch.object(
                self.adapter, "observe", side_effect=missing_snapshot), patch(
                "mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.dispatch_bundle(task)
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "bundle_workspace_evidence_missing"))
        self.assertIsNone(view["agy_capture"]["workspace_changed"])
        self.assertFalse((self.directory / "DONE").exists())

    def test_missing_grant_and_out_of_scope_never_spawn(self):
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            for changes in ({"grant_id": "missing"}, {"effort": "high"}, {"model": "wrong"}):
                with self.assertRaises(LedgerError) as error:
                    self.run_dispatch(**changes)
                self.assertEqual(error.exception.code, 10)
        self.assertEqual(self.started, [])
        self.assertIsNone(self.ledger.get_run(self.run))

    def test_orchestrator_model_is_refused_before_probe_reservation_or_launch(self):
        with patch.object(self.adapter, "probe", side_effect=AssertionError("must not probe")), patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not start a model")):
            for model in ("gemini-pro-orchestrator-neon", "gemini-pro-orchestrator-g1-pro"):
                with self.subTest(model=model), self.assertRaises(LedgerError) as error:
                    self.manager.dispatch(
                        self.adapter, grant_id=self.grant, run_id=self.run, repo_root=self.repo, template="review",
                        model=model, effort="low", task=self.task_bytes(), workspace=self.workspace,
                        worktree_root=self.trees, timeout_s=5)
                self.assertEqual(error.exception.code, 2)
                self.assertIn("子代理", str(error.exception))
        self.assertIsNone(self.ledger.get_run(self.run))
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_orchestrator_grant_from_before_the_rule_still_cannot_dispatch(self):
        model = "gemini-pro-orchestrator-neon"
        card_id = f"card-{uuid4().hex}"
        # A card stored before the refusal existed: create it without the refusal entry.
        with patch.dict(REFUSED_MODELS, clear=True):
            card = self.ledger.create_card(
                card_id, repo_root=self.repo, task_key=card_id, trigger_ids=["T5"], prepare_mode="explicit",
                allowed_templates=["review"], allowed_targets=[{"adapter": "agy-print", "model": model, "effort_max": "low"}],
                max_dispatches=1, ttl_minutes=10, timeout_seconds=5, workspace_path=self.workspace,
                task_sha256=sha256(self.task_bytes()).hexdigest())
            grant = self.ledger.approve(card_id, card, via="manual_cli")
        with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("must not start a model")):
            with self.assertRaises(LedgerError) as error:
                self.run_dispatch(grant_id=grant, model=model)
        self.assertEqual(error.exception.code, 2)
        self.assertIsNone(self.ledger.get_run(self.run))
        self.assertEqual(self.ledger.get_grant(grant)["used_dispatches"], 0)

    def test_scoped_grant_rejects_different_timeout_before_launch(self):
        self.assert_dispatch_denied(grant_id=self.make_grant(timeout=60), timeout_s=120)

    def test_scoped_grant_rejects_different_valid_worktree(self):
        self.assert_dispatch_denied(workspace=self.another_worktree())

    def test_scoped_grant_rejects_wrong_task_sha256(self):
        self.assert_dispatch_denied(grant_id=self.make_grant(task_sha="0" * 64))

    def test_scope_fields_are_hashed_and_snapshot_survives_draft_edits(self):
        grant = self.ledger.get_grant(self.grant)
        card = self.ledger.get_card(grant["card_id"])
        content = json.loads(card["content_json"])
        scope = json.loads(grant["dispatch_scope_json"])
        for name, value in (("timeout_seconds", 120), ("workspace_path", str(self.another_worktree())),
                            ("task_sha256", "0" * 64)):
            with self.subTest(field=name):
                self.assertEqual(scope[name], content[name])
                self.assertNotEqual(sha256(dump(content | {name: value}).encode()).hexdigest(),
                                    card["content_sha256"])
        with self.assertRaises(LedgerError):
            self.ledger.create_card(**{
                key: value for key, value in (content | {"timeout_seconds": 120}).items()
                if key != "workspace_identity"
            })
        self.assertEqual(json.loads(self.ledger.get_grant(self.grant)["dispatch_scope_json"]), scope)
        self.assert_dispatch_denied(timeout_s=120)

    def test_modified_grant_snapshot_cannot_enlarge_approved_card(self):
        scope = json.loads(self.ledger.get_grant(self.grant)["dispatch_scope_json"])
        scope["timeout_seconds"] = 120
        with closing(sqlite3.connect(self.ledger.database)) as db, db:
            db.execute("UPDATE grants SET dispatch_scope_json = ? WHERE grant_id = ?",
                       (dump(scope), self.grant))
        self.assert_dispatch_denied(timeout_s=120)

    @unittest.skipUnless(os.name == "nt", "Windows path aliases")
    def test_relative_case_variant_of_chinese_path_matches_approved_directory(self):
        variant = os.path.relpath(str(self.workspace).swapcase(), str(ROOT).swapcase())
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            self.assertEqual(self.run_dispatch(workspace=variant)["state"], "COLLECTING")
        self.assertEqual(len(self.started), 1)

    @unittest.skipUnless(os.name == "nt", "Windows Junction")
    def test_retargeted_junction_cannot_switch_approved_worktree(self):
        alias = self.trees / f"junction-{uuid4().hex}"

        def junction(target):
            created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
                                     capture_output=True, timeout=10)
            if created.returncode and b"access is denied" in created.stderr.lower():
                self.skipTest("This test process cannot create Windows Junctions")
            created.check_returncode()

        junction(self.workspace)
        self.addCleanup(lambda: alias.rmdir() if alias.exists() else None)
        grant = self.make_grant(workspace=alias)
        alias.rmdir()
        junction(self.another_worktree())
        self.assert_dispatch_denied(grant_id=grant, workspace=alias)
        alias.rmdir()
        junction(self.workspace)
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            self.assertEqual(self.run_dispatch(grant_id=grant, workspace=alias)["state"], "COLLECTING")
        self.assertEqual(len(self.started), 1)

    def test_rebuilt_invocation_with_changed_timeout_is_rejected_at_claim(self):
        launch = self.adapter.launch

        def mutate(plan):
            meta = json.loads(plan.metadata)
            task = self.task_bytes()
            changed = self.adapter.build_invocation(run_id=self.run, template="review", model=MODEL, effort="low", task=task,
                            task_text=task.decode(), cwd=plan.cwd, directory=self.directory, timeout_s=120,
                            executable=Path(plan.argv[0]), baseline=plan.baseline,
                            cli_version=meta["cli_version"], requirements=meta["verification_requirements"])
            return launch(changed)

        with patch.object(self.adapter, "launch", side_effect=mutate), patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not start a model")):
            with self.assertRaisesRegex(LedgerError, "Timeout differs"):
                self.run_dispatch()
        self.assertEqual(self.ledger.get_run(self.run)["state"], "CANCELLED")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_directory_replaced_after_claim_blocks_popen_and_keeps_debit(self):
        claim = self.manager.claim_launch

        def replace_directory(*args, **kwargs):
            scope = claim(*args, **kwargs)
            self.workspace.rename(self.workspace.with_name(self.workspace.name + "-original"))
            self.workspace.mkdir()
            return scope

        with patch.object(self.manager, "claim_launch", side_effect=replace_directory), patch(
                "mah.adapters.agy_print.Popen", side_effect=AssertionError("must not start a model")):
            with self.assertRaises(LedgerError) as error:
                self.run_dispatch()
        self.assertEqual(error.exception.code, 21)
        self.assertEqual(self.ledger.get_run(self.run)["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)
        self.manager.resolve(self.run, action="mark_failed")
        self.assertEqual(self.ledger.get_grant(self.grant)["status"], "EXHAUSTED")
        with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("must not start a model")):
            with self.assertRaises(LedgerError):
                self.run_dispatch(run_id=f"agy-{uuid4().hex}")

    def test_migrated_legacy_agy_grant_cannot_start(self):
        path = self.state / "legacy.sqlite3"
        row = self.ledger.get_grant(self.grant)
        card = dict(self.ledger.get_card(row["card_id"]))
        content = json.loads(card["content_json"])
        for key in json.loads(row["dispatch_scope_json"]):
            content.pop(key, None)
        card["content_json"] = dump(content)
        card["content_sha256"] = sha256(card["content_json"].encode()).hexdigest()
        grant = {key: row[key] for key in row.keys() if key not in ("content_sha256", "dispatch_scope_json")}
        with closing(sqlite3.connect(path)) as db, db:
            for statement in SCHEMA:
                db.execute(statement.replace(", dispatch_scope_json TEXT", ""))
            db.execute("INSERT INTO schema_meta VALUES ('ledger_schema_version', '1')")
            for table, data in (("cards", card), ("grants", grant)):
                columns = ", ".join(data)
                parameters = ", ".join("?" for key in data)
                db.execute(f"INSERT INTO {table} ({columns}) VALUES ({parameters})", tuple(data.values()))
        self.ledger = GrantLedger(path)
        self.manager = RunManager(self.ledger)
        self.adapter = AgyPrintAdapter(self.manager, command=sys.executable)
        self.assertIsNone(self.ledger.get_grant(self.grant)["dispatch_scope_json"])
        self.assert_dispatch_denied()
        self.assertEqual(len(list(self.state.glob("legacy.sqlite3.v1.bak.*"))), 1)
        self.assertEqual(self.ledger.get_card(row["card_id"])["content_sha256"], card["content_sha256"])

    def test_duplicate_run_never_spawns_twice(self):
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            self.run_dispatch()
            with self.assertRaises(LedgerError):
                self.run_dispatch()
        self.assertEqual(len(self.started), 1)

    def test_grant_closed_between_reservation_and_launch_cannot_spawn(self):
        prepare = self.manager.prepare

        def close_after_prepare(*args):
            prepare(*args)
            self.ledger.close(self.grant)

        with patch.object(self.manager, "prepare", side_effect=close_after_prepare), patch(
                "mah.adapters.agy_print.Popen", side_effect=self.substitute):
            with self.assertRaises(LedgerError) as error:
                self.run_dispatch()
        self.assertEqual(error.exception.code, 12)
        self.assertEqual(self.started, [])
        self.assertEqual(self.ledger.get_run(self.run)["state"], "CANCELLED")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_changed_plan_arguments_cannot_escape_reserved_scope(self):
        launch = self.adapter.launch

        def change_model(plan):
            argv = list(plan.argv)
            argv[argv.index("--model") + 1] = "outside-grant"
            return launch(replace(plan, argv=tuple(argv)))

        with patch.object(self.adapter, "launch", side_effect=change_model), patch(
                "mah.adapters.agy_print.Popen", side_effect=self.substitute):
            with self.assertRaises(LedgerError):
                self.run_dispatch()
        self.assertEqual(self.started, [])
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_primary_repo_is_rejected_as_worker_cwd(self):
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            with self.assertRaises(LedgerError):
                self.run_dispatch(workspace=self.repo)
        self.assertEqual(self.started, [])
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_confirmed_process_start_failure_refunds(self):
        with patch("mah.adapters.agy_print.Popen", side_effect=FileNotFoundError(2, "synthetic")):
            view = self.run_dispatch()
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED_PRE_SUBMIT", "process_not_started"))
        grant = self.ledger.get_grant(self.grant)
        self.assertEqual((grant["used_dispatches"], grant["status"]), (0, "CLOSED"))

    def test_child_exit_without_submission_is_unknown(self):
        self.grant = self.make_grant("interrupted")
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.run_dispatch("interrupted")
        self.assertEqual((view["state"], view["exit_code"]), ("SUBMISSION_UNKNOWN", 73))
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_timeout_keeps_partial_stream_and_is_unknown(self):
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.run_dispatch("timeout", timeout_s=1, grant_id=self.make_grant("timeout", timeout=1))
        self.assertEqual(view["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(view["agy_capture"]["termination"], "timeout")
        self.assertEqual((self.directory / "agy.stdout.json").read_bytes(), b"partial")

    def test_timeout_with_log_submission_preserves_conversation_and_does_not_relaunch(self):
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.run_dispatch("logged_timeout", timeout_s=1, grant_id=self.make_grant("logged_timeout", timeout=1))
        self.assertEqual((view["state"], view["state_reason"]), ("TIMED_OUT", "process_timed_out"))
        conversation = "12345678-1234-4234-8234-123456789abc"
        self.assertEqual(view["conversation_id"], conversation)
        evidence = view["agy_capture"]["log_submission"]
        self.assertEqual(evidence["conversation_id"], conversation)
        self.assertEqual(evidence["sha256"], sha256((self.directory / "agy.cli.log").read_bytes()).hexdigest())
        self.assertEqual((self.directory / "agy.stdout.json").read_bytes(), b"partial")
        self.assertFalse((self.directory / "DONE").exists())
        with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("recovery cannot launch")):
            self.assertEqual(self.adapter.collect_saved(self.run)["state"], "TIMED_OUT")
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.ledger.get_grant(view["grant_id"])["used_dispatches"], 1)

    def test_timeout_with_conversation_creation_only_keeps_unknown(self):
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.run_dispatch("created_only", timeout_s=1, grant_id=self.make_grant("created_only", timeout=1))
        self.assertEqual((view["state"], view["state_reason"]), ("SUBMISSION_UNKNOWN", "submission_evidence_missing"))
        self.assertIsNone(view["conversation_id"])

    def test_timeout_with_stream_request_log_counts_as_submitted(self):
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.run_dispatch("stream_timeout", timeout_s=1, grant_id=self.make_grant("stream_timeout", timeout=1))
        self.assertEqual((view["state"], view["state_reason"]), ("TIMED_OUT", "process_timed_out"))
        self.assertEqual(view["conversation_id"], "12345678-1234-4234-8234-123456789abc")

    def test_malformed_streams_are_retained_without_done(self):
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.run_dispatch("corrupt", grant_id=self.make_grant("corrupt"))
        self.assertEqual(view["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual((self.directory / "agy.stdout.json").read_bytes(), b"\xff{")
        self.assertEqual((self.directory / "agy.stderr.txt").read_bytes(), b"\xff")
        self.assertFalse((self.directory / "DONE").exists())

    def test_confirmed_readonly_violation_fails(self):
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute):
            view = self.run_dispatch("dirty", grant_id=self.make_grant("dirty"))
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "readonly_violation"))

    def test_trusted_policy_denial_at_observation_boundary_fails_despite_exit_zero(self):
        observe = self.adapter.observe

        def with_denial(*args, **kwargs):
            return replace(observe(*args, **kwargs), tool_events=(
                {"type": "tool", "name": "read-fixture", "status": "denied", "exit_code": None},
            ))

        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute), patch.object(
                self.adapter, "observe", side_effect=with_denial):
            view = self.run_dispatch(requirements={"required_tools": ["read-fixture"]})
        self.assertEqual((view["state"], view["state_reason"], view["exit_code"]),
                         ("FAILED", "required_action_denied", 0))

    def test_recovery_consumes_saved_process_receipt_without_spawning(self):
        with patch("mah.adapters.agy_print.Popen", side_effect=self.substitute), patch.object(
                self.adapter, "collect_saved", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_dispatch()
        manager = RunManager(GrantLedger(self.ledger.database))
        self.assertEqual(manager.scan()[0]["state"], "SUBMISSION_UNKNOWN")
        recovery = AgyPrintAdapter(manager)
        with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("recovery must not launch")):
            once = recovery.collect_saved(self.run)
            twice = recovery.collect_saved(self.run)
        self.assertEqual((once["state"], once["completion_evidence"]), ("COLLECTING", "verified"))
        self.assertEqual(once, twice)
        self.assertEqual(len(self.started), 1)

    def test_cli_uses_toml_executable_and_dispatches_through_manager(self):
        settings = self.state / "fixture.toml"
        # Keep the configured stand-in on the fixture drive. The Popen substitute runs the real interpreter.
        executable = self.state / Path(sys.executable).name
        shutil.copy2(sys.executable, executable)
        relative_exe = "./" + executable.name
        settings.write_text(
            '[paths]\nstate_dir = "."\nworktree_root = "../trees"\n'
            '[adapters]\nenabled = ["agy-print"]\nagy_command = ' + json.dumps(relative_exe) + '\n',
            encoding="utf-8",
        )
        task = self.state / "task.md"
        task.write_text("中文任務 scenario=success", encoding="utf-8")
        args = ["--config", str(settings), "dispatch", "--grant", self.grant, "--run-id", self.run, "--template", "review",
                "--adapter", "agy-print", "--model", MODEL, "--effort", "low", "--task-file", str(task),
                "--workspace", str(self.workspace)]
        stdout = StringIO()
        # Fixture paths stay within this project for the offline run.
        with patch("mah.config._local_path", side_effect=lambda path, name: path), patch(
                "mah.adapters.agy_print.Popen", side_effect=self.substitute), patch.object(
                AgyPrintAdapter, "probe", return_value={"available": True, "cli_version": "fake-agy", "flags": {flag: True for flag in FAKE_HELP.split()}}), redirect_stdout(stdout):
            task.write_bytes(self.task_bytes() + b"\n")
            with redirect_stderr(StringIO()):
                self.assertEqual(cli.main(args), 10)
            self.assertEqual(self.started, [])
            self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)
            task.write_bytes(self.task_bytes())
            self.assertEqual(cli.main(args), 30)
        self.assertEqual(Path(self.started[0][0]), executable.resolve())
        self.assertEqual(self.started[0][self.started[0].index("--print-timeout") + 1], "5s")
        self.assertEqual(json.loads(stdout.getvalue())["state"], "COLLECTING")
