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
        resp.read.return_value = json.dumps(
            {
                "access_token": "gho_supersecretaccesstoken",
                "refresh_token": "rtk_supersecretrefreshtoken",
                "expires_in": 3600,
            }
        ).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with caplog.at_level(logging.DEBUG):
        store.save_token(
            {
                "access_token": "gho_supersecretaccesstoken",
                "refresh_token": "rtk_supersecretrefreshtoken",
                "expires_at": time.time() - 100,
            }
        )
        store.get_token()
        with patch("auth.token_store.urllib.request.urlopen", side_effect=fake_urlopen):
            store.refresh_if_needed("cid", "secret", "https://token.url")
        store.get_token()
        store.clear()

    leaked = [
        (rec.name, rec.getMessage())
        for rec in caplog.records
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


def test_per_mcp_cli_modules_apply_log_discipline():
    """Each MCP's CLI entrypoint must call log_discipline.apply() at module
    load so OAuth token exchanges don't get logged at httpx DEBUG level.

    We don't try to import the CLI modules here (they live in sibling
    packages); instead we statically grep the files for the discipline
    invocation. This is a regression guard, not a behavioral test.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    targets = [
        repo_root / "mcps" / "microsoft" / "ms_graph_cli.py",
        repo_root / "mcps" / "github" / "github_cli.py",
        repo_root / "mcps" / "atlassian" / "atlassian_cli.py",
    ]
    for path in targets:
        assert path.exists(), f"CLI not found: {path}"
        text = path.read_text()
        assert "log_discipline" in text, (
            f"{path.name} does not import log_discipline — OAuth token "
            "exchanges could leak via httpx/authlib/msal debug logs"
        )
        assert (
            "log_discipline.apply()" in text
        ), f"{path.name} imports log_discipline but never calls apply()"


def test_per_mcp_mcp_servers_apply_log_discipline():
    """Same check for the MCP server entrypoints."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    targets = [
        repo_root / "mcps" / "microsoft" / "ms_graph_mcp.py",
        repo_root / "mcps" / "github" / "github_mcp.py",
        repo_root / "mcps" / "atlassian" / "atlassian_mcp.py",
    ]
    for path in targets:
        text = path.read_text()
        assert "log_discipline.apply()" in text, f"{path.name} does not call log_discipline.apply()"


def test_proxy_server_applies_log_discipline():
    """The auth proxy server is the third process in the auth-package
    deployment and should pin the same third-party log levels for policy
    uniformity, even though it doesn't currently use httpx/authlib/msal."""
    from pathlib import Path

    proxy = Path(__file__).resolve().parents[1] / "auth" / "proxy_server.py"
    text = proxy.read_text()
    assert "log_discipline.apply()" in text, "proxy_server.py does not call log_discipline.apply()"


def test_model_repr_masks_encrypted_columns(repo):
    """ProviderToken __repr__ should never expose ciphertext bytes."""
    store = TokenStore("github", user_key="alice")
    store.save_token({"access_token": "gho_supersecretaccesstoken"})

    from auth.db.models import ProviderToken
    from auth.db.session import get_session_factory

    factory = get_session_factory()
    with factory() as s:
        row = s.get(ProviderToken, ("alice", "github"))
        text = repr(row)

    # Ciphertext bytes should not appear in repr
    assert "access_token_encrypted" not in text
    # But identity is fine to show
    assert "alice" in text and "github" in text
