"""The Ledger columns runs.cli_version, model_observed and effort_observed (refactor spec 2a). No real model."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).with_name("tools")))

import make_agy_golden as golden  # noqa: E402
from mah.adapters.agy_print import AgyPrintAdapter  # noqa: E402
from mah.adapters.claude_print import ClaudePrintAdapter  # noqa: E402
from mah.adapters.codex_exec import CodexExecAdapter  # noqa: E402
from mah.ledger import GrantLedger  # noqa: E402
from mah.run_manager import RunManager, _ledger_text  # noqa: E402

from test_claude_print import FIXTURE as CLAUDE_FIXTURE, FIXTURE_RUN_ID as CLAUDE_RUN_ID  # noqa: E402
from test_cli_worker import WorkerFixture  # noqa: E402
from test_codex_exec import CODEX_STAND_IN, CodexPreflightFixture  # noqa: E402

COLUMNS = ("cli_version", "model_observed", "effort_observed")


def columns(manager, run_id):
    run = manager.ledger.get_run(run_id)
    return {key: run[key] for key in COLUMNS}


def clear_columns(database, run_id):
    """Simulate a Run recorded before these columns were written."""
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE runs SET cli_version = NULL, model_observed = NULL, effort_observed = NULL "
                   "WHERE run_id = ?", (run_id,))


class LedgerTextTests(unittest.TestCase):
    def test_unknown_empty_and_malformed_values_are_null(self):
        for value in ("unknown", "", "  ", None, 3, ["x"], "a\nb", "a\x00", "x" * 201):
            with self.subTest(value=value):
                self.assertIsNone(_ledger_text(value))
        for value in ("claude-sonnet-5-5", "1.5.11", "codex-cli 0.169.0", "2.1.325 (Claude Code)", "x" * 200):
            with self.subTest(value=value):
                self.assertEqual(_ledger_text(value), value)


class ClaudeObservedTests(WorkerFixture, unittest.TestCase):
    adapter_name = "claude-print"
    model = "sonnet"

    def make_adapter(self):
        self.executable = self.root / "bin" / "claude.exe"
        self.executable.write_bytes(b"stand-in path; never executed")
        return ClaudePrintAdapter(self.manager, command=str(self.executable))

    def replay(self, argv, **kwargs):
        code = "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())"
        return self.real_popen([sys.executable, "-I", "-B", "-c", code, str(CLAUDE_FIXTURE)], **kwargs)

    def test_fixture_writes_the_observed_model_and_the_invocation_cli_version(self):
        view = self.dispatch(bundle=True, popen=self.replay, run_id=CLAUDE_RUN_ID)
        self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "structured_result_collected"))
        invocation = json.loads((self.directory() / "invocation.json").read_bytes())
        self.assertEqual(columns(self.manager, self.run_id), {
            "cli_version": invocation["cli_version"], "model_observed": "claude-sonnet-5-5", "effort_observed": None})
        self.assertEqual(view["model_observed"], "claude-sonnet-5-5")
        self.assertEqual(view["cli_version"], "stand-in 1.0")
        receipt = json.loads((self.directory() / "claude.capture.json").read_bytes())
        self.assertEqual(receipt["cli_version"], view["cli_version"])

    def test_terminal_recollection_does_not_backfill(self):
        self.dispatch(bundle=True, popen=self.replay, run_id=CLAUDE_RUN_ID)
        self.assertEqual(self.verify()["state"], "SUCCEEDED")
        clear_columns(self.manager.database, self.run_id)
        with patch("mah.adapters.cli_worker.Popen", side_effect=AssertionError("recollection never launches")):
            view = self.adapter.collect_saved(self.run_id)
        self.assertEqual(view["state"], "SUCCEEDED")
        self.assertEqual(columns(self.manager, self.run_id), dict.fromkeys(COLUMNS))

    def test_second_collection_keeps_the_first_value(self):
        self.dispatch(bundle=True, popen=self.replay, run_id=CLAUDE_RUN_ID)
        before = columns(self.manager, self.run_id)
        with patch("mah.adapters.cli_worker.Popen", side_effect=AssertionError("recollection never launches")):
            self.adapter.collect_saved(self.run_id)
        self.assertEqual(columns(self.manager, self.run_id), before)


class CodexObservedTests(CodexPreflightFixture, WorkerFixture, unittest.TestCase):
    adapter_name = "codex-exec"
    model = "gpt-5.6-luna"

    def make_adapter(self):
        self.executable = self.root / "bin" / "codex.exe"
        self.executable.write_bytes(b"stand-in path; never executed")
        (self.root / "codex_stand_in.py").write_text(CODEX_STAND_IN, encoding="utf-8")
        return CodexExecAdapter(self.manager, command=str(self.executable))

    def substitute(self, argv, **kwargs):
        return self.real_popen([sys.executable, "-I", "-B", str(self.root / "codex_stand_in.py"), *argv[1:]], **kwargs)

    def test_unknown_model_stays_null_and_cli_version_is_written(self):
        view = self.dispatch(bundle=True)
        self.assertEqual(view["codex_capture"]["observation"]["model_observed"], "unknown")
        self.assertEqual(columns(self.manager, self.run_id),
                         {"cli_version": "codex-cli 0.169.0", "model_observed": None, "effort_observed": None})


class AgyObservedTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="agy-observed-")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        patcher = patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("agy tests never launch"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def restore(self, name, *, old_ledger):
        files, ledger = golden.run_files(golden.GOLDEN / name / "run"), json.loads(
            (golden.GOLDEN / name / "ledger.json").read_bytes())
        if old_ledger:
            for row in ledger["runs"]:
                row.update(dict.fromkeys(COLUMNS))
        state = self.scratch / name / "state"
        directory = state / "runs" / golden.SCENARIOS[name]["run_id"]
        directory.mkdir(parents=True)
        for item, content in files.items():
            (directory / item).write_bytes(content)
        golden.restore_ledger(state / "ledger.sqlite3", ledger)
        manager = RunManager(GrantLedger(state / "ledger.sqlite3", clock=lambda: golden.CLOCK),
                             process_probe=golden._probe)
        return manager, directory, ledger

    @unittest.skipUnless(os.name == "nt", "The synthetic paths are Windows paths")
    def test_dispatch_writes_the_receipt_cli_version_to_the_ledger(self):
        for name in golden.SCENARIOS:
            with self.subTest(name):
                state = self.scratch / name / "fresh"
                state.mkdir(parents=True)
                run_id = golden.generate(name, state, golden.fixture_inputs(name))
                manager = RunManager(GrantLedger(state / "ledger.sqlite3", clock=lambda: golden.CLOCK),
                                     process_probe=golden._probe)
                receipt = json.loads((state / "runs" / run_id / "agy.capture.json").read_bytes())
                self.assertEqual(receipt["cli_version"], golden.CLI_VERSION)
                # agy always reports "unknown", so the observed model and effort stay NULL.
                self.assertEqual(columns(manager, run_id), {"cli_version": receipt["cli_version"],
                                                            "model_observed": None, "effort_observed": None})

    def test_old_ledger_runs_are_recollected_without_backfill(self):
        for name, spec in golden.SCENARIOS.items():
            with self.subTest(name):
                manager, directory, ledger = self.restore(name, old_ledger=True)
                files = golden.run_files(directory)
                AgyPrintAdapter(manager).collect_saved(spec["run_id"])
                if spec.get("replay"):
                    AgyPrintAdapter(manager).collect_saved(spec["run_id"], replay_output=True)
                self.assertEqual(columns(manager, spec["run_id"]), dict.fromkeys(COLUMNS))
                self.assertEqual(golden.run_files(directory), files)
                self.assertEqual(golden.dump_ledger(manager.database), ledger)


if __name__ == "__main__":
    unittest.main()
