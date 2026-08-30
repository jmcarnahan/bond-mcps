"""Shared OAuth callback proxy and token store for Bond MCP servers."""

# The submodule imports below make `from auth import log_discipline` and
# `from auth import startup` work without a separate `import auth.startup`
# call site. They are intentionally NOT in __all__ — `from auth import *`
# isn't a supported usage pattern here.
from auth import log_discipline, startup  # noqa: F401
from auth.exceptions import MissingProviderConnection
from auth.options_parser import opt_bool, opt_int, parse_options
from auth.proxy_client import AuthStateExpiredError, OAuthProxyClient
from auth.token_store import (
    TokenStore,
    current_user_key,
    resolve_user_key_for_request,
)

__all__ = [
    "AuthStateExpiredError",
    "MissingProviderConnection",
    "OAuthProxyClient",
    "TokenStore",
    "current_user_key",
    "opt_bool",
    "opt_int",
    "parse_options",
    "resolve_user_key_for_request",
]
