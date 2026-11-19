from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from mah import cli, login_status
from mah.catalog import load_repo_catalog
from mah.config import load_config
from mah.db import transaction
from mah.ledger import GrantLedger
from mah.model_detect import cache_path, models_report, read_routing_cache
from mah.preflight import preflight
from mah.quota import (
    CLAUDE_BREAKER_WARNING,
    CLAUDE_USAGE_ARGS,
    derive_agy_cooldowns,
    get_quota_states,
    parse_chatgpt_quota,
    parse_claude_reset,
    parse_claude_usage_text,
    query_chatgpt_quota,
    query_claude_quota,
    refresh_routing_cache_best_effort,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_catalog"
CLAUDE_USAGE_FIXTURE = (FIXTURES / "claude-2.1.325-usage-redacted.json").read_bytes()
CODEX_LIMITS_FIXTURE = json.loads((FIXTURES / "codex-0.169.0-rate-limits-redacted.json").read_text(encoding="utf-8"))
AGY_429_STDERR = (FIXTURES / "agy-1.5.12-429-stderr-redacted.txt").read_bytes()


def _fake_popen(stdout_bytes, *, returncode=0, stderr_bytes=b""):
    proc = MagicMock()
    proc.communicate.return_value = (stdout_bytes, stderr_bytes)
    proc.returncode = returncode
    return proc


class ChatGptQuotaTests(unittest.TestCase):
    def test_chatgpt_quota_states_and_reset_window_handling(self):
        now = datetime(2026, 11, 19, 6, 0, 0, tzinfo=UTC)
        future_epoch = int((now + timedelta(hours=3)).timestamp())
        past_epoch = int((now - timedelta(minutes=5)).timestamp())

        ok = parse_chatgpt_quota(CODEX_LIMITS_FIXTURE, now=now)
        self.assertEqual(ok.state, "ok")
        self.assertEqual(ok.source, "app_server")
        self.assertEqual([(w.name, w.used_percent) for w in ok.windows], [("five_hour", 35), ("weekly", 60)])

        tight_reply = {
            "id": 2,
            "result": {
                "ordinaryUsageAllowed": True,
                "accountId": "acct-secret",
                "rateLimits": {
                    "primary": {"usedPercent": 95, "windowDurationMins": 300, "resetsAt": future_epoch},
                    "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": future_epoch},
                    "credits": {"hasCredits": False, "balance": "0"},
                    "planType": "plus",
                },
            },
        }
        tight = parse_chatgpt_quota(tight_reply, now=now)
        self.assertEqual(tight.state, "tight")
        serialized = json.dumps(tight.to_dict(), ensure_ascii=False)
        for forbidden in ("accountId", "acct-secret", "credits", "planType", "ResetCredit"):
            self.assertNotIn(forbidden, serialized)

        exhausted_reply = {"id": 2, "result": {"ordinaryUsageAllowed": False, "rateLimits": {
            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": future_epoch},
        }}}
        exhausted = parse_chatgpt_quota(exhausted_reply, now=now)
        self.assertEqual(exhausted.state, "exhausted")
        self.assertIsNotNone(exhausted.until)

        # Past resetsAt window is treated as reset (not tight/exhausted) when ordinaryUsageAllowed is True.
        reset_reply = {"id": 2, "result": {"ordinaryUsageAllowed": True, "rateLimits": {
            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": past_epoch},
            "secondary": {"usedPercent": 50, "windowDurationMins": 10080, "resetsAt": future_epoch},
        }}}
        after_reset = parse_chatgpt_quota(reset_reply, now=now)
        self.assertEqual(after_reset.state, "ok")

        self.assertEqual(parse_chatgpt_quota(None, now=now).state, "unknown")

    def test_codex_app_server_uses_config_parent_cwd_and_preflight_shares_query(self):
        with TemporaryDirectory(prefix="mah-quota-codex-") as tmp:
            root = Path(tmp)
            codex_exe = root / "bin" / "codex.exe"
            codex_exe.parent.mkdir()
            codex_exe.write_bytes(b"fake")
            cfg_path = root / "harness.local.toml"
            cfg_path.write_text(
                f'[paths]\nstate_dir = "state"\n[adapters]\nenabled = ["codex-exec"]\n'
                f'codex_command = {json.dumps(codex_exe.as_posix())}\n',
                encoding="utf-8",
            )
            config = load_config(cfg_path)
            seen_cwd = []

            def fake_popen(argv, **kwargs):
                seen_cwd.append(kwargs.get("cwd"))
                raise OSError("stopped after cwd check")

            with patch("mah.login_status.subprocess.Popen", side_effect=fake_popen):
                query_chatgpt_quota(codex_exe, cwd=str(config.path.parent))
            self.assertEqual(seen_cwd, [str(config.path.parent)])

            # preflight also passes config.path.parent as cwd through shared codex quota path.
            seen_reply_cwd = []

            def fake_reply(exe, env, timeout, *, cwd=None):
                seen_reply_cwd.append(cwd)
                return {"id": 2, "result": {"ordinaryUsageAllowed": True}}

            repo = root / "repo"
            repo.mkdir()
            signed_in = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"Logged in using ChatGPT\n")
            with (patch("mah.preflight.git_toplevel", return_value=(str(repo), None)),
                  patch("mah.login_status._status", return_value=signed_in),
                  patch("mah.login_status._quota_reply", side_effect=fake_reply)):
                report = preflight(repo, "codex-exec", cfg_path)
            self.assertTrue(report["ok"], report["reasons"])
            self.assertEqual(seen_reply_cwd, [str(config.path.parent)])


class ClaudeUsageQuotaTests(unittest.TestCase):
    def test_claude_usage_fixture_and_reset_time_edge_cases(self):
        now = datetime(2026, 11, 19, 6, 0, 0, tzinfo=UTC)
        payload = json.loads(CLAUDE_USAGE_FIXTURE.decode("utf-8"))
        state, windows, until = parse_claude_usage_text(payload["result"], now=now)
        self.assertEqual(state, "ok")
        self.assertIsNone(until)
        self.assertEqual([(w.name, w.used_percent, w.applies_to) for w in windows], [
            ("five_hour", 77, None),
            ("weekly", 56, None),
        ])
        self.assertIsNotNone(windows[0].resets_at)
        self.assertIsNotNone(windows[1].resets_at)  # 8am without minutes parsed cleanly

        # Year rollover: late Dec seeing Jan 2 rolls to next year.
        dec_now = datetime(2026, 12, 30, 12, 0, 0, tzinfo=UTC)
        rolled = parse_claude_reset("Jan 2, 8am (Asia/Taipei)", now=dec_now)
        self.assertIsNotNone(rolled)
        self.assertTrue(rolled.startswith("2027-01-02T08:00:00"))

        # Unparseable text -> unknown.
        self.assertEqual(parse_claude_usage_text("Totally new CLI format", now=now)[0], "unknown")

    def test_claude_sonnet_only_window_applies_only_to_sonnet(self):
        now = datetime(2026, 11, 19, 6, 0, 0, tzinfo=UTC)
        text = (
            "Current session: 20% used · resets Nov 19, 11pm (Asia/Taipei)\n"
            "Current week (all models): 30% used · resets Nov 22, 8am (Asia/Taipei)\n"
            "Current week (Sonnet only): 95% used · resets Nov 22, 8am (Asia/Taipei)\n"
            "Current week (Ignored label): 99% used · resets Nov 22, 8am (Asia/Taipei)\n"
        )
        state, windows, _ = parse_claude_usage_text(text, now=now)
        self.assertEqual(state, "ok")
        self.assertEqual(len(windows), 3)  # Ignored label skipped

        from mah.quota import QuotaState
        qs = QuotaState(pool="claude", state=state, windows=windows, source="claude_usage",
                        fetched_at=now.isoformat())
        self.assertEqual(qs.evaluate(model_hint="sonnet", now=now)[0], "tight")
        self.assertEqual(qs.evaluate(model_hint="claude-sonnet", now=now)[0], "tight")
        self.assertEqual(qs.evaluate(model_hint="opus", now=now)[0], "ok")
        self.assertEqual(qs.evaluate(model_hint="claude-opus", now=now)[0], "ok")

    def test_claude_argv_env_cwd_lock_and_anti_billing_circuit_breaker(self):
        with TemporaryDirectory(prefix="mah-quota-claude-") as tmp:
            root = Path(tmp)
            claude_exe = root / "bin" / "claude.exe"
            claude_exe.parent.mkdir()
            claude_exe.write_bytes(b"fake-claude-v1")
            cfg_path = root / "harness.local.toml"
            cfg_path.write_text(
                f'[paths]\nstate_dir = "state"\n[adapters]\nenabled = ["claude-print"]\n'
                f'claude_command = {json.dumps(claude_exe.as_posix())}\n',
                encoding="utf-8",
            )
            config = load_config(cfg_path)
            catalog = load_repo_catalog()

            # Verify exact argv, shell=False, stripped ANTHROPIC_*, cwd=config.path.parent.
            with (patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-secret"}),
                  patch("mah.quota.subprocess.Popen", return_value=_fake_popen(CLAUDE_USAGE_FIXTURE)) as popen):
                states, warnings = get_quota_states(config, catalog, ["claude"], refresh=True,
                                                    now=datetime(2026, 11, 19, 6, 0, 0, tzinfo=UTC))
            self.assertEqual(states["claude"].state, "ok")
            self.assertEqual(warnings, [])
            argv = popen.call_args.args[0]
            self.assertEqual(argv, [str(claude_exe.resolve()), *CLAUDE_USAGE_ARGS])
            self.assertEqual(argv[2], "/usage")
            self.assertIs(popen.call_args.kwargs["shell"], False)
            self.assertFalse(any(k.upper().startswith("ANTHROPIC_") for k in popen.call_args.kwargs["env"]))
            self.assertEqual(popen.call_args.kwargs["cwd"], str(config.path.parent))

            # Anti-billing failures: missing local_command, num_turns=1, total_cost_usd > 0.
            bad_cases = (
                {"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "total_cost_usd": 0},
                {"type": "result", "subtype": "success", "is_error": False, "local_command": "usage",
                 "num_turns": 1, "total_cost_usd": 0},
                {"type": "result", "subtype": "success", "is_error": False, "local_command": "usage",
                 "num_turns": 0, "total_cost_usd": 0.01},
            )
            for idx, bad in enumerate(bad_cases):
                with self.subTest(case=idx):
                    # Reset cache each subtest.
                    cache_path(config).unlink(missing_ok=True)
                    with patch("mah.quota.subprocess.Popen",
                               return_value=_fake_popen(json.dumps(bad).encode())):
                        states, warnings = get_quota_states(config, catalog, ["claude"], refresh=True)
                    self.assertEqual(states["claude"].state, "unknown")
                    self.assertEqual(warnings, [CLAUDE_BREAKER_WARNING])
                    cached = read_routing_cache(cache_path(config))
                    self.assertIsNotNone(cached["breakers"]["claude_usage"])

            # Breaker blocks next query while executable is unchanged.
            with patch("mah.quota.subprocess.Popen", side_effect=AssertionError("must not run while tripped")):
                states, warnings = get_quota_states(config, catalog, ["claude"], refresh=True)
            self.assertEqual(states["claude"].state, "unknown")
            self.assertEqual(warnings, [])

            # Executable upgrade (mtime/size change) clears breaker and resumes querying.
            claude_exe.write_bytes(b"fake-claude-v2-upgraded")
            with patch("mah.quota.subprocess.Popen", return_value=_fake_popen(CLAUDE_USAGE_FIXTURE)):
                states, warnings = get_quota_states(config, catalog, ["claude"], refresh=True,
                                                    now=datetime(2026, 11, 19, 6, 5, 0, tzinfo=UTC))
            self.assertEqual(states["claude"].state, "ok")
            self.assertIsNone(read_routing_cache(cache_path(config))["breakers"]["claude_usage"])


class Agy429CooldownTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-agy-429-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.cfg_path = self.root / "harness.local.toml"
        self.cfg_path.write_text('[paths]\nstate_dir = "state"\n', encoding="utf-8")
        self.config = load_config(self.cfg_path)
        self.config.database.parent.mkdir(parents=True, exist_ok=True)
        self.ledger = GrantLedger(self.config.database)
        self.catalog = load_repo_catalog()

    def _insert_failed_run(self, run_id, model, finished_at_iso, stderr_bytes=None):
        with transaction(self.ledger.database) as db:
            db.execute(
                """INSERT INTO runs (
                    run_id, template, adapter, model_requested, effort_requested,
                    state, state_reason, repo_root, workspace_kind, workspace_path,
                    task_sha256, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, "review", "agy-print", model, "medium", "FAILED", "process_failed",
                 str(self.root), "read_only", str(self.root), "0" * 64, finished_at_iso, finished_at_iso),
            )
        if stderr_bytes is not None:
            run_dir = self.config.values["paths"]["state_dir"] / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "agy.stderr.txt").write_bytes(stderr_bytes)

    def test_agy_429_fixture_fallback_cap_isolation_and_readonly_ledger(self):
        base = datetime(2026, 11, 15, 10, 0, 0, tzinfo=UTC)
        now = base + timedelta(hours=1)
        # Real fixture on claude-sonnet-5-5-medium -> cools agy:claude ONLY, not agy:gemini.
        self._insert_failed_run("agy-" + "1" * 32, "claude-sonnet-5-5-medium", base.isoformat(), AGY_429_STDERR)
        # Fallback without duration on gemini-4-neon-high -> 60 minutes from finished_at (base + 30m -> cools to base + 90m).
        self._insert_failed_run(
            "agy-" + "2" * 32, "gemini-4-neon-high", (base + timedelta(minutes=30)).isoformat(),
            b"RESOURCE_EXHAUSTED (code 429): quota reached\n",
        )
        # Unclassified model with > 8 day reset -> capped at 8 days and isolated to agy:id:<ID>.
        self._insert_failed_run(
            "agy-" + "3" * 32, "gpt-oss-120b-medium", base.isoformat(),
            b'AGY_ERROR: {"status":"RESOURCE_EXHAUSTED","error_code":429,"short_error":"Resets in 999h0m0s"}\n',
        )
        # Missing run directory -> skipped cleanly.
        self._insert_failed_run("agy-" + "4" * 32, "gemini-3.8-flash-low", base.isoformat(), stderr_bytes=None)

        before_hash = sha256(self.config.database.read_bytes()).hexdigest()
        cooldowns = derive_agy_cooldowns(self.config, self.catalog, now=now)
        after_hash = sha256(self.config.database.read_bytes()).hexdigest()
        self.assertEqual(before_hash, after_hash)

        self.assertIn("agy:claude", cooldowns)
        self.assertEqual(
            cooldowns["agy:claude"].until,
            (base + timedelta(hours=59, minutes=5, seconds=41)).isoformat(timespec="seconds"),
        )
        self.assertIn("agy:gemini", cooldowns)
        self.assertEqual(
            cooldowns["agy:gemini"].until,
            (base + timedelta(minutes=90)).isoformat(timespec="seconds"),
        )
        self.assertIn("agy:id:gpt-oss-120b-medium", cooldowns)
        self.assertEqual(
            cooldowns["agy:id:gpt-oss-120b-medium"].until,
            (base + timedelta(days=8)).isoformat(timespec="seconds"),
        )


class AppendixCD13StaleAndDispatchRefreshTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-d13-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
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
        self.config = load_config(self.cfg_path)
        self.catalog = load_repo_catalog()

    def test_stale_snapshot_keeps_tight_and_exhausted_until_reset_but_drops_ok_and_expired(self):
        t0 = datetime(2026, 11, 19, 6, 0, 0, tzinfo=UTC)
        reset_later = (t0 + timedelta(hours=5)).isoformat(timespec="seconds")
        reset_passed = (t0 + timedelta(hours=1)).isoformat(timespec="seconds")
        cpath = cache_path(self.config)
        cpath.parent.mkdir(parents=True, exist_ok=True)

        # Case 1: tight snapshot from 2h ago, reset still in future -> kept when live fails (sandbox scenario).
        cpath.write_text(json.dumps({
            "version": 1, "detect": {}, "breakers": {"claude_usage": None},
            "quota": {
                "chatgpt": {
                    "state": "tight", "source": "app_server", "fetched_at": t0.isoformat(timespec="seconds"),
                    "ordinary_usage_allowed": True,
                    "windows": [{"name": "five_hour", "used_percent": 94, "resets_at": reset_later}],
                },
                "claude": {
                    "state": "ok", "source": "claude_usage", "fetched_at": t0.isoformat(timespec="seconds"),
                    "windows": [{"name": "five_hour", "used_percent": 40, "resets_at": reset_later}],
                },
            },
        }), encoding="utf-8")

        t_2h = t0 + timedelta(hours=2)
        with (patch("mah.login_status._quota_reply", return_value=None),
              patch("mah.quota.subprocess.Popen", side_effect=OSError("sandbox blocked"))):
            states, _ = get_quota_states(self.config, self.catalog, ["chatgpt", "claude"], now=t_2h)
        self.assertEqual(states["chatgpt"].state, "tight")
        self.assertTrue(states["chatgpt"].stale)
        self.assertEqual(states["chatgpt"].age_seconds, 7200)
        # Stale "ok" snapshot becomes unknown.
        self.assertEqual(states["claude"].state, "unknown")

        # Case 2: exhausted snapshot whose resets_at has passed -> unknown.
        cpath.write_text(json.dumps({
            "version": 1, "detect": {}, "breakers": {"claude_usage": None},
            "quota": {
                "chatgpt": {
                    "state": "exhausted", "source": "app_server", "fetched_at": t0.isoformat(timespec="seconds"),
                    "ordinary_usage_allowed": False, "until": reset_passed,
                    "windows": [{"name": "five_hour", "used_percent": 100, "resets_at": reset_passed}],
                },
            },
        }), encoding="utf-8")
        with patch("mah.login_status._quota_reply", return_value=None):
            states, _ = get_quota_states(self.config, self.catalog, ["chatgpt"], now=t_2h)
        self.assertEqual(states["chatgpt"].state, "unknown")

        # Case 3: tight snapshot older than 24h -> unknown even if reset_later is in future.
        t_25h = t0 + timedelta(hours=25)
        cpath.write_text(json.dumps({
            "version": 1, "detect": {}, "breakers": {"claude_usage": None},
            "quota": {
                "chatgpt": {
                    "state": "tight", "source": "app_server", "fetched_at": t0.isoformat(timespec="seconds"),
                    "windows": [{"name": "weekly", "used_percent": 95,
                                 "resets_at": (t0 + timedelta(days=3)).isoformat(timespec="seconds")}],
                },
            },
        }), encoding="utf-8")
        with patch("mah.login_status._quota_reply", return_value=None):
            states, _ = get_quota_states(self.config, self.catalog, ["chatgpt"], now=t_25h)
        self.assertEqual(states["chatgpt"].state, "unknown")

    def test_post_dispatch_refresh_failure_never_affects_run_or_exit_code(self):
        fake_run = {"run_id": "agy-" + "a" * 32, "state": "SUCCEEDED", "state_reason": None}
        task = self.root / "task.md"
        task.write_text("task", encoding="utf-8")
        ws = self.root / "ws"
        ws.mkdir()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (patch("mah.cli.require_unsandboxed_dispatch"),
              patch("mah.cli.dispatch_run", return_value=fake_run),
              patch("mah.quota.refresh_routing_cache_best_effort", side_effect=RuntimeError("boom")) as refresh,
              redirect_stdout(stdout), redirect_stderr(stderr)):
            code = cli.main([
                "--config", str(self.cfg_path), "dispatch",
                "--grant", "g1", "--template", "review", "--adapter", "agy-print",
                "--model", "gemini-3.8-flash-low", "--effort", "low",
                "--task-file", str(task), "--workspace", str(ws),
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), fake_run)
        refresh.assert_called_once()

    def test_refresh_routing_cache_best_effort_populates_cache_and_swallows_errors(self):
        t0 = datetime(2026, 11, 19, 6, 0, 0, tzinfo=UTC)
        with (patch("mah.login_status._quota_reply", return_value=CODEX_LIMITS_FIXTURE),
              patch("mah.quota.subprocess.Popen", return_value=_fake_popen(CLAUDE_USAGE_FIXTURE)),
              patch("mah.model_detect.subprocess.run", side_effect=OSError("agy offline"))):
            refresh_routing_cache_best_effort(self.config, now=t0)
        cached = read_routing_cache(cache_path(self.config))
        self.assertEqual(cached["quota"]["chatgpt"]["state"], "ok")
        self.assertEqual(cached["quota"]["claude"]["state"], "ok")

    def test_mah_models_displays_quota_without_writing_ledger(self):
        self.config.database.parent.mkdir(parents=True, exist_ok=True)
        GrantLedger(self.config.database)
        before_hash = sha256(self.config.database.read_bytes()).hexdigest()
        with (patch("mah.model_detect.subprocess.run", side_effect=OSError("offline")),
              patch("mah.model_detect.claude_login_reasons", return_value=[]),
              patch("mah.login_status._quota_reply", return_value=CODEX_LIMITS_FIXTURE),
              patch("mah.quota.subprocess.Popen", return_value=_fake_popen(CLAUDE_USAGE_FIXTURE))):
            report = models_report(self.config, now=datetime(2026, 11, 19, 6, 0, 0, tzinfo=UTC))
        after_hash = sha256(self.config.database.read_bytes()).hexdigest()
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(report["quota"]["chatgpt"]["state"], "ok")
        self.assertEqual(report["quota"]["claude"]["state"], "ok")
        self.assertEqual(report["quota"]["agy:gemini"]["state"], "unknown")
        self.assertEqual(report["quota"]["agy:claude"]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
