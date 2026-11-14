import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(os.name == "nt", "setup.ps1 requires Windows")
class SetupPreflightTests(unittest.TestCase):
    def run_without_python(self, *, store_alias=False, script="setup.ps1"):
        with tempfile.TemporaryDirectory(prefix="mah-setup-", dir=ROOT.parent) as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            (checkout / "config").mkdir(parents=True)
            for name in ("setup.ps1", "doctor.ps1", "harness-config.ps1", "config/local.example.json"):
                shutil.copy2(ROOT / name, checkout / name)

            windows = Path(os.environ["SystemRoot"])
            search = [windows / "System32", windows, windows / "System32/WindowsPowerShell/v1.0"]
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
            command = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); & '" + str(checkout / script).replace("'", "''") + "'"
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-Command", command],
                cwd=checkout, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((checkout / ".local").exists(), result.stdout + result.stderr)
            self.assertFalse((root / "appdata").exists())
            self.assertFalse((root / "profile" / ".agents").exists())
            self.assertFalse((root / "profile" / ".claude").exists())
            self.assertIn("winget install -e --id Python.Python.3.13", result.stdout)
            return result.stdout

    def test_missing_python_stops_before_creating_local_config(self):
        self.run_without_python()

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
