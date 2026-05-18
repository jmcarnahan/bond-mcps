"""Startup-time configuration validation for MCP servers.

Each MCP entrypoint should call ``verify_runtime_config()`` before binding
its HTTP port so misconfigured deployments fail at boot (and ECS/Fargate
sees the container die immediately) instead of accepting connections and
failing every request.

What it checks:
- The DB URL is reachable and the schema is at Alembic head.
- The encryption key is set and round-trips through encrypt/decrypt.
- For Postgres deployments: BOND_MCPS_USER_ID is set (via the resolver
  used by current_user_key() — raises DeploymentConfigError otherwise).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

SKIP_ENV_VAR = "BOND_MCPS_SKIP_STARTUP_VERIFY"


def verify_runtime_config() -> None:
    """Fail-fast check used by MCP entrypoints.

    Raises whichever exception fits the misconfiguration so the container
    crashes with a single clear error rather than starting and then
    failing every request.

    Set ``BOND_MCPS_SKIP_STARTUP_VERIFY=1`` to bypass for tests that
    exercise tool behavior through the FastMCP client without standing
    up a real DB. **Never set this in production** — it defeats the
    fail-fast guarantee.
    """
    if os.environ.get(SKIP_ENV_VAR):
        logger.warning(
            "Skipping startup configuration verification (%s is set). "
            "This is intended for tests only — do not use in production.",
            SKIP_ENV_VAR,
        )
        return

    # Lazy imports keep `auth.proxy_server` cheap to import (the proxy
    # process doesn't need the DB stack).
    from auth.db import ensure_schema_current
    from auth.db.repository import _default_resolver
    from auth.encryption import verify_encryption_setup
    from auth.token_store import current_user_key

    # 1. Resolving the user_key may raise DeploymentConfigError if we're
    # on Postgres without BOND_MCPS_USER_ID — surface that first.
    user_key = current_user_key()
    logger.info("Resolved user_key for token storage: %s", user_key)

    # 2. DB engine + schema currency.
    ensure_schema_current()
    logger.info("Token DB schema is at head")

    # 3. Encryption key round-trip.
    verify_encryption_setup(_default_resolver())
    logger.info("Encryption setup verified")
