"""Shared exception types raised by MCP token resolvers."""

from __future__ import annotations


class MissingProviderConnection(PermissionError):
    """The authenticated user has no stored token for a given provider.

    Raised by per-MCP token resolvers in JWT mode (multi-tenant deployments)
    when the user's identity has been validated but they have not yet
    completed the upstream provider OAuth flow for this MCP.

    The MCP server is expected to render :attr:`connect_url` into a
    structured tool-error payload so the calling agent (Claude Code) can
    surface it to the user. The URL points to the per-MCP ``/connect/<provider>``
    endpoint, which kicks off the per-user provider OAuth flow and writes a
    row into ``tokens.db`` keyed by the JWT's ``sub`` claim.

    The ``ticket`` parameter, when provided, is the opaque short-lived
    identifier bound to the user_key by ``auth.connect_tickets``; including
    it in the URL is what makes the resulting OAuth round-trip safe to
    follow from an out-of-band browser session.
    """

    def __init__(
        self,
        provider: str,
        user_key: str,
        *,
        connect_url: str | None = None,
        message: str | None = None,
    ):
        self.provider = provider
        self.user_key = user_key
        self.connect_url = connect_url
        super().__init__(message or self._default_message())

    def _default_message(self) -> str:
        url = self.connect_url or f"/connect/{self.provider}"
        return (
            f"{self.provider} is not connected for the current user. "
            f"Open {url} in a browser to authorize."
        )
