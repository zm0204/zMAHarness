import io
import json
import os
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import dataclass, fields
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from mah import cli
from mah.adapters import registry
from mah.adapters.agy_print import AgyPrintAdapter
from mah.adapters.base import Adapter, AdapterPolicy
from mah.config import ConfigError, load_config
from mah.db import connect, transaction
from mah.ledger import GrantLedger, LedgerError
from mah.run_manager import RunManager
from mah.verifier import VerificationEvidence, Verifier, requirements_for

OLD_ADAPTERS = {"codex-exec", "gui-terra", "agy-print"}
# Name sets that the pre-registry code compared against, one per policy flag.
OLD_NAME_CHECKS = {
    "user_configurable": OLD_ADAPTERS,
    "requires_dispatch_scope": {"agy-print"},
    "attended_approval_only": {"agy-print"},
    "window_approval": {"fake"},
    "replays_structured_output": {"agy-print"},
    "refunds_unstarted_process": {"agy-print"},
    "gui_submission": {"gui-terra"},
    "template_verified": {"fake", "agy-print"},
    # New in the hardening phase: delegate-implement is deferred for every Worker Adapter.
    "allows_delegate_implement": {"fake"},
}


@dataclass(frozen=True)
class DummyPlan:
    run_id: str
    cwd: Path
    workspace_kind: str
    timeout_s: int
    metadata: bytes
    template: str


class DummyAdapter:
    """An in-process worker that exists only in this test module."""

    id = "test-dummy"

    def __init__(self, manager):
        self.manager = manager
        self.launches = 0

    def prepare_invocation(self, *, run_id, repo_root, template, model, effort, task, workspace,
                           worktree_root, timeout_s, requirements):
        cwd = Path(workspace).resolve()
        metadata = json.dumps({
            "run_id": run_id, "task_sha256": sha256(task).hexdigest(), "cwd": str(cwd), "timeout_s": timeout_s,
            "verification_requirements": requirements_for({} if requirements is None else requirements),
        }).encode()
        return DummyPlan(run_id, cwd, "read_only", timeout_s, metadata, template)

    def launch(self, plan):
        self.manager.claim_launch(plan.run_id, invocation=plan.metadata)
        self.launches += 1
        self.manager.mark_running(plan.run_id, pid=os.getpid())
        return plan

    def observe(self, handle):
        return {"schema_version": "2.0", "run_id": handle.run_id, "template": handle.template,
                "status": "completed", "summary": "dummy review", "blocked_reason": None,
                "open_questions": [], "template_payload": {"findings": []}}

    def persist_capture(self, handle, capture):
        (self.manager._directory(handle.run_id) / "dummy.capture.json").write_text(json.dumps(capture))

    def collect_saved(self, run_id, **options):
        capture = json.loads((self.manager._directory(run_id) / "dummy.capture.json").read_text())
        if self.manager.ledger.get_run(run_id)["state"] == "RUNNING":
            self.manager.record_exit(run_id, 0)
            self.manager.publish_result(run_id, capture)
        return self.manager.collect(run_id)


class RegistryTableTests(unittest.TestCase):
    def test_configurable_names_equal_the_old_adapter_list(self):
        self.assertEqual(set(registry.configurable_names()), OLD_ADAPTERS | {"claude-print"})
        self.assertIn("fake", registry.ADAPTER_POLICIES)
        self.assertNotIn("fake", registry.configurable_names())
        self.assertEqual(set(registry.ADAPTER_POLICIES), OLD_ADAPTERS | {"fake", "claude-print"})

    def test_every_flag_matches_the_old_name_comparison(self):
        flags = [field.name for field in fields(AdapterPolicy) if field.name != "run_id_prefix"]
        self.assertEqual(set(flags), set(OLD_NAME_CHECKS))
        for name in (*registry.ADAPTER_POLICIES, "unknown-x"):
            for flag in flags:
                with self.subTest(adapter=name, flag=flag):
                    self.assertIs(getattr(registry.policy_for(name), flag), name in OLD_NAME_CHECKS[flag] or (
                        name in ("claude-print", "codex-exec") and flag in {
                            "user_configurable", "requires_dispatch_scope", "attended_approval_only",
                            "template_verified"}))

    def test_unregistered_names_fall_back_to_the_default_policy(self):
        self.assertFalse(registry.is_registered("unknown-x"))
        self.assertIs(registry.policy_for("unknown-x"), registry.DEFAULT_POLICY)
        self.assertEqual(registry.DEFAULT_POLICY, AdapterPolicy())
        self.assertIsNone(registry.DEFAULT_POLICY.run_id_prefix)
        self.assertIsNone(registry.capture_check("unknown-x"))
        self.assertFalse(registry.has_factory("unknown-x"))

    def test_only_agy_print_has_a_factory_capture_check_and_run_prefix(self):
        self.assertEqual(registry.dispatchable_names(), ("agy-print", "claude-print", "codex-exec"))
        self.assertEqual(set(registry.CAPTURE_CHECKS), {"agy-print", "claude-print", "codex-exec"})
        self.assertEqual(registry.policy_for("agy-print").run_id_prefix, "agy")
        self.assertEqual({name for name, policy in registry.ADAPTER_POLICIES.items() if policy.run_id_prefix},
                         {"agy-print", "claude-print", "codex-exec"})

    def test_agy_print_satisfies_the_protocol(self):
        self.assertIsInstance(AgyPrintAdapter(None), Adapter)
        self.assertIsInstance(DummyAdapter(None), Adapter)
        self.assertNotIsInstance(object(), Adapter)

    def test_registering_an_existing_name_is_refused(self):
        with self.assertRaises(ValueError):
            registry.register("agy-print", AdapterPolicy())
        self.assertIs(registry.policy_for("agy-print"), registry.ADAPTER_POLICIES["agy-print"])


class UnknownAdapterErrorTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-registry-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.config = self.root / "harness.local.toml"

    def write_config(self, text=""):
        self.config.write_text('[paths]\nstate_dir = "state"\n' + text, encoding="utf-8")
        return self.config

    def main(self, *args):
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = cli.main(["--config", str(self.config), *args])
        return code, stderr.getvalue()

    def test_fake_or_unknown_names_are_rejected_in_toml_as_before(self):
        for text, message in (
            ('[adapters]\nenabled = ["fake"]\n', "adapters.enabled 有未知或重複的 Adapter"),
            ('[adapters]\nenabled = ["unknown-x"]\n', "adapters.enabled 有未知或重複的 Adapter"),
            ('[defaults]\nreview = { adapter = "fake", model = "m", effort = "low" }\n',
             "defaults.review 的 Adapter 或思考程度無效"),
        ):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError) as error:
                    load_config(self.write_config(text))
                self.assertEqual(str(error.exception), message)
                code, stderr = self.main("grant", "list")
                self.assertEqual((code, stderr), (2, f"錯誤：{message}\n"))

    def test_cli_adapter_choices_are_unchanged(self):
        action = next(action for action in cli.parser()._subparsers._group_actions[0].choices["dispatch"]._actions
                      if action.dest == "adapter")
        self.assertEqual(action.choices, ("agy-print", "claude-print", "codex-exec"))
        self.write_config()
        with self.assertRaises(SystemExit) as exit_error:
            self.main("dispatch", "--grant", "g", "--template", "review", "--adapter", "unknown-x",
                      "--model", "m", "--effort", "low", "--task-file", "t", "--workspace", "w")
        self.assertEqual(exit_error.exception.code, 2)

    def test_disabled_adapter_keeps_its_exit_code_and_message(self):
        self.write_config('[adapters]\nenabled = ["codex-exec"]\n')
        code, stderr = self.main("dispatch", "--grant", "g", "--template", "review", "--adapter", "agy-print",
                                 "--model", "m", "--effort", "low", "--task-file", "t", "--workspace", "w")
        self.assertEqual((code, stderr), (14, "錯誤：agy-print 尚未在本機設定啟用\n"))

    def test_card_target_outside_enabled_adapters_keeps_ledger_error_14(self):
        (self.root / "state").mkdir(exist_ok=True)
        ledger = GrantLedger(self.root / "state" / "ledger.sqlite3")
        with self.assertRaises(LedgerError) as error:
            ledger.create_card("unknown-card", repo_root=str(self.root), task_key="task", trigger_ids=["T5"],
                               prepare_mode="card", allowed_templates=["review"],
                               allowed_targets=[{"adapter": "unknown-x", "model": "m", "effort_max": "low"}],
                               max_dispatches=1, ttl_minutes=10, enabled_adapters=["agy-print"])
        self.assertEqual((error.exception.code, str(error.exception)), (14, "Adapter is not enabled in local settings"))


class UnregisteredNameBehaviorTests(unittest.TestCase):
    """An unregistered adapter name takes the same else branches as before the registry."""

    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-unknown-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.root / "state").mkdir()
        self.ledger = GrantLedger(self.root / "state" / "ledger.sqlite3")
        self.manager = RunManager(self.ledger)
        self.target = [{"adapter": "unknown-x", "model": "m", "effort_max": "low"}]

    def card(self, card_id, **scope):
        return self.ledger.create_card(card_id, repo_root=str(self.repo), task_key=card_id, trigger_ids=["T5"],
                                       prepare_mode="card", allowed_templates=["review"],
                                       allowed_targets=self.target, max_dispatches=1, ttl_minutes=10, **scope)

    def test_unscoped_card_grant_and_launch_are_accepted(self):
        digest = self.card("plain")
        grant_id = self.ledger.approve("plain", digest, via="manual_cli", evidence="interactive_terminal_confirmation")
        self.assertIsNone(self.ledger.get_grant(grant_id)["dispatch_scope_json"])
        run_id = f"unknown-{uuid4().hex}"
        task = b"synthetic task"
        self.ledger.check_and_reserve(grant_id, run_id, repo_root=self.repo, template="review", adapter="unknown-x",
                                      model="m", effort="low", workspace_kind="read_only",
                                      workspace_path=self.root, task_sha256=sha256(task).hexdigest())
        # Not Verifier-only: a direct verified hash is not refused with "Template verification is required".
        self.assertEqual(self.manager.collect(run_id, verified_sha256="0" * 64)["state"], "RESERVED")
        self.manager.prepare(run_id, task)
        self.assertIsNone(self.manager.claim_launch(run_id))
        self.assertEqual(self.ledger.get_run(run_id)["state"], "LAUNCHING")
        # Not a GUI adapter: no gui_not_submitted cancellation.
        with self.assertRaises(LedgerError) as cancel:
            self.manager.cancel(run_id, gui_not_submitted=True)
        self.assertEqual((cancel.exception.code, str(cancel.exception)),
                         (20, "A GUI launch and explicit not-submitted evidence are required"))
        with closing(connect(self.ledger.database)) as db:
            run = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            for state, reason, message in (
                ("CANCELLED", "gui_not_submitted", "A GUI launch needs explicit not-submitted evidence"),
                ("FAILED_PRE_SUBMIT", "process_not_started", "Local process failure requires a bound agy capture"),
            ):
                with self.subTest(state=state), self.assertRaises(LedgerError) as error:
                    self.ledger._transition(db, run, state, reason=reason, evidence={"agy_capture_sha256": "a" * 64})
                self.assertEqual((error.exception.code, str(error.exception)), (20, message))

    def test_window_approval_refuses_unregistered_targets(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        digest = self.card("scoped", timeout_seconds=60, workspace_path=str(workspace), task_sha256="b" * 64)
        with self.assertRaises(LedgerError) as error:
            self.ledger._window_candidate("scoped", digest)
        self.assertEqual((error.exception.code, str(error.exception)),
                         (10, "核准視窗僅接受已綁定範圍的單次 Fake Adapter 卡片"))


class DummyAdapterRegistrationTests(unittest.TestCase):
    """A new Worker needs only a registry entry, not edits to core modules."""

    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-dummy-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.checked = []

        def capture_check(directory, run, result_sha256):
            self.checked.append((directory.name, run["run_id"], result_sha256))
            return True

        registry.register("test-dummy", AdapterPolicy(requires_dispatch_scope=True, template_verified=True,
                                                      run_id_prefix="dummy"),
                          factory=lambda manager, config: DummyAdapter(manager), capture_check=capture_check)
        self.addCleanup(registry.unregister, "test-dummy")

    def test_full_dispatch_through_run_manager(self):
        self.assertNotIn("test-dummy", registry.configurable_names())
        self.assertIn("test-dummy", registry.dispatchable_names())
        repo, workspace = self.root / "repo", self.root / "workspace"
        repo.mkdir()
        workspace.mkdir()
        (self.root / "state").mkdir(exist_ok=True)
        ledger = GrantLedger(self.root / "state" / "ledger.sqlite3")
        manager = RunManager(ledger)
        task = "合成審查任務".encode()
        digest = ledger.create_card(
            "dummy-card", repo_root=str(repo), task_key="dummy", trigger_ids=["T5"], prepare_mode="card",
            allowed_templates=["review"], allowed_targets=[{"adapter": "test-dummy", "model": "m", "effort_max": "low"}],
            max_dispatches=1, ttl_minutes=10, enabled_adapters=["test-dummy"],
            timeout_seconds=900, workspace_path=str(workspace), task_sha256=sha256(task).hexdigest(),
        )
        grant_id = ledger.approve("dummy-card", digest, via="codex_prompt")

        adapter = registry.create_adapter("test-dummy", manager, None)
        self.assertIsInstance(adapter, Adapter)
        run_id = f"{registry.policy_for('test-dummy').run_id_prefix}-{uuid4().hex}"
        view = manager.dispatch(adapter, grant_id=grant_id, run_id=run_id, repo_root=repo, template="review",
                                model="m", effort="low", task=task, workspace=workspace,
                                worktree_root=self.root, timeout_s=900)
        self.assertEqual((view["state"], view["adapter"], adapter.launches), ("COLLECTING", "test-dummy", 1))
        with self.assertRaises(LedgerError) as direct:
            manager.collect(run_id, verified_sha256="0" * 64)
        self.assertEqual(direct.exception.code, 30)

        directory = manager._directory(run_id)
        result_sha = sha256((directory / "result.json").read_bytes()).hexdigest()
        evidence = VerificationEvidence(task_sha256=sha256(task).hexdigest(), result_sha256=result_sha,
                                        confirmed=True, workspace_changed=False, reviewed_findings=0)
        verified = Verifier(manager).verify(run_id, evidence=evidence)
        self.assertEqual((verified["state"], verified["verification"]["decision"]), ("SUCCEEDED", "verified"))
        self.assertEqual(self.checked, [(run_id, run_id, result_sha)])
        with self.assertRaises(LedgerError) as total:
            manager.record_acceptance(run_id, accepted=True, reason="ok", adopted=0, total=1)
        self.assertEqual(total.exception.code, 2)
        accepted = manager.record_acceptance(run_id, accepted=True, reason="ok", adopted=0, total=0)
        self.assertEqual(accepted["acceptance_status"], "ACCEPTED")
        self.assertEqual(ledger.get_grant(grant_id)["used_dispatches"], 1)

    def test_unregister_removes_every_entry(self):
        registry.unregister("test-dummy")
        self.assertFalse(registry.is_registered("test-dummy"))
        self.assertFalse(registry.has_factory("test-dummy"))
        self.assertIsNone(registry.capture_check("test-dummy"))
        self.assertEqual(registry.dispatchable_names(), ("agy-print", "claude-print", "codex-exec"))


class RefundCaptureKeyTests(unittest.TestCase):
    """The unstarted-process refund accepts the shared base key and the historical agy key."""

    MESSAGE = "Local process failure requires a bound agy capture"

    def setUp(self):
        fixture = TemporaryDirectory(prefix="mah-refund-key-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        (self.root / "state").mkdir()
        self.ledger = GrantLedger(self.root / "state" / "ledger.sqlite3")
        self.manager = RunManager(self.ledger)
        for name, refunds in (("test-refund", True), ("test-norefund", False)):
            registry.register(name, AdapterPolicy(refunds_unstarted_process=refunds))
            self.addCleanup(registry.unregister, name)

    def launching(self, adapter):
        card_id = f"card-{uuid4().hex}"
        digest = self.ledger.create_card(card_id, repo_root=str(self.root), task_key=card_id, trigger_ids=["T5"],
                                         prepare_mode="card", allowed_templates=["review"], max_dispatches=1,
                                         allowed_targets=[{"adapter": adapter, "model": "m", "effort_max": "low"}],
                                         ttl_minutes=10)
        grant_id = self.ledger.approve(card_id, digest, via="manual_cli")
        run_id = f"refund-{uuid4().hex}"
        task = b"synthetic task"
        self.ledger.check_and_reserve(grant_id, run_id, repo_root=self.root, template="review", adapter=adapter,
                                      model="m", effort="low", workspace_kind="read_only",
                                      workspace_path=self.root, task_sha256=sha256(task).hexdigest())
        self.manager.prepare(run_id, task)
        self.manager.claim_launch(run_id)
        return run_id, grant_id

    def refund(self, run_id, evidence):
        with transaction(self.ledger.database) as db:
            run = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            self.ledger._transition(db, run, "FAILED_PRE_SUBMIT", reason="process_not_started", evidence=evidence)

    def assert_refunded(self, key):
        run_id, grant_id = self.launching("test-refund")
        self.refund(run_id, {key: "a" * 64})
        run = self.ledger.get_run(run_id)
        self.assertEqual((run["state"], run["dispatch_consumed"]), ("FAILED_PRE_SUBMIT", 0))
        self.assertEqual(self.ledger.get_grant(grant_id)["used_dispatches"], 0)

    def assert_refused(self, adapter, evidence):
        run_id, grant_id = self.launching(adapter)
        with self.assertRaises(LedgerError) as error:
            self.refund(run_id, evidence)
        self.assertEqual((error.exception.code, str(error.exception)), (20, self.MESSAGE))
        self.assertEqual(self.ledger.get_run(run_id)["state"], "LAUNCHING")
        self.assertEqual(self.ledger.get_grant(grant_id)["used_dispatches"], 1)
        self.manager.ledger.transition(run_id, "LAUNCHING", "SUBMISSION_UNKNOWN")
        self.manager.resolve(run_id, action="abandon")

    def test_shared_base_capture_key_refunds(self):
        self.assert_refunded("capture_sha256")

    def test_historical_agy_capture_key_still_refunds(self):
        self.assert_refunded("agy_capture_sha256")

    def test_a_value_that_is_not_sha256_is_refused(self):
        for key in ("capture_sha256", "agy_capture_sha256"):
            for value in ("yes", "A" * 64, "a" * 63, "a" * 65, 1, True, None):
                with self.subTest(key=key, value=value):
                    self.assert_refused("test-refund", {key: value})
        self.assert_refused("test-refund", None)

    def test_a_policy_without_refunds_is_refused_with_either_key(self):
        for key in ("capture_sha256", "agy_capture_sha256"):
            with self.subTest(key=key):
                self.assert_refused("test-norefund", {key: "a" * 64})


class RefusedModelRegistrationTests(unittest.TestCase):
    """register() checks the (prefixes, reason) shape; a reason fills only {adapter} and {model}."""

    NAME = "test-refusal"

    def setUp(self):
        self.addCleanup(registry.unregister, self.NAME)

    def assert_not_registered(self):
        self.assertFalse(registry.is_registered(self.NAME))
        self.assertNotIn(self.NAME, registry.REFUSED_MODELS)

    def test_malformed_refused_models_are_refused_and_register_nothing(self):
        for value, error in (
                (("gemini-pro-orchestrator-", "single string as prefixes"), TypeError),
                ((b"gemini-pro-orchestrator-", None), TypeError),
                ((["gemini-pro-orchestrator-"], None), TypeError),
                (((), None), TypeError),
                ((("gemini-pro-orchestrator-", 7), None), TypeError),
                ((("gemini-pro-orchestrator-",), 7), TypeError),
                ((("gemini-pro-orchestrator-",), None, "extra"), TypeError),
                ([("gemini-pro-orchestrator-",), None], TypeError),
                ("gemini-pro-orchestrator-", TypeError),
                ((("",), None), ValueError),
                ((("gemini-pro-orchestrator-", ""), None), ValueError),
                ((("   ",), None), ValueError),
        ):
            with self.subTest(value=value), self.assertRaises(error):
                registry.register(self.NAME, AdapterPolicy(), factory=lambda manager, config: None,
                                  refused_models=value)
            self.assert_not_registered()
            self.assertFalse(registry.has_factory(self.NAME))
        self.assertIsNone(registry.refused_model_reason(self.NAME, "gemini-4-argon-high"))

    def test_well_formed_refused_models_block_only_their_prefixes(self):
        registry.register(self.NAME, AdapterPolicy(), refused_models=(("bad-", "Worse-"), None))
        self.assertEqual(registry.refused_model_reason(self.NAME, "bad-1"), f"{self.NAME} 不接受模型 bad-1")
        self.assertEqual(registry.refused_model_reason(self.NAME, "WORSE-2"), f"{self.NAME} 不接受模型 WORSE-2")
        for model in ("gemini-4-argon-high", "good-bad-1", "claude-sonnet-5-5-high"):
            with self.subTest(model=model):
                self.assertIsNone(registry.refused_model_reason(self.NAME, model))

    def test_reason_keeps_other_braces_and_never_raises(self):
        reason = 'json {"k": 1} {} {0} {other} {{model}} { unbalanced } and { — {adapter} refuses {model}'
        registry.register(self.NAME, AdapterPolicy(), refused_models=(("bad-",), reason))
        self.assertEqual(
            registry.refused_model_reason(self.NAME, "bad-1"),
            'json {"k": 1} {} {0} {other} {bad-1} { unbalanced } and { — test-refusal refuses bad-1')

    def test_substituted_values_are_not_expanded_again(self):
        registry.register(self.NAME, AdapterPolicy(), refused_models=(("bad-",), "{model} / {adapter}"))
        self.assertEqual(registry.refused_model_reason(self.NAME, "bad-{adapter}{0}"),
                         f"bad-{{adapter}}{{0}} / {self.NAME}")

    def test_built_in_orchestrator_reason_is_unchanged(self):
        model = "gemini-pro-orchestrator-neon"
        self.assertEqual(registry.refused_model_reason("agy-print", model),
                         registry.AGY_ORCHESTRATOR_REASON.replace("{model}", model))


if __name__ == "__main__":
    unittest.main()
