"""bond-mcps OAuth 2.1 Authorization Server.

A standalone Starlette ASGI app, separate from the OAuth callback proxy
(``auth.proxy_server``). The two services run as independent processes:

* ``auth.proxy_server`` (port 8000) — laptop/CLI callback relay (unchanged).
* ``auth.auth_server`` (port 8001 by default) — OAuth 2.1 AS endpoints.

This split keeps the existing stdlib proxy and its test surface intact,
while letting the AS take advantage of Starlette's request parsing and
response helpers for the form-encoded + JSON heavy OAuth wire protocol.

When opted out (``BOND_MCPS_AS_ENABLED`` unset / falsy), the AS doesn't run
at all — laptop mode keeps using the existing single-tenant fallback.

Endpoints, when enabled:

* ``GET /.well-known/oauth-authorization-server`` (RFC 8414)
* ``GET /.well-known/jwks.json``
* ``GET /oauth/authorize``       — public-client PKCE start
* ``GET /oauth/upstream/callback`` — receives the upstream OIDC IdP callback
* ``POST /oauth/token``          — code+refresh exchange
* ``POST /oauth/register``       — RFC 7591 Dynamic Client Registration
* ``GET /healthz``               — k8s probe
"""

from auth.auth_server.endpoints import build_app, run

__all__ = ["build_app", "run"]
