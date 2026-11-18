"""Re-collect the author's real terminal agy Runs with the current code, on a copy (refactor spec §1.6).

Author-only and read-only; not part of CI:

    python -B tests/v2/tools/recollect_real_agy.py [--config <harness.local.toml>]

The Ledger is copied with the SQLite backup API and each agy-print Run directory is copied into a temporary
directory. collect_saved then runs on every terminal agy-print Run of the copy; no Worker is started (Popen
is replaced by a stub that raises) and nothing is written back to the real state. Pass criterion: zero
exceptions and zero differences in the Run files and in that Run's Ledger rows. The report is JSON on stdout;
the exit code is 0 only when every checked Run passes.
"""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mah.adapters.agy_print import AgyPrintAdapter  # noqa: E402
from mah.config import load_config  # noqa: E402
from mah.ledger import TERMINAL_STATES, GrantLedger  # noqa: E402
from mah.run_manager import RunManager  # noqa: E402


def _digests(directory):
    return {item.name: sha256(item.read_bytes()).hexdigest() for item in sorted(directory.iterdir()) if item.is_file()}


def _rows(database, run_id):
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        run = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        events = db.execute("SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return {"run": dict(run), "events": [dict(row) for row in events]}


def _copy_database(source, target):
    with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as reader, closing(
            sqlite3.connect(target)) as writer:
        reader.backup(writer)


def recollect(config_path=None):
    config = load_config(config_path)
    source_state = config.database.parent
    report = {"checked": 0, "passed": 0, "skipped": [], "failures": []}
    with TemporaryDirectory(prefix="mah-recollect-") as scratch:
        state = Path(scratch) / "state"
        (state / "runs").mkdir(parents=True)
        database = state / "ledger.sqlite3"
        _copy_database(config.database, database)
        with closing(sqlite3.connect(database)) as db:
            runs = db.execute("SELECT run_id, state FROM runs WHERE adapter = 'agy-print' ORDER BY created_at, run_id"
                              ).fetchall()
        manager = RunManager(GrantLedger(database), max_summary_chars=config.values["limits"]["max_result_summary_chars"])
        adapter = AgyPrintAdapter(manager)
        with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("recollection never launches")):
            for run_id, run_state in runs:
                original = source_state / "runs" / run_id
                if run_state not in TERMINAL_STATES:
                    report["skipped"].append({"run_id": run_id, "reason": f"not terminal ({run_state})"})
                    continue
                if not (original / "agy.process.json").is_file():
                    report["skipped"].append({"run_id": run_id, "reason": "no sealed process receipt"})
                    continue
                copy = state / "runs" / run_id
                shutil.copytree(original, copy, symlinks=True)
                before_files, before_rows = _digests(copy), _rows(database, run_id)
                report["checked"] += 1
                try:
                    adapter.collect_saved(run_id)
                except Exception as exc:  # Every failure is reported, none is fatal to the scan.
                    report["failures"].append({"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})
                    continue
                changes = {}
                after_files = _digests(copy)
                if after_files != before_files:
                    changes["files"] = sorted(name for name in before_files.keys() | after_files.keys()
                                              if before_files.get(name) != after_files.get(name))
                if _rows(database, run_id) != before_rows:
                    changes["ledger"] = True
                if changes:
                    report["failures"].append({"run_id": run_id, "differences": changes})
                else:
                    report["passed"] += 1
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="harness.local.toml; the default lookup is used when omitted")
    report = recollect(parser.parse_args(argv).config)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    sys.exit(main())
