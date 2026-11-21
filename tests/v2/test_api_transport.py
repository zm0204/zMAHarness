from http.client import HTTPConnection, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import errno
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from mah.adapters.api_providers import (
    ApiProviderConfig,
    check_api_key_env,
    is_model_not_found_404,
    parse_base_url,
    parse_providers,
    split_provider_model,
)
from mah.adapters.api_transport import (
    MAX_ERROR_BODY_BYTES,
    MAX_RESPONSE_BYTES,
    classify_connect_error,
    execute_request,
    redact_bytes,
    redact_text,
    sanitize_exception,
)
from mah.config import ConfigError, DEFAULTS, load_config
from mah.ledger import LedgerError


class _RecordingServer:
    def __init__(self, handler_fn):
        self.handler_fn = handler_fn
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def _handle(self, method):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b""
                outer.requests.append({
                    "method": method,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                })
                outer.handler_fn(self)

            def log_message(self, fmt, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ApiProviderConfigTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="mah-api-cfg-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config_path = self.root / "harness.local.toml"

    def write_config(self, body):
        self.config_path.write_text('[paths]\nstate_dir = "state"\n' + body, encoding="utf-8")
        return self.config_path

    def test_openai_api_is_disabled_by_default_and_skips_providers_with_warning_when_disabled(self):
        self.assertNotIn("openai-api", DEFAULTS["adapters"]["enabled"])
        cfg = load_config(self.write_config(""))
        self.assertNotIn("openai-api", cfg.values["adapters"]["enabled"])
        self.assertEqual(cfg.values["api"]["providers"], {})
        self.assertEqual(cfg.warnings, ())

        cfg_skipped = load_config(self.write_config("""
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://127.0.0.1:1234/v1"
"""))
        self.assertEqual(cfg_skipped.values["api"]["providers"], {})
        self.assertEqual(len(cfg_skipped.warnings), 1)
        self.assertIn("openai-api 未啟用", cfg_skipped.warnings[0])

        cfg_bad_skipped = load_config(self.write_config("""
[api.providers.bad]
kind = "deepseek"
base_url = "https://api.deepseek.com"
api_key = "sk-should-not-crash-disabled-cli"
"""))
        self.assertEqual(cfg_bad_skipped.values["api"]["providers"], {})
        self.assertEqual(len(cfg_bad_skipped.warnings), 1)
        self.assertIn("openai-api 未啟用", cfg_bad_skipped.warnings[0])

    def test_enabled_openai_api_parses_loopback_providers_and_allows_empty_provider_table(self):
        cfg_empty = load_config(self.write_config("""
[adapters]
enabled = ["agy-print", "openai-api"]
"""))
        self.assertEqual(cfg_empty.values["api"]["providers"], {})

        cfg = load_config(self.write_config("""
[adapters]
enabled = ["agy-print", "openai-api"]

[api]
max_output_tokens = 4096
connect_timeout_s = 7

[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://localhost:1234/v1/"
api_key_env = "LMSTUDIO_API_KEY"

[api.providers.ollama]
kind = "ollama"
base_url = "http://[::1]:11434/v1"
reasoning_effort = true
max_output_tokens = 2048
timeout_s = 300
"""))
        providers = cfg.values["api"]["providers"]
        self.assertEqual(set(providers), {"lmstudio", "ollama"})
        lms = providers["lmstudio"]
        self.assertIsInstance(lms, ApiProviderConfig)
        self.assertEqual(lms.base_url, "http://127.0.0.1:1234/v1")
        self.assertEqual((lms.scheme, lms.host, lms.port, lms.base_path), ("http", "127.0.0.1", 1234, "/v1"))
        self.assertEqual(lms.structured_output, "json_schema_strict")
        self.assertEqual(lms.max_output_tokens, 4096)
        self.assertEqual(lms.connect_timeout_s, 7)
        self.assertEqual(lms.allowed_efforts(), frozenset({"medium"}))
        self.assertTrue(lms.local)
        self.assertEqual(lms.billing(), "local")
        self.assertIsNone(lms.billing_notice())

        ollama = providers["ollama"]
        self.assertEqual(ollama.base_url, "http://[::1]:11434/v1")
        self.assertEqual((ollama.scheme, ollama.host, ollama.port, ollama.base_path), ("http", "::1", 11434, "/v1"))
        self.assertEqual(ollama.structured_output, "json_object")
        self.assertEqual(ollama.max_output_tokens, 2048)
        self.assertEqual(ollama.timeout_s, 300)
        self.assertEqual(ollama.allowed_efforts(), frozenset({"low", "medium", "high"}))

    def test_invalid_provider_configs_and_defaults_are_rejected(self):
        enable_header = '[adapters]\nenabled = ["agy-print", "openai-api"]\n'
        cases = (
            ("secret_api_key", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://127.0.0.1:1234/v1"
api_key = "sk-secret"
""", "禁止"),
            ("secret_authorization", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://127.0.0.1:1234/v1"
authorization = "Bearer secret"
""", "禁止"),
            ("secret_token", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://127.0.0.1:1234/v1"
token = "secret"
""", "禁止"),
            ("key_like_api_key_env", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://127.0.0.1:1234/v1"
api_key_env = "sk-1234567890abcdef"
""", "api_key_env"),
            ("lowercase_api_key_env", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://127.0.0.1:1234/v1"
api_key_env = "lmstudio_api_key"
""", "api_key_env"),
            ("non_loopback_domain", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "https://api.openai.com/v1"
""", "loopback"),
            ("non_canonical_loopback_127_0_0_2", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://127.0.0.2:1234/v1"
""", "loopback"),
            ("non_canonical_loopback_integer", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://2130706433:1234/v1"
""", "loopback"),
            ("non_canonical_ipv6_mapped", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://[::ffff:127.0.0.1]:1234/v1"
""", "loopback"),
            ("url_with_userinfo", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://user:pass@127.0.0.1:1234/v1"
""", "base_url"),
            ("url_with_query", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://127.0.0.1:1234/v1?x=1"
""", "base_url"),
            ("url_with_fragment", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://127.0.0.1:1234/v1#frag"
""", "base_url"),
            ("cloud_kind_deepseek_in_4a", """
[api.providers.ds]
kind = "deepseek"
base_url = "http://127.0.0.1:1234/v1"
""", "4b 才支援"),
            ("custom_kind_in_4a", """
[api.providers.c]
kind = "custom"
base_url = "http://127.0.0.1:1234/v1"
""", "4b 才支援"),
            ("structured_output_on_4a_provider", """
[api.providers.lmstudio]
kind = "lmstudio"
base_url = "http://127.0.0.1:1234/v1"
structured_output = "json_object"
""", "structured_output"),
            ("defaults_review_openai_api", """
[defaults]
review = { adapter = "openai-api", model = "lmstudio:qwen", effort = "medium" }
""", "defaults.review"),
        )
        for name, snippet, expected in cases:
            with self.subTest(case=name):
                with self.assertRaises(ConfigError) as ctx:
                    load_config(self.write_config(enable_header + snippet))
                self.assertIn(expected, str(ctx.exception))

    def test_split_provider_model_and_api_key_env_and_404_matcher(self):
        self.assertEqual(split_provider_model("lmstudio:qwen/qwen2.5-coder:7b"), ("lmstudio", "qwen/qwen2.5-coder:7b"))
        for bad in ("no-colon", ":missing-provider", "lmstudio:", "bad provider:model", "lmstudio:bad model"):
            with self.subTest(bad=bad), self.assertRaises(LedgerError) as ctx:
                split_provider_model(bad)
            self.assertEqual(ctx.exception.code, 2)

        self.assertEqual(check_api_key_env(None), (True, None))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_MAH_API_KEY", None)
            ok, reason = check_api_key_env("TEST_MAH_API_KEY")
            self.assertFalse(ok)
            self.assertIn("TEST_MAH_API_KEY", reason)
        for invalid_value in ("", "   ", "sk-key\ninjected", "sk-key with space", "sk-中文金鑰"):
            with self.subTest(invalid_value=invalid_value), patch.dict(
                os.environ, {"TEST_MAH_API_KEY": invalid_value}
            ):
                ok, reason = check_api_key_env("TEST_MAH_API_KEY")
                self.assertFalse(ok)
                self.assertNotIn(invalid_value.strip() or "SECRET", reason)
        with patch.dict(os.environ, {"TEST_MAH_API_KEY": "sk-valid-ascii-key_123"}):
            self.assertEqual(check_api_key_env("TEST_MAH_API_KEY"), (True, None))

        self.assertTrue(
            is_model_not_found_404(
                "lmstudio",
                b'{"error":{"message":"Model \\"qwen\\" not found","type":"invalid_request_error","code":"model_not_found"}}',
            )
        )
        self.assertTrue(is_model_not_found_404("ollama", b'{"error":"model \'qwen2.5:7b\' not found"}'))
        self.assertFalse(is_model_not_found_404("lmstudio", b"404 page not found"))
        self.assertFalse(is_model_not_found_404("lmstudio", b'{"error":{"message":"Unknown endpoint /chat/completions"}}'))


class ApiTransportTests(unittest.TestCase):
    def make_provider(self, port, **overrides):
        defaults = dict(
            name="lmstudio",
            kind="lmstudio",
            base_url=f"http://127.0.0.1:{port}/v1",
            scheme="http",
            host="127.0.0.1",
            port=port,
            base_path="/v1",
            api_key_env=None,
            timeout_s=None,
            max_output_tokens=1024,
            connect_timeout_s=3,
            reasoning_effort=False,
            structured_output="json_schema_strict",
            local=True,
        )
        return ApiProviderConfig(**(defaults | overrides))

    def test_redaction_and_exception_sanitization_never_leak_secret(self):
        secret = "sk-top-secret-value-987654321"
        raw = f'{{"error":"invalid token {secret}; Authorization: Bearer {secret}"}}'.encode("utf-8")
        redacted, changed = redact_bytes(raw, secret)
        self.assertTrue(changed)
        self.assertNotIn(secret.encode("utf-8"), redacted)
        self.assertIn(b"[REDACTED]", redacted)

        exc = OSError(errno.ECONNRESET, f"Failed with header Authorization: Bearer {secret} and key={secret}")
        detail, err_changed = sanitize_exception(exc, stage="recv", secret=secret)
        self.assertTrue(err_changed)
        self.assertEqual(detail["stage"], "recv")
        self.assertEqual(detail["errno"], errno.ECONNRESET)
        self.assertNotIn(secret, json.dumps(detail))

    def test_classify_connect_error_distinguishes_10061_and_10013(self):
        refused = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        self.assertEqual(classify_connect_error(refused), "connection_refused")
        win_refused = OSError("refused")
        win_refused.winerror = 10061
        self.assertEqual(classify_connect_error(win_refused), "connection_refused")

        denied = PermissionError(errno.EACCES, "denied")
        self.assertEqual(classify_connect_error(denied), "permission_denied")
        win_denied = OSError("denied")
        win_denied.winerror = 10013
        self.assertEqual(classify_connect_error(win_denied), "permission_denied")

    def test_three_phase_transport_handles_connect_send_recv_interrupts_and_limits(self):
        secret = "sk-unit-test-secret-key-999"

        # 1. Connect refused (using a fake connection that raises ConnectionRefusedError on connect)
        class RefusedConn:
            def __init__(self, *args, **kwargs):
                self.sock = None

            def connect(self):
                err = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
                err.winerror = 10061
                raise err

            def close(self):
                pass

        provider = self.make_provider(12345)
        res_refused = execute_request(
            provider, "POST", "/v1/chat/completions", body=b"{}", secret=secret, total_timeout_s=5,
            connection_factory=lambda scheme, host, port, timeout: RefusedConn(),
        )
        self.assertEqual(
            (res_refused.stage, res_refused.termination, res_refused.launch_error, res_refused.http_status),
            ("connect", "not_started", "request_not_sent", None),
        )
        self.assertEqual(res_refused.error_detail["winerror"], 10061)

        # 2. KeyboardInterrupt during connect() -> stage="connect", termination="interrupted", launch_error="request_not_sent"
        class InterruptConnectConn:
            def __init__(self):
                self.closed = False
                self.sock = None

            def connect(self):
                raise KeyboardInterrupt

            def close(self):
                self.closed = True

        ic_conn = InterruptConnectConn()
        res_ic = execute_request(
            provider, "POST", "/v1/chat/completions", body=b"{}", secret=secret, total_timeout_s=5,
            connection_factory=lambda scheme, host, port, timeout: ic_conn,
        )
        self.assertTrue(ic_conn.closed)
        self.assertEqual(
            (res_ic.stage, res_ic.termination, res_ic.launch_error, res_ic.http_status),
            ("connect", "interrupted", "request_not_sent", None),
        )

        # 3. IncompleteRead / KeyboardInterrupt during recv -> stage="recv", launch_error=None
        class FakeResponse:
            status = 200

            def getheaders(self):
                return [("Content-Type", "application/json"), ("Set-Cookie", "sid=secret"), ("X-Request-Id", "req-1")]

            def read(self, amt=None):
                raise IncompleteRead(f"partial {secret}".encode("utf-8"), 100)

        class IncompleteRecvConn:
            def __init__(self):
                self.sock = None

            def connect(self):
                pass

            def request(self, method, path, body=None, headers=None):
                pass

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        res_inc = execute_request(
            provider, "POST", "/v1/chat/completions", body=b"{}", secret=secret, total_timeout_s=5,
            connection_factory=lambda scheme, host, port, timeout: IncompleteRecvConn(),
        )
        self.assertEqual((res_inc.stage, res_inc.termination, res_inc.launch_error, res_inc.http_status),
                         ("recv", "exited", None, 200))
        self.assertEqual(res_inc.headers, {"content-type": "application/json", "x-request-id": "req-1"})
        self.assertNotIn(secret.encode("utf-8"), res_inc.body)
        self.assertTrue(res_inc.redacted)

        # 4. Oversized response (> 4 MB) -> response_too_large=True
        class OversizedResponse:
            status = 200

            def __init__(self):
                self.remaining = MAX_RESPONSE_BYTES + 1024

            def getheaders(self):
                return [("Content-Type", "application/json")]

            def read(self, amt=65536):
                if self.remaining <= 0:
                    return b""
                chunk = min(amt, self.remaining)
                self.remaining -= chunk
                return b"x" * chunk

        class OversizedConn(IncompleteRecvConn):
            def getresponse(self):
                return OversizedResponse()

        res_big = execute_request(
            provider, "POST", "/v1/chat/completions", body=b"{}", secret=None, total_timeout_s=5,
            connection_factory=lambda scheme, host, port, timeout: OversizedConn(),
        )
        self.assertTrue(res_big.response_too_large)
        self.assertLessEqual(len(res_big.body), MAX_ERROR_BODY_BYTES)

    def test_live_loopback_http_server_roundtrip_and_no_redirect_follow(self):
        secret = "sk-live-loopback-secret-777"

        def handle(req):
            if req.path == "/v1/redirect":
                req.send_response(302)
                req.send_header("Location", "http://127.0.0.1:1/should-never-follow")
                req.send_header("Content-Length", "0")
                req.end_headers()
                return
            payload = json.dumps({"ok": True, "echo": secret}).encode("utf-8")
            req.send_response(200)
            req.send_header("Content-Type", "application/json")
            req.send_header("Set-Cookie", "must_be_dropped=1")
            req.send_header("X-Request-Id", "req-live-1")
            req.send_header("Content-Length", str(len(payload)))
            req.end_headers()
            req.wfile.write(payload)

        server = _RecordingServer(handle)
        self.addCleanup(server.close)
        provider = self.make_provider(server.port)

        res = execute_request(provider, "POST", "/v1/chat/completions", body=b'{"ping":1}', secret=secret, total_timeout_s=5)
        self.assertEqual((res.stage, res.termination, res.http_status, res.redacted), ("done", "exited", 200, True))
        self.assertEqual(res.headers, {"content-type": "application/json", "x-request-id": "req-live-1"})
        self.assertNotIn(secret.encode("utf-8"), res.body)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["headers"].get("authorization"), f"Bearer {secret}")

        res_redirect = execute_request(provider, "POST", "/v1/redirect", body=b"{}", secret=None, total_timeout_s=5)
        self.assertEqual((res_redirect.stage, res_redirect.http_status), ("done", 302))
        self.assertEqual(len(server.requests), 2)


if __name__ == "__main__":
    unittest.main()
