"""Tests for CLI version drift reminders in mah models, Doctor, and cached mah route."""
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mah import model_detect, process_tree
from mah.config import load_config
from mah.model_detect import cache_path, executable_key, models_report, read_routing_cache
from mah.route import route

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_catalog"
AGY_FIXTURE = (FIXTURES / "agy-1.5.12-models-redacted.txt").read_bytes()
CODEX_FIXTURE = (FIXTURES / "codex-0.169.0-debug-models-redacted.json").read_bytes()


def _completed(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class CliVersionDriftUnitTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-cliver-")
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
        self.config = load_config(self.cfg_path)
        self.now = datetime(2026, 11, 20, 1, 0, 0, tzinfo=UTC)

    def test_parse_cli_version_handles_agy_codex_claude_and_malformed_outputs(self):
        self.assertEqual(model_detect.parse_cli_version("1.5.12\n"), "1.5.12")
        self.assertEqual(model_detect.parse_cli_version("codex-cli 0.169.0\r\n"), "0.169.0")
        self.assertEqual(model_detect.parse_cli_version("codex-cli 0.170.0-beta.1"), "0.170.0-beta.1")
        self.assertEqual(model_detect.parse_cli_version("2.1.325 (Claude Code)\n"), "2.1.325")
        self.assertEqual(model_detect.parse_cli_version("v1.5.12"), "1.5.12")
        for bad in ("", "   \n", "codex-cli latest", "error: unknown flag", "version 2.0"):
            with self.subTest(raw=bad):
                self.assertIsNone(model_detect.parse_cli_version(bad))

    def test_query_cli_version_uses_run_contained_with_timeout_and_returns_unknown_on_failure(self):
        calls = []

        def fake_contained(argv, **kwargs):
            calls.append((argv, kwargs))
            return _completed(0, stdout=b"codex-cli 0.170.0\n")

        with patch.object(process_tree, "run_contained", side_effect=fake_contained):
            version = model_detect.query_cli_version(self.config, "codex", self.codex)
        self.assertEqual(version, "0.170.0")
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv, [str(self.codex), "--version"])
        self.assertEqual(kwargs["cwd"], str(self.config.path.parent))
        self.assertGreater(kwargs["timeout"], 0)

        failure_cases = (
            {"return_value": _completed(1, stdout=b"codex-cli 0.170.0\n")},
            {"return_value": _completed(0, stdout=b"unparseable output")},
            {"side_effect": subprocess.TimeoutExpired(["codex", "--version"], 5)},
            {"side_effect": OSError("cannot execute")},
        )
        for opts in failure_cases:
            with self.subTest(opts=opts), patch.object(process_tree, "run_contained", **opts):
                self.assertEqual(model_detect.query_cli_version(self.config, "codex", self.codex), "unknown")

    def test_models_report_cli_versions_matched_drifted_and_unknown(self):
        version_calls = []

        def fake_contained(argv, **kwargs):
            if argv[1:] == ["--version"]:
                exe_name = Path(argv[0]).name.lower()
                version_calls.append(exe_name)
                if exe_name == "agy.exe":
                    return _completed(0, stdout=b"1.5.12\n")
                if exe_name == "codex.exe":
                    return _completed(0, stdout=b"codex-cli 0.170.0\n")
                if exe_name == "claude.exe":
                    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5))
            raise AssertionError(f"unexpected run_contained: {argv}")

        def fake_lister_run(argv, **kwargs):
            if argv[1:] == ["models"]:
                return _completed(0, stdout=AGY_FIXTURE)
            if argv[1:] == ["debug", "models"]:
                return _completed(0, stdout=CODEX_FIXTURE)
            raise AssertionError(f"unexpected subprocess.run: {argv}")

        with (patch.object(process_tree, "run_contained", side_effect=fake_contained),
              patch("mah.model_detect.subprocess.run", side_effect=fake_lister_run),
              patch("mah.model_detect.claude_login_reasons", return_value=[]),
              patch("mah.quota.get_quota_states", return_value=({}, []))):
            report = models_report(self.config, now=self.now)

        cli_versions = report["cli_versions"]
        self.assertEqual(cli_versions["agy"], {"actual": "1.5.12", "tested": "1.5.12", "status": "matched"})
        expected_warning = "codex 0.170.0 尚未經過 zMAHarness 驗證（已驗證 0.169.0）"
        self.assertEqual(cli_versions["codex"], {
            "actual": "0.170.0", "tested": "0.169.0", "status": "drifted", "warning": expected_warning,
        })
        self.assertEqual(cli_versions["claude"], {"actual": "unknown", "tested": "2.1.325", "status": "unknown"})
        self.assertIn(expected_warning, report["warnings"])
        self.assertFalse(any("agy" in w for w in report["warnings"]))
        self.assertFalse(any("claude" in w for w in report["warnings"]))

        # Version drift never blocks model availability.
        by_key = {item["key"]: item for item in report["models"]}
        self.assertTrue(by_key["gpt-6.2-sol"]["available"])

        # Known versions are cached in routing_cache.json; unknown is not cached.
        cached = read_routing_cache(cache_path(self.config))
        self.assertEqual(cached["cli_versions"]["agy"]["version"], "1.5.12")
        self.assertEqual(cached["cli_versions"]["codex"]["version"], "0.170.0")
        self.assertNotIn("claude", cached["cli_versions"])

        # Second call reuses cache for agy/codex and only retries claude (which had no cached version).
        version_calls.clear()
        with (patch.object(process_tree, "run_contained", side_effect=fake_contained),
              patch("mah.model_detect.subprocess.run", side_effect=fake_lister_run),
              patch("mah.model_detect.claude_login_reasons", return_value=[]),
              patch("mah.quota.get_quota_states", return_value=({}, []))):
            second = models_report(self.config, now=self.now)
        self.assertEqual(version_calls, ["claude.exe"])
        self.assertEqual(second["cli_versions"]["codex"]["status"], "drifted")

    def test_mah_route_never_spawns_version_subprocess_and_surfaces_cached_drift_only(self):
        version_calls = []

        def guard_contained(argv, **kwargs):
            if "--version" in argv:
                version_calls.append(argv)
                raise AssertionError(f"mah route must not query CLI version: {argv}")
            raise OSError("unexpected subprocess")

        def fake_lister_run(argv, **kwargs):
            if argv[1:] == ["models"]:
                return _completed(0, stdout=AGY_FIXTURE)
            if argv[1:] == ["debug", "models"]:
                return _completed(0, stdout=CODEX_FIXTURE)
            raise AssertionError(f"unexpected subprocess.run: {argv}")

        def fake_status(argv, env):
            if "codex" in Path(argv[0]).name.lower():
                return _completed(0, stdout=b"Logged in using ChatGPT\n")
            return _completed(0, stdout=b'{"loggedIn":true,"authMethod":"claude.ai"}')

        def run_route():
            with (patch.object(process_tree, "run_contained", side_effect=guard_contained),
                  patch("mah.preflight.git_toplevel", return_value=(str(self.repo), None)),
                  patch("mah.model_detect.subprocess.run", side_effect=fake_lister_run),
                  patch("mah.login_status._status", side_effect=fake_status),
                  patch("mah.route.get_quota_states", return_value=({}, []))):
                return route(
                    self.repo, task="review", level="standard", mode="bundle", controller="claude",
                    config_path=self.cfg_path, now=self.now,
                )

        # 1. Empty cache -> no --version subprocess, no drift warning.
        baseline = run_route()
        self.assertEqual(version_calls, [])
        self.assertTrue(baseline["ok"])
        self.assertEqual(baseline["recommended"]["adapter"], "codex-exec")
        self.assertEqual(baseline["recommended"]["model"], "gpt-6.2-sol")
        self.assertFalse(any("尚未經過 zMAHarness 驗證" in w for w in baseline["warnings"]))

        # 2. Seed cached CLI versions (agy matched, codex drifted, claude unknown) -> drift warning surfaced,
        #    zero --version subprocesses, routing choice unchanged.
        cpath = cache_path(self.config)
        cache_doc = read_routing_cache(cpath)
        cache_doc["cli_versions"] = {
            "agy": {"exe_key": executable_key(self.agy), "version": "1.5.12", "fetched_at": "2026-11-20T01:00:00+00:00"},
            "codex": {"exe_key": executable_key(self.codex), "version": "0.170.0", "fetched_at": "2026-11-20T01:00:00+00:00"},
        }
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text(json.dumps(cache_doc, ensure_ascii=False, indent=2), encoding="utf-8")

        drifted = run_route()
        self.assertEqual(version_calls, [])
        self.assertEqual(drifted["recommended"], baseline["recommended"])
        self.assertEqual(drifted["alternate"], baseline["alternate"])
        self.assertIn("codex 0.170.0 尚未經過 zMAHarness 驗證（已驗證 0.169.0）", drifted["warnings"])
        self.assertFalse(any("agy" in w for w in drifted["warnings"]))

        # 3. Binary mtime changes -> stale cache invalidated, no false drift warning.
        st = self.codex.stat()
        os.utime(self.codex, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        after_upgrade = run_route()
        self.assertEqual(version_calls, [])
        self.assertFalse(any("codex 0.170.0" in w for w in after_upgrade["warnings"]))


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "requires Windows PowerShell")
class DoctorCliVersionDriftTests(unittest.TestCase):
    def test_doctor_displays_cli_version_drift_reminder_without_failing(self):
        fixture = TemporaryDirectory(prefix="mah-doc-ver-")
        self.addCleanup(fixture.cleanup)
        root = Path(fixture.name)
        engine = root / "engine"
        for name in ("setup.ps1", "doctor.ps1", "harness-config.ps1", "harness.local.example.toml",
                     "config/defaults.json", "config/local.example.json"):
            dest = engine / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, dest)
        shutil.copytree(REPO / ".agents/skills/zmaharness", engine / ".agents/skills/zmaharness")
        shutil.copytree(REPO / "mah", engine / "mah", ignore=shutil.ignore_patterns("__pycache__"))

        profile, local, roaming, home = (root / n for n in ("profile", "local", "roaming", "home"))
        for directory in (profile, local, roaming, home):
            directory.mkdir()
        tools = root / "tools"
        tools.mkdir()
        agy = tools / "agy.exe"
        codex = tools / "codex.exe"
        claude = tools / "claude.exe"
        for exe in (agy, codex, claude):
            exe.write_bytes(b"fake")

        env = os.environ | {
            "USERPROFILE": str(profile),
            "LOCALAPPDATA": str(local),
            "APPDATA": str(roaming),
            "PYTHONUSERBASE": str(root / "user-site"),
            "ZMAHARNESS_HOME": str(home),
        }
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE", "VIRTUAL_ENV"):
            env.pop(name, None)

        def run_ps(script_name, expected_code=0):
            res = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-Command",
                 f"[Console]::OutputEncoding = [Text.Encoding]::UTF8; & ./{script_name}; exit $LASTEXITCODE"],
                cwd=engine, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=45,
            )
            self.assertEqual(res.returncode, expected_code, (res.stdout, res.stderr))
            return res.stdout

        run_ps("setup.ps1")
        cfg_file = home / "harness.local.toml"
        cfg_file.write_text(
            '[paths]\nstate_dir = "state"\n[adapters]\n'
            f'agy_command = "{agy.as_posix()}"\n'
            f'codex_command = "{codex.as_posix()}"\n'
            f'claude_command = "{claude.as_posix()}"\n',
            encoding="utf-8",
        )
        # Seed cached versions: agy matched (1.5.12), codex drifted (0.170.0 vs 0.169.0); claude uncached -> unknown.
        cpath = home / "state" / "routing_cache.json"
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text(json.dumps({
            "version": 1,
            "detect": {},
            "quota": {},
            "breakers": {"claude_usage": None},
            "cli_versions": {
                "agy": {"exe_key": executable_key(agy), "version": "1.5.12", "fetched_at": "2026-11-20T01:00:00+00:00"},
                "codex": {"exe_key": executable_key(codex), "version": "0.170.0", "fetched_at": "2026-11-20T01:00:00+00:00"},
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        server = sys.getwindowsversion().product_type in (2, 3)
        output = run_ps("doctor.ps1", expected_code=1 if server else 0)
        failures = [line for line in output.splitlines() if line.startswith("[失敗]")]
        if server:
            self.assertEqual(len(failures), 1, output)
            self.assertTrue(failures[0].startswith("[失敗] Windows 11"), output)
        else:
            self.assertEqual(failures, [], output)
        self.assertIn("[通過] CLI 版本：agy 1.5.12、codex 0.170.0、claude unknown", output)
        self.assertIn("[警告] CLI 版本提醒：codex 0.170.0 尚未經過 zMAHarness 驗證（已驗證 0.169.0）", output)
        self.assertNotIn("agy 1.5.12 尚未經過", output)
        self.assertNotIn("claude unknown 尚未經過", output)


if __name__ == "__main__":
    unittest.main()
