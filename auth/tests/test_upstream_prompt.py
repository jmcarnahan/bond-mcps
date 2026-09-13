"""A user with a live IdP session should not be asked for credentials again.

The AS used to send ``prompt=login`` on every upstream authorize, which tells
Okta to demand a password no matter how fresh the corporate SSO session is.
Bond Desktop refreshes on every launch, so the odd occasion it does need an
interactive sign-in should be a browser bounce the user barely sees. The
parameter is now opt-in via ``BOND_MCPS_UPSTREAM_PROMPT`` for deployments that
genuinely want re-authentication.
"""

from urllib.parse import parse_qs, urlsplit

import pytest

from auth.auth_server.upstream import OIDCUpstreamIdP, get_upstream_idp

AUTHORIZE_ENDPOINT = "https://example.okta.com/oauth2/v1/authorize"


def _build(monkeypatch, **kwargs) -> OIDCUpstreamIdP:
    idp = OIDCUpstreamIdP(
        idp="okta",
        issuer="https://example.okta.com",
        client_id="upstream-cid",
        client_secret="upstream-secret",
        redirect_uri="https://auth.example.com/oauth/upstream/callback",
        scopes="openid email profile",
        allowed_domains=[],
        **kwargs,
    )
    # No discovery over the wire: the only thing under test is the query.
    monkeypatch.setattr(
        OIDCUpstreamIdP,
        "_meta",
        lambda self: {
            "authorization_endpoint": AUTHORIZE_ENDPOINT,
            "token_endpoint": "https://example.okta.com/oauth2/v1/token",
        },
    )
    return idp


def _query(url: str) -> dict:
    return parse_qs(urlsplit(url).query)


def test_authorize_url_sends_no_prompt_by_default(monkeypatch):
    """An existing Okta session carries the user straight through."""
    idp = _build(monkeypatch)
    query = _query(idp.authorize_url(state="s", code_challenge="c"))
    assert "prompt" not in query
    # The rest of the request is unchanged.
    assert query["client_id"] == ["upstream-cid"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["s"]


def test_authorize_url_sends_the_configured_prompt(monkeypatch):
    """Operators who want a credential form on every sign-in still get one."""
    idp = _build(monkeypatch, prompt="login")
    assert _query(idp.authorize_url(state="s", code_challenge="c"))["prompt"] == ["login"]


@pytest.fixture
def upstream_env(monkeypatch):
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_IDP", "okta")
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_ISSUER", "https://example.okta.com")
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_CLIENT_ID", "upstream-cid")
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv(
        "BOND_MCPS_UPSTREAM_REDIRECT_URI",
        "https://auth.example.com/oauth/upstream/callback",
    )
    monkeypatch.delenv("BOND_MCPS_UPSTREAM_PROMPT", raising=False)


def test_builder_leaves_the_prompt_unset(upstream_env):
    """An unset variable is the deployed default, so it must mean no prompt."""
    assert get_upstream_idp()._prompt is None


def test_builder_reads_the_prompt_from_env(upstream_env, monkeypatch):
    """A set value is passed through; blank is treated as unset, which is
    what an empty Helm value or a stray space in a tfvars file looks like."""
    monkeypatch.setenv("BOND_MCPS_UPSTREAM_PROMPT", "login")
    assert get_upstream_idp()._prompt == "login"

    monkeypatch.setenv("BOND_MCPS_UPSTREAM_PROMPT", "  ")
    assert get_upstream_idp()._prompt is None
