"""
Client library for the shared OAuth callback proxy.

MCPs import this to register auth flows and wait for OAuth callbacks
routed through the shared proxy on port 8000.

The proxy must be started separately by the user before starting MCP servers
that use local OAuth authentication.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8000


class OAuthProxyClient:
    """Client for the shared OAuth callback proxy."""

    def __init__(self, port: int | None = None, host: str = "127.0.0.1"):
        self.port = port or int(os.environ.get("BOND_AUTH_PROXY_PORT", DEFAULT_PORT))
        self.host = host
        self.base_url = f"http://{host}:{self.port}"

    def check_proxy(self) -> None:
        """Verify the auth proxy is running. Raises RuntimeError if not."""
        if self._health_check():
            logger.info("Auth proxy verified on port %d", self.port)
            return
        raise RuntimeError(
            f"Bond AI auth proxy is not running on port {self.port}.\n"
            f"Start it in a separate terminal:\n"
            f"  cd auth && poetry run python -m auth\n"
            f"Or set BOND_AUTH_PROXY_PORT to use a different port."
        )

    def register_auth(self, state: str, provider: str) -> None:
        """Register a pending auth flow with the proxy."""
        data = json.dumps({"state": state, "provider": provider}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/auth/register",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
                if resp.status != 200:
                    body = resp.read().decode()
                    raise RuntimeError(f"Failed to register auth: {body}")
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"Failed to register auth: {body}") from e

    def wait_for_callback(
        self, state: str, timeout: float = 120.0, poll_interval: float = 0.5
    ) -> dict:
        """Poll for the OAuth callback result.

        Returns query params dict (with internal 'status' key stripped so only
        OAuth parameters remain).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/auth/result/{state}",
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
                    data = json.loads(resp.read().decode())
                    if data.get("status") == "complete":
                        # Strip internal proxy key before returning to caller
                        data.pop("status", None)
                        return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise TimeoutError(
                        "Auth state expired or unknown"
                    ) from e
                raise
            except urllib.error.URLError:
                raise RuntimeError("Auth proxy is not running")
            time.sleep(poll_interval)
        raise TimeoutError("Timed out waiting for OAuth callback")

    def get_redirect_uri(self, provider: str) -> str:
        """Return the redirect URI for the given provider."""
        return f"http://localhost:{self.port}/connections/{provider}/callback"

    def _health_check(self) -> bool:
        """Check if the proxy is running and healthy.

        Validates the response body to avoid false positives from other
        services that might be running on the same port.
        """
        try:
            req = urllib.request.Request(f"{self.base_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:  # nosec B310
                if resp.status != 200:
                    return False
                body = json.loads(resp.read().decode())
                return body.get("status") == "ok"
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
            return False
