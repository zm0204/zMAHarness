import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from mah.atomic_io import atomic_write
from mah.fake_adapter import FakeAdapter
from mah.ledger import GrantLedger, LedgerError
from mah.run_manager import RunManager
from mah.verifier import VerificationEvidence, Verifier

ROOT = Path(__file__).resolve().parents[2]
PAYLOADS = {
    "review": {"findings": [{"severity": "minor", "file": "sample.py", "line": 1,
                            "title": "合成發現", "rationale": "合成依據", "suggestion": "合成建議"}]},
    "second-opinion": {"hypotheses": [{"hypothesis": "合成假設", "evidence": "觀察",
                                      "experiment": "重跑合成測試"}],
                       "recommended_next_step": "比較輸出"},
    "research": {"answers": [{"question": "合成問題？", "answer": "合成答案",
                              "citations": [{"path": "sample.py", "lines": "L1-L2", "claim": "合成依據"}]}]},
    "delegate-implement": {"changed_files": ["sample.py"],
                           "commands_run": [{"cmd": "python -B fixture.py", "exit_code": 0, "summary": "自報"}],
                           "notes": "合成修改"},
}
TEST_COMMAND = (sys.executable, "-I", "-B", "-c", "assert 2 + 2 == 4")


class VerifierTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="Verifier 中文 ", dir=ROOT.parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "ledger.sqlite3"
        self.ledger = GrantLedger(self.database)
        self.manager = RunManager(self.ledger)
        self.verifier = Verifier(self.manager, allowed_command_prefixes=((sys.executable, "-I", "-B"),))
        digest = self.ledger.create_card(
            "card-1", repo_root=self.root, task_key="fixture", trigger_ids=["T2"],
            prepare_mode="explicit", allowed_templates=list(PAYLOADS),
            allowed_targets=[{"adapter": "fake", "model": "synthetic", "effort_max": "low"}],
            max_dispatches=5, ttl_minutes=10,
        )
        self.grant = self.ledger.approve("card-1", digest, via="manual_cli")

    def dispatch(self, template="review", *, scenario="success", payload=None, **requirements):
        run_id = f"fixture-{uuid4().hex}"
        adapter = FakeAdapter(scenario=scenario, payload=PAYLOADS[template] if payload is None else payload)
        adapter.dispatch(self.manager, self.grant, run_id, repo_root=self.root,
                         template=template, model="synthetic", effort="low",
                         task="# 中文合成任務\n".encode(), requirements=requirements)
        return run_id

    def files(self, run):
        return self.root / "runs" / run

    def evidence(self, run, **changes):
        options = {"task_sha256": self.ledger.get_run(run)["task_sha256"],
                   "result_sha256": sha256((self.files(run) / "result.json").read_bytes()).hexdigest(),
                   "confirmed": True, "workspace_changed": False}
        return VerificationEvidence(**(options | changes))

    def events(self, run):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("SELECT count(*) FROM run_events WHERE run_id=?", (run,)).fetchone()[0]

    def test_four_templates_have_independent_evidence_and_remain_pending_adoption(self):
        cases = (
            ("review", {}, {"reviewed_findings": 1}),
            ("second-opinion", {}, {"experiments": ("重跑合成測試",)}),
            ("research", {"questions": ["合成問題？"]},
             {"checked_citations": (("sample.py", "L1-L2", "合成依據"),)}),
            ("delegate-implement", {"allowed_files": ["sample.py"], "commands": [TEST_COMMAND]}, {}),
        )
        for template, requirements, extra in cases:
            with self.subTest(template=template):
                run = self.dispatch(template, required_tools=["inspect"], **requirements)
                self.assertEqual(self.verifier.verify(run)["state"], "COLLECTING")
                if template == "delegate-implement":
                    diff = b"diff --git a/sample.py b/sample.py\n"
                    atomic_write(self.files(run) / "diff.patch", diff)
                    test = subprocess.run(TEST_COMMAND, cwd=self.files(run) / "workspace", timeout=10)
                    extra = {"diff_sha256": sha256(diff).hexdigest(), "changed_files": ("sample.py",),
                             "command_results": ((TEST_COMMAND, test.returncode),)}
                result = self.verifier.verify(run, evidence=self.evidence(run, **extra))
                self.assertEqual((result["state"], result["acceptance_status"]), ("SUCCEEDED", "PENDING"))
                self.assertIsNone(self.ledger.get_run(run)["model_observed"])

    def test_worker_exit_zero_and_self_report_cannot_replace_necessary_tool_evidence(self):
        run = self.dispatch(scenario="missing_tool", required_tools=["inspect"])
        view = self.verifier.verify(run, evidence=self.evidence(run, reviewed_findings=1))
        self.assertEqual((view["state"], view["verification"]["reason"]), ("COLLECTING", "required_tool_missing"))
        self.assertEqual(self.ledger.get_run(run)["exit_code"], 0)

    def test_denied_tool_with_exit_zero_is_failure_and_cannot_be_adopted(self):
        run = self.dispatch(scenario="denied", required_tools=["inspect"])
        view = self.verifier.verify(run, evidence=self.evidence(run, reviewed_findings=1))
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "required_action_denied"))
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)
        with self.assertRaises(LedgerError):
            self.manager.record_acceptance(run, accepted=True, reason="不應通過")

    def test_wrong_run_id_and_result_hash_remain_uncollectable(self):
        run = self.dispatch()
        path = self.files(run) / "DONE"
        original = json.loads(path.read_bytes())
        for changes in ({"run_id": "another"}, {"result_sha256": "0" * 64}):
            atomic_write(path, json.dumps(original | changes).encode())
            view = self.verifier.verify(run)
            self.assertEqual(view["state"], "COLLECTING")
            self.assertEqual(view["verification"]["decision"], "pending")

    def assert_unfinished_output(self, scenario):
        run = self.dispatch(scenario=scenario)
        self.assertFalse((self.files(run) / "DONE").exists())
        result = self.verifier.verify(run)
        self.assertEqual((result["state"], result["state_reason"]), ("FAILED", "schema_invalid"))
        self.assertTrue((self.files(run) / "last_message.txt").is_file())
        with self.assertRaises(LedgerError):
            self.manager.record_acceptance(run, accepted=True, reason="不完整輸出")

    def test_incomplete_output_never_produces_done(self):
        self.assert_unfinished_output("incomplete")

    def test_corrupt_output_never_produces_done(self):
        self.assert_unfinished_output("corrupt")

    def test_controller_evidence_and_success_must_match_the_collected_result(self):
        run = self.dispatch()
        evidence = self.evidence(run, reviewed_findings=1)
        with self.assertRaises(LedgerError):
            self.manager.collect(run, verified_sha256=evidence.result_sha256)
        with self.assertRaises(LedgerError):
            self.verifier.verify(run, evidence=self.evidence(run, result_sha256="0" * 64))
        self.assertEqual(self.manager.status(run)["state"], "COLLECTING")

    def test_required_denial_without_done_still_fails(self):
        run = self.dispatch(scenario="denied", required_tools=["inspect"])
        (self.files(run) / "DONE").unlink()
        result = self.verifier.verify(run)
        self.assertEqual((result["state"], result["state_reason"]), ("FAILED", "required_action_denied"))
        self.assertEqual(self.verifier.verify(run)["verification"], result["verification"])

    def test_each_invalid_template_schema_fails_and_preserves_bytes(self):
        for template in PAYLOADS:
            run = self.dispatch(template, payload={})
            view = self.verifier.verify(run)
            self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "schema_invalid"))
            self.assertTrue((self.files(run) / "result.json").is_file())

    def test_delegate_reported_commands_are_not_a_verifier_receipt(self):
        run = self.dispatch("delegate-implement", commands=[TEST_COMMAND], allowed_files=["sample.py"])
        view = self.verifier.verify(run, evidence=self.evidence(run))
        self.assertEqual((view["state"], view["verification"]["reason"]), ("COLLECTING", "diff_missing"))

    def test_interrupted_and_unknown_stay_unknown_without_resending(self):
        for scenario in ("interrupted", "unknown"):
            run = self.dispatch(scenario=scenario)
            manager = RunManager(GrantLedger(self.database))
            before = self.events(run)
            self.assertEqual(manager.scan()[0]["state"], "SUBMISSION_UNKNOWN")
            self.assertEqual(manager.resolve(run)["state"], "SUBMISSION_UNKNOWN")
            self.assertEqual(self.events(run), before)
            manager.resolve(run, action="abandon")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 2)

    def test_restart_after_controller_interruption_recovers_without_launching_again(self):
        adapter = FakeAdapter(payload=PAYLOADS["review"])
        run_id = f"fixture-{uuid4().hex}"
        with patch.object(adapter, "observe", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                adapter.dispatch(self.manager, self.grant, run_id, repo_root=self.root,
                                 template="review", model="synthetic", effort="low", task=b"synthetic")
        self.assertEqual(self.ledger.get_run(run_id)["state"], "RUNNING")
        recovered = RunManager(GrantLedger(self.database))
        self.assertEqual(recovered.scan()[0]["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(recovered.resolve(run_id)["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_restart_collects_published_result_without_launching_again(self):
        adapter = FakeAdapter(payload=PAYLOADS["review"])
        run_id = f"fixture-{uuid4().hex}"
        with patch.object(self.manager, "collect", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                adapter.dispatch(self.manager, self.grant, run_id, repo_root=self.root,
                                 template="review", model="synthetic", effort="low", task=b"synthetic")
        self.assertTrue((self.files(run_id) / "DONE").is_file())
        recovered = RunManager(GrantLedger(self.database))
        self.assertEqual(recovered.scan()[0]["completion_evidence"], "verified")
        verified = Verifier(recovered).verify(run_id, evidence=self.evidence(run_id, reviewed_findings=1))
        self.assertEqual((verified["state"], verified["acceptance_status"]), ("SUCCEEDED", "PENDING"))
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_timeout_and_failure_keep_the_debit(self):
        failed = self.dispatch(scenario="failed")
        self.assertEqual(self.ledger.get_run(failed)["state"], "FAILED")
        timed_out = self.dispatch(scenario="timeout")
        self.assertEqual(self.ledger.get_run(timed_out)["state"], "TIMED_OUT")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 2)

    def test_unauthorized_run_never_launches_a_worker(self):
        adapter = FakeAdapter(payload=PAYLOADS["review"])
        with self.assertRaises(LedgerError):
            adapter.dispatch(self.manager, "missing", f"fixture-{uuid4().hex}", repo_root=self.root,
                             template="review", model="synthetic", effort="low", task=b"synthetic")
        self.assertFalse((self.root / "runs").exists())

    def test_repeat_collection_verification_and_adoption_are_idempotent(self):
        run = self.dispatch()
        self.verifier.verify(run, evidence=self.evidence(run, reviewed_findings=1))
        before = self.events(run)
        self.manager.collect(run)
        self.verifier.verify(run)
        self.assertEqual(self.events(run), before)
        self.assertEqual(self.ledger.get_run(run)["acceptance_status"], "PENDING")
        accepted = self.manager.record_acceptance(run, accepted=True, reason="合成核對", adopted=1, total=1)
        self.assertEqual(accepted["acceptance_status"], "ACCEPTED")
        self.manager.record_acceptance(run, accepted=True, reason="合成核對", adopted=1, total=1)
        self.assertEqual(self.events(run), before + 1)

    def test_readonly_change_and_false_citation_cannot_be_accepted(self):
        run = self.dispatch()
        view = self.verifier.verify(run, evidence=self.evidence(run, workspace_changed=True, reviewed_findings=1))
        self.assertEqual(view["state_reason"], "readonly_violation")
        run = self.dispatch("research", questions=["合成問題？"])
        view = self.verifier.verify(run, evidence=self.evidence(run, invalid_citation=True))
        self.assertEqual((view["state"], view["acceptance_status"]), ("FAILED", "REJECTED"))

    def test_fake_worker_cannot_run_in_repository_state_directory(self):
        adapter = FakeAdapter(payload=PAYLOADS["review"])
        with TemporaryDirectory(dir=ROOT / "tests" / "v2") as temp:
            manager = RunManager(GrantLedger(Path(temp) / "ledger.sqlite3"))
            with self.assertRaises(LedgerError):
                adapter.dispatch(manager, self.grant, f"fixture-{uuid4().hex}", repo_root=self.root,
                                 template="review", model="synthetic", effort="low", task=b"x")
