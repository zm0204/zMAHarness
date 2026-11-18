"""Golden lock for the agy Run directory and Ledger events (refactor spec §1.6).

The fixtures under fixtures/agy-golden were produced once by tools/make_agy_golden.py from the agy code
before it moved onto the shared CLI Worker base. Two checks per scenario:
- recollection: the committed Run directory and Ledger are restored and collect_saved runs again; it must
  not raise, change any file, or add or change any Ledger row;
- regeneration: the scenario is produced again from its original inputs and must equal the fixture.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).with_name("tools")))

import make_agy_golden as golden  # noqa: E402
from mah.adapters.agy_print import AgyPrintAdapter, capture_matches  # noqa: E402
from mah.ledger import GrantLedger  # noqa: E402
from mah.run_manager import RunManager  # noqa: E402

PROCESS_KEYS = ["exit_code", "invocation_sha256", "launch_error", "pre_submit_reason", "process", "run_id",
                "started_at", "stderr", "stdout", "task_sha256", "termination", "tool_events", "wall_milliseconds",
                "workspace_after_sha256", "workspace_changed"]
CAPTURE_KEYS = ["adapter", "classification", "cli_version", "effort_requested", "events_sha256", "exit_code",
                "invocation_sha256", "model_requested", "observation", "process_receipt_sha256", "reason",
                "result_sha256", "run_id", "stderr_sha256", "stdout_sha256", "task_sha256", "termination",
                "wall_milliseconds", "workspace_after_sha256", "workspace_changed"]
INVOCATION_KEYS = ["argv", "cli_version", "cwd", "prompt_sha256", "run_id", "stdin_file", "task_sha256", "timeout_s",
                   "verification_requirements", "workspace_baseline_sha256", "workspace_kind"]
BUNDLE_INVOCATION_KEYS = sorted(INVOCATION_KEYS + ["bundle_manifest_sha256", "launch_profile",
                                                   "permissions_observed", "sandbox_observed", "tools_observed"])
EXPECTED = {
    "success": ("COLLECTING", "structured_result_collected", 1),
    "output_conflict_replay": ("COLLECTING", "agy_output_recollected", 1),
    "process_not_started": ("FAILED_PRE_SUBMIT", "process_not_started", 0),
    "logged_timeout": ("TIMED_OUT", "process_timed_out", 1),
    "interrupted_unsubmitted": ("SUBMISSION_UNKNOWN", "submission_evidence_missing", 1),
    "bundle_evidence_missing": ("COLLECTING", "bundle_workspace_evidence_missing", 1),
}


def fixture(name):
    target = golden.GOLDEN / name
    return golden.run_files(target / "run"), json.loads((target / "ledger.json").read_bytes())


class AgyGoldenTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="agy-golden-test-")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        patcher = patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("golden tests never launch"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def restore(self, name):
        files, ledger = fixture(name)
        state = self.scratch / name / "state"
        directory = state / "runs" / golden.SCENARIOS[name]["run_id"]
        directory.mkdir(parents=True)
        for item, content in files.items():
            (directory / item).write_bytes(content)
        golden.restore_ledger(state / "ledger.sqlite3", ledger)
        manager = RunManager(GrantLedger(state / "ledger.sqlite3", clock=lambda: golden.CLOCK),
                             process_probe=golden._probe)
        return manager, directory

    def test_scenarios_cover_the_spec_list(self):
        self.assertEqual(sorted(golden.SCENARIOS), sorted(EXPECTED))
        self.assertEqual(sorted(path.name for path in golden.GOLDEN.iterdir()), sorted(EXPECTED))
        for name, (state, reason, consumed) in EXPECTED.items():
            with self.subTest(name):
                run, = fixture(name)[1]["runs"]
                self.assertEqual((run["state"], run["state_reason"], run["dispatch_consumed"]),
                                 (state, reason, consumed))

    def test_recollection_keeps_every_byte_and_ledger_row(self):
        for name, spec in golden.SCENARIOS.items():
            with self.subTest(name):
                files, ledger = fixture(name)
                manager, directory = self.restore(name)
                adapter = AgyPrintAdapter(manager)
                view = adapter.collect_saved(spec["run_id"])
                self.assertEqual((view["state"], view["state_reason"]), EXPECTED[name][:2])
                if spec.get("replay"):
                    adapter.collect_saved(spec["run_id"], replay_output=True)
                self.assertEqual(golden.run_files(directory), files)
                self.assertEqual(golden.dump_ledger(manager.database), ledger)

    @unittest.skipUnless(os.name == "nt", "The synthetic paths are Windows paths")
    def test_regeneration_matches_the_fixture(self):
        for name in golden.SCENARIOS:
            with self.subTest(name):
                files, ledger = fixture(name)
                state = self.scratch / name / "regenerated"
                state.mkdir(parents=True)
                run_id = golden.generate(name, state, golden.fixture_inputs(name))
                self.assertEqual(golden.run_files(state / "runs" / run_id), files)
                self.assertEqual(golden.dump_ledger(state / "ledger.sqlite3"), ledger)

    def test_receipt_key_sets(self):
        for name, spec in golden.SCENARIOS.items():
            with self.subTest(name):
                files, _ = fixture(name)
                logged = ["log_submission"] if name == "logged_timeout" else []
                self.assertEqual(sorted(json.loads(files["agy.process.json"])), sorted(PROCESS_KEYS + logged))
                self.assertEqual(sorted(json.loads(files["agy.capture.json"])), sorted(CAPTURE_KEYS + logged))
                self.assertEqual(sorted(json.loads(files["invocation.json"])),
                                 BUNDLE_INVOCATION_KEYS if spec.get("bundle") else INVOCATION_KEYS)

    def test_verifier_reads_the_collected_results_from_the_same_capture(self):
        for name in ("success", "output_conflict_replay"):
            with self.subTest(name):
                manager, directory = self.restore(name)
                run = manager.ledger.get_run(golden.SCENARIOS[name]["run_id"])
                digest = sha256((directory / "result.json").read_bytes()).hexdigest()
                self.assertEqual(run["result_sha256"], digest)
                self.assertTrue(capture_matches(directory, run, digest))
                (directory / "events.jsonl").write_bytes(b"")
                self.assertFalse(capture_matches(directory, run, digest))


if __name__ == "__main__":
    unittest.main()
