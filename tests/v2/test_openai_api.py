from contextlib import closing, redirect_stderr, redirect_stdout
from hashlib import sha256
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import errno
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from mah import cli
from mah.adapters import registry
from mah.adapters.api_providers import ApiProviderConfig
from mah.adapters.base import Adapter
from mah.adapters.openai_api import OpenAIApiAdapter, capture_matches
from mah.context_bundle import HEADER, _entry, _json_bytes, prepare_bundle
from mah.ledger import GrantLedger, LedgerError
from mah.preflight import adapter_checks, preflight
from mah.run_manager import RunManager
from mah.verifier import VerificationEvidence, Verifier


class _FakeOpenAIServer:
    def __init__(self):
        self.requests = []
        self.routes = {}
        self.default_handler = self._default_response
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def _dispatch(self, method):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b""
                outer.requests.append({
                    "method": method,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                })
                handler = outer.routes.get((method, self.path), outer.default_handler)
                handler(self, body)

            def log_message(self, fmt, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _default_response(self, req, body):
        if req.path == "/v1/models":
            self.send_json(req, 200, {"object": "list", "data": [{"id": "qwen2.5-7b-instruct"}, {"id": "llama3:8b"}]})
            return
        req.send_response(404)
        req.send_header("Content-Length", "0")
        req.end_headers()

    @staticmethod
    def send_json(req, status, payload, extra_headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req.send_response(status)
        req.send_header("Content-Type", "application/json")
        if extra_headers:
            for k, v in extra_headers.items():
                req.send_header(k, v)
        req.send_header("Content-Length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class OpenAIApiAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = TemporaryDirectory(prefix="mah-openai-api-")
        cls.addClassCleanup(scratch.cleanup)
        cls.scratch = Path(scratch.name)
        cls.repo = cls.scratch / "repo"
        cls.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(cls.repo)], check=True, timeout=15)
        subprocess.run(["git", "-C", str(cls.repo), "config", "user.email", "test@example.com"], check=True, timeout=15)
        subprocess.run(["git", "-C", str(cls.repo), "config", "user.name", "Test"], check=True, timeout=15)
        (cls.repo / "sample.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cls.repo), "add", "sample.py"], check=True, timeout=15)
        subprocess.run(["git", "-C", str(cls.repo), "commit", "-q", "-m", "init"], check=True, timeout=15)
        instr_path = cls.scratch / "instructions.txt"
        instr_path.write_text("請審查 sample.py，回傳單一結果 JSON。", encoding="utf-8")
        bundle_dir = cls.scratch / "bundle-out"
        prepare_bundle(cls.repo, ["sample.py"], instr_path, bundle_dir, max_bytes=32768)
        cls.bundle_task = (bundle_dir / "task.md").read_bytes()

    def setUp(self):
        case_dir = TemporaryDirectory(prefix="case-", dir=self.scratch)
        self.addCleanup(case_dir.cleanup)
        self.root = Path(case_dir.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.worktrees = self.root / "worktrees"
        self.worktrees.mkdir()
        self.ledger = GrantLedger(self.state / "ledger.sqlite3")
        self.manager = RunManager(self.ledger)
        self.server = _FakeOpenAIServer()
        self.addCleanup(self.server.close)
        self.lmstudio = ApiProviderConfig(
            name="lmstudio",
            kind="lmstudio",
            base_url=f"http://127.0.0.1:{self.server.port}/v1",
            scheme="http",
            host="127.0.0.1",
            port=self.server.port,
            base_path="/v1",
            api_key_env=None,
            timeout_s=None,
            max_output_tokens=4096,
            connect_timeout_s=5,
            reasoning_effort=False,
            structured_output="json_schema_strict",
            local=True,
        )
        self.ollama = ApiProviderConfig(
            name="ollama",
            kind="ollama",
            base_url=f"http://127.0.0.1:{self.server.port}/v1",
            scheme="http",
            host="127.0.0.1",
            port=self.server.port,
            base_path="/v1",
            api_key_env=None,
            timeout_s=None,
            max_output_tokens=4096,
            connect_timeout_s=5,
            reasoning_effort=False,
            structured_output="json_object",
            local=True,
        )
        self.providers = {"lmstudio": self.lmstudio, "ollama": self.ollama}
        self.adapter = OpenAIApiAdapter(self.manager, providers=self.providers)

    def make_workspace(self):
        ws = self.worktrees / f"bundle-{uuid4().hex}"
        ws.mkdir(parents=True)
        return ws

    def make_grant(
        self,
        *,
        model="lmstudio:qwen2.5-7b-instruct",
        effort_max="medium",
        template="review",
        task=None,
        workspace=None,
        timeout_s=900,
        max_dispatches=1,
        via="codex_prompt",
    ):
        task_bytes = self.bundle_task if task is None else task
        ws = self.make_workspace() if workspace is None else workspace
        card_id = f"card-{uuid4().hex}"
        digest = self.ledger.create_card(
            card_id,
            repo_root=str(self.repo),
            task_key=card_id,
            trigger_ids=["T2"],
            prepare_mode="card",
            allowed_templates=[template],
            allowed_targets=[{"adapter": "openai-api", "model": model, "effort_max": effort_max}],
            max_dispatches=max_dispatches,
            ttl_minutes=15,
            enabled_adapters=["openai-api"],
            timeout_seconds=timeout_s,
            workspace_path=str(ws),
            task_sha256=sha256(task_bytes).hexdigest(),
        )
        grant_id = self.ledger.approve(card_id, digest, via=via)
        return card_id, digest, grant_id, ws

    @staticmethod
    def valid_result_dict(run_id, template="review", status="completed", summary="審查完成，無問題。"):
        return {
            "schema_version": "2.0",
            "run_id": run_id,
            "template": template,
            "status": status,
            "summary": summary,
            "blocked_reason": "缺少權限" if status == "blocked" else None,
            "open_questions": [],
            "template_payload": {"findings": []},
        }

    def install_chat_completion(self, *, status=200, result_override=None, envelope_override=None, raw_bytes=None):
        def handle(req, body):
            if raw_bytes is not None:
                req.send_response(status)
                req.send_header("Content-Type", "application/json")
                req.send_header("Content-Length", str(len(raw_bytes)))
                req.end_headers()
                req.wfile.write(raw_bytes)
                return
            req_json = json.loads(body.decode("utf-8"))
            user_text = req_json["messages"][1]["content"]
            first_line = user_text.splitlines()[0]
            run_id = first_line.split("run_id=", 1)[1].split(",", 1)[0]
            template = first_line.split("template=", 1)[1].strip()
            result_obj = self.valid_result_dict(run_id, template=template)
            if result_override:
                result_obj.update(result_override)
            envelope = {
                "id": "chatcmpl-fixed-123",
                "object": "chat.completion",
                "model": req_json["model"] + "-observed",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(result_obj, ensure_ascii=False),
                            "tool_calls": None,
                            "function_call": None,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 42, "completion_tokens": 18, "total_tokens": 60},
            }
            if envelope_override:
                envelope.update(envelope_override)
            self.server.send_json(req, status, envelope, extra_headers={"Set-Cookie": "secret=1", "X-Request-Id": "r-1"})

        self.server.routes[("POST", "/v1/chat/completions")] = handle

    def test_protocol_policy_flags_workspace_support_and_never_calls_mark_running(self):
        self.assertIsInstance(self.adapter, Adapter)
        self.assertFalse(hasattr(self.adapter, "find_executable"))
        self.assertIsNone(registry.executable_lookup("openai-api"))
        self.assertEqual(registry.workspace_support("openai-api"), frozenset({"bundle"}))
        self.assertEqual(registry.capture_reason("openai-api"), "api_capture_integrity")

        policy = registry.policy_for("openai-api")
        self.assertTrue(policy.user_configurable)
        self.assertTrue(policy.requires_dispatch_scope)
        self.assertTrue(policy.attended_approval_only)
        self.assertTrue(policy.template_verified)
        self.assertTrue(policy.refunds_unstarted_process)
        self.assertTrue(policy.closes_grant_on_refund)
        self.assertEqual(policy.run_id_prefix, "api")
        self.assertFalse(policy.replays_structured_output)
        self.assertFalse(policy.allows_delegate_implement)
        self.assertFalse(policy.gui_submission)
        self.assertFalse(policy.window_approval)

        self.install_chat_completion()
        _, _, grant_id, ws = self.make_grant()
        run_id = f"api-{uuid4().hex}"
        with patch.object(self.manager, "mark_running", side_effect=AssertionError("mark_running must not be called")):
            view = self.manager.dispatch(
                self.adapter,
                grant_id=grant_id,
                run_id=run_id,
                repo_root=self.repo,
                template="review",
                model="lmstudio:qwen2.5-7b-instruct",
                effort="medium",
                task=self.bundle_task,
                workspace=ws,
                worktree_root=self.worktrees,
                timeout_s=900,
            )
        self.assertEqual(view["state"], "COLLECTING")

    def test_input_guards_reject_worktree_v1_bundle_non_readonly_template_and_multi_dispatch_grant(self):
        ws = self.make_workspace()
        # 1. Non-read-only template -> exit code 17
        with self.assertRaises(LedgerError) as ctx_tpl:
            self.adapter.prepare_invocation(
                run_id=f"api-{uuid4().hex}",
                repo_root=self.repo,
                template="delegate-implement",
                model="lmstudio:qwen2.5-7b-instruct",
                effort="medium",
                task=self.bundle_task,
                workspace=ws,
                worktree_root=self.worktrees,
                timeout_s=900,
                requirements=None,
            )
        self.assertEqual(ctx_tpl.exception.code, 17)

        # 2. Non-bundle (Worktree) task -> exit code 17
        with self.assertRaises(LedgerError) as ctx_wt:
            self.adapter.prepare_invocation(
                run_id=f"api-{uuid4().hex}",
                repo_root=self.repo,
                template="review",
                model="lmstudio:qwen2.5-7b-instruct",
                effort="medium",
                task=b"Plain worktree task without bundle manifest",
                workspace=ws,
                worktree_root=self.worktrees,
                timeout_s=900,
                requirements=None,
            )
        self.assertEqual(ctx_wt.exception.code, 17)

        # 3. Manifest v1 bundle -> exit code 17
        v1_file_text = "def add(a, b):\n    return a + b\n"
        v1_manifest = {
            "version": 1,
            "source": "git_head",
            "head": "a" * 40,
            "files": [_entry("sample.py", v1_file_text)],
        }
        v1_task = HEADER + _json_bytes({
            "instructions": "請審查 sample.py",
            "manifest": v1_manifest,
            "manifest_sha256": sha256(_json_bytes(v1_manifest)).hexdigest(),
            "files": {"sample.py": v1_file_text},
        })
        with self.assertRaises(LedgerError) as ctx_v1:
            self.adapter.prepare_invocation(
                run_id=f"api-{uuid4().hex}",
                repo_root=self.repo,
                template="review",
                model="lmstudio:qwen2.5-7b-instruct",
                effort="medium",
                task=v1_task,
                workspace=ws,
                worktree_root=self.worktrees,
                timeout_s=900,
                requirements=None,
            )
        self.assertEqual(ctx_v1.exception.code, 17)

        # 4. Grant with max_dispatches != 1 -> exit code 10 before claim_launch
        _, _, grant_multi, ws_multi = self.make_grant(max_dispatches=2, via="manual_cli")
        run_multi = f"api-{uuid4().hex}"
        with self.assertRaises(LedgerError) as ctx_multi:
            self.manager.dispatch(
                self.adapter,
                grant_id=grant_multi,
                run_id=run_multi,
                repo_root=self.repo,
                template="review",
                model="lmstudio:qwen2.5-7b-instruct",
                effort="medium",
                task=self.bundle_task,
                workspace=ws_multi,
                worktree_root=self.worktrees,
                timeout_s=900,
            )
        self.assertEqual(ctx_multi.exception.code, 10)
        self.assertEqual(self.ledger.get_run(run_multi)["state"], "CANCELLED")
        self.assertEqual(self.ledger.get_grant(grant_multi)["status"], "CLOSED")

    def test_full_offline_dispatch_verify_and_accept_for_both_output_modes_and_duplicate_response_ids(self):
        self.install_chat_completion()
        for provider_name, model_id, expected_mode in (
            ("lmstudio", "lmstudio:qwen2.5-7b-instruct", "json_schema_strict"),
            ("ollama", "ollama:llama3:8b", "json_object"),
        ):
            with self.subTest(provider=provider_name):
                _, _, grant_id, ws = self.make_grant(model=model_id)
                run_id = f"api-{uuid4().hex}"
                view = self.manager.dispatch(
                    self.adapter,
                    grant_id=grant_id,
                    run_id=run_id,
                    repo_root=self.repo,
                    template="review",
                    model=model_id,
                    effort="medium",
                    task=self.bundle_task,
                    workspace=ws,
                    worktree_root=self.worktrees,
                    timeout_s=900,
                )
                self.assertEqual((view["state"], view["state_reason"]), ("COLLECTING", "structured_result_collected"))
                run_row = self.ledger.get_run(run_id)
                self.assertIsNone(run_row["pid"])
                self.assertIsNone(run_row["exit_code"])
                self.assertIsNone(run_row["cli_version"])
                self.assertEqual(run_row["conversation_id"], f"api:{run_id}")
                self.assertTrue(run_row["model_observed"].endswith("-observed"))

                directory = self.manager._directory(run_id)
                for filename in (
                    "task.md",
                    "invocation.json",
                    "api.request.json",
                    "api.response.raw",
                    "api.process.json",
                    "api.capture.json",
                    "events.jsonl",
                    "result.json",
                    "DONE",
                ):
                    self.assertTrue((directory / filename).is_file(), f"Missing {filename}")

                invocation = json.loads((directory / "invocation.json").read_bytes())
                self.assertNotIn("cli_version", invocation)
                self.assertEqual(invocation["workspace_kind"], "read_only")
                self.assertEqual(invocation["structured_output"], expected_mode)
                self.assertEqual(
                    invocation["request_sha256"],
                    sha256((directory / "api.request.json").read_bytes()).hexdigest(),
                )

                req_body = json.loads((directory / "api.request.json").read_bytes())
                self.assertTrue(
                    set(req_body) <= {"model", "messages", "response_format", "max_tokens", "stream",
                                      "temperature", "reasoning_effort", "provider"}
                )
                for forbidden in ("tools", "tool_choice", "functions", "function_call", "plugins", "web_search_options"):
                    self.assertNotIn(forbidden, req_body)
                if expected_mode == "json_schema_strict":
                    self.assertEqual(req_body["response_format"]["type"], "json_schema")
                else:
                    self.assertEqual(req_body["response_format"], {"type": "json_object"})
                    self.assertIn("json", req_body["messages"][0]["content"].lower())

                capture_doc = json.loads((directory / "api.capture.json").read_bytes())
                self.assertIsNone(capture_doc["exit_code"])
                self.assertEqual(capture_doc["http_status"], 200)
                self.assertEqual(capture_doc["observation"]["conversation_id"], f"api:{run_id}")
                self.assertEqual(capture_doc["observation"]["response_id"], "chatcmpl-fixed-123")

                result_sha = sha256((directory / "result.json").read_bytes()).hexdigest()
                self.assertTrue(capture_matches(directory, self.ledger.get_run(run_id), result_sha))

                evidence = VerificationEvidence(
                    task_sha256=sha256(self.bundle_task).hexdigest(),
                    result_sha256=result_sha,
                    bundle_manifest_sha256=invocation["bundle_manifest_sha256"],
                    confirmed=True,
                    workspace_changed=False,
                    reviewed_findings=0,
                )
                verified = Verifier(self.manager).verify(run_id, evidence=evidence)
                self.assertEqual((verified["state"], verified["verification"]["decision"]), ("SUCCEEDED", "verified"))
                accepted = self.manager.record_acceptance(run_id, accepted=True, reason="ok", adopted=0, total=0)
                self.assertEqual(accepted["acceptance_status"], "ACCEPTED")

    def test_api_key_precheck_post_claim_disappearance_and_zero_leak_scan(self):
        secret = "sk-never-persist-secret-key-9988776655"
        key_provider = ApiProviderConfig(
            name="lmstudio",
            kind="lmstudio",
            base_url=f"http://127.0.0.1:{self.server.port}/v1",
            scheme="http",
            host="127.0.0.1",
            port=self.server.port,
            base_path="/v1",
            api_key_env="TEST_LMSTUDIO_SECRET_KEY",
            timeout_s=None,
            max_output_tokens=4096,
            connect_timeout_s=5,
            reasoning_effort=False,
            structured_output="json_schema_strict",
            local=True,
        )
        adapter = OpenAIApiAdapter(self.manager, providers={"lmstudio": key_provider})

        # 1. Missing/empty/invalid key before reservation -> exit code 14, 0 Runs, Grant untouched
        for bad_env in ({}, {"TEST_LMSTUDIO_SECRET_KEY": ""}, {"TEST_LMSTUDIO_SECRET_KEY": "bad\nkey"}):
            with self.subTest(bad_env=bad_env), patch.dict(os.environ, bad_env, clear=False):
                if not bad_env:
                    os.environ.pop("TEST_LMSTUDIO_SECRET_KEY", None)
                _, _, grant_id, ws = self.make_grant()
                run_id = f"api-{uuid4().hex}"
                with self.assertRaises(LedgerError) as ctx:
                    self.manager.dispatch(
                        adapter,
                        grant_id=grant_id,
                        run_id=run_id,
                        repo_root=self.repo,
                        template="review",
                        model="lmstudio:qwen2.5-7b-instruct",
                        effort="medium",
                        task=self.bundle_task,
                        workspace=ws,
                        worktree_root=self.worktrees,
                        timeout_s=900,
                    )
                self.assertEqual(ctx.exception.code, 14)
                self.assertIsNone(self.ledger.get_run(run_id))
                self.assertEqual(self.ledger.get_grant(grant_id)["used_dispatches"], 0)
                self.assertEqual(self.ledger.get_grant(grant_id)["status"], "ACTIVE")

        # 2. Key disappears after claim_launch -> FAILED_PRE_SUBMIT / auth_missing, Grant refunded & CLOSED
        _, _, grant_disappear, ws_disappear = self.make_grant()
        run_disappear = f"api-{uuid4().hex}"
        orig_claim = self.manager.claim_launch

        def claim_and_drop_key(*args, **kwargs):
            res = orig_claim(*args, **kwargs)
            os.environ.pop("TEST_LMSTUDIO_SECRET_KEY", None)
            return res

        with patch.dict(os.environ, {"TEST_LMSTUDIO_SECRET_KEY": secret}, clear=False), patch.object(
            self.manager, "claim_launch", side_effect=claim_and_drop_key
        ):
            view = self.manager.dispatch(
                adapter,
                grant_id=grant_disappear,
                run_id=run_disappear,
                repo_root=self.repo,
                template="review",
                model="lmstudio:qwen2.5-7b-instruct",
                effort="medium",
                task=self.bundle_task,
                workspace=ws_disappear,
                worktree_root=self.worktrees,
                timeout_s=900,
            )
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED_PRE_SUBMIT", "auth_missing"))
        self.assertEqual(self.ledger.get_grant(grant_disappear)["used_dispatches"], 0)
        self.assertEqual(self.ledger.get_grant(grant_disappear)["status"], "CLOSED")

        # 3. Full dispatch + preflight where server echoes the secret in the response -> scan all files & DB & outputs
        self.install_chat_completion(result_override={"summary": f"Echoed secret {secret} in summary"})
        _, _, grant_leak, ws_leak = self.make_grant()
        run_leak = f"api-{uuid4().hex}"
        cfg_path = self.root / "harness.local.toml"
        cfg_path.write_text(
            f'[paths]\nstate_dir = "state"\nworktree_root = "worktrees"\n'
            f'[adapters]\nenabled = ["openai-api"]\n'
            f'[api.providers.lmstudio]\nkind = "lmstudio"\n'
            f'base_url = "http://127.0.0.1:{self.server.port}/v1"\n'
            f'api_key_env = "TEST_LMSTUDIO_SECRET_KEY"\n',
            encoding="utf-8",
        )
        out_buf, err_buf = StringIO(), StringIO()
        with patch.dict(os.environ, {"TEST_LMSTUDIO_SECRET_KEY": secret}, clear=False), redirect_stdout(
            out_buf
        ), redirect_stderr(err_buf):
            pf_report = preflight(self.repo, "openai-api", cfg_path, model="lmstudio:qwen2.5-7b-instruct")
            view_leak = self.manager.dispatch(
                adapter,
                grant_id=grant_leak,
                run_id=run_leak,
                repo_root=self.repo,
                template="review",
                model="lmstudio:qwen2.5-7b-instruct",
                effort="medium",
                task=self.bundle_task,
                workspace=ws_leak,
                worktree_root=self.worktrees,
                timeout_s=900,
            )
        self.assertTrue(pf_report["ok"])
        self.assertEqual(view_leak["state"], "COLLECTING")
        secret_bytes = secret.encode("utf-8")
        secret_sha = sha256(secret_bytes).hexdigest().encode("utf-8")
        for path in self.root.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                self.assertNotIn(secret_bytes, raw, f"Secret leaked in {path}")
                self.assertNotIn(secret_sha, raw, f"Secret hash leaked in {path}")
        self.assertNotIn(secret, out_buf.getvalue())
        self.assertNotIn(secret, err_buf.getvalue())
        self.assertNotIn(secret, json.dumps(pf_report))
        self.assertNotIn(secret, json.dumps(view_leak))

    def test_section_6_2_table_every_row_and_grant_closing(self):
        def run_case(*, connection_factory=None, status=200, envelope_override=None, result_override=None,
                     raw_bytes=None, mutate_workspace=False, fail_post_snapshot=False):
            self.install_chat_completion(
                status=status,
                envelope_override=envelope_override,
                result_override=result_override,
                raw_bytes=raw_bytes,
            )
            card_id, digest, grant_id, ws = self.make_grant()
            run_id = f"api-{uuid4().hex}"
            adapter = OpenAIApiAdapter(
                self.manager,
                providers=self.providers,
                connection_factory=connection_factory,
            )
            if mutate_workspace:
                orig_exec = adapter._transport

                def mutating_transport(*args, **kwargs):
                    res = orig_exec(*args, **kwargs)
                    (ws / "dirty.txt").write_text("modified", encoding="utf-8")
                    return res

                adapter._transport = mutating_transport
            if fail_post_snapshot:
                calls = []
                orig_snap = adapter._snapshot

                def failing_after_snapshot(plan):
                    calls.append(1)
                    if len(calls) >= 3:
                        raise OSError("snapshot failed after request")
                    return orig_snap(plan)

                adapter._snapshot = failing_after_snapshot

            before_reqs = len(self.server.requests)
            view = self.manager.dispatch(
                adapter,
                grant_id=grant_id,
                run_id=run_id,
                repo_root=self.repo,
                template="review",
                model="lmstudio:qwen2.5-7b-instruct",
                effort="medium",
                task=self.bundle_task,
                workspace=ws,
                worktree_root=self.worktrees,
                timeout_s=900,
            )
            req_delta = len(self.server.requests) - before_reqs
            return card_id, digest, grant_id, run_id, view, req_delta

        # 1. Connect refused -> FAILED_PRE_SUBMIT / process_not_started, Grant refunded & CLOSED
        class RefusedConn:
            sock = None
            def connect(self):
                err = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
                err.winerror = 10061
                raise err
            def close(self):
                pass

        card_id, digest, grant_id, run_id, view, _ = run_case(
            connection_factory=lambda s, h, p, t: RefusedConn()
        )
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED_PRE_SUBMIT", "process_not_started"))
        self.assertEqual(view["api_capture"]["launch_error"], "request_not_sent")
        grant_row = self.ledger.get_grant(grant_id)
        self.assertEqual((grant_row["status"], grant_row["used_dispatches"]), ("CLOSED", 0))
        with self.assertRaises(LedgerError) as reuse_err:
            self.manager.dispatch(
                self.adapter, grant_id=grant_id, run_id=f"api-{uuid4().hex}", repo_root=self.repo,
                template="review", model="lmstudio:qwen2.5-7b-instruct", effort="medium",
                task=self.bundle_task, workspace=self.make_workspace(), worktree_root=self.worktrees, timeout_s=900,
            )
        self.assertEqual(reuse_err.exception.code, 12)
        with self.assertRaises(LedgerError) as reapprove_err:
            self.ledger.approve(card_id, digest, via="codex_prompt")
        self.assertEqual(reapprove_err.exception.code, 10)
        with self.assertRaises(LedgerError) as resolve_err:
            self.manager.resolve(run_id, action="mark_failed", reason="cannot resolve terminal")
        self.assertEqual(resolve_err.exception.code, 20)

        # 2. Connect Ctrl+C (KeyboardInterrupt) -> FAILED_PRE_SUBMIT / process_not_started, Grant CLOSED
        class ConnectCtrlCConn:
            sock = None
            def connect(self):
                raise KeyboardInterrupt
            def close(self):
                pass

        _, _, grant_id, _, view, _ = run_case(connection_factory=lambda s, h, p, t: ConnectCtrlCConn())
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED_PRE_SUBMIT", "process_not_started"))
        self.assertEqual(self.ledger.get_grant(grant_id)["status"], "CLOSED")

        # 3. Disconnect after send -> SUBMISSION_UNKNOWN / response_unknown
        class DisconnectAfterSendConn:
            sock = None
            def connect(self):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                raise ConnectionResetError(errno.ECONNRESET, "reset after send")
            def close(self):
                pass

        _, _, grant_id, run_id, view, _ = run_case(connection_factory=lambda s, h, p, t: DisconnectAfterSendConn())
        self.assertEqual((view["state"], view["state_reason"]), ("SUBMISSION_UNKNOWN", "response_unknown"))
        self.assertEqual(self.ledger.get_grant(grant_id)["used_dispatches"], 1)
        self.manager.resolve(run_id, action="abandon")

        # 4. IncompleteRead during body read -> SUBMISSION_UNKNOWN / response_unknown
        class IncompleteBodyConn(DisconnectAfterSendConn):
            def getresponse(self):
                class Resp:
                    status = 200
                    def getheaders(self):
                        return [("Content-Type", "application/json")]
                    def read(self, amt=None):
                        raise IncompleteRead(b'{"id":"1"', 50)
                return Resp()

        _, _, _, run_id, view, _ = run_case(connection_factory=lambda s, h, p, t: IncompleteBodyConn())
        self.assertEqual((view["state"], view["state_reason"]), ("SUBMISSION_UNKNOWN", "response_unknown"))
        self.manager.resolve(run_id, action="abandon")

        # 5. Timeout after send -> SUBMISSION_UNKNOWN / request_timed_out
        class TimeoutAfterSendConn(DisconnectAfterSendConn):
            def getresponse(self):
                raise TimeoutError("read timed out")

        _, _, _, run_id, view, _ = run_case(connection_factory=lambda s, h, p, t: TimeoutAfterSendConn())
        self.assertEqual((view["state"], view["state_reason"]), ("SUBMISSION_UNKNOWN", "request_timed_out"))
        self.manager.resolve(run_id, action="abandon")

        # 6. Ctrl+C after send -> SUBMISSION_UNKNOWN / response_unknown
        class CtrlCAfterSendConn(DisconnectAfterSendConn):
            def getresponse(self):
                raise KeyboardInterrupt

        _, _, _, run_id, view, _ = run_case(connection_factory=lambda s, h, p, t: CtrlCAfterSendConn())
        self.assertEqual((view["state"], view["state_reason"]), ("SUBMISSION_UNKNOWN", "response_unknown"))
        self.manager.resolve(run_id, action="abandon")

        # 7. HTTP status codes
        status_cases = (
            (401, b'{"error":"unauthorized"}', "FAILED_PRE_SUBMIT", "auth_missing", True),
            (403, b'{"error":"forbidden"}', "FAILED_PRE_SUBMIT", "auth_missing", True),
            (404, b'{"error":{"message":"Model \'qwen\' not found","code":"model_not_found"}}',
             "FAILED_PRE_SUBMIT", "model_not_found", True),
            (404, b"404 page not found", "FAILED", "api_request_invalid", False),
            (400, b'{"error":"bad request"}', "FAILED", "api_request_invalid", False),
            (405, b'{"error":"method not allowed"}', "FAILED", "api_request_invalid", False),
            (408, b'{"error":"request timeout"}', "FAILED", "api_request_invalid", False),
            (413, b'{"error":"payload too large"}', "FAILED", "api_request_invalid", False),
            (422, b'{"error":"unprocessable"}', "FAILED", "api_request_invalid", False),
            (402, b'{"error":"insufficient balance"}', "FAILED", "api_insufficient_balance", False),
            (429, b'{"error":"rate limit"}', "FAILED", "api_rate_limited", False),
            (500, b'{"error":"internal error"}', "FAILED", "api_server_error", False),
            (522, b'{"error":"origin timeout"}', "FAILED", "api_server_error", False),
            (302, b"", "FAILED", "api_redirect_refused", False),
        )
        for code, raw, exp_state, exp_reason, refunded in status_cases:
            with self.subTest(http_status=code, raw=raw):
                _, _, grant_id, run_id, view, req_delta = run_case(status=code, raw_bytes=raw)
                self.assertEqual((view["state"], view["state_reason"]), (exp_state, exp_reason))
                self.assertEqual(req_delta, 1)
                self.assertIsNone(self.ledger.get_run(run_id)["conversation_id"])
                self.assertIsNone(self.ledger.get_run(run_id)["exit_code"])
                grant_row = self.ledger.get_grant(grant_id)
                if refunded:
                    self.assertEqual((grant_row["status"], grant_row["used_dispatches"]), ("CLOSED", 0))
                else:
                    self.assertEqual((grant_row["status"], grant_row["used_dispatches"]), ("EXHAUSTED", 1))

        # 8. Oversized response (> 4 MB) -> FAILED / api_response_too_large
        class HugeBodyConn(DisconnectAfterSendConn):
            def getresponse(self):
                class HugeResp:
                    status = 200
                    remaining = 4 * 1024 * 1024 + 512
                    def getheaders(self):
                        return [("Content-Type", "application/json")]
                    def read(self, amt=65536):
                        if self.remaining <= 0:
                            return b""
                        chunk = min(amt, self.remaining)
                        self.remaining -= chunk
                        return b"a" * chunk
                return HugeResp()

        _, _, _, _, view, _ = run_case(connection_factory=lambda s, h, p, t: HugeBodyConn())
        self.assertEqual((view["state"], view["state_reason"]), ("FAILED", "api_response_too_large"))

        # 9. HTTP 200 with error in body or missing choices -> FAILED / api_error_in_body
        _, _, _, _, view_err, _ = run_case(envelope_override={"error": {"message": "provider failed"}})
        self.assertEqual((view_err["state"], view_err["state_reason"]), ("FAILED", "api_error_in_body"))
        _, _, _, _, view_no_choices, _ = run_case(envelope_override={"choices": []})
        self.assertEqual((view_no_choices["state"], view_no_choices["state_reason"]), ("FAILED", "api_error_in_body"))

        # 10. HTTP 200 with tool_calls or function_call -> FAILED / api_tool_call_observed; empty/null allowed
        for bad_choice in (
            {"message": {"role": "assistant", "content": "{}", "tool_calls": [{"id": "1"}]}, "finish_reason": "stop"},
            {"message": {"role": "assistant", "content": "{}", "function_call": {"name": "fn"}}, "finish_reason": "stop"},
            {"message": {"role": "assistant", "content": "{}"}, "finish_reason": "tool_calls"},
            {"message": {"role": "assistant", "content": "{}"}, "finish_reason": "function_call"},
        ):
            with self.subTest(bad_choice=bad_choice):
                _, _, _, _, view_tc, _ = run_case(envelope_override={"choices": [bad_choice]})
                self.assertEqual((view_tc["state"], view_tc["state_reason"]), ("FAILED", "api_tool_call_observed"))

        # Empty tool_calls=[] and function_call=None must NOT be rejected
        def allow_empty_tools(req, body):
            req_json = json.loads(body.decode("utf-8"))
            first_line = req_json["messages"][1]["content"].splitlines()[0]
            r_id = first_line.split("run_id=", 1)[1].split(",", 1)[0]
            res_obj = self.valid_result_dict(r_id)
            self.server.send_json(req, 200, {
                "id": "chatcmpl-empty-tools",
                "model": "qwen-observed",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "```json\n" + json.dumps(res_obj, ensure_ascii=False) + "\n```",
                        "tool_calls": [],
                        "function_call": None,
                    },
                    "finish_reason": "stop",
                }],
            })

        self.server.routes[("POST", "/v1/chat/completions")] = allow_empty_tools
        _, _, grant_ok, _, view_ok, _ = (lambda: (
            None, None, *self.make_grant()[2:], None, None
        ))()
        run_ok = f"api-{uuid4().hex}"
        view_ok = self.manager.dispatch(
            self.adapter, grant_id=grant_ok, run_id=run_ok, repo_root=self.repo, template="review",
            model="lmstudio:qwen2.5-7b-instruct", effort="medium", task=self.bundle_task,
            workspace=Path(json.loads(self.ledger.get_grant(grant_ok)["dispatch_scope_json"])["workspace_path"]),
            worktree_root=self.worktrees, timeout_s=900,
        )
        self.assertEqual((view_ok["state"], view_ok["state_reason"]), ("COLLECTING", "structured_result_collected"))
        self.manager.resolve(run_ok, action="mark_failed", reason="close collecting run before next subcase")

        # 11. finish_reason == "length" -> FAILED / output_truncated
        _, _, _, _, view_len, _ = run_case(
            envelope_override={"choices": [{"index": 0, "message": {"role": "assistant", "content": "{}"}, "finish_reason": "length"}]}
        )
        self.assertEqual((view_len["state"], view_len["state_reason"]), ("FAILED", "output_truncated"))

        # 12. Blank content or <think> prefix -> FAILED / schema_invalid (and reasoning_field_present recorded)
        _, _, _, _, view_blank, _ = run_case(
            envelope_override={"choices": [{"index": 0, "message": {"role": "assistant", "content": "   "}, "finish_reason": "stop"}]}
        )
        self.assertEqual((view_blank["state"], view_blank["state_reason"]), ("FAILED", "schema_invalid"))

        _, _, _, _, view_think, _ = run_case(
            envelope_override={"choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "<think>考慮中</think>\n{}", "reasoning_content": "thought"},
                "finish_reason": "stop",
            }]}
        )
        self.assertEqual((view_think["state"], view_think["state_reason"]), ("FAILED", "schema_invalid"))
        self.assertTrue(view_think["api_capture"]["observation"]["reasoning_field_present"])

        # 13. Result status == "failed" or "blocked" -> FAILED / worker_failed or worker_blocked, no result.json or DONE
        for worker_status in ("failed", "blocked"):
            with self.subTest(worker_status=worker_status):
                _, _, _, run_st, view_st, _ = run_case(result_override={"status": worker_status})
                self.assertEqual((view_st["state"], view_st["state_reason"]), ("FAILED", f"worker_{worker_status}"))
                dir_st = self.manager._directory(run_st)
                self.assertFalse((dir_st / "result.json").exists())
                self.assertFalse((dir_st / "DONE").exists())

        # 14. Bundle workspace modified -> FAILED / readonly_violation
        _, _, _, _, view_dirty, _ = run_case(mutate_workspace=True)
        self.assertEqual((view_dirty["state"], view_dirty["state_reason"]), ("FAILED", "readonly_violation"))

        # 15. Post-request workspace snapshot fails -> COLLECTING / bundle_workspace_evidence_missing, resolvable via --mark-failed
        _, _, _, run_ms, view_ms, _ = run_case(fail_post_snapshot=True)
        self.assertEqual((view_ms["state"], view_ms["state_reason"]), ("COLLECTING", "bundle_workspace_evidence_missing"))
        self.assertFalse((self.manager._directory(run_ms) / "DONE").exists())
        resolved_ms = self.manager.resolve(run_ms, action="mark_failed", reason="missing workspace snapshot")
        self.assertEqual(resolved_ms["state"], "FAILED")

    def test_interruption_reconcile_recollect_idempotency_and_damaged_receipt_recovery(self):
        self.install_chat_completion()

        # 1. Process killed after claim_launch before api.process.json -> _reconcile -> SUBMISSION_UNKNOWN / process_gone -> resolve --mark-failed
        _, _, grant_kill, ws_kill = self.make_grant()
        run_kill = f"api-{uuid4().hex}"
        plan = self.adapter.prepare_invocation(
            run_id=run_kill, repo_root=self.repo, template="review", model="lmstudio:qwen2.5-7b-instruct",
            effort="medium", task=self.bundle_task, workspace=ws_kill, worktree_root=self.worktrees,
            timeout_s=900, requirements=None,
        )
        self.ledger.check_and_reserve(
            grant_kill, run_kill, repo_root=self.repo, template="review", adapter="openai-api",
            model="lmstudio:qwen2.5-7b-instruct", effort="medium", workspace_kind="read_only",
            workspace_path=ws_kill, task_sha256=sha256(self.bundle_task).hexdigest(), timeout_seconds=900,
        )
        self.manager.prepare(run_kill, self.bundle_task)
        self.manager.claim_launch(run_kill, invocation=plan.metadata)

        dead_manager = RunManager(self.ledger, process_probe=lambda pid: ("gone", None))
        dead_adapter = OpenAIApiAdapter(dead_manager, providers=self.providers)
        with self.assertRaises(LedgerError) as replay_err:
            dead_adapter.collect_saved(run_kill, replay_output=True)
        self.assertEqual(replay_err.exception.code, 20)

        reconciled = dead_adapter.collect_saved(run_kill)
        self.assertEqual((reconciled["state"], reconciled["state_reason"]), ("SUBMISSION_UNKNOWN", "process_gone"))
        resolved = dead_manager.resolve(run_kill, action="mark_failed", reason="killed before process receipt")
        self.assertEqual(resolved["state"], "FAILED")

        # 2. collect_saved idempotency (does not hit server again) and damaged receipt recovery via cli resolve --mark-failed
        _, _, grant_ok, ws_ok = self.make_grant()
        run_ok = f"api-{uuid4().hex}"
        view = self.manager.dispatch(
            self.adapter, grant_id=grant_ok, run_id=run_ok, repo_root=self.repo, template="review",
            model="lmstudio:qwen2.5-7b-instruct", effort="medium", task=self.bundle_task,
            workspace=ws_ok, worktree_root=self.worktrees, timeout_s=900,
        )
        self.assertEqual(view["state"], "COLLECTING")
        req_count = len(self.server.requests)
        recollected = self.adapter.collect_saved(run_ok)
        self.assertEqual(recollected["state"], "COLLECTING")
        self.assertEqual(len(self.server.requests), req_count)

        # Tamper with api.response.raw -> capture_matches is False, collect_saved raises LedgerError(30), resolve --mark-failed succeeds
        directory = self.manager._directory(run_ok)
        result_sha = sha256((directory / "result.json").read_bytes()).hexdigest()
        (directory / "api.response.raw").write_bytes(b'{"tampered": true}')
        self.assertFalse(capture_matches(directory, self.ledger.get_run(run_ok), result_sha))
        with self.assertRaises(LedgerError) as tamper_err:
            self.adapter.collect_saved(run_ok)
        self.assertEqual(tamper_err.exception.code, 30)

        cfg_path = self.root / "harness.local.toml"
        cfg_path.write_text(
            f'[paths]\nstate_dir = "state"\nworktree_root = "worktrees"\n'
            f'[adapters]\nenabled = ["openai-api"]\n'
            f'[api.providers.lmstudio]\nkind = "lmstudio"\n'
            f'base_url = "http://127.0.0.1:{self.server.port}/v1"\n',
            encoding="utf-8",
        )
        out_buf = StringIO()
        with redirect_stdout(out_buf), redirect_stderr(StringIO()):
            code = cli.main(["--config", str(cfg_path), "resolve", run_ok, "--mark-failed", "--reason", "tampered receipt"])
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger.get_run(run_ok)["state"], "FAILED")


if __name__ == "__main__":
    unittest.main()
