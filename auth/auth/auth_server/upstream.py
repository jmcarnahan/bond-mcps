"""Upstream OIDC IdP integration — single configured provider per deployment.

The bond-mcps Authorization Server is itself an OIDC client to one of:

* **Cognito** -- ``BOND_MCPS_UPSTREAM_IDP=cognito``. Issuer:
  ``https://cognito-idp.<region>.amazonaws.com/<user-pool-id>``. Cognito
  exposes a standard OIDC discovery document; we use it directly.
* **Okta** -- ``BOND_MCPS_UPSTREAM_IDP=okta``. Issuer:
  ``https://<org>.okta.com/oauth2/default`` (or any custom authorization
  server). Standard OIDC discovery.

Both are full OIDC providers, so a single ``OIDCUpstreamIdP`` class handles
both — the only deployment difference is the issuer URL and client
credentials. The plugin shape leaves room to add other IdPs later (e.g.
internal Azure AD) without touching the AS endpoints.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

ENV_IDP = "BOND_MCPS_UPSTREAM_IDP"
ENV_ISSUER = "BOND_MCPS_UPSTREAM_ISSUER"
ENV_CLIENT_ID = "BOND_MCPS_UPSTREAM_CLIENT_ID"
ENV_CLIENT_SECRET = "BOND_MCPS_UPSTREAM_CLIENT_SECRET"
ENV_REDIRECT_URI = "BOND_MCPS_UPSTREAM_REDIRECT_URI"
ENV_SCOPES = "BOND_MCPS_UPSTREAM_SCOPES"
ENV_ALLOWED_DOMAINS = "BOND_MCPS_UPSTREAM_ALLOWED_DOMAINS"

DEFAULT_SCOPES = "openid email profile"
SUPPORTED_IDPS = {"cognito", "okta"}


class UpstreamConfigError(RuntimeError):
    """Upstream IdP configuration is missing or invalid."""


class UpstreamAuthError(RuntimeError):
    """Upstream IdP rejected a code exchange or returned malformed data."""


@dataclass(frozen=True)
class UpstreamUserInfo:
    sub: str
    email: str | None
    name: str | None
    raw_claims: dict


class UpstreamIdP(Protocol):
    def authorize_url(
        self,
        *,
        state: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> str: ...

    def exchange_code(self, *, code: str, code_verifier: str) -> UpstreamUserInfo: ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_upstream_idp() -> UpstreamIdP:
    """Build the configured upstream IdP from environment.

    Discovery is cached for the lifetime of the returned object.
    """
    idp = os.environ.get(ENV_IDP, "").strip().lower()
    if not idp:
        raise UpstreamConfigError(f"{ENV_IDP} is not set.")
    if idp not in SUPPORTED_IDPS:
        raise UpstreamConfigError(
            f"Unsupported {ENV_IDP}={idp!r}. Supported: {sorted(SUPPORTED_IDPS)}."
        )

    issuer = os.environ.get(ENV_ISSUER, "").strip()
    client_id = os.environ.get(ENV_CLIENT_ID, "").strip()
    # Public clients (Cognito user-pool clients without "generate secret",
    # or any OIDC provider configured as a PKCE-only public client) have no
    # secret. Empty is allowed; we just skip basic auth on the token call.
    client_secret = os.environ.get(ENV_CLIENT_SECRET, "").strip()
    redirect_uri = os.environ.get(ENV_REDIRECT_URI, "").strip()
    if not (issuer and client_id and redirect_uri):
        raise UpstreamConfigError(
            f"Upstream {idp!r} requires {ENV_ISSUER}, {ENV_CLIENT_ID}, "
            f"and {ENV_REDIRECT_URI}. {ENV_CLIENT_SECRET} is optional "
            "(set it only for confidential clients)."
        )
    scopes = os.environ.get(ENV_SCOPES, "").strip() or DEFAULT_SCOPES
    allowed_domains = _split_csv(os.environ.get(ENV_ALLOWED_DOMAINS, ""))

    return OIDCUpstreamIdP(
        idp=idp,
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=scopes,
        allowed_domains=allowed_domains,
    )


# ---------------------------------------------------------------------------
# Generic OIDC implementation (Cognito + Okta both fit)
# ---------------------------------------------------------------------------


class OIDCUpstreamIdP:
    def __init__(
        self,
        *,
        idp: str,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: str,
        allowed_domains: list[str],
    ):
        self._idp = idp
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._scopes = scopes
        self._allowed_domains = [d.lower() for d in allowed_domains]
        self._discovery: dict | None = None

    # -- Public API ----------------------------------------------------------

    def authorize_url(
        self,
        *,
        state: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> str:
        meta = self._meta()
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": self._redirect_uri,
            "scope": self._scopes,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "prompt": "login",
        }
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    def exchange_code(self, *, code: str, code_verifier: str) -> UpstreamUserInfo:
        meta = self._meta()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "code_verifier": code_verifier,
        }
        if self._client_secret:
            auth = (self._client_id, self._client_secret)
        else:
            auth = None
            data["client_id"] = self._client_id
        with httpx.Client(timeout=15.0) as http:
            resp = http.post(meta["token_endpoint"], data=data, auth=auth)
        if resp.status_code != 200:
            raise UpstreamAuthError(
                f"Upstream token exchange failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()
        id_token = body.get("id_token")
        access_token = body.get("access_token")
        if not id_token or not access_token:
            raise UpstreamAuthError("Upstream did not return both id_token and access_token.")

        claims = _decode_id_token_claims_unsafe(id_token)
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise UpstreamAuthError("Upstream id_token has no usable sub claim.")
        email = claims.get("email")
        if self._allowed_domains:
            if not email or "@" not in email:
                raise UpstreamAuthError("Allowed-domain check failed: no email in id_token.")
            domain = email.rsplit("@", 1)[1].lower()
            if domain not in self._allowed_domains:
                raise UpstreamAuthError(f"Sign-in domain {domain!r} not in allowlist.")
        return UpstreamUserInfo(
            sub=sub,
            email=email,
            name=claims.get("name") or claims.get("preferred_username"),
            raw_claims=claims,
        )

    # -- Internals -----------------------------------------------------------

    def _meta(self) -> dict:
        if self._discovery is None:
            url = f"{self._issuer}/.well-known/openid-configuration"
            with httpx.Client(timeout=10.0) as http:
                resp = http.get(url)
            if resp.status_code != 200:
                raise UpstreamConfigError(
                    f"Upstream discovery failed: GET {url} → HTTP {resp.status_code}"
                )
            self._discovery = resp.json()
            for required in ("authorization_endpoint", "token_endpoint"):
                if required not in self._discovery:
                    raise UpstreamConfigError(f"Upstream discovery missing {required!r} at {url}.")
        return self._discovery


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_id_token_claims_unsafe(token: str) -> dict:
    """Decode the JWT body WITHOUT verifying the signature.

    We trust the upstream id_token because we just received it over TLS from
    the configured token endpoint in direct response to our PKCE exchange.
    Signature verification would require fetching the upstream JWKS and is
    redundant in this configuration. If/when we accept upstream tokens via
    a less-trusted channel, we'll add verification (RFC 7515).
    """
    import base64

    parts = token.split(".")
    if len(parts) != 3:
        raise UpstreamAuthError("Malformed upstream id_token.")
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + padding)
        return json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise UpstreamAuthError(f"Could not parse upstream id_token: {exc}") from exc


def _split_csv(raw: str) -> list[str]:
    return [piece.strip() for piece in raw.split(",") if piece.strip()]
