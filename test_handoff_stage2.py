"""執行方式：python -m unittest -v"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
import time
import unittest
from unittest.mock import patch

from handoff_sim import LocalCoordinator


EXPECTED = (('Owner', 'Lin.'), ('Deadline', '2026-11-13.'), ('Next step', 'Send the revised agenda.'))
REQUEST = '以 Owner、Deadline、Next step 順序輸出匿名會議紀錄。'


def publish(folder, name, content):
    temp = folder / (name + '.tmp')
    temp.write_text(content, encoding='utf-8')
    temp.replace(folder / name)


def rendered(task_id, fields=EXPECTED):
    return f'# {task_id}\n\n' + ''.join(f'- {label}: {value}\n' for label, value in fields)


class MockAntigravity:
    """本機替身，只產生合成檔與權限訊號。"""

    def __init__(self, action=None, *, permission=False):
        self.action = action
        self.permission = permission
        self.submit_count = 0
        self.approval_count = 0
        self.lock = Lock()

    def needs_permission(self):
        return self.permission

    def approve(self):
        self.approval_count += 1
        raise AssertionError('測試不應核准任何權限')

    def submit(self, task_id, job):
        with self.lock:
            self.submit_count += 1
        if self.action:
            self.action(task_id, job)


def success(task_id, job):
    publish(job, 'result.md', rendered(task_id))
    publish(job, 'DONE', task_id + '\n')


class StageTwoTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sol = LocalCoordinator(self.root)

    def run_task(self, worker, task_id='TEST-01', request=REQUEST, **kwargs):
        return self.sol.run(task_id, request, EXPECTED, worker, **kwargs)

    def test_phase_one_format_positive_control(self):
        worker = MockAntigravity(success)
        self.assertEqual(self.run_task(worker).status, 'SUCCEEDED')
        self.assertEqual(worker.submit_count, 1)

    def test_timeout_and_late_done_cannot_change_receipt(self):
        worker = MockAntigravity()
        started = time.monotonic()
        result = self.run_task(worker, timeout=0.12)
        elapsed = time.monotonic() - started
        self.assertEqual(result.status, 'TIMEOUT')
        self.assertGreaterEqual(elapsed, 0.12)
        self.assertLess(elapsed, 2)
        success('TEST-01', self.root / 'TEST-01')
        self.sol = LocalCoordinator(self.root)
        duplicate = self.run_task(worker)
        self.assertEqual((duplicate.status, duplicate.detail), ('DUPLICATE', 'TIMEOUT'))
        self.assertEqual(worker.submit_count, 1)

    def test_unbounded_timeout_configuration_cannot_dispatch(self):
        worker = MockAntigravity()
        for timeout in (float('inf'), float('nan'), 0):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.run_task(worker, timeout=timeout)
        self.assertEqual(worker.submit_count, 0)

    def test_duplicate_after_success_and_payload_conflict(self):
        worker = MockAntigravity(success)
        self.assertEqual(self.run_task(worker).status, 'SUCCEEDED')
        job = self.root / 'TEST-01'
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in job.iterdir()}
        self.sol = LocalCoordinator(self.root)
        duplicate = self.run_task(worker)
        self.assertEqual((duplicate.status, duplicate.detail), ('DUPLICATE', 'SUCCEEDED'))
        self.assertEqual(self.run_task(worker, request='不同任務內容').status, 'TASK_ID_CONFLICT')
        self.assertEqual(worker.submit_count, 1)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in job.iterdir()})

    def test_concurrent_duplicate_calls_submit_once(self):
        worker = MockAntigravity(success)
        gate = Barrier(2)
        other = LocalCoordinator(self.root)

        def execute(coordinator):
            gate.wait(timeout=2)
            return coordinator.run('TEST-01', REQUEST, EXPECTED, worker, timeout=1).status

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(execute, sol) for sol in (self.sol, other)]
            self.assertCountEqual([f.result(timeout=3) for f in futures], ['SUCCEEDED', 'DUPLICATE'])
        self.assertEqual(worker.submit_count, 1)

    def test_invalid_results_with_done_are_rejected(self):
        cases = (
            ('MISSING', None, None, '找不到 result.md'),
            ('INCOMPLETE', rendered('INCOMPLETE', EXPECTED[:2]), None, '欄位缺漏'),
            ('WRONG-RESULT-ID', rendered('OTHER'), None, 'result.md 識別碼'),
            ('WRONG-DONE-ID', rendered('WRONG-DONE-ID'), 'OTHER', 'DONE 識別碼'),
            ('EMPTY-VALUE', rendered('EMPTY-VALUE', (('Owner', ''), *EXPECTED[1:])), None, '欄位缺漏'),
            ('WRONG-FACT', rendered('WRONG-FACT', (('Owner', '另一人'), *EXPECTED[1:])), None, '值不符'),
        )
        for task_id, result, done, reason in cases:
            with self.subTest(task_id=task_id):
                def invalid(run_id, job):
                    if result is not None:
                        publish(job, 'result.md', result.replace(f'# {task_id}\n', f'# {run_id}\n', 1))
                    publish(job, 'DONE', (done or run_id) + '\n')
                outcome = self.run_task(MockAntigravity(invalid), task_id)
                self.assertEqual(outcome.status, 'INVALID_RESULT')
                self.assertIn(reason, outcome.detail)

    def test_permission_prompt_before_submit_never_approved(self):
        worker = MockAntigravity(permission=True)
        self.assertEqual(self.run_task(worker).status, 'PERMISSION_REQUIRED')
        self.assertEqual((worker.submit_count, worker.approval_count), (0, 0))
        self.assertFalse((self.root / 'TEST-01').exists())

    def test_permission_prompt_after_submit_takes_priority_over_done(self):
        worker = MockAntigravity()

        def prompt(task_id, job):
            success(task_id, job)
            worker.permission = True

        worker.action = prompt
        self.assertEqual(self.run_task(worker).status, 'PERMISSION_REQUIRED')
        self.assertEqual((worker.submit_count, worker.approval_count), (1, 0))

    def test_permission_prompt_during_result_read_blocks_success(self):
        original_read = Path.read_text
        worker = MockAntigravity(success)

        def read(path, *args, **kwargs):
            result = original_read(path, *args, **kwargs)
            if path.name == 'result.md':
                worker.permission = True
            return result

        with patch.object(Path, 'read_text', read):
            self.assertEqual(self.run_task(worker).status, 'PERMISSION_REQUIRED')
        self.assertEqual(worker.approval_count, 0)

    def test_simulated_file_permission_error_is_reported(self):
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if path.name == 'result.md':
                raise PermissionError('合成讀取拒絕')
            return original_read(path, *args, **kwargs)

        worker = MockAntigravity(success)
        with patch.object(Path, 'read_text', read):
            result = self.run_task(worker)
        self.assertEqual(result.status, 'PERMISSION_REQUIRED')
        self.assertEqual(worker.approval_count, 0)

    def test_interrupted_claim_is_not_resent_automatically(self):
        from hashlib import sha256

        task_hash = sha256(repr((REQUEST, EXPECTED)).encode('utf-8')).hexdigest()
        self.assertIsNone(self.sol.claim('TEST-01', task_hash))
        self.sol = LocalCoordinator(self.root)
        worker = MockAntigravity(success)
        result = self.run_task(worker)
        self.assertEqual((result.status, result.detail), ('DUPLICATE', 'CLAIMED'))
        self.assertEqual(worker.submit_count, 0)
        self.assertFalse((self.root / 'TEST-01' / 'DONE').exists())


if __name__ == '__main__':
    unittest.main()
