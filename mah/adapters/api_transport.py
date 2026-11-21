"""Single-request three-phase HTTP transport for openai-api using standard library http.client."""
from dataclasses import dataclass
import errno
import http.client
import re
import socket
from time import monotonic

from .api_providers import ApiProviderConfig

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 2048
READ_CHUNK_BYTES = 65536
ALLOWED_RESPONSE_HEADERS = frozenset({
    "content-type",
    "x-request-id",
    "openrouter-generation-id",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "retry-after",
})
AUTH_BEARER_TEXT_RE = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,'\"\r\n;]+")
AUTH_BEARER_BYTES_RE = re.compile(rb"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,'\"\r\n;]+")


@dataclass(frozen=True)
class TransportResult:
    stage: str
    termination: str
    launch_error: str | None
    http_status: int | None
    headers: dict[str, str]
    body: bytes
    response_too_large: bool
    redacted: bool
    error_detail: dict | None
    wall_milliseconds: int


def redact_text(text: str, secret: str | None = None) -> tuple[str, bool]:
    """Scrub secret and any Authorization: Bearer header token from text."""
    if not isinstance(text, str) or not text:
        return "", False
    changed = False
    result = text
    if secret:
        replaced = result.replace(secret, "[REDACTED]")
        if replaced != result:
            result = replaced
            changed = True
    scrubbed, count = AUTH_BEARER_TEXT_RE.subn(r"\1[REDACTED]", result)
    if count > 0 and scrubbed != result:
        result = scrubbed
        changed = True
    return result, changed


def redact_bytes(data: bytes, secret: str | None = None) -> tuple[bytes, bool]:
    """Scrub secret and any Authorization: Bearer header token from raw bytes."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return b"", False
    raw = bytes(data)
    changed = False
    if secret:
        secret_bytes = secret.encode("utf-8")
        if secret_bytes in raw:
            raw = raw.replace(secret_bytes, b"[REDACTED]")
            changed = True
    scrubbed, count = AUTH_BEARER_BYTES_RE.subn(rb"\1[REDACTED]", raw)
    if count > 0 and scrubbed != raw:
        raw = scrubbed
        changed = True
    return raw, changed


def sanitize_exception(exc: BaseException, *, stage: str, secret: str | None = None) -> tuple[dict, bool]:
    """Build a safe, redacted diagnostic dictionary from a transport exception."""
    exc_type = type(exc).__name__
    errno_val = getattr(exc, "errno", None)
    winerror_val = getattr(exc, "winerror", None)
    if type(errno_val) is not int:
        errno_val = None
    if type(winerror_val) is not int:
        winerror_val = None
    raw_msg = str(exc) if not isinstance(exc, KeyboardInterrupt) else f"interrupted during {stage}"
    message, was_redacted = redact_text(raw_msg, secret)
    return {
        "stage": stage,
        "type": exc_type,
        "errno": errno_val,
        "winerror": winerror_val,
        "message": message[:512],
    }, was_redacted


def classify_connect_error(exc_or_detail) -> str:
    """Classify a connect error into connection_refused (10061), permission_denied (10013), timeout, or other."""
    if exc_or_detail is None:
        return "other"
    if isinstance(exc_or_detail, dict):
        exc_type = str(exc_or_detail.get("type") or "")
        errno_val = exc_or_detail.get("errno")
        winerror_val = exc_or_detail.get("winerror")
    else:
        exc_type = type(exc_or_detail).__name__
        errno_val = getattr(exc_or_detail, "errno", None)
        winerror_val = getattr(exc_or_detail, "winerror", None)

    if winerror_val == 10061 or errno_val == errno.ECONNREFUSED or exc_type == "ConnectionRefusedError":
        return "connection_refused"
    if winerror_val == 10013 or errno_val in (errno.EACCES, errno.EPERM) or exc_type == "PermissionError":
        return "permission_denied"
    if exc_type in ("TimeoutError", "timeout") or errno_val == errno.ETIMEDOUT or winerror_val == 10060:
        return "timeout"
    return "other"


def _default_connection(scheme: str, host: str, port: int, timeout: float):
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _is_timeout_error(exc: BaseException, deadline: float, clock) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if getattr(exc, "errno", None) == errno.ETIMEDOUT or getattr(exc, "winerror", None) == 10060:
        return True
    return clock() >= deadline


def _safe_close(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def execute_request(
    provider: ApiProviderConfig,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    secret: str | None = None,
    total_timeout_s: float = 60.0,
    clock=monotonic,
    connection_factory=None,
) -> TransportResult:
    """Execute one HTTP request split strictly into connect -> send -> recv phases without retry or redirect."""
    started = clock()
    deadline = started + float(total_timeout_s)
    factory = connection_factory or _default_connection

    def elapsed_ms() -> int:
        return max(0, round((clock() - started) * 1000))

    def remaining_s() -> float:
        return max(0.05, deadline - clock())

    request_headers = {"Accept": "application/json"}
    if body is not None:
        request_headers["Content-Type"] = "application/json; charset=utf-8"
        request_headers["Content-Length"] = str(len(body))
    if secret is not None:
        request_headers["Authorization"] = f"Bearer {secret}"

    # Phase 1: connect() only. No application bytes are sent here.
    connect_timeout = max(0.05, min(float(provider.connect_timeout_s), remaining_s()))
    conn = None
    try:
        conn = factory(provider.scheme, provider.host, provider.port, connect_timeout)
        conn.connect()
    except KeyboardInterrupt as exc:
        _safe_close(conn)
        detail, redacted = sanitize_exception(exc, stage="connect", secret=secret)
        return TransportResult(
            stage="connect",
            termination="interrupted",
            launch_error="request_not_sent",
            http_status=None,
            headers={},
            body=b"",
            response_too_large=False,
            redacted=redacted,
            error_detail=detail,
            wall_milliseconds=elapsed_ms(),
        )
    except (OSError, http.client.HTTPException, ValueError) as exc:
        _safe_close(conn)
        detail, redacted = sanitize_exception(exc, stage="connect", secret=secret)
        return TransportResult(
            stage="connect",
            termination="not_started",
            launch_error="request_not_sent",
            http_status=None,
            headers={},
            body=b"",
            response_too_large=False,
            redacted=redacted,
            error_detail=detail,
            wall_milliseconds=elapsed_ms(),
        )

    # Phase 2: send (conn.request). Once this begins, bytes may have reached the socket.
    try:
        sock = getattr(conn, "sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(remaining_s())
        conn.request(method, path, body=body, headers=request_headers)
    except KeyboardInterrupt as exc:
        request_headers.clear()
        _safe_close(conn)
        detail, redacted = sanitize_exception(exc, stage="send", secret=secret)
        return TransportResult(
            stage="send",
            termination="interrupted",
            launch_error=None,
            http_status=None,
            headers={},
            body=b"",
            response_too_large=False,
            redacted=redacted,
            error_detail=detail,
            wall_milliseconds=elapsed_ms(),
        )
    except (OSError, http.client.HTTPException, ValueError) as exc:
        request_headers.clear()
        _safe_close(conn)
        termination = "timeout" if _is_timeout_error(exc, deadline, clock) else "exited"
        detail, redacted = sanitize_exception(exc, stage="send", secret=secret)
        return TransportResult(
            stage="send",
            termination=termination,
            launch_error=None,
            http_status=None,
            headers={},
            body=b"",
            response_too_large=False,
            redacted=redacted,
            error_detail=detail,
            wall_milliseconds=elapsed_ms(),
        )
    finally:
        request_headers.clear()

    # Phase 3: recv (getresponse + full body read up to MAX_RESPONSE_BYTES).
    http_status: int | None = None
    allowlisted_headers: dict[str, str] = {}
    buf = bytearray()
    try:
        sock = getattr(conn, "sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(remaining_s())
        response = conn.getresponse()
        http_status = int(response.status)
        for header_name, header_val in response.getheaders():
            lower_name = header_name.lower()
            if lower_name in ALLOWED_RESPONSE_HEADERS and isinstance(header_val, str):
                allowlisted_headers[lower_name] = header_val

        while True:
            if clock() >= deadline:
                raise TimeoutError("HTTP response body read timed out")
            if sock is not None and hasattr(sock, "settimeout"):
                sock.settimeout(remaining_s())
            chunk = response.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_RESPONSE_BYTES:
                _safe_close(conn)
                redacted_body, was_redacted = redact_bytes(bytes(buf[:MAX_ERROR_BODY_BYTES]), secret)
                return TransportResult(
                    stage="done",
                    termination="exited",
                    launch_error=None,
                    http_status=http_status,
                    headers=allowlisted_headers,
                    body=redacted_body,
                    response_too_large=True,
                    redacted=was_redacted,
                    error_detail=None,
                    wall_milliseconds=elapsed_ms(),
                )
    except KeyboardInterrupt as exc:
        _safe_close(conn)
        partial_body, body_redacted = redact_bytes(bytes(buf[:MAX_ERROR_BODY_BYTES]), secret)
        detail, err_redacted = sanitize_exception(exc, stage="recv", secret=secret)
        return TransportResult(
            stage="recv",
            termination="interrupted",
            launch_error=None,
            http_status=http_status,
            headers=allowlisted_headers,
            body=partial_body,
            response_too_large=False,
            redacted=body_redacted or err_redacted,
            error_detail=detail,
            wall_milliseconds=elapsed_ms(),
        )
    except (OSError, http.client.HTTPException, ValueError) as exc:
        _safe_close(conn)
        if isinstance(exc, http.client.IncompleteRead) and getattr(exc, "partial", None):
            buf.extend(exc.partial[:MAX_ERROR_BODY_BYTES])
        partial_body, body_redacted = redact_bytes(bytes(buf[:MAX_ERROR_BODY_BYTES]), secret)
        termination = "timeout" if _is_timeout_error(exc, deadline, clock) else "exited"
        detail, err_redacted = sanitize_exception(exc, stage="recv", secret=secret)
        return TransportResult(
            stage="recv",
            termination=termination,
            launch_error=None,
            http_status=http_status,
            headers=allowlisted_headers,
            body=partial_body,
            response_too_large=False,
            redacted=body_redacted or err_redacted,
            error_detail=detail,
            wall_milliseconds=elapsed_ms(),
        )

    _safe_close(conn)
    raw_body = bytes(buf) if http_status == 200 else bytes(buf[:MAX_ERROR_BODY_BYTES])
    redacted_body, was_redacted = redact_bytes(raw_body, secret)
    return TransportResult(
        stage="done",
        termination="exited",
        launch_error=None,
        http_status=http_status,
        headers=allowlisted_headers,
        body=redacted_body,
        response_too_large=False,
        redacted=was_redacted,
        error_detail=None,
        wall_milliseconds=elapsed_ms(),
    )
