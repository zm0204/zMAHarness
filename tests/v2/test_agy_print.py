from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from mah.adapters.agy_print import AgyCapture, AgyPrintAdapter, LaunchHandle, normalize_stdout
from mah.ledger import GrantLedger, LedgerError
from mah.run_manager import RunManager, _bytes, _publish
from mah.verifier import VerificationEvidence, Verifier


ROOT = Path(__file__).resolve().parents[2]
REAL_STDOUT = Path(__file__).with_name("fixtures") / "agy-1.5.5-phase0b2-redacted.json"
REAL_REVIEW = Path(__file__).with_name("fixtures") / "agy-1.5.5-review-smoke-redacted.json"
NARRATED_REVIEW = Path(__file__).with_name("fixtures") / "agy-1.5.9-claude-sonnet-narration-redacted.json"
MODEL = "gemini-3.8-flash-low"
TASK = "# 合成任務\n".encode()


class AgyPrintTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="agy 離線 ", dir=ROOT.parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "fixture-worktree"
        self.workspace.mkdir()
        self.ledger = GrantLedger(self.root / "ledger.sqlite3")
        self.manager = RunManager(self.ledger)
        self.adapter = AgyPrintAdapter(self.manager)
        card = self.ledger.create_card(
            "card-1", repo_root=self.root, task_key="agy-offline", trigger_ids=["T2"],
            prepare_mode="explicit", allowed_templates=["review"],
            allowed_targets=[{"adapter": "agy-print", "model": MODEL, "effort_max": "low"}],
            max_dispatches=5, ttl_minutes=10, timeout_seconds=60,
            workspace_path=self.workspace, task_sha256=sha256(TASK).hexdigest(),
        )
        self.grant = self.ledger.approve("card-1", card, via="manual_cli")

    def prepare(self, required=(), *, claim=True, run_id=None):
        run_id = run_id or f"offline-{uuid4().hex}"
        directory = self.root / "runs" / run_id
        task_sha = sha256(TASK).hexdigest()
        self.ledger.check_and_reserve(
            self.grant, run_id, repo_root=self.root, template="review", adapter="agy-print",
            model=MODEL, effort="low", workspace_kind="disposable_worktree",
            workspace_path=self.workspace, task_sha256=task_sha, timeout_seconds=60,
        )
        self.manager.prepare(run_id, TASK)
        invocation = _bytes({"run_id": run_id, "task_sha256": task_sha, "cli_version": "1.5.5",
                             "cwd": str(self.workspace), "timeout_s": 60,
                             "verification_requirements": {"required_tools": list(required)}})
        _publish(directory / "invocation.json", invocation)
        if claim:
            self.manager.claim_launch(run_id, invocation=invocation)
        return AgyCapture(run_id, task_sha, sha256(invocation).hexdigest(), b"", b"", 0)

    def files(self, capture):
        return self.root / "runs" / capture.run_id

    def synthetic_stdout(self, capture, **overrides):
        result = {"schema_version": "2.0", "run_id": capture.run_id, "template": "review",
                  "status": "completed", "summary": "合成結論\n第二行", "blocked_reason": None,
                  "open_questions": [], "template_payload": {"findings": []}}
        raw = {"conversation_id": str(uuid4()), "status": "SUCCESS",
               "response": _bytes(result).decode(), "num_turns": 1,
               "duration_seconds": 1.25, "usage": {"input_tokens": 8, "output_tokens": 4}}
        return _bytes(raw | overrides)

    def collect(self, capture):
        return self.adapter.collect(capture.run_id, capture)

    def test_redacted_real_stdout_normalizes_with_actual_fields_and_types(self):
        raw = REAL_STDOUT.read_bytes()
        self.assertEqual(sha256(raw).hexdigest(), "d55e493430a217b91feea66c9cf41a4dd68cae2dea4fcde190e699676f9e60e5")
        observed = normalize_stdout(raw)
        self.assertEqual(observed, {
            "conversation_id": "00000000-0000-4000-8000-000000000001",
            "raw_status": "SUCCESS", "response": "OK\n", "duration_seconds": 2.0439713,
            "num_turns": 1, "usage": {"input_tokens": 10207, "output_tokens": 1,
                                     "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 10208},
            "model_observed": "unknown", "effort_observed": "unknown", "rate_limits": None,
        })
        capture = replace(self.prepare(), stdout=raw, wall_milliseconds=3919)
        view = self.collect(capture)
        self.assertEqual(view["agy_capture"]["wall_milliseconds"], 3919)
        self.assertEqual(view["state"], "COLLECTING")
        self.assertEqual(view["agy_capture"]["reason"], "structured_result_missing")
        self.assertEqual((self.files(capture) / "last_message.txt").read_bytes(), b"OK\n")
        self.assertFalse((self.files(capture) / "DONE").exists())
        self.assertEqual(view["model_requested"], MODEL)
        self.assertIsNone(view["model_observed"])

    def replay_review(self, *, changed_draft=False):
        raw = REAL_REVIEW.read_bytes()
        self.assertEqual(sha256(raw).hexdigest(), "3d07bbea66397041bbd9eb19df5d3a2afda5458dbaa5ab2d820d14a3f4ea5f37")
        document = json.loads(raw)
        if changed_draft:
            final = document["structured_output"]
            draft = final | {"schema_version": "v2", "summary": "Different draft"}
            document["response"] = _bytes(draft).decode() + "\n" + _bytes(final).decode()
            raw = _bytes(document)
        capture = replace(self.prepare(run_id=document["structured_output"]["run_id"]), stdout=raw,
                          stderr=b"warning: --mode plan has no effect while slash command expansion is disabled.\n",
                          wall_milliseconds=10492, workspace_changed=False)
        directory = self.files(capture)
        _publish(directory / "agy.stdout.pending", capture.stdout)
        _publish(directory / "agy.stderr.pending", capture.stderr)
        self.adapter.persist_capture(SimpleNamespace(process_identity={"pid": 123, "identity": "offline"},
            started_at="2026-11-11T09:36:37+00:00", launch_error=None), capture)
        return capture, document, directory

    def test_actual_review_envelope_replays_through_independent_verifier(self):
        capture, document, directory = self.replay_review()
        view = self.adapter.collect_saved(capture.run_id)
        self.assertEqual((view["state"], view["completion_evidence"]), ("COLLECTING", "verified"))
        self.assertEqual((directory / "last_message.txt").read_bytes(), document["response"].encode("utf-8"))
        self.assertEqual((directory / "agy.stdout.json").read_bytes(), capture.stdout)
        self.assertEqual(view["conversation_id"], document["conversation_id"])
        self.assertEqual(view["agy_capture"]["process_receipt_sha256"],
                         sha256((directory / "agy.process.json").read_bytes()).hexdigest())
        result = (directory / "result.json").read_bytes()
        self.assertEqual(json.loads(result), document["structured_output"])
        self.assertEqual(json.loads(result)["summary"], "隔離測試｜" + "0123456789abcdef" * 2)
        verifier = Verifier(self.manager)
        self.assertEqual(verifier.verify(capture.run_id)["verification"]["reason"], "controller_evidence_missing")
        evidence = VerificationEvidence(capture.task_sha256, sha256(result).hexdigest(),
                                        confirmed=True, workspace_changed=False, reviewed_findings=0)
        verified = verifier.verify(capture.run_id, evidence=evidence)
        self.assertEqual((verified["state"], verified["acceptance_status"]), ("SUCCEEDED", "PENDING"))
        before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        self.assertEqual(self.adapter.collect_saved(capture.run_id)["state"], "SUCCEEDED")
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()})
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_old_terminal_classification_is_not_rewritten_on_recollection(self):
        capture, _, directory = self.replay_review()
        with patch.object(self.adapter, "_classify", return_value=("FAILED", "schema_invalid", None)):
            view = self.adapter.collect_saved(capture.run_id)
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "schema_invalid"))
        before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        self.assertEqual(self.adapter.collect_saved(capture.run_id), view)
        with self.assertRaisesRegex(LedgerError, "Only a recorded structured output conflict"):
            self.adapter.collect_saved(capture.run_id, replay_output=True)
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()})
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_sealed_output_conflict_replays_without_launch_and_retains_original_receipts(self):
        capture, _, directory = self.replay_review(changed_draft=True)
        with patch.object(self.adapter, "_classify", return_value=("FAILED", "structured_output_conflict", None)):
            self.adapter.collect_saved(capture.run_id)
        original = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        self.assertEqual(self.adapter.collect_saved(capture.run_id)["state"], "FAILED")
        publish_result = self.manager._publish_result

        def interrupt_after_done(*args):
            publish_result(*args)
            raise OSError("interrupted publication")

        with patch("mah.adapters.agy_print.Popen", side_effect=AssertionError("replay cannot launch")):
            with patch.object(self.manager, "_publish_result", side_effect=interrupt_after_done):
                with self.assertRaisesRegex(OSError, "interrupted publication"):
                    self.adapter.collect_saved(capture.run_id, replay_output=True)
            self.assertEqual(self.ledger.get_run(capture.run_id)["state"], "FAILED")
            self.assertTrue((directory / "DONE").is_file())
            view = self.adapter.collect_saved(capture.run_id, replay_output=True)
            self.assertEqual((view["state"], view["completion_evidence"]), ("COLLECTING", "verified"))
            self.assertEqual(self.adapter.collect_saved(capture.run_id, replay_output=True)["state"], "COLLECTING")
        self.assertEqual(original, {name: (directory / name).read_bytes() for name in original})
        evidence = VerificationEvidence(capture.task_sha256, view["result_sha256"],
                                        confirmed=True, workspace_changed=False, reviewed_findings=0)
        replay_file = directory / "agy.replay.json"
        replay = replay_file.read_bytes()
        replay_file.write_bytes(replay + b"\n")
        self.assertEqual(Verifier(self.manager).verify(capture.run_id, evidence=evidence)
                         ["verification"]["reason"], "agy_capture_integrity")
        replay_file.write_bytes(replay)
        self.assertEqual(Verifier(self.manager).verify(capture.run_id, evidence=evidence)["state"], "SUCCEEDED")
        self.assertEqual(self.manager.record_acceptance(capture.run_id, accepted=False, reason="Fixture")
                         ["acceptance_status"], "REJECTED")
        self.assertEqual(self.adapter.collect_saved(capture.run_id)["state"], "SUCCEEDED")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_present_but_malformed_structured_output_never_falls_back(self):
        for broken in (None, [], "not an object", {"schema_version": "2.0"}):
            with self.subTest(broken=broken):
                capture = self.prepare()
                raw = self.synthetic_stdout(capture, structured_output=broken)
                self.assertEqual(self.collect(replace(capture, stdout=raw))["state_reason"], "schema_invalid")
                self.assertFalse((self.files(capture) / "DONE").exists())

    def test_structured_output_requires_template_payload_schema(self):
        capture = self.prepare()
        raw = json.loads(self.synthetic_stdout(capture))
        result = json.loads(raw["response"])
        result["template_payload"] = {"findings": [None]}
        view = self.collect(replace(capture, stdout=_bytes(raw | {
            "structured_output": result, "response": _bytes(result).decode(),
        })))
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "schema_invalid"))
        self.assertFalse((self.files(capture) / "DONE").exists())

    def test_conflicting_response_cannot_override_structured_output(self):
        for change in ({"summary": "different"}, {"status": "failed"}):
            with self.subTest(change=change):
                capture = self.prepare()
                raw = json.loads(self.synthetic_stdout(capture))
                structured = json.loads(raw["response"])
                raw.update(structured_output=structured, response=_bytes(structured | change).decode())
                view = self.collect(replace(capture, stdout=_bytes(raw)))
                self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "structured_output_conflict"))
                self.assertFalse((self.files(capture) / "DONE").exists())

    def test_incompatible_multi_json_response_cannot_be_cherry_picked(self):
        capture, document, _ = self.replay_review()
        last = document["structured_output"] | {"summary": "Not the structured output"}
        raw = document | {"response": document["response"] + "\n" + _bytes(last).decode()}
        view = self.collect(replace(capture, stdout=_bytes(raw)))
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "structured_output_conflict"))

    def test_final_object_may_correct_an_earlier_draft_without_schema_echo(self):
        capture = self.prepare()
        raw = json.loads(self.synthetic_stdout(capture))
        final = json.loads(raw["response"])
        draft = final | {"schema_version": "v2", "summary": "A draft", "status": "partial"}
        raw.update(structured_output=final, response=_bytes(draft).decode() + "\n" + _bytes(final).decode())
        view = self.collect(replace(capture, stdout=_bytes(raw)))
        self.assertEqual((view["state"], view["completion_evidence"]), ("COLLECTING", "verified"))
        self.assertEqual(json.loads((self.files(capture) / "result.json").read_bytes()), final)

    def narrated_capture(self):
        """The real claude-sonnet-5-5-medium Review (agy 1.5.9) that opens with plain narration."""
        raw = NARRATED_REVIEW.read_bytes()
        self.assertEqual(sha256(raw).hexdigest(), "1bcebbd6c257a8e76dabeabf57348625127035f1cdc9f88be935aaf5d3876dd6")
        document = json.loads(raw)
        capture = replace(self.prepare(run_id=document["structured_output"]["run_id"]), stdout=raw,
                          wall_milliseconds=194330, workspace_changed=False)
        return capture, document

    def test_real_narrated_review_collects_without_the_narration(self):
        capture, document = self.narrated_capture()
        response, final = document["response"], document["structured_output"]
        self.assertTrue(response.startswith("我先閱讀相關檔案。\nUse read_file instead.\n"))
        directory = self.files(capture)
        _publish(directory / "agy.stdout.pending", capture.stdout)
        _publish(directory / "agy.stderr.pending", capture.stderr)
        self.adapter.persist_capture(SimpleNamespace(process_identity={"pid": 123, "identity": "offline"},
            started_at="2026-11-18T14:11:50+00:00", launch_error=None), capture)
        view = self.adapter.collect_saved(capture.run_id)
        self.assertEqual((view["state"], view["state_reason"], view["completion_evidence"]),
                         ("COLLECTING", "structured_result_collected", "verified"))
        result = (directory / "result.json").read_bytes()
        self.assertEqual(json.loads(result), final)
        self.assertNotIn("我先閱讀相關檔案", result.decode("utf-8"))
        # The narration stays only in the sealed evidence, never in the result.
        self.assertEqual((directory / "last_message.txt").read_bytes(), response.encode("utf-8"))
        evidence = VerificationEvidence(capture.task_sha256, sha256(result).hexdigest(), confirmed=True,
                                        workspace_changed=False, reviewed_findings=len(final["template_payload"]["findings"]))
        verified = Verifier(self.manager).verify(capture.run_id, evidence=evidence)
        self.assertEqual((verified["state"], verified["verification"]["decision"]), ("SUCCEEDED", "verified"))

    def assert_narrated_response_conflicts(self, build):
        """`build(final)` returns the response text; each case runs in a fresh Run with the real envelope."""
        document = json.loads(NARRATED_REVIEW.read_bytes())
        capture = replace(self.prepare(run_id=f"agy-{uuid4().hex}"), workspace_changed=False)
        document = json.loads(_bytes(document).decode().replace(document["structured_output"]["run_id"],
                                                                capture.run_id))
        view = self.collect(replace(capture, stdout=_bytes(document | {
            "response": build(document["structured_output"])})))
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "structured_output_conflict"))
        self.assertFalse((self.files(capture) / "DONE").exists())

    def test_narration_carrying_a_different_json_object_still_conflicts(self):
        def build(final):
            other = final | {"summary": "Another answer inside the narration"}
            return "說明 " + _bytes(other).decode() + " 之後還有說明。\n" + _bytes(final).decode()
        self.assert_narrated_response_conflicts(build)

    def test_narration_before_a_different_final_object_still_conflicts(self):
        self.assert_narrated_response_conflicts(
            lambda final: "我先閱讀相關檔案。\n" + _bytes(final | {"summary": "Not the structured output"}).decode())

    def test_text_after_the_final_object_still_conflicts(self):
        self.assert_narrated_response_conflicts(
            lambda final: "我先閱讀相關檔案。\n" + _bytes(final).decode() + "\n以上是結論。")

    def test_structured_result_can_stand_alone_but_still_needs_verifier(self):
        capture = self.prepare()
        raw = json.loads(self.synthetic_stdout(capture))
        raw["structured_output"] = json.loads(raw.pop("response"))
        view = self.collect(replace(capture, stdout=_bytes(raw)))
        self.assertEqual((view["state"], view["completion_evidence"]), ("COLLECTING", "verified"))
        self.assertEqual(Verifier(self.manager).verify(capture.run_id)["verification"]["reason"],
                         "controller_evidence_missing")

    def test_structured_result_with_mismatched_schema_echo_fails_closed(self):
        capture = self.prepare()
        raw = json.loads(self.synthetic_stdout(capture))
        raw.update(structured_output=json.loads(raw["response"]), json_schema={"type": "array"})
        view = self.collect(replace(capture, stdout=_bytes(raw)))
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "schema_invalid"))
        self.assertFalse((self.files(capture) / "DONE").exists())

    def test_legacy_concatenated_response_is_rejected(self):
        capture = self.prepare()
        raw = json.loads(self.synthetic_stdout(capture))
        raw["response"] += "\n" + raw["response"]
        view = self.collect(replace(capture, stdout=_bytes(raw)))
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "schema_invalid"))

    def test_structured_result_keeps_utf8_and_requires_independent_verification(self):
        capture = self.prepare()
        capture = replace(capture, stdout=self.synthetic_stdout(capture),
                          stderr="合成警告\r\n".encode())
        view = self.collect(capture)
        self.assertEqual(view["state"], "COLLECTING")
        self.assertEqual((self.files(capture) / "agy.stderr.txt").read_bytes(), capture.stderr)
        result = (self.files(capture) / "result.json").read_bytes()
        self.assertEqual(json.loads(result)["summary"], "合成結論\n第二行")
        verifier = Verifier(self.manager)
        with self.assertRaises(LedgerError):
            self.manager.collect(capture.run_id, verified_sha256=sha256(result).hexdigest())
        self.assertEqual(verifier.verify(capture.run_id)["verification"]["reason"], "controller_evidence_missing")
        evidence = VerificationEvidence(capture.task_sha256, sha256(result).hexdigest(),
                                        confirmed=True, workspace_changed=False, reviewed_findings=0)
        verified = verifier.verify(capture.run_id, evidence=evidence)
        self.assertEqual((verified["state"], verified["acceptance_status"]), ("SUCCEEDED", "PENDING"))
        self.assertEqual(self.collect(capture)["state"], "SUCCEEDED")

    def test_missing_tool_audit_stays_pending(self):
        capture = self.prepare(required=["read-fixture"])
        capture = replace(capture, stdout=self.synthetic_stdout(capture))
        self.collect(capture)
        self.assertEqual(Verifier(self.manager).verify(capture.run_id)["verification"]["reason"],
                         "required_tool_missing")

    def test_policy_denial_with_exit_zero_is_failed(self):
        capture = self.prepare(required=["read-fixture"])
        capture = replace(capture, stdout=self.synthetic_stdout(capture),
                          tool_events=({"type": "tool", "name": "read-fixture", "status": "denied", "exit_code": None},))
        result = self.collect(capture)
        self.assertEqual((result["state"], result["state_reason"]), ("FAILED", "required_action_denied"))
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)
        self.assertFalse((self.files(capture) / "DONE").exists())

    def test_model_failure_and_nonzero_exit_with_submission_are_failed(self):
        for status, exit_code, reason in (("FAILURE", 0, "worker_status_not_success"), ("SUCCESS", 5, "process_failed")):
            with self.subTest(status=status, exit_code=exit_code):
                capture = self.prepare()
                result = self.collect(replace(capture, stdout=self.synthetic_stdout(capture, status=status),
                                              exit_code=exit_code))
                self.assertEqual((result["state"], result["state_reason"]), ("FAILED", reason))
                self.assertFalse((self.files(capture) / "DONE").exists())
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 2)

    def test_malformed_stdout_is_unknown_and_exact_bytes_are_preserved(self):
        for raw in (b"\xff", b'{"status":', b'debug\n{"status":"SUCCESS"}',
                    b'{"status":"FAILURE","status":"SUCCESS"}'):
            with self.subTest(raw=raw):
                capture = replace(self.prepare(), stdout=raw, stderr=b"\xff")
                result = self.collect(capture)
                self.assertEqual(result["state"], "SUBMISSION_UNKNOWN")
                self.assertEqual((self.files(capture) / "agy.stdout.json").read_bytes(), raw)
                self.manager.resolve(capture.run_id, action="abandon")

    def test_nonzero_exit_with_only_unverified_stderr_is_unknown(self):
        capture = replace(self.prepare(), exit_code=2, stderr=b"unknown flag: --something")
        self.assertEqual(self.collect(capture)["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_zero_turn_success_is_unknown_even_if_response_present(self):
        capture = self.prepare()
        capture = replace(capture, stdout=self.synthetic_stdout(capture, num_turns=0))
        self.assertEqual(self.collect(capture)["state"], "SUBMISSION_UNKNOWN")

    def test_timeout_and_interruption_without_submission_stay_unknown_after_restart(self):
        for termination in ("timeout", "interrupted"):
            with self.subTest(termination=termination):
                capture = replace(self.prepare(), termination=termination, exit_code=None)
                self.assertEqual(self.collect(capture)["state"], "SUBMISSION_UNKNOWN")
                manager = RunManager(GrantLedger(self.ledger.database))
                self.assertEqual(manager.scan()[0]["state"], "SUBMISSION_UNKNOWN")
                self.assertEqual(AgyPrintAdapter(manager).collect(capture.run_id, capture)["state"], "SUBMISSION_UNKNOWN")
                manager.resolve(capture.run_id, action="abandon")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 2)

    def test_observer_waits_remaining_template_budget_and_keeps_final_json(self):
        capture = self.prepare()
        directory = self.files(capture)
        _publish(directory / "agy.stdout.pending", self.synthetic_stdout(capture))
        _publish(directory / "agy.stderr.pending", b"")
        plan = SimpleNamespace(run_id=capture.run_id, timeout_s=900, task_sha256=capture.task_sha256,
                               metadata=(directory / "invocation.json").read_bytes(), baseline="unchanged")
        process = Mock()
        process.wait.return_value = 0
        handle = LaunchHandle(plan, process, None, "2026-11-12T14:00:00+00:00", 0)
        with patch("mah.adapters.agy_print.monotonic", side_effect=[180, 240]), patch(
                "mah.adapters.agy_print._invocation_snapshot", return_value=("unchanged", False)):
            observed = self.adapter.observe(handle)
        process.wait.assert_called_once_with(timeout=720)
        process.kill.assert_not_called()
        self.assertEqual((observed.exit_code, observed.termination), (0, "exited"))

    def test_interrupted_observer_recovers_logged_submission_after_unknown_state(self):
        capture = self.prepare()
        directory = self.files(capture)
        _publish(directory / "agy.stdout.pending", b"")
        _publish(directory / "agy.stderr.pending", b"")
        conversation = "12345678-1234-4234-8234-123456789abc"
        log = f"Created conversation {conversation}\nSending user message\n".encode()
        _publish(directory / "agy.cli.log", log)
        plan = SimpleNamespace(run_id=capture.run_id, timeout_s=60, task_sha256=capture.task_sha256,
                               metadata=(directory / "invocation.json").read_bytes(), baseline="unchanged")
        process = Mock()
        process.wait.side_effect = [KeyboardInterrupt, 1]
        process.poll.return_value = None
        handle = LaunchHandle(plan, process, None, "2026-11-12T14:00:00+00:00", 0)
        with patch("mah.adapters.agy_print.monotonic", side_effect=[10, 11]), patch(
                "mah.adapters.agy_print._invocation_snapshot", return_value=("unchanged", False)):
            observed = self.adapter.observe(handle)
        process.kill.assert_called_once_with()
        self.assertEqual(observed.termination, "interrupted")
        self.adapter.persist_capture(handle, observed)
        self.ledger.transition(capture.run_id, "LAUNCHING", "SUBMISSION_UNKNOWN")
        view = self.adapter.collect_saved(capture.run_id)
        self.assertEqual((view["state"], view["state_reason"], view["conversation_id"]),
                         ("TIMED_OUT", "process_timed_out", conversation))
        (directory / "agy.cli.log").write_bytes(log + b"changed\n")
        with self.assertRaisesRegex(LedgerError, "differs from the sealed process capture"):
            self.adapter.collect_saved(capture.run_id)
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_unknown_state_is_readable_by_a_new_cli_process(self):
        capture = replace(self.prepare(), termination="interrupted", exit_code=None)
        self.collect(capture)
        settings = self.root / "harness.local.toml"
        settings.write_text('[paths]\nstate_dir = "."\n', encoding="utf-8")
        proc = subprocess.run([sys.executable, "-B", "-m", "mah", "--config", str(settings),
                               "status", capture.run_id], cwd=ROOT, capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        self.assertEqual(json.loads(proc.stdout)["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)
        self.assertFalse((self.files(capture) / "DONE").exists())

    def test_confirmed_pre_submit_receipt_refunds_only_once(self):
        capture = replace(self.prepare(), termination="not_started", exit_code=None, pre_submit_reason="config_parse_error")
        result = self.collect(capture)
        self.assertEqual(result["state"], "FAILED_PRE_SUBMIT")
        self.assertEqual(self.collect(capture)["state"], "FAILED_PRE_SUBMIT")
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 0)

    def test_claim_and_pre_submit_receipt_cannot_contradict_other_evidence(self):
        capture = self.prepare(claim=False)
        with self.assertRaises(LedgerError):
            self.collect(replace(capture, stdout=self.synthetic_stdout(capture)))
        self.manager.claim_launch(capture.run_id, invocation=(self.files(capture) / "invocation.json").read_bytes())
        with self.assertRaises(LedgerError):
            self.collect(replace(capture, termination="not_started", pre_submit_reason="config_parse_error"))
        self.assertFalse((self.files(capture) / "agy.capture.json").exists())
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_cross_run_capture_wrong_invocation_and_wrong_result_id(self):
        capture = self.prepare()
        with self.assertRaises(LedgerError):
            self.adapter.collect(f"other-{uuid4().hex}", capture)
        with self.assertRaises(LedgerError):
            self.collect(replace(capture, invocation_sha256="0" * 64))
        self.assertFalse((self.files(capture) / "agy.capture.json").exists())
        foreign = replace(capture, run_id=f"other-{uuid4().hex}")
        result = self.collect(replace(capture, stdout=self.synthetic_stdout(foreign)))
        self.assertEqual((result["state"], result["state_reason"]), ("FAILED", "schema_invalid"))
        self.assertFalse((self.files(capture) / "DONE").exists())

    def test_same_capture_repeats_without_overwrite_and_mismatch_rejected(self):
        capture = self.prepare()
        capture = replace(capture, stdout=self.synthetic_stdout(capture))
        first = self.collect(capture)
        files = {item.name: item.read_bytes() for item in self.files(capture).iterdir() if item.is_file()}
        self.assertEqual(self.collect(capture), first)
        with self.assertRaises(LedgerError):
            self.collect(replace(capture, stderr=b"different"))
        self.assertEqual(files, {item.name: item.read_bytes() for item in self.files(capture).iterdir() if item.is_file()})
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_verifier_checks_raw_capture_binding_before_accepting_controller_evidence(self):
        capture = self.prepare()
        capture = replace(capture, stdout=self.synthetic_stdout(capture))
        self.collect(capture)
        directory = self.files(capture)
        result_sha = sha256((directory / "result.json").read_bytes()).hexdigest()
        evidence = VerificationEvidence(capture.task_sha256, result_sha, confirmed=True,
                                        workspace_changed=False, reviewed_findings=0)
        (directory / "agy.stdout.json").write_bytes(b"synthetic tamper")
        view = Verifier(self.manager).verify(capture.run_id, evidence=evidence)
        self.assertEqual(view["state"], "COLLECTING")
        self.assertEqual(view["verification"]["reason"], "agy_capture_integrity")

    def test_foreign_conversation_is_rejected_even_in_rebound_capture(self):
        first = self.prepare()
        raw = self.synthetic_stdout(first, status="FAILURE")
        self.assertEqual(self.collect(replace(first, stdout=raw))["state"], "FAILED")
        second = self.prepare()
        previous_id = json.loads(raw)["conversation_id"]
        with self.assertRaises(LedgerError):
            self.collect(replace(second, stdout=self.synthetic_stdout(second, conversation_id=previous_id)))
        self.assertFalse((self.files(second) / "agy.capture.json").exists())

    def test_confirmed_submission_followed_by_timeout_preserves_evidence_and_debit(self):
        capture = self.prepare()
        capture = replace(capture, stdout=self.synthetic_stdout(capture), exit_code=None, termination="timeout")
        view = self.collect(capture)
        self.assertEqual(view["state"], "TIMED_OUT")
        self.assertEqual(view["conversation_id"], json.loads(capture.stdout)["conversation_id"])
        self.assertFalse((self.files(capture) / "DONE").exists())
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_replay_after_done_was_published_before_sqlite_commit(self):
        capture = self.prepare()
        capture = replace(capture, stdout=self.synthetic_stdout(capture))
        publish = self.manager._publish_result

        def fail_after_publish(*args):
            publish(*args)
            raise OSError("synthetic interruption after DONE")

        with patch.object(self.manager, "_publish_result", side_effect=fail_after_publish):
            with self.assertRaises(OSError):
                self.collect(capture)
        manager = RunManager(GrantLedger(self.ledger.database))
        self.assertEqual(manager.scan()[0]["state"], "COLLECTING")
        view = AgyPrintAdapter(manager).collect(capture.run_id, capture)
        self.assertEqual(view["state"], "COLLECTING")
        self.assertEqual(view["conversation_id"], json.loads(capture.stdout)["conversation_id"])
        self.assertEqual(view["exit_code"], 0)
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)

    def test_invalid_optional_field_types_do_not_fabricate_observations(self):
        capture = self.prepare()
        raw = self.synthetic_stdout(capture, duration_seconds="1.25",
                                    usage={"input_tokens": True, "output_tokens": -1, "total_tokens": 0},
                                    model=MODEL, effort="low")
        observed = normalize_stdout(raw)
        self.assertEqual(observed["usage"], {"total_tokens": 0})
        self.assertIsNone(observed["duration_seconds"])
        self.assertEqual((observed["model_observed"], observed["effort_observed"]), ("unknown", "unknown"))

    def test_replay_after_atomic_publish_failure(self):
        capture = self.prepare()
        capture = replace(capture, stdout=self.synthetic_stdout(capture))
        with patch.object(self.manager, "_publish_result", side_effect=OSError("synthetic file lock")):
            with self.assertRaises(OSError):
                self.collect(capture)
        self.assertEqual(self.ledger.get_run(capture.run_id)["state"], "LAUNCHING")
        self.assertFalse((self.files(capture) / "DONE").exists())
        manager = RunManager(GrantLedger(self.ledger.database))
        recovered = AgyPrintAdapter(manager).collect(capture.run_id, capture)
        self.assertEqual((recovered["state"], recovered["completion_evidence"]), ("COLLECTING", "verified"))
        self.assertEqual(self.ledger.get_grant(self.grant)["used_dispatches"], 1)
