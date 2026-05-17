"""
Generic file-based token cache for OAuth providers.

For non-MSAL providers (Atlassian, GitHub). Microsoft uses MSAL's own
SerializableTokenCache at ~/.bond_mcps/microsoft.json. All providers
share the ~/.bond_mcps/ directory.
"""

import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".bond_mcps"


class TokenStore:
    """File-based token cache for a single OAuth provider."""

    def __init__(self, provider: str, cache_dir: Path | None = None):
        self.provider = provider
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.cache_file = self.cache_dir / f"{provider}.json"

    def get_token(self) -> dict | None:
        """Load cached token data. Returns None if missing or expired."""
        if not self.cache_file.exists():
            return None
        try:
            data = json.loads(self.cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        if self._is_expired(data):
            return None
        return data

    def save_token(self, data: dict) -> None:
        """Save token data atomically with 0600 file permissions.

        Uses tempfile.mkstemp (which creates with 0600) + os.replace to ensure
        the file is never observable with broader permissions, even if a
        previous version of the file existed with weaker perms.
        """
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{self.provider}.",
            suffix=".tmp",
            dir=str(self.cache_dir),
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.cache_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def refresh_if_needed(
        self, client_id: str, client_secret: str, token_url: str
    ) -> str | None:
        """Refresh the access token if expired. Returns new access_token or None."""
        data = self._load_raw()
        if data is None:
            return None

        if not self._is_expired(data):
            return data.get("access_token")

        refresh_token = data.get("refresh_token")
        if not refresh_token:
            return None

        try:
            new_data = self._do_refresh(
                client_id, client_secret, token_url, refresh_token
            )
        except Exception:
            logger.exception("Token refresh failed for %s", self.provider)
            return None

        if not new_data or "access_token" not in new_data:
            return None

        # Preserve cloud_id and other metadata from old data
        merged = {**data, **new_data}
        if "expires_in" in new_data:
            merged["expires_at"] = time.time() + new_data["expires_in"]
        self.save_token(merged)
        return merged["access_token"]

    def clear(self) -> None:
        """Delete cached token file."""
        try:
            self.cache_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _load_raw(self) -> dict | None:
        """Load cache file without expiry check."""
        if not self.cache_file.exists():
            return None
        try:
            return json.loads(self.cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _is_expired(self, data: dict) -> bool:
        """Check if token has expired (with 60s buffer)."""
        expires_at = data.get("expires_at")
        if expires_at is None:
            return False  # No expiry info — assume valid
        return time.time() >= (expires_at - 60)

    def _do_refresh(
        self,
        client_id: str,
        client_secret: str,
        token_url: str,
        refresh_token: str,
    ) -> dict | None:
        """POST to token_url with grant_type=refresh_token.

        Uses application/x-www-form-urlencoded per RFC 6749 §4.1.3 — required
        for GitHub, accepted by Atlassian/Microsoft. Asks for a JSON response
        via the Accept header.
        """
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }).encode()

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
