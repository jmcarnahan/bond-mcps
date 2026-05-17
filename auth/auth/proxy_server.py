"""
Shared OAuth callback proxy server.

Runs on 127.0.0.1:8000, routes OAuth callbacks from multiple providers
(Microsoft, Atlassian, GitHub) and relays auth codes back to the requesting
MCP server via polling.

Uses ThreadingHTTPServer so concurrent requests (browser callback + client
polling) are handled without blocking each other.
"""

import atexit
import json
import logging
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

PID_FILE = Path.home() / ".bond_ai_auth_proxy.pid"
STATE_TTL = 300  # 5 minutes

_lock = threading.Lock()
_pending: dict[str, "PendingAuth"] = {}


@dataclass
class PendingAuth:
    provider: str
    created_at: float
    result: dict | None = None


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new daemon thread."""

    daemon_threads = True


def _cleanup_expired() -> None:
    """Remove pending entries older than STATE_TTL. Caller must hold _lock."""
    now = time.time()
    expired = [s for s, p in _pending.items() if now - p.created_at > STATE_TTL]
    for s in expired:
        del _pending[s]


class AuthProxyHandler(BaseHTTPRequestHandler):
    """HTTP handler for the OAuth callback proxy."""

    def do_GET(self) -> None:
        with _lock:
            _cleanup_expired()
        path = urlparse(self.path).path

        if path == "/health":
            self._handle_health()
        elif path.startswith("/auth/result/"):
            state = path[len("/auth/result/"):]
            self._handle_result(state)
        elif re.match(r"^/connections/[^/]+/callback$", path):
            provider = path.split("/")[2]
            self._handle_callback(provider)
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        with _lock:
            _cleanup_expired()
        path = urlparse(self.path).path

        if path == "/auth/register":
            self._handle_register()
        else:
            self._send_json(404, {"error": "not_found"})

    def _handle_health(self) -> None:
        self._send_json(200, {"status": "ok"})

    def _handle_register(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid_json"})
            return

        state = data.get("state")
        provider = data.get("provider")
        if not state or not provider:
            self._send_json(400, {"error": "state and provider required"})
            return

        with _lock:
            if state in _pending:
                duplicate = True
            else:
                _pending[state] = PendingAuth(
                    provider=provider, created_at=time.time(),
                )
                duplicate = False

        if duplicate:
            self._send_json(409, {"error": "state_already_registered"})
        else:
            logger.info("Auth registered for %s (state=%s...)", provider, state[:8])
            self._send_json(200, {"registered": True})

    def _handle_callback(self, provider: str) -> None:
        qs = parse_qs(urlparse(self.path).query)
        # Flatten single-value lists
        params = {k: v[0] if len(v) == 1 else v for k, v in qs.items()}
        state = params.get("state")

        with _lock:
            if not state or state not in _pending:
                outcome = "unknown"
            elif _pending[state].provider != provider:
                outcome = "mismatch"
            else:
                _pending[state].result = params
                outcome = "ok"

        if outcome == "unknown":
            logger.warning("Callback with unknown/expired state for %s", provider)
            self._send_html(400,
                "<h2>Authentication failed</h2>"
                "<p>Unknown or expired auth session. Please try again.</p>")
        elif outcome == "mismatch":
            logger.warning("Callback provider mismatch for state=%s", state[:8] if state else "?")
            self._send_html(400,
                "<h2>Authentication failed</h2>"
                "<p>Provider mismatch. Please try again.</p>")
        else:
            has_code = "code" in params
            logger.info("OAuth callback received for %s (code=%s)", provider, "yes" if has_code else "NO")
            self._send_html(200,
                "<h2>Authentication successful!</h2>"
                "<p>You can close this tab and return to your terminal.</p>")

    def _handle_result(self, state: str) -> None:
        with _lock:
            if state not in _pending:
                status_code, data = 404, {"error": "unknown_state"}
            elif _pending[state].result is None:
                status_code, data = 200, {"status": "pending"}
            else:
                provider = _pending[state].provider
                result = _pending[state].result
                del _pending[state]
                status_code, data = 200, {"status": "complete", **result}
                logger.info("Auth result delivered for %s — flow complete", provider)

        self._send_json(status_code, data)

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, body: str) -> None:
        html = f"<html><body>{body}</body></html>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, format, *args) -> None:
        logger.debug(format, *args)


def _write_pid_file() -> None:
    PID_FILE.write_text(str(os.getpid()))


def _remove_pid_file() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def run_proxy(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the proxy server. Blocks until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = _ThreadingHTTPServer((host, port), AuthProxyHandler)
    _write_pid_file()
    atexit.register(_remove_pid_file)

    def _shutdown(signum, frame):
        logger.info("Shutting down auth proxy")
        # shutdown() must be called from a different thread than serve_forever()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("Auth proxy listening on %s:%d", host, port)
    print(f"\n{'=' * 50}", flush=True)
    print(f"  Bond AI OAuth Proxy", flush=True)
    print(f"  Listening on {host}:{port}", flush=True)
    print(f"  PID: {os.getpid()}", flush=True)
    print(f"  Press Ctrl+C to stop", flush=True)
    print(f"{'=' * 50}\n", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        _remove_pid_file()
