import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "nt", "setup.ps1 requires Windows")
class SetupPreflightTests(unittest.TestCase):
    def run_without_python(self, *, store_alias=False, script="setup.ps1"):
        with tempfile.TemporaryDirectory(prefix="mah-setup-中文-") as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            (checkout / "config").mkdir(parents=True)
            for name in ("setup.ps1", "doctor.ps1", "harness-config.ps1", "config/local.example.json"):
                shutil.copy2(ROOT / name, checkout / name)

            windows = Path(os.environ["SystemRoot"])
            # C:\Windows can contain py.exe on CI; exclude it to model no usable Python.
            search = [windows / "System32", windows / "System32/WindowsPowerShell/v1.0"]
            if store_alias:
                alias_dir = root / "Microsoft" / "WindowsApps"
                alias_dir.mkdir(parents=True)
                (alias_dir / "python.exe").write_bytes(b"not an executable")
                search.insert(0, alias_dir)

            environment = os.environ.copy()
            environment["PATH"] = os.pathsep.join(map(str, search))
            environment["LOCALAPPDATA"] = str(root / "appdata")
            environment["USERPROFILE"] = str(root / "profile")
            powershell = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
            output_file = root / "output.txt"
            source = str(checkout / script).replace("'", "''")
            output = str(output_file).replace("'", "''")
            invoke = "& '" + source + "' *>&1 | Out-File -LiteralPath '" + output + "' -Encoding UTF8"
            console_file = root / "console.txt"
            with console_file.open("wb") as console:
                result = subprocess.run(
                    [str(powershell), "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-Command", invoke],
                    cwd=checkout, env=environment, stdout=console, stderr=subprocess.STDOUT, timeout=30,
                )
            message = (
                output_file.read_text(encoding="utf-8-sig")
                if output_file.exists()
                else console_file.read_text(encoding="utf-8", errors="replace")
            )
            self.assertNotEqual(result.returncode, 0, message)
            self.assertFalse((checkout / ".local").exists(), message)
            self.assertFalse((root / "appdata" / "zMAHarness").exists())
            self.assertFalse((root / "profile" / ".agents").exists())
            self.assertFalse((root / "profile" / ".claude").exists())
            self.assertFalse((root / "profile" / ".gemini").exists())
            self.assertIn("winget install -e --id Python.Python.3.13", message)
            return message

    def test_missing_python_stops_before_creating_local_config(self):
        self.run_without_python()

    def test_setup_installs_skill_and_reference_in_all_three_directories(self):
        with tempfile.TemporaryDirectory(prefix="mah-setup-skills-") as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            for name in ("setup.ps1", "harness-config.ps1", "harness.local.example.toml"):
                shutil.copy2(ROOT / name, checkout / name)
            for name in ("config", "mah", ".agents/skills/zmaharness"):
                shutil.copytree(ROOT / name, checkout / name, ignore=shutil.ignore_patterns("__pycache__"))

            profile = root / "profile"
            environment = os.environ | {
                "USERPROFILE": str(profile), "LOCALAPPDATA": str(root / "local"),
                "APPDATA": str(root / "roaming"), "PYTHONUSERBASE": str(root / "user-site"),
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
            }
            for name in ("ZMAHARNESS_HOME", "PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE"):
                environment.pop(name, None)
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", "setup.ps1"],
                cwd=checkout, env=environment, capture_output=True, timeout=45,
            )
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
            source = ROOT / ".agents/skills/zmaharness"
            for skill_parent in (".agents/skills", ".claude/skills", ".gemini/config/skills"):
                for filename in ("SKILL.md", "references/dispatch.md"):
                    with self.subTest(skill_parent=skill_parent, filename=filename):
                        installed = profile / skill_parent / "zmaharness" / filename
                        self.assertTrue(installed.is_file(), installed)
                        self.assertEqual(installed.read_bytes(), (source / filename).read_bytes())

    def test_windowsapps_alias_explains_it_is_not_python(self):
        output = self.run_without_python(store_alias=True)
        self.assertIn("Microsoft Store", output)

    def test_doctor_points_dependent_failures_to_python(self):
        output = self.run_without_python(script="doctor.ps1")
        self.assertIn("winget install -e --id Python.Python.3.13", output)
        self.assertEqual(output.count("請先解決上方的 Python 問題。"), 4)

    def test_doctor_identifies_windowsapps_alias(self):
        output = self.run_without_python(store_alias=True, script="doctor.ps1")
        self.assertIn("Microsoft Store 的假捷徑", output)


if __name__ == "__main__":
    unittest.main()
