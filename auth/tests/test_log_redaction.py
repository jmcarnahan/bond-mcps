"""Sanity check: no log record should contain a token-shaped substring.

This catches future code that accidentally logs raw OAuth tokens via any
path (our code, or third-party libraries) during the save/get/refresh
flow. It does NOT exhaustively prove no leak — it pins a regression
guard for the most common token-shape patterns.
"""

import json
import logging
import time
from unittest.mock import MagicMock, patch

from auth.token_store import TokenStore


TOKEN_PATTERNS = [
    "gho_supersecretaccesstoken",
    "ghp_supersecretaccesstoken",
    "rtk_supersecretrefreshtoken",
    "bearer-secret-token",
]


def _record_contains_any(record, needles):
    text = record.getMessage()
    # Also scan args separately since formatting may not yet have happened.
    text += " " + str(record.args) if record.args else ""
    return any(n in text for n in needles)


def test_no_token_substrings_in_logs(repo, caplog):
    """Drive save → get → refresh and verify no token plaintext leaks into logs."""

    store = TokenStore("github", user_key="alice")

    def fake_urlopen(req, timeout=None):
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "access_token": "gho_supersecretaccesstoken",
            "refresh_token": "rtk_supersecretrefreshtoken",
            "expires_in": 3600,
        }).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with caplog.at_level(logging.DEBUG):
        store.save_token({
            "access_token": "gho_supersecretaccesstoken",
            "refresh_token": "rtk_supersecretrefreshtoken",
            "expires_at": time.time() - 100,
        })
        store.get_token()
        with patch("auth.token_store.urllib.request.urlopen", side_effect=fake_urlopen):
            store.refresh_if_needed("cid", "secret", "https://token.url")
        store.get_token()
        store.clear()

    leaked = [
        (rec.name, rec.getMessage()) for rec in caplog.records
        if _record_contains_any(rec, TOKEN_PATTERNS)
    ]
    assert leaked == [], f"Token leaked into logs: {leaked}"


def test_log_discipline_pins_third_party_levels():
    """Calling apply() should raise httpx/authlib/msal log levels."""
    import logging

    # First, dirty the levels to DEBUG
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("authlib").setLevel(logging.DEBUG)
    logging.getLogger("msal").setLevel(logging.DEBUG)

    from auth import log_discipline

    log_discipline.apply()

    assert logging.getLogger("httpx").level == logging.INFO
    assert logging.getLogger("authlib").level == logging.INFO
    assert logging.getLogger("msal").level == logging.WARNING


def test_model_repr_masks_encrypted_columns(repo):
    """ProviderToken __repr__ should never expose ciphertext bytes."""
    store = TokenStore("github", user_key="alice")
    store.save_token({"access_token": "gho_supersecretaccesstoken"})

    from auth.db.session import get_session_factory
    from auth.db.models import ProviderToken

    factory = get_session_factory()
    with factory() as s:
        row = s.get(ProviderToken, ("alice", "github"))
        text = repr(row)

    # Ciphertext bytes should not appear in repr
    assert "access_token_encrypted" not in text
    # But identity is fine to show
    assert "alice" in text and "github" in text
