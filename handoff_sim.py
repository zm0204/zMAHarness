"""本機檔案交接與中斷復原模擬。"""
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from os import fsync
from pathlib import Path
import json
import re
import sqlite3
import time
from uuid import uuid4


@dataclass(frozen=True)
class Outcome:
    status: str
    detail: str = ''


def validate_handoff(task_id, done, result, expected):
    if done not in (task_id, task_id + '\n'):
        return Outcome('INVALID_RESULT', 'DONE 識別碼不一致或格式錯誤')
    lines = result.splitlines()
    if not lines or lines[0] != f'# {task_id}':
        return Outcome('INVALID_RESULT', 'result.md 識別碼不一致')
    expected_lines = [f'- {label}: {value}' for label, value in expected]
    if lines[1:] != ['', *expected_lines]:
        return Outcome('INVALID_RESULT', 'result.md 欄位缺漏、次序錯誤或值不符')
    return Outcome('SUCCEEDED')


class LocalCoordinator:
    """SQLite 收據先於模擬送出落盤。復原只驗收，沒有送出路徑。"""

    ACTIVE = ('DISPATCHING', 'WAITING', 'SUBMISSION_UNKNOWN')

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.database = self.directory / 'receipts.sqlite3'
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS receipts ('
                       'task_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, '
                       'status TEXT NOT NULL, detail TEXT NOT NULL)')
            columns = {column['name'] for column in db.execute('PRAGMA table_info(receipts)')}
            for name, kind in (('run_id', 'TEXT'), ('deadline', 'REAL'), ('expected', 'TEXT')):
                if name not in columns:
                    db.execute(f'ALTER TABLE receipts ADD COLUMN {name} {kind}')

    def connect(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def find(db, task_id):
        return db.execute('SELECT * FROM receipts WHERE task_id = ? COLLATE NOCASE', (task_id,)).fetchone()

    def receipt(self, task_id):
        with closing(self.connect()) as db:
            return self.find(db, task_id)

    def claim(self, task_id, fingerprint):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = self.find(db, task_id)
            if previous is None:
                run_id = f'{task_id}-{uuid4().hex}'
                db.execute('INSERT INTO receipts (task_id, fingerprint, status, detail, run_id) '
                           'VALUES (?, ?, ?, ?, ?)', (task_id, fingerprint, 'CLAIMED', '', run_id))
                return None
            status = 'DUPLICATE' if previous['fingerprint'] == fingerprint else 'TASK_ID_CONFLICT'
            return Outcome(status, previous['status'])

    def record(self, task_id, outcome, allowed):
        """只更新允許的來源狀態，避免並行驗收覆蓋已定案的結果。"""
        with closing(self.connect()) as db, db:
            slots = ','.join('?' for _ in allowed)
            db.execute(f'UPDATE receipts SET status = ?, detail = ? '
                       f'WHERE task_id = ? AND status IN ({slots})',
                       (outcome.status, outcome.detail, task_id, *allowed))
            row = self.find(db, task_id)
            return Outcome(row['status'], row['detail'])

    def run(self, task_id, request, expected, worker, *, timeout=0.15, poll=0.01, resume_prepared=False):
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', task_id)
                or re.fullmatch(r'CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]', task_id, re.IGNORECASE)):
            raise ValueError('無效的 task_id')
        if not (isfinite(timeout) and isfinite(poll) and timeout > 0 and poll > 0):
            raise ValueError('等待與檢查間隔必須為有限正數')
        fingerprint = sha256(repr((request, expected)).encode('utf-8')).hexdigest()
        previous = self.claim(task_id, fingerprint)
        if previous is not None and not (resume_prepared and previous == Outcome('DUPLICATE', 'CLAIMED')):
            return previous

        row = self.receipt(task_id)
        task_id, run_id = row['task_id'], row['run_id']
        if run_id is None:
            return Outcome('DUPLICATE', 'SUBMISSION_UNKNOWN')
        if worker.needs_permission():
            return self.record(task_id, Outcome('PERMISSION_REQUIRED', '送出前偵測到權限提示'), ('CLAIMED',))

        deadline = time.time() + timeout
        with closing(self.connect()) as db, db:
            advanced = db.execute("UPDATE receipts SET status = 'DISPATCHING', deadline = ?, expected = ? "
                                  "WHERE task_id = ? AND status = 'CLAIMED' AND run_id = ?",
                                  (deadline, json.dumps(expected, ensure_ascii=False), task_id, run_id)).rowcount
        if not advanced:
            return Outcome('DUPLICATE', self.receipt(task_id)['status'])

        job = self.directory / task_id
        try:
            job.mkdir()
            temporary = job / 'task.md.tmp'
            with temporary.open('x', encoding='utf-8') as file:
                file.write(f'Run ID: {run_id}\n\n{request}')
                file.flush()
                fsync(file.fileno())
            temporary.replace(job / 'task.md')
            if worker.needs_permission():
                outcome = Outcome('PERMISSION_REQUIRED', '送出前偵測到權限提示')
            elif time.time() >= deadline:
                outcome = Outcome('TIMEOUT', '準備交接時已超過期限')
            else:
                try:
                    worker.submit(run_id, job)
                except PermissionError:
                    raise
                except Exception as exc:
                    return self.record(task_id, Outcome('SUBMISSION_UNKNOWN', type(exc).__name__),
                                       ('DISPATCHING', 'SUBMISSION_UNKNOWN'))
                current = self.record(task_id, Outcome('WAITING'), ('DISPATCHING', 'SUBMISSION_UNKNOWN'))
                if current.status != 'WAITING':
                    return current
                outcome = self.await_result(run_id, job, expected, worker.needs_permission, deadline, poll)
        except PermissionError:
            outcome = Outcome('PERMISSION_REQUIRED', '本機交接檔案無法存取或送出需要權限')
        except OSError as exc:
            outcome = Outcome('HANDOFF_ERROR', type(exc).__name__)
        return self.record(task_id, outcome, self.ACTIVE)

    def recover(self, task_id, worker=None, *, poll=0.01):
        """讀取既有收據。worker 僅供查詢權限訊號。"""
        if not (isfinite(poll) and poll > 0):
            raise ValueError('檢查間隔必須為有限正數')
        row = self.receipt(task_id)
        if row is None:
            return Outcome('NOT_FOUND')
        task_id, status, run_id = row['task_id'], row['status'], row['run_id']
        if status not in ('CLAIMED', *self.ACTIVE):
            return Outcome(status, row['detail'])
        if run_id is None:
            return self.record(task_id, Outcome('SUBMISSION_UNKNOWN', '舊版收據缺少派工識別碼，請人工核對'),
                               ('CLAIMED', *self.ACTIVE))
        if status == 'CLAIMED':
            return Outcome('CLAIMED', '尚未進入派工，核對原始內容後可明確要求繼續')

        job = self.directory / task_id
        needs_permission = worker.needs_permission if worker else lambda: False
        expected = json.loads(row['expected'])
        deadline = row['deadline']
        try:
            if status == 'WAITING':
                outcome = self.await_result(run_id, job, expected, needs_permission, deadline, poll)
            else:
                outcome = self.read_completion(run_id, job, expected, needs_permission, deadline)
        except PermissionError:
            outcome = Outcome('PERMISSION_REQUIRED', '本機交接檔案無法存取')
        except OSError as exc:
            outcome = Outcome('HANDOFF_ERROR', type(exc).__name__)
        if outcome is None:
            return self.record(task_id, Outcome('SUBMISSION_UNKNOWN', '未見有效交接，請人工核對是否已送出'),
                               ('DISPATCHING', 'SUBMISSION_UNKNOWN'))
        return self.record(task_id, outcome, self.ACTIVE)

    @classmethod
    def await_result(cls, run_id, job, expected, needs_permission, deadline, poll):
        stop = time.monotonic() + max(0, deadline - time.time())
        while True:
            outcome = cls.read_completion(run_id, job, expected, needs_permission, deadline)
            if outcome is not None:
                return outcome
            remaining = min(stop - time.monotonic(), deadline - time.time())
            if remaining <= 0:
                return Outcome('TIMEOUT', '期限內未收到有效交接')
            time.sleep(min(poll, remaining))

    @staticmethod
    def read_completion(run_id, job, expected, needs_permission, deadline):
        if needs_permission():
            return Outcome('PERMISSION_REQUIRED', '驗收前偵測到權限提示')
        done_path, result_path = job / 'DONE', job / 'result.md'
        try:
            done_stamp = done_path.stat().st_mtime_ns
            done = done_path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return None
        except UnicodeError:
            return Outcome('INVALID_RESULT', 'DONE 非 UTF-8')
        if done not in (run_id, run_id + '\n'):
            return Outcome('INVALID_RESULT', 'DONE 識別碼不一致或格式錯誤')
        try:
            result_stamp = result_path.stat().st_mtime_ns
            result = result_path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return Outcome('INVALID_RESULT', 'DONE 存在，但找不到 result.md')
        except UnicodeError:
            return Outcome('INVALID_RESULT', 'result.md 非 UTF-8')
        try:
            task_stamp = (job / 'task.md').stat().st_mtime_ns
            if done_stamp != done_path.stat().st_mtime_ns or result_stamp != result_path.stat().st_mtime_ns:
                return Outcome('INVALID_RESULT', '驗收期間交接檔案變更')
        except FileNotFoundError:
            return Outcome('INVALID_RESULT', '驗收期間交接檔案缺失')
        if needs_permission():
            return Outcome('PERMISSION_REQUIRED', '驗收前偵測到權限提示')
        if not task_stamp <= result_stamp <= done_stamp:
            return Outcome('INVALID_RESULT', '交接檔案時間順序不符')
        if done_stamp > int(deadline * 1_000_000_000):
            return Outcome('TIMEOUT', 'DONE 在原定期限後才發布')
        return validate_handoff(run_id, done, result, expected)
