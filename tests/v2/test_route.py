from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mah import cli
from mah.adapters import registry
from mah.adapters.base import AdapterPolicy
from mah.config import load_config
from mah.ledger import GrantLedger
from mah.model_detect import DetectionRecord, ListResult
from mah.preflight import adapter_checks, common_checks, preflight
from mah.quota import QuotaState, QuotaWindow
from mah.route import route

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_catalog"
AGY_FIXTURE = (FIXTURES / "agy-1.5.12-models-redacted.txt").read_bytes()
CODEX_FIXTURE = (FIXTURES / "codex-0.169.0-debug-models-redacted.json").read_bytes()


def _completed(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class WorkspaceSupportAndPreflightSplitTests(unittest.TestCase):
    def test_workspace_support_registry_and_custom_registration(self):
        self.assertEqual(registry.workspace_support("codex-exec"), frozenset({"bundle"}))
        self.assertEqual(registry.workspace_support("agy-print"), frozenset({"bundle", "worktree"}))
        self.assertEqual(registry.workspace_support("claude-print"), frozenset({"bundle", "worktree"}))
        self.assertEqual(registry.workspace_support("unregistered-x"), frozenset({"bundle", "worktree"}))

        name = "test-ws-adapter"
        self.addCleanup(registry.unregister, name)
        registry.register(name, AdapterPolicy(), workspace_support={"bundle"})
        self.assertEqual(registry.workspace_support(name), frozenset({"bundle"}))
        registry.unregister(name)
        self.assertNotIn(name, registry.WORKSPACE_SUPPORT)

    def test_preflight_common_and_adapter_checks_split_matches_preflight(self):
        with TemporaryDirectory(prefix="mah-pf-split-") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            agy = root / "bin" / "agy.exe"
            agy.parent.mkdir()
            agy.write_bytes(b"fake")
            cfg = root / "harness.local.toml"
            cfg.write_text(
                f'[paths]\nstate_dir = "state"\n[adapters]\nenabled = ["agy-print"]\n'
                f'agy_command = {json.dumps(agy.as_posix())}\n',
                encoding="utf-8",
            )
            with patch("mah.preflight.git_toplevel", return_value=(str(repo), None)):
                common = common_checks(repo, cfg)
                checked = adapter_checks(common.config, "agy-print", common.repo_root)
                full = preflight(repo, "agy-print", cfg)
            self.assertTrue(common.ok)
            self.assertTrue(checked.ok)
            self.assertTrue(full["ok"])
            self.assertEqual(full["repo_root"], common.repo_root)
            self.assertEqual(full["adapter_executable_found"], checked.executable_found)


class RouteTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-route-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.agy = self.tools / "agy.exe"
        self.codex = self.tools / "codex.exe"
        self.claude = self.tools / "claude.exe"
        for exe in (self.agy, self.codex, self.claude):
            exe.write_bytes(b"fake")
        self.cfg_path = self.root / "harness.local.toml"
        self.cfg_path.write_text(
            '[paths]\nstate_dir = "state"\n[adapters]\n'
            f'agy_command = "{self.agy.as_posix()}"\n'
            f'codex_command = "{self.codex.as_posix()}"\n'
            f'claude_command = "{self.claude.as_posix()}"\n',
            encoding="utf-8",
        )
        self.now = datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)

    def _all_ok_env(self, *, quota_overrides=None, agy_ids=None, claude_login=None, codex_login=None):
        """Context helper mocking git, logins, detection, and quota with zero real CLI calls."""
        default_quotas = {
            "agy:gemini": QuotaState(pool="agy:gemini", state="unknown", source="none"),
            "agy:claude": QuotaState(pool="agy:claude", state="unknown", source="none"),
            "claude": QuotaState(pool="claude", state="ok", source="claude_usage", fetched_at=self.now.isoformat()),
            "chatgpt": QuotaState(pool="chatgpt", state="ok", source="app_server", fetched_at=self.now.isoformat()),
        }
        if quota_overrides:
            default_quotas.update(quota_overrides)

        def fake_run(argv, **kwargs):
            if argv[1:] == ["models"]:
                if agy_ids is not None:
                    lines = "\n".join(f"{mid}\t{mid}" for mid in agy_ids).encode("utf-8")
                    return _completed(0, stdout=lines)
                return _completed(0, stdout=AGY_FIXTURE)
            if argv[1:] == ["debug", "models"]:
                return _completed(0, stdout=CODEX_FIXTURE)
            raise AssertionError(f"unexpected subprocess.run: {argv}")

        def fake_quotas(config, catalog=None, pools=None, **kwargs):
            requested = pools if pools is not None else list(default_quotas)
            return {p: default_quotas.get(p, QuotaState(pool=p, state="unknown")) for p in requested}, []

        def fake_login(adapter, exe, **kwargs):
            if adapter == "claude-print" and claude_login is not None:
                return list(claude_login)
            if adapter == "codex-exec" and codex_login is not None:
                return list(codex_login)
            return []

        return (
            patch("mah.preflight.git_toplevel", return_value=(str(self.repo), None)),
            patch("mah.preflight.login_reasons", side_effect=fake_login),
            patch("mah.preflight.codex_login_reasons", return_value=list(codex_login or [])),
            patch("mah.model_detect.subprocess.run", side_effect=fake_run),
            patch("mah.route.get_quota_states", side_effect=fake_quotas),
        )

    def _run_route(self, *, quota_overrides=None, agy_ids=None, claude_login=None, codex_login=None, **kwargs):
        patches = self._all_ok_env(
            quota_overrides=quota_overrides, agy_ids=agy_ids, claude_login=claude_login, codex_login=codex_login,
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            return route(self.repo, config_path=self.cfg_path, now=self.now, **kwargs)

    def test_golden_table_every_row(self):
        # Spec §2.5 golden table (all controller × task × level × mode rows).
        cases = [
            # (controller, task, level, mode, expected_opt2_display, expected_opt2_adapter, expected_opt3_display)
            ("codex", "review", "standard", "bundle", "gemini-4-neon-medium · medium", "agy-print",
             "gemini-3.8-flash-low · low"),
            ("codex", "review", "standard", "worktree", "gemini-4-neon-medium · medium", "agy-print",
             "gemini-3.8-flash-low · low"),
            ("codex", "review", "deep", "bundle", "gemini-4-neon-high · high", "agy-print",
             "gemini-3.8-flash-low · low"),
            ("codex", "review", "deep", "worktree", "gemini-4-neon-high · high", "agy-print",
             "gemini-3.8-flash-low · low"),
            ("codex", "second_opinion", "deep", "bundle", "gemini-4-neon-high · high", "agy-print",
             "gemini-3.8-flash-low · low"),
            ("codex", "second_opinion", "deep", "worktree", "gemini-4-neon-high · high", "agy-print",
             "gemini-3.8-flash-low · low"),
            ("codex", "research", "light", "bundle", "gemini-3.8-flash-low · low", "agy-print",
             "gemini-4-neon-high · high"),
            ("codex", "review", "light", "worktree", "gemini-3.8-flash-low · low", "agy-print",
             "gemini-4-neon-high · high"),
            ("codex", "research", "standard", "bundle", "gemini-3.8-flash-medium · medium", "agy-print",
             "gemini-4-neon-high · high"),
            ("claude", "review", "standard", "bundle", "gpt-6.2-sol · medium", "codex-exec",
             "gemini-4-neon-high · high"),
            ("claude", "second_opinion", "deep", "bundle", "gpt-6.2-sol · high", "codex-exec",
             "gemini-4-neon-high · high"),
            ("claude", "review", "standard", "worktree", "gemini-4-neon-medium · medium", "agy-print",
             "gemini-3.8-flash-low · low"),
            ("claude", "second_opinion", "deep", "worktree", "gemini-4-neon-high · high", "agy-print",
             "gemini-3.8-flash-low · low"),
            ("claude", "research", "light", "bundle", "gemini-3.8-flash-low · low", "agy-print",
             "gemini-4-neon-high · high"),
            ("claude", "review", "light", "bundle", "gemini-3.8-flash-low · low", "agy-print",
             "gemini-4-neon-high · high"),
        ]
        for controller, task, level, mode, exp2, exp_adapter, exp3 in cases:
            with self.subTest(controller=controller, task=task, level=level, mode=mode):
                res = self._run_route(task=task, level=level, mode=mode, controller=controller)
                self.assertTrue(res["ok"], res)
                self.assertTrue(res["card"])
                self.assertEqual(res["recommended"]["display"], exp2)
                self.assertEqual(res["recommended"]["adapter"], exp_adapter)
                self.assertEqual(res["alternate"]["display"], exp3)
                self.assertIsNone(res["note"])

    def test_rule1_never_recommends_same_family_and_other_allows_all(self):
        for controller, forbidden in (("codex", "gpt"), ("claude", "claude")):
            for task in ("review", "second_opinion", "research"):
                for level in ("light", "standard", "deep"):
                    for mode in ("bundle", "worktree"):
                        res = self._run_route(task=task, level=level, mode=mode, controller=controller)
                        self.assertTrue(res["ok"])
                        self.assertNotEqual(res["recommended"]["family"], forbidden)
                        if res["alternate"]:
                            self.assertFalse(res["alternate"]["model"].startswith(forbidden))
        # controller=other does not exclude GPT -> picks 6.2 Sol on review standard bundle.
        other = self._run_route(task="review", level="standard", mode="bundle", controller="other")
        self.assertEqual(other["recommended"]["model"], "gpt-6.2-sol")

    def test_quota_reroute_appendix_c_d7_save_alternates(self):
        until = (self.now + timedelta(hours=59, minutes=5, seconds=41)).isoformat(timespec="seconds")
        cooling = {"agy:gemini": QuotaState(pool="agy:gemini", state="cooling", until=until, source="agy_429")}

        # Codex T2 Review + Gemini cooling -> Sonnet medium + option3 Sonnet low (Appendix C D7!).
        codex_res = self._run_route(task="review", level="standard", mode="bundle", controller="codex",
                                    quota_overrides=cooling)
        self.assertTrue(codex_res["ok"] and codex_res["card"])
        self.assertEqual(codex_res["recommended"]["adapter"], "claude-print")
        self.assertEqual(codex_res["recommended"]["display"], "sonnet · medium")
        self.assertIsNotNone(codex_res["alternate"])
        self.assertEqual(codex_res["alternate"]["display"], "sonnet · low")
        self.assertEqual(codex_res["alternate"]["purpose"], "save")
        self.assertIn("Gemini 額度冷卻中", codex_res["note"])
        self.assertIn("59 小時", codex_res["note"])
        self.assertEqual(codex_res["note_data"]["kind"], "rerouted")
        self.assertEqual(codex_res["note_data"]["pool"], "agy:gemini")

        # Claude T2 Review (bundle) + Gemini cooling -> 6.2 Sol medium + option3 gpt-6-luna low (Appendix C D7!).
        claude_res = self._run_route(task="review", level="standard", mode="bundle", controller="claude",
                                     quota_overrides=cooling)
        self.assertEqual(claude_res["recommended"]["display"], "gpt-6.2-sol · medium")
        self.assertIsNotNone(claude_res["alternate"])
        self.assertEqual(claude_res["alternate"]["display"], "gpt-6-luna · low")
        self.assertEqual(claude_res["alternate"]["purpose"], "save")

    def test_quota_tight_reroutes_and_all_tight_keeps_first_with_stale_note(self):
        future = (self.now + timedelta(hours=3)).isoformat(timespec="seconds")
        tight_gemini = QuotaState(
            pool="agy:gemini", state="tight", source="agy_429",
            windows=(QuotaWindow("weekly", 93, future),), stale=True, age_seconds=7200,
        )
        tight_claude = QuotaState(
            pool="claude", state="tight", source="claude_usage",
            windows=(QuotaWindow("weekly", 95, future),),
        )
        tight_agy_claude = QuotaState(
            pool="agy:claude", state="tight", source="agy_429",
            windows=(QuotaWindow("weekly", 92, future),),
        )
        # First tight (Gemini), second ok (Claude) -> reroutes to Claude with note mentioning stale data time.
        rerouted = self._run_route(
            task="review", level="standard", mode="bundle", controller="codex",
            quota_overrides={"agy:gemini": tight_gemini},
        )
        self.assertEqual(rerouted["recommended"]["display"], "sonnet · medium")
        self.assertIn("2 小時前的資料", rerouted["note"])
        self.assertEqual(rerouted["note_data"]["kind"], "rerouted")

        # All available pools tight -> picks first (Gemini Neon) and emits tight note.
        all_tight = self._run_route(
            task="review", level="standard", mode="bundle", controller="codex",
            quota_overrides={"agy:gemini": tight_gemini, "claude": tight_claude, "agy:claude": tight_agy_claude},
        )
        self.assertEqual(all_tight["recommended"]["display"], "gemini-4-neon-medium · medium")
        self.assertIn("仍建議", all_tight["note"])
        self.assertEqual(all_tight["note_data"]["kind"], "tight")

    def test_all_excluded_by_quota_returns_ok_true_card_false(self):
        until = (self.now + timedelta(hours=4)).isoformat(timespec="seconds")
        all_out = {
            "agy:gemini": QuotaState(pool="agy:gemini", state="cooling", until=until, source="agy_429"),
            "agy:claude": QuotaState(pool="agy:claude", state="cooling", until=until, source="agy_429"),
            "claude": QuotaState(pool="claude", state="exhausted", until=until, source="claude_usage",
                                 windows=(QuotaWindow("weekly", 100, until),)),
        }
        res = self._run_route(task="review", level="standard", mode="bundle", controller="codex",
                              quota_overrides=all_out)
        self.assertTrue(res["ok"])
        self.assertFalse(res["card"])
        self.assertIsNone(res["recommended"])
        self.assertTrue(res["reasons"] and "冷卻或額度已用完" in res["reasons"][0])

    def test_unknown_quota_identical_to_ok(self):
        ok_res = self._run_route(task="review", level="standard", mode="bundle", controller="codex")
        all_unknown = {
            p: QuotaState(pool=p, state="unknown", source="none")
            for p in ("agy:gemini", "agy:claude", "claude", "chatgpt")
        }
        unk_res = self._run_route(task="review", level="standard", mode="bundle", controller="codex",
                                  quota_overrides=all_unknown)
        self.assertEqual(ok_res, unk_res)

    def test_detection_failure_fail_open_and_missing_model_no_note(self):
        # agy detection failure (timeout) -> fail-open, identical to all available.
        with (patch("mah.preflight.git_toplevel", return_value=(str(self.repo), None)),
              patch("mah.preflight.login_reasons", return_value=[]),
              patch("mah.preflight.codex_login_reasons", return_value=[]),
              patch("mah.model_detect.subprocess.run", side_effect=subprocess.TimeoutExpired(["agy"], 10)),
              patch("mah.route.get_quota_states", return_value=({}, []))):
            fail_open = route(self.repo, task="review", level="standard", mode="bundle",
                              controller="codex", config_path=self.cfg_path, now=self.now)
        self.assertEqual(fail_open["recommended"]["display"], "gemini-4-neon-medium · medium")
        self.assertIsNone(fail_open["note"])

        # Detection succeeds but account lacks gemini-4-neon -> falls back to sonnet with NO note.
        no_neon = self._run_route(
            task="review", level="standard", mode="bundle", controller="codex",
            agy_ids=["gemini-3.8-flash-low", "claude-sonnet-5-5-medium"], verbose=True,
        )
        self.assertEqual(no_neon["recommended"]["display"], "sonnet · medium")
        self.assertIsNone(no_neon["note"])
        self.assertTrue(any(s["reason"] == "not_in_account_models" for s in no_neon["skipped"]))

    def test_claude_route_order_falls_back_to_agy_when_claude_print_login_fails(self):
        until = (self.now + timedelta(hours=5)).isoformat(timespec="seconds")
        res = self._run_route(
            task="review", level="standard", mode="bundle", controller="codex",
            quota_overrides={"agy:gemini": QuotaState(pool="agy:gemini", state="cooling", until=until, source="agy_429")},
            claude_login=["Claude CLI 尚未登入"],
        )
        self.assertTrue(res["ok"] and res["card"])
        self.assertEqual(res["recommended"]["adapter"], "agy-print")
        self.assertEqual(res["recommended"]["model"], "claude-sonnet-5-5-medium")

    def test_max_effort_high_never_recommends_xhigh(self):
        local = self.root / "models.local.toml"
        local.write_text("""
schema_version = 1
[[cells]]
task = "review"
level = "deep"
picks = [
  { model = "gemini-4-neon", effort = "xhigh" },
  { model = "gemini-4-neon", effort = "high" },
]
""", encoding="utf-8")
        res = self._run_route(task="review", level="deep", mode="bundle", controller="codex")
        self.assertEqual(res["recommended"]["effort"], "high")
        self.assertEqual(res["recommended"]["model"], "gemini-4-neon-high")

    def test_manual_override_cases(self):
        # Alias 4argon -> gemini-4-argon-high (uses alias_effort=high over card_effort=medium).
        argon = self._run_route(task="review", level="standard", mode="bundle", controller="codex",
                                override="4argon", card_effort="medium")
        self.assertTrue(argon["ok"] and argon["resolved"])
        self.assertEqual(argon["recommended"]["model"], "gemini-4-argon-high")
        self.assertEqual(argon["recommended"]["effort"], "high")

        # Bare effort override (--override high) keeps cell top model with high effort.
        bare = self._run_route(task="review", level="standard", mode="bundle", controller="codex",
                               override="high")
        self.assertTrue(bare["ok"] and bare["resolved"])
        self.assertEqual(bare["recommended"]["model"], "gemini-4-neon-high")
        self.assertEqual(bare["recommended"]["effort"], "high")

        # Unclassified but detected (gpt-oss-120b-medium on agy-print).
        unclassified = self._run_route(task="research", level="light", mode="bundle", controller="codex",
                                       override="gpt-oss-120b-medium")
        self.assertTrue(unclassified["ok"] and unclassified["resolved"])
        self.assertEqual(unclassified["recommended"]["adapter"], "agy-print")
        self.assertEqual(unclassified["recommended"]["status"], "unclassified")

        # Completely unknown ID with --adapter allowed; without --adapter returns resolved=False.
        with_adapter = self._run_route(task="review", level="standard", mode="bundle", controller="codex",
                                       override="brand-new-99", adapter="codex-exec", effort="medium")
        self.assertTrue(with_adapter["ok"] and with_adapter["resolved"])
        self.assertEqual(with_adapter["recommended"]["model"], "brand-new-99")
        self.assertEqual(with_adapter["recommended"]["status"], "unclassified")

        no_adapter = self._run_route(task="review", level="standard", mode="bundle", controller="codex",
                                     override="brand-new-99")
        self.assertTrue(no_adapter["ok"])
        self.assertFalse(no_adapter["resolved"])
        self.assertTrue(no_adapter["choices"])

        # Ambiguous token ("sol" matches multiple GPT Sol models) -> resolved=False + choices.
        ambig = self._run_route(task="review", level="standard", mode="bundle", controller="codex",
                                override="sol")
        self.assertTrue(ambig["ok"])
        self.assertFalse(ambig["resolved"])
        self.assertGreaterEqual(len(ambig["choices"]), 2)

        # Orchestrator refused with exact registry message.
        orch = self._run_route(task="review", level="standard", mode="bundle", controller="codex",
                               override="gemini-pro-orchestrator-neon")
        self.assertFalse(orch["ok"])
        self.assertEqual(orch["reasons"], [registry.refused_model_reason("agy-print", "gemini-pro-orchestrator-neon")])

        # Same-family manual override allowed (Codex controller manually requesting terra) + cooling only warns.
        until = (self.now + timedelta(hours=3)).isoformat(timespec="seconds")
        same_fam = self._run_route(
            task="review", level="standard", mode="bundle", controller="codex",
            override="terra", card_effort="medium",
            quota_overrides={"chatgpt": QuotaState(pool="chatgpt", state="exhausted", until=until,
                                                   source="app_server", ordinary_usage_allowed=False)},
        )
        self.assertTrue(same_fam["ok"] and same_fam["card"])
        self.assertEqual(same_fam["recommended"]["model"], "gpt-6.1-terra")
        self.assertIn("你指定的 gpt-6.1-terra", same_fam["note"])
        # Terra is no longer the default but stays selectable, never refused.
        self.assertEqual(same_fam["recommended"]["status"], "selectable")
        self.assertIsNone(registry.refused_model_reason("codex-exec", "gpt-6.1-terra"))

    def test_only_needed_adapters_and_quota_pools_are_queried(self):
        seen_detect = []
        seen_pools = []

        def fake_detect(config, adapters, **kwargs):
            seen_detect.extend(adapters)
            return {a: DetectionRecord(adapter=a, status="unknown", source="none") for a in adapters}

        def fake_quotas(config, catalog=None, pools=None, **kwargs):
            seen_pools.extend(pools or ())
            return {p: QuotaState(pool=p, state="unknown") for p in (pools or ())}, []

        with (patch("mah.preflight.git_toplevel", return_value=(str(self.repo), None)),
              patch("mah.preflight.login_reasons", return_value=[]),
              patch("mah.preflight.codex_login_reasons", return_value=[]),
              patch("mah.route.detect_adapters", side_effect=fake_detect),
              patch("mah.route.get_quota_states", side_effect=fake_quotas)):
            # Codex controller T2 must NOT query codex-exec or chatgpt pool.
            route(self.repo, task="review", level="standard", mode="bundle", controller="codex",
                  config_path=self.cfg_path, now=self.now)
            self.assertNotIn("codex-exec", seen_detect)
            self.assertNotIn("chatgpt", seen_pools)

            seen_detect.clear()
            seen_pools.clear()
            # Claude controller T2 must NOT query claude-print or claude pool.
            route(self.repo, task="review", level="standard", mode="bundle", controller="claude",
                  config_path=self.cfg_path, now=self.now)
            self.assertNotIn("claude-print", seen_detect)
            self.assertNotIn("claude", seen_pools)

    def test_catalog_load_failure_and_unexpected_exception_fall_back_to_defaults(self):
        bad_repo = self.root / "bad_catalog.toml"
        bad_repo.write_text('schema_version = 999\nupdated = "2026-11-19"\n', encoding="utf-8")
        res = self._run_route(task="review", level="standard", mode="bundle", controller="codex",
                              repo_catalog_path=bad_repo)
        self.assertTrue(res["ok"] and res["card"])
        self.assertEqual(res["fallback"], "defaults")
        self.assertEqual(res["recommended"]["adapter"], "agy-print")
        self.assertEqual(res["recommended"]["model"], "gemini-3.8-flash-low")
        self.assertIsNone(res["alternate"])

        with (patch("mah.preflight.git_toplevel", return_value=(str(self.repo), None)),
              patch("mah.preflight.login_reasons", return_value=[]),
              patch("mah.route.load_catalog", side_effect=RuntimeError("boom"))):
            crashed = route(self.repo, task="review", level="standard", mode="bundle",
                            controller="codex", config_path=self.cfg_path, now=self.now)
        self.assertTrue(crashed["ok"] and crashed["card"])
        self.assertEqual(crashed["fallback"], "defaults")
        self.assertTrue(crashed["warnings"])

    def test_cli_mah_route_compact_output_under_2kb_exit_codes_and_never_writes_ledger(self):
        cfg = load_config(self.cfg_path)
        cfg.database.parent.mkdir(parents=True, exist_ok=True)
        GrantLedger(cfg.database)
        before_hash = sha256(cfg.database.read_bytes()).hexdigest()

        patches = self._all_ok_env()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main([
                    "route", "--repo", str(self.repo), "--task", "review", "--level", "standard",
                    "--mode", "bundle", "--controller", "codex", "--config", str(self.cfg_path),
                ])
            self.assertEqual(code, 0, stderr.getvalue())
            raw = stdout.getvalue()
            self.assertLessEqual(len(raw.encode("utf-8")), 2048)
            payload = json.loads(raw)
            self.assertEqual(payload["recommended"]["display"], "gemini-4-neon-medium · medium")

            # Refused override exits with code 30.
            out_err = io.StringIO()
            with redirect_stdout(out_err), redirect_stderr(io.StringIO()):
                bad_code = cli.main([
                    "--config", str(self.cfg_path), "route", "--repo", str(self.repo),
                    "--task", "review", "--level", "standard", "--mode", "bundle",
                    "--controller", "codex", "--override", "gemini-pro-orchestrator-neon",
                ])
            self.assertEqual(bad_code, 30)
            self.assertFalse(json.loads(out_err.getvalue())["ok"])

        after_hash = sha256(cfg.database.read_bytes()).hexdigest()
        self.assertEqual(before_hash, after_hash)


if __name__ == "__main__":
    unittest.main()
