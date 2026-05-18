"""Pin third-party log levels to prevent OAuth token leakage via debug logs.

httpx, Authlib, and MSAL all log request bodies at DEBUG and sometimes
INFO — those bodies include refresh_token=... when token endpoints are
called. Setting these loggers to a quieter level by default keeps tokens
out of logs without requiring every call site to remember.

Importing this module is a no-op; call apply() from your process start.
"""

from __future__ import annotations

import logging


def apply() -> None:
    """Set quieter log levels for libraries that may log token material."""
    # httpx: at DEBUG, logs full request bodies including form-encoded
    # refresh_token grants. INFO is request line only — safe.
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Authlib: similar concern in its client modules.
    logging.getLogger("authlib").setLevel(logging.INFO)
    # MSAL: PII logging is off by default but DEBUG can still echo tokens.
    logging.getLogger("msal").setLevel(logging.WARNING)
