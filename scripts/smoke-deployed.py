#!/usr/bin/env python3
"""Post-deploy smoke test for a bond-mcps stack.

Verifies the wire-protocol shape every Claude Code OAuth client depends on,
without requiring a human-in-the-loop Cognito sign-in. Run it after
``terraform apply`` and after seeding the SM secrets to catch the common
deployment misconfigurations before involving a real user.

Checks against the Authorization Server:
  * /healthz returns 200
  * /.well-known/oauth-authorization-server is RFC 8414-shaped
  * /.well-known/jwks.json contains at least one RS256 key with kid
  * /oauth/register accepts a public-client DCR with loopback redirect
  * /oauth/register rejects non-loopback HTTPS when the allowlist is empty
    (fail-closed security guard)

Per-MCP checks:
  * /healthz returns 200
  * /.well-known/oauth-protected-resource/mcp advertises the canonical
    resource URI + the AS URL in authorization_servers
  * POST /mcp without a Bearer returns 401 with WWW-Authenticate Bearer
    ... resource_metadata="..." per RFC 9728

Exit code: 0 = all checks pass; 1 = at least one check failed.

Usage:
  python scripts/smoke-deployed.py \\
    --as-url https://auth.mcps.example.com \\
    --mcp-url https://microsoft.mcps.example.com \\
    --mcp-url https://atlassian.mcps.example.com

Requires: httpx (pip install httpx). No bond-mcps repo dependency, so this
script can live on any operator workstation that has Python 3.10+ and httpx.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Callable, List
from urllib.parse import urljoin

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.stderr.write("Error: httpx not installed. `pip install httpx` and retry.\n")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Small CLI plumbing
# ---------------------------------------------------------------------------


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass
class Result:
    passed: bool
    name: str
    detail: str = ""

    def fmt(self) -> str:
        if self.passed:
            return f"  {GREEN}✓{RESET} {self.name}"
        return f"  {RED}✗{RESET} {self.name}\n      {RED}{self.detail}{RESET}"


@dataclass
class Section:
    title: str
    results: List[Result] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)


# ---------------------------------------------------------------------------
# AS checks
# ---------------------------------------------------------------------------


def check_as(client: httpx.Client, as_url: str) -> Section:
    section = Section(f"Authorization Server: {as_url}")
    add = section.results.append

    # 1. /healthz
    try:
        r = client.get(urljoin(as_url + "/", "healthz"), timeout=10)
        if r.status_code == 200 and r.json().get("status") == "ok":
            add(Result(True, "GET /healthz → 200 status:ok"))
        else:
            add(Result(False, "GET /healthz", f"status={r.status_code} body={r.text[:160]}"))
    except Exception as exc:
        add(Result(False, "GET /healthz", f"connect error: {exc}"))
        return section  # bail; nothing else will work

    # 2. /.well-known/oauth-authorization-server
    try:
        r = client.get(
            urljoin(as_url + "/", ".well-known/oauth-authorization-server"),
            timeout=10,
        )
        r.raise_for_status()
        meta = r.json()
    except Exception as exc:
        add(Result(False, "GET /.well-known/oauth-authorization-server", str(exc)))
        return section

    if meta.get("issuer", "").rstrip("/") == as_url.rstrip("/"):
        add(Result(True, f"AS issuer matches ({meta['issuer']})"))
    else:
        add(Result(
            False, "AS issuer mismatch",
            f"metadata issuer={meta.get('issuer')!r} vs as-url={as_url!r}",
        ))

    grants = meta.get("grant_types_supported", [])
    if {"authorization_code", "refresh_token"}.issubset(grants):
        add(Result(True, "advertises authorization_code + refresh_token grants"))
    else:
        add(Result(False, "grant_types_supported", f"got {grants}"))

    if meta.get("code_challenge_methods_supported") == ["S256"]:
        add(Result(True, "code_challenge_methods_supported = [S256]"))
    else:
        add(Result(
            False, "code_challenge_methods_supported",
            f"got {meta.get('code_challenge_methods_supported')!r}",
        ))

    if meta.get("token_endpoint_auth_methods_supported") == ["none"]:
        add(Result(True, "token_endpoint_auth_methods_supported = [none]"))
    else:
        add(Result(
            False, "token_endpoint_auth_methods_supported",
            f"got {meta.get('token_endpoint_auth_methods_supported')!r}",
        ))

    for ep in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "jwks_uri"):
        if not isinstance(meta.get(ep), str) or not meta[ep].startswith(("http://", "https://")):
            add(Result(False, f"AS metadata.{ep}", f"missing or malformed: {meta.get(ep)!r}"))
        else:
            add(Result(True, f"AS metadata.{ep} present"))

    # 3. /.well-known/jwks.json
    try:
        r = client.get(meta["jwks_uri"], timeout=10)
        r.raise_for_status()
        jwks = r.json()
    except Exception as exc:
        add(Result(False, "GET jwks_uri", str(exc)))
        return section

    keys = jwks.get("keys") or []
    if not keys:
        add(Result(False, "JWKS has at least one key", f"keys={keys!r}"))
        return section

    k0 = keys[0]
    if k0.get("kty") == "RSA" and k0.get("alg") == "RS256" and k0.get("use") == "sig":
        add(Result(True, f"JWKS publishes RS256 signing key (kid={k0.get('kid', '?')})"))
    else:
        add(Result(False, "JWKS key shape", f"got {k0!r}"))

    # 4. DCR happy path — public client, loopback redirect
    dcr_payload = {
        "client_name": "bond-mcps-smoke",
        "redirect_uris": ["http://127.0.0.1:55555/callback"],
    }
    try:
        r = client.post(
            meta["registration_endpoint"], json=dcr_payload, timeout=10,
        )
    except Exception as exc:
        add(Result(False, "POST /oauth/register (loopback)", str(exc)))
        return section
    if r.status_code == 201:
        body = r.json()
        cid = body.get("client_id", "")
        if cid.startswith("bm-") and body.get("client_secret_expires_at") == 0:
            add(Result(True, f"DCR happy path → 201, client_id={cid}"))
        else:
            add(Result(
                False, "DCR response shape",
                f"got {json.dumps(body)[:200]}",
            ))
    else:
        add(Result(
            False, "DCR happy path",
            f"status={r.status_code} body={r.text[:200]}",
        ))

    # 5. DCR fail-closed — non-loopback HTTPS without allowlist entry
    dcr_evil = {
        "client_name": "bond-mcps-smoke-evil",
        "redirect_uris": ["https://evil.example/cb"],
    }
    try:
        r = client.post(meta["registration_endpoint"], json=dcr_evil, timeout=10)
    except Exception as exc:
        add(Result(False, "POST /oauth/register (https unallowlisted)", str(exc)))
        return section
    if r.status_code == 400 and "ALLOWED_REDIRECT_HOSTS" in r.text:
        add(Result(
            True,
            "DCR rejects non-loopback HTTPS by default (security guard)",
        ))
    else:
        add(Result(
            False, "DCR fail-closed",
            f"expected 400 + ALLOWED_REDIRECT_HOSTS hint; got {r.status_code}: {r.text[:200]}",
        ))

    return section


# ---------------------------------------------------------------------------
# MCP checks
# ---------------------------------------------------------------------------


def check_mcp(client: httpx.Client, mcp_url: str, as_url: str) -> Section:
    section = Section(f"MCP: {mcp_url}")
    add = section.results.append

    base = mcp_url.rstrip("/")
    if not base.endswith("/mcp"):
        # Accept either form: the user passed the public base or the /mcp endpoint.
        mcp_path_url = f"{base}/mcp"
        mcp_base_url = base
    else:
        mcp_path_url = base
        mcp_base_url = base[: -len("/mcp")]

    # 1. /healthz
    try:
        r = client.get(f"{mcp_base_url}/healthz", timeout=10)
        if r.status_code == 200:
            add(Result(True, "GET /healthz → 200"))
        else:
            add(Result(False, "GET /healthz", f"status={r.status_code}"))
    except Exception as exc:
        add(Result(False, "GET /healthz", f"connect error: {exc}"))
        return section

    # 2. /.well-known/oauth-protected-resource/mcp
    prm_url = f"{mcp_base_url}/.well-known/oauth-protected-resource/mcp"
    try:
        r = client.get(prm_url, timeout=10)
        r.raise_for_status()
        prm = r.json()
    except Exception as exc:
        add(Result(False, f"GET {prm_url}", str(exc)))
        return section

    if prm.get("resource", "").rstrip("/") == mcp_path_url.rstrip("/"):
        add(Result(True, f"PRM resource = {prm['resource']}"))
    else:
        add(Result(
            False, "PRM resource URI",
            f"got {prm.get('resource')!r}; expected {mcp_path_url!r}",
        ))

    auth_servers = [s.rstrip("/") for s in (prm.get("authorization_servers") or [])]
    if as_url.rstrip("/") in auth_servers:
        add(Result(True, f"PRM authorization_servers includes {as_url}"))
    else:
        add(Result(
            False, "PRM authorization_servers",
            f"got {auth_servers!r}; expected to contain {as_url!r}",
        ))

    if "header" in (prm.get("bearer_methods_supported") or []):
        add(Result(True, "PRM bearer_methods_supported includes 'header'"))
    else:
        add(Result(
            False, "PRM bearer_methods_supported",
            f"got {prm.get('bearer_methods_supported')!r}",
        ))

    # 3. POST /mcp without Bearer → 401 with WWW-Authenticate Bearer ... resource_metadata=...
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "1"},
        },
    }
    try:
        r = client.post(
            mcp_path_url, json=init_body, timeout=10,
            headers={"Accept": "application/json, text/event-stream"},
        )
    except Exception as exc:
        add(Result(False, "POST /mcp without Bearer", str(exc)))
        return section

    if r.status_code != 401:
        add(Result(
            False, "POST /mcp without Bearer",
            f"expected 401; got {r.status_code}",
        ))
        return section

    www_auth = r.headers.get("www-authenticate", "")
    if "Bearer" in www_auth and "resource_metadata=" in www_auth:
        add(Result(True, "401 includes WWW-Authenticate Bearer + resource_metadata"))
    else:
        add(Result(
            False, "WWW-Authenticate shape",
            f"got {www_auth!r}",
        ))

    return section


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--as-url", required=True,
        help="Authorization Server base URL, e.g. https://auth.mcps.example.com",
    )
    parser.add_argument(
        "--mcp-url", action="append", default=[], dest="mcp_urls",
        help="MCP base URL (without /mcp suffix; or with). Pass once per MCP.",
    )
    parser.add_argument(
        "--skip-tls-verify", action="store_true",
        help="Disable TLS verification (for dev with self-signed certs).",
    )
    args = parser.parse_args(argv)

    print(f"{BOLD}bond-mcps deployment smoke{RESET}\n")
    print(f"  AS:     {args.as_url}")
    if args.mcp_urls:
        for u in args.mcp_urls:
            print(f"  MCP:    {u}")
    print()

    transport_kwargs = {"verify": not args.skip_tls_verify}
    with httpx.Client(follow_redirects=False, **transport_kwargs) as client:
        sections: list[Section] = [check_as(client, args.as_url.rstrip("/"))]
        for mcp_url in args.mcp_urls:
            sections.append(check_mcp(client, mcp_url, args.as_url))

    overall_ok = True
    for s in sections:
        print(f"{BOLD}{s.title}{RESET}")
        for r in s.results:
            print(r.fmt())
        print()
        overall_ok &= s.all_passed

    if overall_ok:
        print(f"{GREEN}{BOLD}All smoke checks passed.{RESET}")
        return 0
    print(f"{RED}{BOLD}Smoke check failures above. See specific messages.{RESET}")
    print(f"\n{YELLOW}Next steps for the common failure modes:{RESET}")
    print("  - AS issuer mismatch  → BOND_MCPS_AS_BASE_URL trailing slash / typo")
    print("  - PRM resource URI    → BOND_MCPS_PUBLIC_URL on the MCP")
    print("  - WWW-Authenticate    → JWT mode env vars missing on the MCP pod")
    print("  - 401 from /healthz   → service down; check kubectl logs")
    print("  - TLS errors          → ACM cert validation pending; wait + retry")
    print("  - 5xx everywhere      → SM secret unseeded; see docs/deployment/pre-deploy-checklist.md")
    return 1


if __name__ == "__main__":
    sys.exit(main())
