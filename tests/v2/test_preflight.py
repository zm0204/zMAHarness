import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mah import cli
from mah.config import ConfigError, default_config_path, path_report, sandbox_redirect_detected, \
    user_local_appdata, zmaharness_home
from mah.ledger import GrantLedger, LedgerError
from mah.preflight import GIT_NOT_INSTALLED, INVALID_MODEL_ID, NOT_A_GIT_REPOSITORY, UNSANDBOXED_DISPATCH_REQUIRED, \
    git_toplevel, preflight, require_unsandboxed_dispatch

REPO = Path(__file__).resolve().parents[2]
SANDBOX_LOCAL = r"C:\Users\user\AppData\Local\Packages\openai.codex_2p2nqsd0c76g0\AC\#!sandbox.{0F1E2D3C}"
WINDOWS = unittest.skipUnless(os.name == "nt", "AppContainer redirects exist only on Windows")


def clean_environment(**values):
    environment = {key: value for key, value in os.environ.items() if key != "ZMAHARNESS_HOME"}
    return patch.dict(os.environ, environment | values, clear=True)


def snapshot(root):
    return {str(path.relative_to(root)): (path.is_dir(), None if path.is_dir() else path.read_bytes(),
                                          path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*"))}


class PathResolutionTests(unittest.TestCase):
    @WINDOWS
    def test_appcontainer_redirect_uses_the_profile_local_folder(self):
        with clean_environment(LOCALAPPDATA=SANDBOX_LOCAL, USERPROFILE=r"C:\Users\user"):
            self.assertTrue(sandbox_redirect_detected())
            self.assertEqual(user_local_appdata(), Path(r"C:\Users\user\AppData\Local"))
            self.assertEqual(zmaharness_home(), (Path(r"C:\Users\user\AppData\Local\zMAHarness"), "sandbox_redirect"))
            self.assertEqual(default_config_path(), Path(r"C:\Users\user\AppData\Local\zMAHarness\harness.local.toml"))
        with clean_environment(LOCALAPPDATA=SANDBOX_LOCAL.lower(), USERPROFILE=r"C:\Users\user"):
            self.assertTrue(sandbox_redirect_detected())

    def test_ordinary_local_folder_is_unchanged(self):
        with TemporaryDirectory() as base, clean_environment(LOCALAPPDATA=base, XDG_STATE_HOME=base):
            self.assertFalse(sandbox_redirect_detected())
            self.assertEqual(zmaharness_home(), (Path(base) / "zMAHarness", "default"))
            self.assertEqual(default_config_path(), Path(base) / "zMAHarness" / "harness.local.toml")
            if os.name == "nt":
                self.assertEqual(user_local_appdata(), Path(base))

    @WINDOWS
    def test_zmaharness_home_has_the_highest_priority(self):
        with TemporaryDirectory() as home, clean_environment(LOCALAPPDATA=SANDBOX_LOCAL, USERPROFILE=r"C:\Users\user",
                                                             ZMAHARNESS_HOME=home):
            self.assertEqual(zmaharness_home(), (Path(home), "ZMAHARNESS_HOME"))
            self.assertEqual(default_config_path(), Path(home) / "harness.local.toml")
            report = path_report()
            self.assertEqual((report["source"], report["legacy_config"], report["sandbox_redirect_detected"]),
                             ("ZMAHARNESS_HOME", None, True))

    def test_zmaharness_home_never_falls_back_to_the_legacy_folder(self):
        with TemporaryDirectory() as base:
            legacy = Path(base) / "MultiAgentHarness" / "harness.local.toml"
            legacy.parent.mkdir()
            legacy.write_text("", encoding="utf-8")
            with clean_environment(LOCALAPPDATA=base, XDG_STATE_HOME=base):
                self.assertEqual(default_config_path(), legacy)
            home = Path(base) / "explicit"
            with clean_environment(LOCALAPPDATA=base, XDG_STATE_HOME=base, ZMAHARNESS_HOME=str(home)):
                self.assertEqual(default_config_path(), home / "harness.local.toml")

    def test_relative_or_empty_zmaharness_home_is_rejected(self):
        for value in ("relative-home", "", " "):
            with self.subTest(value=value), clean_environment(ZMAHARNESS_HOME=value):
                with self.assertRaises(ConfigError) as error:
                    default_config_path()
                self.assertEqual(str(error.exception), f"ZMAHARNESS_HOME 必須是絕對路徑，目前為「{value}」")
                stderr = io.StringIO()
                with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                    code = cli.main(["preflight", "--repo", str(REPO)])
                self.assertEqual((code, stderr.getvalue()), (2, f"錯誤：{error.exception}\n"))


class PreflightTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-preflight-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True, timeout=15)
        self.agy = self.root / "tools" / "agy.exe"
        self.agy.parent.mkdir()
        self.agy.write_bytes(b"never executed by preflight")
        self.plain = self.root / "drive-export"
        self.plain.mkdir()

    def write_config(self, *, command=None, enabled='["agy-print"]'):
        command = str(self.agy) if command is None else command
        (self.home / "harness.local.toml").write_text(
            f'[adapters]\nenabled = {enabled}\nagy_command = {json.dumps(command)}\n', encoding="utf-8")

    def preflight(self, repo, *args):
        stdout = io.StringIO()
        with clean_environment(ZMAHARNESS_HOME=str(self.home)), redirect_stdout(stdout), \
                redirect_stderr(io.StringIO()):
            code = cli.main(["preflight", "--repo", str(repo), *args])
        return code, json.loads(stdout.getvalue())

    def assert_read_only(self, *args):
        before = snapshot(self.root)
        result = self.preflight(*args)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.home / "state").exists())
        return result

    def test_non_git_directory_is_not_ok(self):
        self.write_config()
        code, report = self.assert_read_only(self.plain)
        self.assertEqual(code, 30)
        self.assertIsNone(report["repo_root"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["reasons"], ["目標不是本機 Git 專案；雲端硬碟、網頁等資料不在協作範圍內。"])

    def test_missing_worker_executable_is_not_ok(self):
        self.write_config(command=str(self.root / "missing" / "agy.exe"))
        code, report = self.assert_read_only(self.repo)
        self.assertEqual(code, 30)
        self.assertEqual((report["adapter"], report["adapter_executable_found"], report["ok"]),
                         ("agy-print", False, False))
        self.assertEqual(report["reasons"], ["找不到 agy-print 的 Worker 執行檔，請安裝或檢查本機設定。"])

    @unittest.skipUnless(os.name == "nt", "agy must resolve to a native .exe on Windows")
    def test_everything_in_place_is_ok(self):
        self.write_config()
        code, report = self.assert_read_only(self.repo, "--adapter", "agy-print")
        self.assertEqual(code, 0)
        self.assertEqual(Path(report["repo_root"]).resolve(), self.repo.resolve())
        self.assertEqual(report["config_path"], str(self.home / "harness.local.toml"))
        self.assertEqual(report, report | {
            "config_found": True, "adapter": "agy-print", "adapter_enabled": True, "adapter_executable_found": True,
            "sandbox_redirect_detected": False, "needs_unsandboxed_dispatch": False, "ok": True, "reasons": [],
        })
        self.assertEqual(set(report), {"repo_root", "config_path", "config_found", "adapter", "adapter_enabled",
                                       "adapter_executable_found", "sandbox_redirect_detected",
                                       "needs_unsandboxed_dispatch", "ok", "reasons"})

    @unittest.skipUnless(os.name == "nt", "agy must resolve to a native .exe on Windows")
    def test_registered_but_disabled_adapter_is_not_ok(self):
        self.write_config(enabled='["codex-exec"]')
        code, report = self.assert_read_only(self.repo)
        self.assertEqual(code, 30)
        self.assertEqual((report["adapter_enabled"], report["adapter_executable_found"]), (False, True))
        self.assertEqual(report["reasons"], ["agy-print 未在本機設定啟用（adapters.enabled）"])

    def test_zmaharness_home_without_settings_is_reported(self):
        code, report = self.assert_read_only(self.repo)
        self.assertEqual(code, 30)
        self.assertFalse(report["config_found"])
        self.assertIn(f"ZMAHARNESS_HOME 指定的位置沒有設定檔：{self.home / 'harness.local.toml'}", report["reasons"])

    @WINDOWS
    def test_sandbox_redirect_is_reported_without_blocking_the_card(self):
        self.write_config()
        stdout = io.StringIO()
        with clean_environment(ZMAHARNESS_HOME=str(self.home), LOCALAPPDATA=SANDBOX_LOCAL), \
                redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = cli.main(["preflight", "--repo", str(self.repo)])
        report = json.loads(stdout.getvalue())
        self.assertEqual((code, report["sandbox_redirect_detected"], report["needs_unsandboxed_dispatch"]),
                         (0, True, True))

    def test_orchestrator_model_is_not_ok_for_agy(self):
        self.write_config()
        for model in ("gemini-pro-orchestrator-neon", "gemini-pro-orchestrator-g1-pro"):
            with self.subTest(model=model):
                code, report = self.assert_read_only(self.repo, "--adapter", "agy-print", "--model", model)
                self.assertEqual((code, report["ok"]), (30, False))
                self.assertTrue(any(reason.startswith(f"agy-print 不接受 Orchestrator 模型 {model}：")
                                    for reason in report["reasons"]))

    @WINDOWS
    def test_ordinary_gemini_model_keeps_the_report_ok(self):
        self.write_config()
        for model in ("gemini-4-neon-xhigh", "gemini-4-hydrogen-high", "gemini-4-argon-medium"):
            with self.subTest(model=model):
                code, report = self.assert_read_only(self.repo, "--adapter", "agy-print", "--model", model)
                self.assertEqual((code, report["ok"], report["reasons"]), (0, True, []))

    def test_malformed_model_id_is_not_ok_just_as_card_create_rejects_it(self):
        self.write_config()
        state = TemporaryDirectory(prefix="mah-preflight-ledger-")
        self.addCleanup(state.cleanup)
        ledger = GrantLedger(Path(state.name) / "ledger.sqlite3")
        for index, model in enumerate((" gemini-pro-orchestrator-neon", "gemini-4-neon-high ", "", "-gemini-4-neon",
                                       "gemini 4 neon", "gemini-{model}", "g" * 129)):
            with self.subTest(model=model):
                # --model=<value> so argparse keeps a value that starts with "-" or is empty.
                code, report = self.assert_read_only(self.repo, "--adapter", "agy-print", f"--model={model}")
                self.assertEqual((code, report["ok"]), (30, False))
                self.assertIn(INVALID_MODEL_ID.format(model=model), report["reasons"])
                self.assertFalse(any("Orchestrator" in reason for reason in report["reasons"]))
                with self.assertRaises(LedgerError) as failure:
                    ledger.create_card(
                        f"card-bad-{index}", repo_root=str(self.repo), task_key=f"bad-{index}", trigger_ids=["T5"],
                        prepare_mode="card", allowed_templates=["review"], max_dispatches=1, ttl_minutes=10,
                        allowed_targets=[{"adapter": "legacy-fixture", "model": model, "effort_max": "low"}])
                self.assertEqual(failure.exception.code, 2)

    def test_longest_well_formed_model_id_passes_the_format_check(self):
        self.write_config()
        model = "g" * 128
        _, report = self.assert_read_only(self.repo, "--adapter", "agy-print", "--model", model)
        self.assertNotIn(INVALID_MODEL_ID.format(model=model), report["reasons"])

    def test_unknown_adapter_is_an_argparse_error(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit_error:
            cli.main(["preflight", "--repo", str(self.repo), "--adapter", "unknown-x"])
        self.assertEqual(exit_error.exception.code, 2)


class GitClassificationTests(unittest.TestCase):
    """Only a certain cause gets its own advice; anything else is reported as a Git failure."""

    HINT = "如果目標不在主控目前的工作資料夾內，請在該專案開啟主控後再試。"

    @staticmethod
    def completed(code, stderr="", stdout=""):
        return subprocess.CompletedProcess(["git"], code, stdout=stdout, stderr=stderr)

    @staticmethod
    def git_result(outcome):
        option = {"side_effect": outcome} if isinstance(outcome, BaseException) else {"return_value": outcome}
        return patch("mah.preflight.subprocess.run", **option)

    def classify(self, outcome):
        with self.git_result(outcome):
            return git_toplevel(Path("target"))

    def test_each_git_failure_has_its_own_reason(self):
        cases = {
            "not_repository": (self.completed(128, "fatal: not a git repository (or any of the parent directories): .git\n"),
                               NOT_A_GIT_REPOSITORY),
            "not_installed": (FileNotFoundError(2, "The system cannot find the file specified"), GIT_NOT_INSTALLED),
            "launch_denied": (PermissionError(13, "Permission denied"),
                              f"無法啟動 git：[Errno 13] Permission denied。可能是被主控的沙盒限制；{self.HINT}"),
            "stderr_denied": (self.completed(128, "\nfatal: cannot change to 'B': Permission denied\nsecond line\n"),
                              f"無法檢查目標的 Git 專案：fatal: cannot change to 'B': Permission denied。{self.HINT}"),
            "timeout": (subprocess.TimeoutExpired(["git"], 10),
                        f"無法檢查目標的 Git 專案：TimeoutExpired: Command '['git']' timed out after 10 seconds。{self.HINT}"),
            "other_oserror": (OSError(22, "Invalid argument"),
                              f"無法檢查目標的 Git 專案：OSError: [Errno 22] Invalid argument。{self.HINT}"),
            "silent_failure": (self.completed(1), f"無法檢查目標的 Git 專案：git 結束碼 1，沒有錯誤訊息。{self.HINT}"),
        }
        reasons = {}
        for name, (outcome, expected) in cases.items():
            with self.subTest(case=name):
                root, reason = self.classify(outcome)
                self.assertIsNone(root)
                self.assertEqual(reason, expected)
                reasons[name] = reason
        self.assertEqual(len(set(reasons.values())), len(reasons))
        self.assertEqual([name for name, reason in reasons.items() if reason == NOT_A_GIT_REPOSITORY], ["not_repository"])
        self.assertEqual([name for name, reason in reasons.items() if "請安裝 Git" in reason], ["not_installed"])

    def test_git_runs_with_english_messages_in_every_locale(self):
        with patch.dict(os.environ, {"LC_ALL": "zh_TW.UTF-8", "LANGUAGE": "zh_TW"}), \
                patch("mah.preflight.subprocess.run", return_value=self.completed(0, stdout="C:/x\n")) as run:
            git_toplevel(Path("target"))
        environment = run.call_args.kwargs["env"]
        self.assertEqual((environment["LC_ALL"], environment["LANGUAGE"]), ("C", "C"))
        self.assertEqual(environment["PATH"], os.environ["PATH"])

    def test_a_working_git_returns_the_root(self):
        self.assertEqual(self.classify(self.completed(0, stdout="C:/work/project\n")), ("C:/work/project", None))

    def test_preflight_reports_git_failures_without_a_repo_root(self):
        for outcome in (PermissionError(13, "Permission denied"), FileNotFoundError(2, "missing"),
                        self.completed(128, "fatal: not a git repository\n")):
            with self.subTest(outcome=outcome), TemporaryDirectory() as home, \
                    clean_environment(ZMAHARNESS_HOME=home):
                _, expected = self.classify(outcome)
                with self.git_result(outcome):
                    report = preflight(Path("target"), "agy-print")
                self.assertEqual((report["repo_root"], report["ok"]), (None, False))
                self.assertEqual(report["reasons"][0], expected)
                self.assertNotIn(expected, report["reasons"][1:])


class SandboxDispatchTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-sandbox-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.home = self.root / "home"
        self.state = self.home / "state"
        self.state.mkdir(parents=True)
        self.repo = self.root / "repo"
        workspace = self.root / "workspace"
        self.repo.mkdir()
        workspace.mkdir()
        self.ledger = GrantLedger(self.state / "ledger.sqlite3")
        digest = self.ledger.create_card(
            "sandbox-card", repo_root=str(self.repo), task_key="sandbox", trigger_ids=["T5"], prepare_mode="card",
            allowed_templates=["review"], allowed_targets=[{"adapter": "agy-print", "model": "m", "effort_max": "low"}],
            max_dispatches=1, ttl_minutes=10, timeout_seconds=900, workspace_path=str(workspace),
            task_sha256=sha256(b"task").hexdigest(),
        )
        self.grant_id = self.ledger.approve("sandbox-card", digest, via="codex_prompt")

    def dispatch(self, enabled='["agy-print"]', **environment):
        (self.home / "harness.local.toml").write_text(f"[adapters]\nenabled = {enabled}\n", encoding="utf-8")
        stderr = io.StringIO()
        with clean_environment(ZMAHARNESS_HOME=str(self.home), **environment), redirect_stderr(stderr), \
                redirect_stdout(io.StringIO()):
            code = cli.main(["dispatch", "--grant", self.grant_id, "--template", "review", "--adapter", "agy-print",
                             "--model", "m", "--effort", "low", "--task-file", "task.md",
                             "--workspace", str(self.root / "workspace")])
        return code, stderr.getvalue()

    def assert_grant_unused(self):
        grant = self.ledger.get_grant(self.grant_id)
        self.assertEqual((grant["status"], grant["used_dispatches"]), ("ACTIVE", 0))
        self.assertFalse((self.state / "runs").exists())
        self.assertEqual([path.name for path in self.state.iterdir() if path.name.startswith(".mah-write-probe")], [])

    @WINDOWS
    def test_redirected_sandbox_is_refused_before_the_grant_is_used(self):
        code, stderr = self.dispatch(LOCALAPPDATA=SANDBOX_LOCAL)
        self.assertEqual((code, stderr), (14, f"錯誤：{UNSANDBOXED_DISPATCH_REQUIRED}\n"))
        self.assert_grant_unused()

    def test_unwritable_state_is_refused_with_the_same_message(self):
        with patch("mah.preflight.mkstemp", side_effect=PermissionError("sandbox denied")):
            code, stderr = self.dispatch()
        self.assertEqual((code, stderr), (14, f"錯誤：{UNSANDBOXED_DISPATCH_REQUIRED}\n"))
        self.assert_grant_unused()

    def test_writable_state_leaves_no_probe_file(self):
        before = sorted(path.name for path in self.state.iterdir())
        require_unsandboxed_dispatch(self.state)
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), before)
        # The probe passes, and the later enabled-adapter check stops the dispatch as before.
        code, stderr = self.dispatch(enabled='["codex-exec"]')
        self.assertEqual((code, stderr), (14, "錯誤：agy-print 尚未在本機設定啟用\n"))
        self.assert_grant_unused()


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "requires Windows PowerShell")
class SetupHomeTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-home-", dir=REPO.parent)
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.engine = self.root / "engine"
        for name in ("setup.ps1", "doctor.ps1", "harness-config.ps1", "harness.local.example.toml",
                     "config/defaults.json", "config/local.example.json"):
            destination = self.engine / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, destination)
        shutil.copytree(REPO / ".agents/skills/zmaharness", self.engine / ".agents/skills/zmaharness")
        shutil.copytree(REPO / "mah", self.engine / "mah", ignore=shutil.ignore_patterns("__pycache__"))
        self.local = self.root / "local"
        for name in ("profile", "local", "roaming"):
            (self.root / name).mkdir()
        self.env = os.environ | {"USERPROFILE": str(self.root / "profile"), "LOCALAPPDATA": str(self.local),
                                 "APPDATA": str(self.root / "roaming"), "PYTHONUSERBASE": str(self.root / "user-site")}
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE", "VIRTUAL_ENV", "ZMAHARNESS_HOME"):
            self.env.pop(name, None)

    def setup_script(self, home, expected_code):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-Command",
             "[Console]::OutputEncoding = [Text.Encoding]::UTF8; & ./setup.ps1; exit $LASTEXITCODE"],
            cwd=self.engine, env=self.env | {"ZMAHARNESS_HOME": home}, capture_output=True, encoding="utf-8",
            errors="replace", timeout=60,
        )
        self.assertEqual(result.returncode, expected_code, (result.stdout, result.stderr))
        return result.stdout

    def test_setup_and_mah_use_the_same_zmaharness_home(self):
        home = self.root / "custom home"
        output = self.setup_script(str(home), 0)
        config = home / "harness.local.toml"
        self.assertTrue(config.is_file(), output)
        self.assertIn(f"[通過] zMAHarness 本機設定：{config}", output)
        self.assertFalse((self.local / "zMAHarness").exists())
        result = subprocess.run([sys.executable, "-B", "-m", "mah", "preflight", "--repo", str(self.engine)],
                                cwd=self.engine, env=self.env | {"ZMAHARNESS_HOME": str(home)},
                                capture_output=True, encoding="utf-8", timeout=30)
        report = json.loads(result.stdout)
        self.assertEqual((report["config_path"], report["config_found"]), (str(config), True))

    def test_setup_rejects_a_relative_zmaharness_home_before_writing(self):
        output = self.setup_script("relative-home", 1)
        self.assertIn("[失敗] 初始化：ZMAHARNESS_HOME 必須是絕對路徑，目前為「relative-home」", output)
        self.assertFalse((self.engine / ".local").exists())
        self.assertFalse((self.engine / "relative-home").exists())
        self.assertFalse((self.local / "zMAHarness").exists())
        self.assertFalse((self.root / "profile" / ".agents").exists())


if __name__ == "__main__":
    unittest.main()
