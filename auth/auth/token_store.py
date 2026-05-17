"""OAuth token cache backed by the encrypted SQLAlchemy database.

Public API is the same as the previous file-based TokenStore so the three
MCP call sites (github, atlassian, atlassian_cli) need no behavioral
changes — only the constructor optionally accepts a ``user_key`` kwarg.

For the read-modify-write refresh path we use a row-level lock so two
processes (e.g., the MCP server and the CLI logged in simultaneously)
don't both call the provider's refresh endpoint with the same refresh
token (which providers like Atlassian and Microsoft invalidate on use).
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)


def _current_user_key() -> str:
    explicit = os.environ.get("BOND_MCPS_USER_ID")
    if explicit:
        return explicit
    # Postgres deployments without an explicit user are refused at the
    # repository level via _default_resolver(); here we still fall back so
    # local SQLite use is convenient.
    return getpass.getuser()


class TokenStore:
    """DB-backed token store for a single (user_key, provider) pair."""

    def __init__(self, provider: str, *, user_key: str | None = None):
        self.provider = provider
        self.user_key = user_key or _current_user_key()
        # Lazy repo construction: the DB stack is only touched on first method call.
        self._repo = None

    @property
    def repo(self):
        if self._repo is None:
            from auth.db.repository import TokenRepository

            self._repo = TokenRepository()
        return self._repo

    def get_token(self) -> dict | None:
        """Return decrypted token dict, or None if missing or expired."""
        data = self.repo.get_token(self.user_key, self.provider)
        if data is None:
            return None
        if self._is_expired(data):
            return None
        return data

    def save_token(self, data: dict) -> None:
        self.repo.save_token(self.user_key, self.provider, data)

    def clear(self) -> None:
        self.repo.clear_token(self.user_key, self.provider)

    def refresh_if_needed(
        self, client_id: str, client_secret: str, token_url: str
    ) -> str | None:
        """Refresh the access token if expired. Returns new access_token or None.

        Wraps the read → refresh → write in a row-level lock so concurrent
        processes don't burn refresh tokens on duplicate calls.
        """
        with self.repo.locked_token(self.user_key, self.provider) as locked:
            if locked.data is None:
                return None

            if not locked.is_expired():
                return locked.data.get("access_token")

            refresh_token = locked.data.get("refresh_token")
            if not refresh_token:
                return None

            try:
                new_data = _do_refresh(
                    client_id, client_secret, token_url, refresh_token
                )
            except Exception:
                logger.exception("Token refresh failed for %s", self.provider)
                return None

            if not new_data or "access_token" not in new_data:
                return None

            if "expires_in" in new_data:
                new_data["expires_at"] = time.time() + new_data["expires_in"]
                new_data.pop("expires_in", None)

            locked.update(new_data)
            return locked.data["access_token"]

    @staticmethod
    def _is_expired(data: dict) -> bool:
        expires_at = data.get("expires_at")
        if expires_at is None:
            return False
        return time.time() >= (expires_at - 60)


def _do_refresh(
    client_id: str, client_secret: str, token_url: str, refresh_token: str
) -> dict | None:
    """POST grant_type=refresh_token per RFC 6749 §4.1.3.

    Form-encoded body (required for GitHub, accepted by Atlassian/Microsoft);
    Accept: application/json. Returns parsed JSON dict, or None on HTTP error.
    """
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
    ).encode()

    req = urllib.request.Request(
        token_url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        logger.warning("Token refresh HTTP error: %s", e.code)
        return None
