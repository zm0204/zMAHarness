"""第三階段中斷、復原與交接完整性測試。"""
from hashlib import sha256
import os
from pathlib import Path
from shutil import copytree
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from handoff_sim import LocalCoordinator
from test_handoff_stage2 import EXPECTED, REQUEST, MockAntigravity, publish, rendered, success


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / '工作目錄'
        self.root.mkdir()
        self.sol = LocalCoordinator(self.root)

    def run_task(self, worker, **kwargs):
        return self.sol.run('TEST-01', REQUEST, EXPECTED, worker, **kwargs)

    def restart(self):
        return LocalCoordinator(self.root)

    def crash_during_submit(self, with_result):
        child = """
import os, sys
from pathlib import Path
from shutil import copytree
from handoff_sim import LocalCoordinator
from test_handoff_stage2 import EXPECTED, REQUEST, MockAntigravity, success
def interrupted(run_id, job):
    if sys.argv[2] == 'yes':
        success(run_id, job)
    os._exit(73)
LocalCoordinator(Path(sys.argv[1])).run(
    'TEST-01', REQUEST, EXPECTED, MockAntigravity(interrupted), timeout=10)
"""
        process = subprocess.run([sys.executable, '-B', '-c', child, str(self.root),
                                  'yes' if with_result else 'no'],
                                 cwd=Path(__file__).resolve().parent, timeout=10)
        self.assertEqual(process.returncode, 73)

    def test_process_dies_after_handoff_and_recovery_accepts_existing_result(self):
        self.crash_during_submit(with_result=True)
        result = self.restart().recover('TEST-01')
        self.assertEqual(result.status, 'SUCCEEDED')
        worker = MockAntigravity(success)
        self.assertEqual(self.run_task(worker).status, 'DUPLICATE')
        self.assertEqual(worker.submit_count, 0)

    def test_process_dies_during_submit_without_done_waits_for_human(self):
        self.crash_during_submit(with_result=False)
        self.assertEqual(self.restart().recover('TEST-01').status, 'SUBMISSION_UNKNOWN')
        worker = MockAntigravity(success)
        self.assertEqual(self.run_task(worker).detail, 'SUBMISSION_UNKNOWN')
        self.assertEqual(worker.submit_count, 0)

    def test_confirmed_waiting_resumes_tracking_on_original_deadline(self):
        worker = MockAntigravity()
        with patch.object(self.sol, 'await_result', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_task(worker, timeout=0.18)
        time.sleep(0.2)
        self.assertEqual(self.restart().recover('TEST-01').status, 'TIMEOUT')
        self.assertEqual(worker.submit_count, 1)

    def test_known_unsent_can_be_explicitly_resumed(self):
        fingerprint = sha256(repr((REQUEST, EXPECTED)).encode('utf-8')).hexdigest()
        self.assertIsNone(self.sol.claim('TEST-01', fingerprint))
        self.sol = self.restart()
        self.assertEqual(self.sol.recover('TEST-01').status, 'CLAIMED')
        worker = MockAntigravity(success)
        self.assertEqual(self.run_task(worker, resume_prepared=True).status, 'SUCCEEDED')
        self.assertEqual(worker.submit_count, 1)

    def test_stale_done_and_result_with_logical_id_do_not_match_attempt(self):
        def stale(_, job):
            success('TEST-01', job)
        self.assertEqual(self.run_task(MockAntigravity(stale)).status, 'INVALID_RESULT')

    def test_unknown_receipt_is_reconciled_when_result_appears(self):
        self.crash_during_submit(with_result=False)
        coordinator = self.restart()
        self.assertEqual(coordinator.recover('TEST-01').status, 'SUBMISSION_UNKNOWN')
        run_id = coordinator.receipt('TEST-01')['run_id']
        success(run_id, self.root / 'TEST-01')
        self.assertEqual(coordinator.recover('TEST-01').status, 'SUCCEEDED')

    def test_on_time_files_can_be_verified_after_deadline_on_restart(self):
        with patch.object(self.sol, 'await_result', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_task(MockAntigravity(success), timeout=10)
        deadline = self.sol.receipt('TEST-01')['deadline']
        with patch('handoff_sim.time.time', return_value=deadline + 5):
            self.assertEqual(self.restart().recover('TEST-01').status, 'SUCCEEDED')

    def test_late_done_with_current_run_id_is_rejected_on_restart(self):
        self.crash_during_submit(with_result=True)
        deadline = self.sol.receipt('TEST-01')['deadline']
        done = self.root / 'TEST-01' / 'DONE'
        late = int((deadline + 1) * 1_000_000_000)
        os.utime(done, ns=(late, late))
        self.assertEqual(self.restart().recover('TEST-01').status, 'TIMEOUT')

    def test_current_done_without_result_is_invalid_on_restart(self):
        self.crash_during_submit(with_result=False)
        run_id = self.sol.receipt('TEST-01')['run_id']
        publish(self.root / 'TEST-01', 'DONE', run_id + '\n')
        self.assertEqual(self.restart().recover('TEST-01').status, 'INVALID_RESULT')

    def test_interrupted_task_file_publication_never_submits_partial_request(self):
        worker = MockAntigravity(success)
        with patch.object(Path, 'replace', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_task(worker)
        job = self.root / 'TEST-01'
        self.assertFalse((job / 'task.md').exists())
        self.assertTrue((job / 'task.md.tmp').exists())
        self.assertEqual(worker.submit_count, 0)
        self.assertEqual(self.restart().recover('TEST-01').status, 'SUBMISSION_UNKNOWN')

    def test_working_directory_can_be_relocated_after_process_exit(self):
        self.crash_during_submit(with_result=True)
        relocated = copytree(self.root, self.root.parent / '搬移後')
        self.assertEqual(LocalCoordinator(relocated).recover('TEST-01').status, 'SUCCEEDED')
        self.assertEqual(self.sol.receipt('TEST-01')['status'], 'DISPATCHING')

    def test_windows_case_variant_does_not_reuse_same_directory(self):
        worker = MockAntigravity(success)
        self.assertEqual(self.run_task(worker).status, 'SUCCEEDED')
        duplicate = self.sol.run('test-01', REQUEST, EXPECTED, worker)
        self.assertEqual(duplicate.status, 'DUPLICATE')
        self.assertEqual(worker.submit_count, 1)

    def test_legacy_active_receipt_cannot_be_replayed(self):
        legacy = self.root / '舊版'
        legacy.mkdir()
        db = sqlite3.connect(legacy / 'receipts.sqlite3')
        db.execute('CREATE TABLE receipts (task_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, '
                   'status TEXT NOT NULL, detail TEXT NOT NULL)')
        db.executemany('INSERT INTO receipts VALUES (?, ?, ?, ?)',
                       (('LEGACY', 'old', 'CLAIMED', ''), ('FINISHED', 'old', 'SUCCEEDED', '')))
        db.commit()
        db.close()
        upgraded = LocalCoordinator(legacy)
        self.assertEqual(upgraded.recover('LEGACY').status, 'SUBMISSION_UNKNOWN')
        self.assertEqual(upgraded.recover('FINISHED').status, 'SUCCEEDED')



if __name__ == '__main__':
    unittest.main()
