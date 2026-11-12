import json
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from mah.config import default_config_path, load_config
from mah.fake_adapter import FakeAdapter
from mah.ledger import GrantLedger, LedgerError
from mah.run_manager import RunManager

REPO = Path(__file__).resolve().parents[2]
# The test provides synthetic terminal input. Production has no such option.
CONSOLE_FIXTURE = """
import builtins
import re
import sys
from mah.cli import main

sys.stdin.isatty = lambda: True
sys.stdout.isatty = lambda: True
if sys.argv.pop(1) == "confirm":
    builtins.input = lambda prompt: re.search(r"APPROVE [A-Za-z0-9_-]+ [A-F0-9]{8}", prompt).group()
else:
    builtins.input = lambda prompt: "decline"
raise SystemExit(main())
"""


class CliTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah CLI 中文 ", dir=REPO.parent)
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.config = self.root / "harness.local.toml"
        self.config.write_text('[paths]\nstate_dir = "本機狀態"\n', encoding="utf-8")
        self.database = self.root / "本機狀態" / "ledger.sqlite3"
        self.card_file = self.root / "測試卡.json"

    def cli(self, *args, console=None, data=None):
        command = [sys.executable, "-B"]
        command += ["-c", CONSOLE_FIXTURE, console] if console else ["-m", "mah"]
        return subprocess.run(
            [*command, "--config", str(self.config), *args],
            cwd=REPO, input=data, capture_output=True, text=True, encoding="utf-8",
            timeout=15, check=False,
        )

    def check(self, code, *args, **kwargs):
        result = self.cli(*args, **kwargs)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return result

    def card(self, card_id="demo-1", **changes):
        card = dict(
            card_id=card_id, repo_root=str(REPO), task_key="sample-review",
            trigger_ids=["T2"], prepare_mode="card", allowed_templates=["review"],
            allowed_targets=[{"adapter": "codex-exec", "model": "synthetic", "effort_max": "low"}],
            max_dispatches=1,
        )
        self.card_file.write_text(json.dumps(card | changes), encoding="utf-8")
        return str(self.card_file)

    def create(self, card_id="demo-1", **changes):
        result = self.check(0, "card", "create", "--file", self.card(card_id, **changes))
        return next(line.split("SHA-256 ", 1)[1] for line in result.stdout.splitlines()
                    if line.startswith("SHA-256 "))

    def approve(self, card_id="demo-1"):
        digest = self.create(card_id)
        result = self.check(0, "grant", "approve", card_id, "--sha", digest,
                            "--via", "manual_cli", console="confirm")
        return json.loads(result.stdout[result.stdout.index("\n{") + 1:])

    def test_card_output_is_eight_lines_and_reject_is_persistent(self):
        digest = self.create()
        shown = self.check(0, "card", "show", "demo-1").stdout
        self.assertLessEqual(len(shown.splitlines()), 8)
        self.assertIn("PROPOSED", shown)
        self.assertIn("codex-exec / synthetic / low", shown)
        self.assertIn(digest, shown)
        self.check(0, "card", "reject", "demo-1")
        self.assertIn("REJECTED", self.check(0, "card", "show", "demo-1").stdout)
        self.check(10, "grant", "approve", "demo-1", "--sha", digest,
                   "--via", "manual_cli", console="confirm")
        self.check(2, "card", "reject", "demo-1")

    def test_scoped_cards_default_to_the_template_timeout(self):
        for template, timeout in (("review", 900), ("second-opinion", 900), ("research", 1200)):
            with self.subTest(template=template):
                digest = self.create(
                    template, allowed_templates=[template], task_key=template, workspace_path=str(self.root),
                    task_sha256=sha256(template.encode()).hexdigest(),
                    allowed_targets=[{"adapter": "agy-print", "model": "gemini-3.8-flash-low", "effort_max": "low"}],
                )
                approved = self.check(0, "grant", "approve", template, "--sha", digest, "--via", "codex_prompt")
                self.assertEqual(json.loads(approved.stdout)["dispatch_scope"]["timeout_seconds"], timeout)

    def test_noninteractive_flag_and_pipe_cannot_approve(self):
        digest = self.create()
        args = ("grant", "approve", "demo-1", "--sha", digest)
        self.check(10, *args, "--via", "manual_cli", data="APPROVE demo-1 ABCDEFGH\n")
        self.check(10, *args, "--via", "manual_cli", console="deny")
        self.check(10, *args, "--via", "codex_prompt", console="confirm")
        self.check(2, *args, "--via", "chat_ack")
        self.assertEqual(json.loads(self.check(0, "grant", "list").stdout), [])

    def test_desktop_decision_approves_scoped_card_without_terminal(self):
        self.config.write_text('[paths]\nstate_dir = "本機狀態"\n[approval]\nmode = "codex_prompt"\n', encoding="utf-8")
        scope = dict(timeout_seconds=60, workspace_path=str(self.root), task_sha256=sha256(b"review").hexdigest())
        digest = self.create(**scope)
        args = ("grant", "approve", "demo-1", "--via", "codex_prompt")
        self.check(10, *args, "--sha", digest[:16])
        grant = json.loads(self.check(0, *args, "--sha", digest).stdout)
        self.assertEqual((grant["approved_via"], grant["approval_evidence"], grant["status"],
                          grant["max_dispatches"], grant["used_dispatches"]),
                         ("codex_prompt", "codex_desktop_decision", "ACTIVE", 1, 0))
        self.assertEqual(grant["dispatch_scope"]["card_sha256"], digest)
        self.assertEqual(grant["dispatch_scope"]["task_sha256"], scope["task_sha256"])
        self.check(10, *args, "--sha", digest)
        multi = self.create("demo-2", **scope, max_dispatches=2)
        self.check(10, "grant", "approve", "demo-2", "--via", "codex_prompt", "--sha", multi)

    def test_bad_hash_never_opens_confirmation_or_writes_grant(self):
        self.create()
        outcome = self.check(10, "grant", "approve", "demo-1", "--sha", "0" * 64,
                             "--via", "manual_cli", console="confirm")
        self.assertNotIn("APPROVE", outcome.stdout)
        self.assertEqual(json.loads(self.check(0, "grant", "list").stdout), [])

    def test_manual_approval_show_list_and_close_are_auditable(self):
        grant = self.approve()
        grant_id = grant["grant_id"]
        view = json.loads(self.check(0, "grant", "show", grant_id).stdout)
        self.assertEqual(view, grant)
        self.assertEqual(view["approval_evidence"], "interactive_terminal_confirmation")
        self.assertEqual(view["approved_via"], "manual_cli")
        self.assertEqual(view["card_id"], "demo-1")
        self.assertEqual(len(view["content_sha256"]), 64)
        self.assertEqual(view["allowed_templates"], ["review"])
        self.assertEqual(view["allowed_targets"][0]["model"], "synthetic")
        self.assertEqual(view["max_dispatches"], 1)
        self.assertGreater(view["expires_at"], view["approved_at"])
        self.assertEqual(json.loads(self.check(0, "grant", "list").stdout), [view])
        closed = json.loads(self.check(0, "grant", "close", grant_id).stdout)
        self.assertEqual(closed["status"], "CLOSED")
        self.check(12, "grant", "close", grant_id)

    def test_two_approval_processes_can_only_record_one_grant(self):
        digest = self.create()
        args = ("grant", "approve", "demo-1", "--sha", digest, "--via", "manual_cli")
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.cli, *args, console="confirm") for _ in range(2)]
            results = [job.result() for job in jobs]
        self.assertEqual(sorted(item.returncode for item in results), [0, 10])
        self.assertEqual(len(json.loads(self.check(0, "grant", "list").stdout)), 1)

    def test_expired_and_exhausted_grants_are_visible_to_cli(self):
        grant = self.approve()
        grant_id = grant["grant_id"]
        clock = lambda: datetime.fromisoformat(grant["expires_at"])
        GrantLedger(self.database, clock=clock).get_grant(grant_id)
        self.assertEqual(json.loads(self.check(0, "grant", "show", grant_id).stdout)["status"], "EXPIRED")
        self.check(12, "grant", "close", grant_id)
        current = self.approve("demo-2")["grant_id"]
        ledger = GrantLedger(self.database)
        run = ledger.check_and_reserve(
            current, f"sample-review-{uuid4().hex}", repo_root=REPO,
            template="review", adapter="codex-exec", model="synthetic", effort="low",
            workspace_kind="read_only", workspace_path=REPO,
            task_sha256=sha256(b"synthetic").hexdigest(),
        )
        for old, new in (("RESERVED", "PREPARED"), ("PREPARED", "LAUNCHING"),
                         ("LAUNCHING", "SUBMISSION_UNKNOWN"), ("SUBMISSION_UNKNOWN", "ABANDONED")):
            ledger.transition(run, old, new)
        shown = json.loads(self.check(0, "grant", "show", current).stdout)
        self.assertEqual((shown["status"], shown["used_dispatches"]), ("EXHAUSTED", 1))
        self.check(12, "grant", "close", current)

    def desktop_fixture(self):
        self.desktop_run = f"desktop-run-{uuid4().hex}"
        source = self.root / "synthetic-source"
        source.mkdir()
        source.joinpath("sample.txt").write_bytes(b"synthetic source\n")
        self.database.parent.mkdir()
        ledger = GrantLedger(self.database)
        digest = ledger.create_card(
            "desktop-1", repo_root=source, task_key="desktop-review", trigger_ids=["T5"],
            prepare_mode="explicit", allowed_templates=["review"],
            allowed_targets=[{"adapter": "fake", "model": "synthetic", "effort_max": "low"}],
            max_dispatches=1, ttl_minutes=5,
        )
        adapter = FakeAdapter(payload={"findings": []})
        return source, ledger, adapter, digest

    def desktop_dispatch(self, adapter, ledger, source, grant_id, run_id=None, **overrides):
        request = dict(repo_root=source, template="review", model="synthetic", effort="low",
                       task="合成 Desktop 審查任務".encode())
        return adapter.dispatch(RunManager(ledger), grant_id, run_id or self.desktop_run, **(request | overrides))

    def test_desktop_decline_and_unscoped_approval_cannot_dispatch(self):
        source, ledger, adapter, digest = self.desktop_fixture()
        args = ("grant", "approve", "desktop-1", "--sha", digest)
        self.check(10, *args, "--via", "manual_cli", console="deny")
        self.check(10, *args, "--via", "codex_prompt", console="confirm")
        for claim, code in (("user_accepted_in_desktop", 2), ("codex_desktop_decision", 10)):
            with self.subTest(claim=claim), self.assertRaises(LedgerError) as untrusted:
                ledger.approve("desktop-1", digest, via="codex_prompt", evidence=claim)
            self.assertEqual(untrusted.exception.code, code)
        self.assertEqual(ledger.get_card("desktop-1")["status"], "PROPOSED")
        self.assertEqual(ledger.list_grants(), [])
        with patch.object(adapter, "launch", side_effect=AssertionError("no approval")):
            with self.assertRaises(LedgerError) as denied:
                self.desktop_dispatch(adapter, ledger, source, "forged-grant")
        self.assertEqual(denied.exception.code, 10)
        self.assertIsNone(ledger.get_run(self.desktop_run))

    def test_desktop_expired_grant_never_launches_fake_worker(self):
        source, ledger, adapter, digest = self.desktop_fixture()
        args = ("grant", "approve", "desktop-1", "--sha", digest, "--via", "manual_cli")
        self.check(0, *args, console="confirm")
        grant, = ledger.list_grants()
        expired = GrantLedger(self.database, clock=lambda: datetime.fromisoformat(grant["expires_at"]))
        with patch.object(adapter, "launch", side_effect=AssertionError("expired approval")):
            with self.assertRaises(LedgerError) as error:
                self.desktop_dispatch(adapter, expired, source, grant["grant_id"])
        self.assertEqual(error.exception.code, 12)
        self.assertEqual(expired.get_grant(grant["grant_id"])["used_dispatches"], 0)
        self.assertIsNone(expired.get_run(self.desktop_run))

    def test_desktop_single_approval_and_independent_fake_review(self):
        source, ledger, adapter, digest = self.desktop_fixture()
        args = ("grant", "approve", "desktop-1", "--sha", digest, "--via", "manual_cli")
        self.check(10, "grant", "approve", "desktop-1", "--sha", digest[:12], "--via", "manual_cli", console="confirm")
        self.check(10, "grant", "approve", "another-card", "--sha", digest, "--via", "manual_cli", console="confirm")
        self.assertEqual(ledger.list_grants(), [])
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [pool.submit(self.cli, *args, console="confirm") for _ in range(2)]
            self.assertEqual(sorted(result.result().returncode for result in attempts), [0, 10])
        grant, = ledger.list_grants()
        self.assertEqual((grant["card_id"], grant["content_sha256"], grant["max_dispatches"]),
                         ("desktop-1", digest, 1))
        with patch.object(adapter, "launch", side_effect=AssertionError("out of scope")):
            for overrides in ({"template": "research"}, {"model": "other"}, {"effort": "high"}):
                with self.subTest(overrides=overrides), self.assertRaises(LedgerError) as mismatch:
                    self.desktop_dispatch(adapter, ledger, source, grant["grant_id"], **overrides)
                self.assertEqual(mismatch.exception.code, 10)
        self.assertEqual(ledger.get_grant(grant["grant_id"])["used_dispatches"], 0)

        view = self.desktop_dispatch(adapter, ledger, source, grant["grant_id"])
        self.assertEqual(view["state"], "COLLECTING")
        self.check(30, "collect", self.desktop_run)
        pending = json.loads(self.check(30, "verify", self.desktop_run).stdout)
        self.assertEqual(pending["verification"]["reason"], "controller_evidence_missing")
        directory = self.database.parent / "runs" / self.desktop_run
        task = (directory / "task.md").read_bytes()
        result = (directory / "result.json").read_bytes()
        self.assertEqual(task, "合成 Desktop 審查任務".encode())
        self.assertEqual(json.loads(result)["template_payload"], {"findings": []})
        self.assertEqual((source / "sample.txt").read_bytes(), b"synthetic source\n")
        proof = {"run_id": self.desktop_run, "task_sha256": sha256(task).hexdigest(),
                 "result_sha256": sha256(result).hexdigest(), "confirmed": True,
                 "workspace_changed": False, "reviewed_findings": 0}
        evidence = self.root / "controller-evidence.json"
        evidence.write_text(json.dumps(proof | {"result_sha256": "0" * 64}), encoding="utf-8")
        self.check(30, "verify", self.desktop_run, "--evidence-file", str(evidence))
        self.assertEqual(ledger.get_run(self.desktop_run)["state"], "COLLECTING")
        evidence.write_text(json.dumps(proof), encoding="utf-8")
        verified = json.loads(self.check(0, "verify", self.desktop_run, "--evidence-file", str(evidence)).stdout)
        self.assertEqual((verified["state"], verified["acceptance_status"]), ("SUCCEEDED", "PENDING"))
        self.assertEqual(ledger.get_grant(grant["grant_id"])["used_dispatches"], 1)
        second_run = f"second-desktop-run-{uuid4().hex}"
        with patch.object(adapter, "launch", side_effect=AssertionError("already used")):
            with self.assertRaises(LedgerError) as replay:
                self.desktop_dispatch(adapter, ledger, source, grant["grant_id"], run_id=second_run)
        self.assertEqual(replay.exception.code, 11)
        self.assertIsNone(ledger.get_run(second_run))

    def test_config_clamps_concurrency_and_enforces_limits(self):
        with self.config.open("a", encoding="utf-8") as file:
            file.write("\n[limits]\nmax_dispatches_per_grant = 1\nmax_concurrency = 3\n")
        result = self.check(16, "card", "create", "--file", self.card(max_dispatches=2))
        self.assertIn("max_concurrency", result.stderr)
        shown = self.check(0, "card", "create", "--file", self.card()).stdout
        self.assertIn("併發 1", shown)

    def test_invalid_config_and_git_or_sync_paths_fail_closed(self):
        for content in (
            "[limits\n",
            "[limits]\nmax_concurrency = true\n",
            '[approval]\nmode = "chat_ack"\n',
            '[paths]\nstate_dir = "OneDrive - Fixture/private"\n',
            f"[paths]\nstate_dir = {json.dumps(str(REPO / 'state'))}\n",
        ):
            with self.subTest(content=content):
                self.config.write_text(content, encoding="utf-8")
                self.check(2, "grant", "list")
                self.assertFalse(self.database.exists())

    def test_invalid_card_and_missing_trigger_return_usage_error(self):
        self.check(2, "card", "create", "--file", self.card(trigger_ids=[]))
        self.card_file.write_text('{"card_id":"one","card_id":"two"}', encoding="utf-8")
        self.check(2, "card", "create", "--file", str(self.card_file))
        self.check(2, "card", "show", "missing")

    def test_third_card_needs_an_explicit_override(self):
        self.create("demo-1")
        self.create("demo-2")
        path = self.card("demo-3")
        outcome = self.check(2, "card", "create", "--file", path)
        self.assertIn("--override-suggestion-limit", outcome.stderr)
        self.check(0, "card", "create", "--file", path, "--override-suggestion-limit")

    def test_default_paths_are_external_and_example_can_be_loaded(self):
        base = self.root / "AppData"
        env = os.environ | {"LOCALAPPDATA": str(base), "XDG_STATE_HOME": str(base)}
        command = [sys.executable, "-B", "-m", "mah"]
        result = subprocess.run(
            [*command, "grant", "list"], cwd=REPO, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
        )
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "[]"), result.stderr)
        local = base / "zMAHarness"
        self.assertTrue((local / "state" / "ledger.sqlite3").is_file())
        self.assertFalse((local / "harness.local.toml").exists())
        self.config.write_bytes((REPO / "harness.local.example.toml").read_bytes())
        self.check(0, "grant", "list")

    def test_default_config_prefers_brand_path_and_falls_back_to_existing_legacy_file(self):
        base = self.root / "LocalAppData"
        new = base / "zMAHarness" / "harness.local.toml"
        legacy = base / "MultiAgentHarness" / "harness.local.toml"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(base), "XDG_STATE_HOME": str(base)}):
            self.assertEqual(default_config_path(), new)
            legacy.parent.mkdir(parents=True)
            legacy.write_text('[paths]\nstate_dir = "既有狀態"\n', encoding="utf-8")
            self.assertEqual(load_config().database, legacy.parent / "既有狀態" / "ledger.sqlite3")
            new.parent.mkdir()
            new.write_text("", encoding="utf-8")
            self.assertEqual(load_config().path, new)

    def test_cli_help_and_card_use_zmaharness_brand(self):
        self.assertIn("zMAHarness 派工與授權", self.check(0, "--help").stdout)
        self.create()
        self.assertIn("【zMAHarness 協作建議】", self.check(0, "card", "show", "demo-1").stdout)

    def test_defaults_enable_agy_and_codex_prompt_without_local_config(self):
        missing_config = self.root / "missing.toml"
        with patch("mah.config.default_config_path", return_value=missing_config):
            values = load_config().values
        self.assertEqual(values["adapters"]["enabled"], ["codex-exec", "gui-terra", "agy-print"])
        self.assertEqual(values["approval"]["mode"], "codex_prompt")
        for template in ("review", "second_opinion", "research"):
            self.assertEqual(values["defaults"][template],
                             {"adapter": "agy-print", "model": "gemini-3.8-flash-low", "effort": "low"})
        self.assertEqual(values["defaults"]["delegate_implement"]["adapter"], "codex-exec")
        self.assertFalse(missing_config.exists())

    def test_switching_default_adapter_does_not_inherit_a_gemini_model(self):
        with self.config.open("a", encoding="utf-8") as settings:
            settings.write('\n[defaults.review]\nadapter = "codex-exec"\n')
        self.assertEqual(load_config(self.config).values["defaults"]["review"]["model"], "")

    def test_duplicate_card_id_and_disabled_adapter(self):
        with self.config.open("a", encoding="utf-8") as settings:
            settings.write('\n[adapters]\nenabled = ["codex-exec", "gui-terra"]\n')
        self.create()
        self.check(2, "card", "create", "--file", self.card())
        self.check(14, "card", "create", "--file", self.card("other", allowed_targets=[
            {"adapter": "agy-print", "model": "synthetic", "effort_max": "low"}
        ]))
