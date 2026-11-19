from datetime import UTC, datetime, timedelta
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from mah import cli
from mah.adapters import registry
from mah.adapters.base import AdapterPolicy
from mah.catalog import effective_efforts, load_repo_catalog
from mah.config import load_config
from mah.db import transaction
from mah.ledger import GrantLedger
from mah.model_detect import (
    ListResult,
    cache_path,
    detect_adapters,
    list_agy_models,
    list_codex_models,
    models_report,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_catalog"
AGY_FIXTURE = (FIXTURES / "agy-1.5.12-models-redacted.txt").read_bytes()
CODEX_FIXTURE = (FIXTURES / "codex-0.169.0-debug-models-redacted.json").read_bytes()


def _completed(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ModelListerRegistryTests(unittest.TestCase):
    def test_registry_model_listers_registered_for_agy_and_codex_only(self):
        self.assertEqual(set(registry.MODEL_LISTERS), {"agy-print", "codex-exec"})
        self.assertIsNotNone(registry.model_lister("agy-print"))
        self.assertIsNotNone(registry.model_lister("codex-exec"))
        self.assertIsNone(registry.model_lister("claude-print"))

    def test_registry_register_and_unregister_custom_model_lister(self):
        name = "test-lister-adapter"
        self.addCleanup(registry.unregister, name)
        lister = lambda config, exe: ListResult("ok", ids=("demo-1",))
        registry.register(name, AdapterPolicy(), model_lister=lister)
        self.assertIs(registry.model_lister(name), lister)
        registry.unregister(name)
        self.assertIsNone(registry.model_lister(name))


class AgyAndCodexListerTests(unittest.TestCase):
    def test_agy_lister_parses_1_5_12_fixture_and_ignores_stderr(self):
        with patch("mah.model_detect.subprocess.run",
                   return_value=_completed(0, stdout=AGY_FIXTURE, stderr=b"Fetching available models...\n")) as run:
            result = list_agy_models(None, Path("C:/tools/agy.exe"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.ids), 23)
        self.assertIn("gemini-4-neon-high", result.ids)
        self.assertIn("gpt-oss-120b-medium", result.ids)
        self.assertEqual(run.call_args.args[0], [str(Path("C:/tools/agy.exe")), "models"])
        self.assertIs(run.call_args.kwargs["shell"], False)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

        catalog = load_repo_catalog()
        classified = [catalog.classify_adapter_id("agy-print", mid)[0] for mid in result.ids]
        self.assertEqual(classified.count("known"), 20)
        self.assertEqual(classified.count("refused"), 2)
        self.assertEqual(classified.count("unclassified"), 1)
        unclassified = [mid for mid in result.ids if catalog.classify_adapter_id("agy-print", mid)[0] == "unclassified"]
        self.assertEqual(unclassified, ["gpt-oss-120b-medium"])

    def test_agy_lister_failure_modes_return_unknown(self):
        cases = [
            {"return_value": _completed(1, stdout=AGY_FIXTURE)},
            {"return_value": _completed(0, stdout=b"")},
            {"return_value": _completed(0, stdout=b"Fetching available models...\nError\nno-tab-line\n\tbad\n")},
            {"side_effect": subprocess.TimeoutExpired(["agy", "models"], 10)},
            {"side_effect": OSError("cannot execute")},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), patch("mah.model_detect.subprocess.run", **kwargs):
                result = list_agy_models(None, Path("C:/tools/agy.exe"))
                self.assertEqual(result.status, "unknown")
                self.assertEqual(result.ids, ())

    def test_codex_lister_parses_0_169_0_fixture_filters_hide_and_handles_bom(self):
        bom_payload = b"\xef\xbb\xbf" + CODEX_FIXTURE
        with patch("mah.model_detect.subprocess.run", return_value=_completed(0, stdout=bom_payload)) as run:
            result = list_codex_models(None, Path("C:/tools/codex.exe"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.ids), 10)
        self.assertNotIn("gpt-reserve", result.ids)
        self.assertNotIn("codex-auto-review", result.ids)
        self.assertIn("gpt-6.1-terra", result.ids)
        self.assertEqual(result.efforts["gpt-6.1-terra"], ("low", "medium", "high", "xhigh", "max", "ultra"))
        self.assertEqual(run.call_args.args[0], [str(Path("C:/tools/codex.exe")), "debug", "models"])
        self.assertIs(run.call_args.kwargs["shell"], False)

        catalog = load_repo_catalog()
        terra = catalog.model_by_key("gpt-6.1-terra")
        self.assertEqual(
            effective_efforts(terra, terra.routes[0], detected_efforts=result.efforts["gpt-6.1-terra"]),
            ("low", "medium", "high", "xhigh"),
        )

    def test_codex_lister_failure_modes_return_unknown(self):
        hidden_only = json.dumps({"models": [{"slug": "gpt-reserve", "visibility": "hide"}]}).encode()
        cases = [
            {"return_value": _completed(2, stdout=CODEX_FIXTURE)},
            {"return_value": _completed(0, stdout=b"not json")},
            {"return_value": _completed(0, stdout=b'{"other": []}')},
            {"return_value": _completed(0, stdout=hidden_only)},
            {"side_effect": subprocess.TimeoutExpired(["codex", "debug", "models"], 10)},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), patch("mah.model_detect.subprocess.run", **kwargs):
                result = list_codex_models(None, Path("C:/tools/codex.exe"))
                self.assertEqual(result.status, "unknown")
                self.assertEqual(result.ids, ())


class DetectionCacheAndModelsReportTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-detect-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.agy = self.tools / "agy.exe"
        self.codex = self.tools / "codex.exe"
        self.claude = self.tools / "claude.exe"
        for exe in (self.agy, self.codex, self.claude):
            exe.write_bytes(b"fake-exe")
        self.config_file = self.root / "harness.local.toml"
        self.config_file.write_text(
            '[paths]\nstate_dir = "state"\n'
            f'[adapters]\n'
            f'agy_command = "{self.agy.as_posix()}"\n'
            f'codex_command = "{self.codex.as_posix()}"\n'
            f'claude_command = "{self.claude.as_posix()}"\n',
            encoding="utf-8",
        )
        self.config = load_config(self.config_file)

    def test_detect_cache_hits_when_fresh_and_invalidates_on_exe_change_or_refresh(self):
        t0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)
        agy_calls = []

        def fake_lister(config, exe):
            agy_calls.append(exe)
            return ListResult("ok", ids=("gemini-4-neon-high", "gemini-3.8-flash-low"))

        with patch.dict(registry.MODEL_LISTERS, {"agy-print": fake_lister}):
            first = detect_adapters(self.config, ["agy-print"], ttl_hours=12, now=t0)
            self.assertEqual((first["agy-print"].status, first["agy-print"].source), ("ok", "live"))
            self.assertEqual(len(agy_calls), 1)
            self.assertTrue(cache_path(self.config).is_file())

            # Fresh cache hit -> no lister call.
            second = detect_adapters(self.config, ["agy-print"], ttl_hours=12, now=t0 + timedelta(hours=2))
            self.assertEqual((second["agy-print"].status, second["agy-print"].source), ("ok", "cache"))
            self.assertEqual(len(agy_calls), 1)

            # --refresh bypasses cache.
            refreshed = detect_adapters(self.config, ["agy-print"], ttl_hours=12, refresh=True,
                                        now=t0 + timedelta(hours=2))
            self.assertEqual((refreshed["agy-print"].status, refreshed["agy-print"].source), ("ok", "live"))
            self.assertEqual(len(agy_calls), 2)

            # Changing executable mtime invalidates cache.
            stat = self.agy.stat()
            os.utime(self.agy, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
            after_upgrade = detect_adapters(self.config, ["agy-print"], ttl_hours=12, now=t0 + timedelta(hours=3))
            self.assertEqual((after_upgrade["agy-print"].status, after_upgrade["agy-print"].source), ("ok", "live"))
            self.assertEqual(len(agy_calls), 3)

    def test_expired_cache_does_not_fall_back_on_live_failure(self):
        t0 = datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)
        with patch.dict(registry.MODEL_LISTERS, {
            "agy-print": lambda config, exe: ListResult("ok", ids=("gemini-4-neon-high",)),
        }):
            first = detect_adapters(self.config, ["agy-print"], ttl_hours=12, now=t0)
            self.assertEqual(first["agy-print"].status, "ok")

        with patch.dict(registry.MODEL_LISTERS, {
            "agy-print": lambda config, exe: ListResult("unknown", detail="offline"),
        }):
            expired = detect_adapters(self.config, ["agy-print"], ttl_hours=12, now=t0 + timedelta(hours=13))
            self.assertEqual(expired["agy-print"].status, "unknown")
            self.assertEqual(expired["agy-print"].ids, ())

    def test_cache_write_permission_error_is_ignored(self):
        with (patch.dict(registry.MODEL_LISTERS, {
                  "agy-print": lambda config, exe: ListResult("ok", ids=("gemini-4-neon-high",)),
              }),
              patch("mah.model_detect.atomic_write", side_effect=PermissionError("read-only sandbox"))):
            records = detect_adapters(self.config, ["agy-print"], ttl_hours=12)
        self.assertEqual(records["agy-print"].status, "ok")
        self.assertEqual(records["agy-print"].ids, ("gemini-4-neon-high",))

    def test_models_report_classifies_fixtures_and_respects_all_flag(self):
        def fake_run(argv, **kwargs):
            if argv[1:] == ["models"]:
                return _completed(0, stdout=AGY_FIXTURE, stderr=b"Fetching available models...\n")
            if argv[1:] == ["debug", "models"]:
                return _completed(0, stdout=CODEX_FIXTURE)
            raise AssertionError(f"unexpected subprocess: {argv}")

        with (patch("mah.model_detect.subprocess.run", side_effect=fake_run),
              patch("mah.model_detect.claude_login_reasons", return_value=[])):
            default_report = models_report(self.config)
            all_report = models_report(self.config, include_all=True)

        self.assertNotIn("quota", default_report)
        self.assertEqual(default_report["detection"]["agy-print"]["status"], "ok")
        self.assertEqual(default_report["detection"]["agy-print"]["detected_count"], 23)
        self.assertEqual(default_report["detection"]["agy-print"]["unclassified_count"], 1)
        self.assertNotIn("unclassified", default_report["detection"]["agy-print"])
        self.assertEqual(all_report["detection"]["agy-print"]["source"], "cache")
        self.assertEqual(all_report["detection"]["agy-print"]["unclassified"], ["gpt-oss-120b-medium"])

        self.assertEqual(default_report["detection"]["codex-exec"]["status"], "ok")
        self.assertEqual(default_report["detection"]["codex-exec"]["detected_count"], 10)
        self.assertEqual(default_report["detection"]["codex-exec"]["unclassified_count"], 0)
        self.assertEqual(default_report["detection"]["claude-print"], {"status": "alias", "login": "ok", "observed": {}})

        by_key = {item["key"]: item for item in default_report["models"]}
        self.assertTrue(by_key["gemini-4-neon"]["available"])
        # Default max_effort = high filters out xhigh.
        self.assertEqual(
            by_key["gemini-4-neon"]["routes"][0]["ids"],
            ["gemini-4-neon-low", "gemini-4-neon-medium", "gemini-4-neon-high"],
        )
        self.assertFalse(by_key["gemini-pro-orchestrator"]["available"])
        self.assertEqual(by_key["gemini-pro-orchestrator"]["status"], "refused")
        self.assertEqual(
            by_key["gemini-pro-orchestrator"]["routes"][0]["matched_ids"],
            ["gemini-pro-orchestrator-neon", "gemini-pro-orchestrator-g1-pro"],
        )
        # Codex efforts intersect with CODEX_EFFORTS and max_effort=high.
        self.assertEqual(by_key["gpt-6.1-terra"]["routes"][0]["efforts"], ["low", "medium", "high"])

    def test_cli_mah_models_reads_claude_observed_and_never_writes_ledger(self):
        state_dir = self.config.database.parent
        state_dir.mkdir(parents=True, exist_ok=True)
        ledger = GrantLedger(self.config.database)
        with transaction(ledger.database) as db:
            db.execute(
                """INSERT INTO runs (
                    run_id, template, adapter, model_requested, effort_requested,
                    model_observed, state, repo_root, workspace_kind, workspace_path,
                    task_sha256, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("claude-" + "a" * 32, "review", "claude-print", "opus", "high",
                 "claude-opus-5-5", "SUCCEEDED", str(self.root), "read_only", str(self.root),
                 "0" * 64, "2026-11-15T01:00:00+00:00", "2026-11-15T01:02:00+00:00"),
            )
        before_hash = sha256(self.config.database.read_bytes()).hexdigest()

        def fake_run(argv, **kwargs):
            if argv[1:] == ["models"]:
                return _completed(0, stdout=AGY_FIXTURE)
            if argv[1:] == ["debug", "models"]:
                return _completed(0, stdout=CODEX_FIXTURE)
            raise AssertionError(f"unexpected subprocess: {argv}")

        with (patch("mah.model_detect.subprocess.run", side_effect=fake_run),
              patch("mah.model_detect.claude_login_reasons", return_value=[])):
            for argv in (
                ["--config", str(self.config_file), "models"],
                ["models", "--config", str(self.config_file), "--all"],
            ):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(argv)
                self.assertEqual(code, 0, stderr.getvalue())
                payload = json.loads(stdout.getvalue())
                self.assertEqual(payload["detection"]["claude-print"]["observed"], {"opus": "claude-opus-5-5"})

        after_hash = sha256(self.config.database.read_bytes()).hexdigest()
        self.assertEqual(before_hash, after_hash)

    def test_corrupt_cache_and_fail_open_vs_missing_executable(self):
        cpath = cache_path(self.config)
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text("{not valid json", encoding="utf-8")

        def fail_run(argv, **kwargs):
            return _completed(1, stderr=b"offline")

        with (patch("mah.model_detect.subprocess.run", side_effect=fail_run),
              patch("mah.model_detect.claude_login_reasons", return_value=["not logged in"])):
            report = models_report(self.config)

        # Executable exists, live query failed -> unknown -> fail-open (catalog models stay available).
        self.assertEqual(report["detection"]["agy-print"]["status"], "unknown")
        self.assertEqual(report["detection"]["claude-print"]["login"], "error")
        by_key = {item["key"]: item for item in report["models"]}
        self.assertTrue(by_key["gemini-4-neon"]["available"])
        # claude-print failed login, but claude-sonnet's agy-print fallback route is still available (fail-open).
        sonnet_routes = {r["adapter"]: r["available"] for r in by_key["claude-sonnet"]["routes"]}
        self.assertEqual(sonnet_routes, {"claude-print": False, "agy-print": True})
        self.assertTrue(by_key["claude-sonnet"]["available"])

        # Remove agy.exe -> missing executable makes agy routes unavailable.
        self.agy.unlink()
        with (patch("mah.model_detect.subprocess.run", side_effect=fail_run),
              patch("mah.model_detect.claude_login_reasons", return_value=["not logged in"])):
            missing_report = models_report(self.config)
        by_key_missing = {item["key"]: item for item in missing_report["models"]}
        self.assertFalse(by_key_missing["gemini-4-neon"]["available"])
        self.assertFalse(by_key_missing["claude-sonnet"]["available"])


if __name__ == "__main__":
    unittest.main()
