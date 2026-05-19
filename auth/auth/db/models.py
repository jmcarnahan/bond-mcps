"""SQLAlchemy models for the encrypted token store.

Two tables. Composite primary keys. No surrogate IDs. No users table —
`user_key` is the identity column, set from BOND_MCPS_USER_ID env var or
getpass.getuser().

LargeBinary columns hold raw `nonce || ciphertext`. Encryption is done in
the repository layer; the model is unaware of encryption semantics.

All DateTime columns use ``timezone=True`` so timestamps are unambiguous
across SQLite (which stores them as TEXT in UTC) and Postgres (which uses
TIMESTAMP WITH TIME ZONE — no implicit server-TZ conversion).
"""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class ProviderToken(Base):
    __tablename__ = "provider_tokens"

    user_key = Column(String, primary_key=True)
    provider = Column(String, primary_key=True)
    access_token_encrypted = Column(LargeBinary, nullable=False)
    refresh_token_encrypted = Column(LargeBinary, nullable=True)
    refresh_token_key_version = Column(Integer, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    scopes = Column(String, nullable=True)
    extra_metadata = Column(JSON, nullable=False, default=dict)
    key_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<ProviderToken user_key={self.user_key!r} provider={self.provider!r} "
            f"key_version={self.key_version} expires_at={self.expires_at}>"
        )


class MsalTokenCache(Base):
    __tablename__ = "msal_token_caches"

    user_key = Column(String, primary_key=True)
    cache_data_encrypted = Column(LargeBinary, nullable=False)
    key_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<MsalTokenCache user_key={self.user_key!r} " f"key_version={self.key_version}>"


# ---------------------------------------------------------------------------
# Authorization Server tables (see auth.auth_server)
# ---------------------------------------------------------------------------


class OAuthClient(Base):
    """Registered OAuth 2.1 clients.

    Populated by RFC 7591 Dynamic Client Registration (``POST /oauth/register``)
    or pre-seeded for deployments that disable DCR.
    """

    __tablename__ = "oauth_clients"

    client_id = Column(String, primary_key=True)
    client_name = Column(String, nullable=True)
    redirect_uris = Column(JSON, nullable=False)
    token_endpoint_auth_method = Column(String, nullable=False, default="none")
    grant_types = Column(JSON, nullable=False)
    response_types = Column(JSON, nullable=False)
    scope = Column(String, nullable=True)
    is_static = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)


class OAuthPendingAuth(Base):
    """In-flight ``/oauth/authorize`` requests awaiting upstream callback."""

    __tablename__ = "oauth_pending_auth"

    bond_state = Column(String, primary_key=True)
    client_id = Column(String, nullable=False)
    redirect_uri = Column(String, nullable=False)
    client_state = Column(String, nullable=True)
    code_challenge = Column(String, nullable=False)
    code_challenge_method = Column(String, nullable=False, default="S256")
    resource = Column(String, nullable=True)
    scope = Column(String, nullable=True)
    upstream_code_verifier_encrypted = Column(LargeBinary, nullable=False)
    key_version = Column(Integer, nullable=False, default=1)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)


class OAuthAuthCode(Base):
    """One-shot authorization codes issued to OAuth clients."""

    __tablename__ = "oauth_auth_codes"

    code_hash = Column(String, primary_key=True)
    client_id = Column(String, nullable=False)
    user_key = Column(String, nullable=False)
    email = Column(String, nullable=True)
    code_challenge = Column(String, nullable=False)
    code_challenge_method = Column(String, nullable=False, default="S256")
    redirect_uri = Column(String, nullable=False)
    resource = Column(String, nullable=True)
    scope = Column(String, nullable=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)


class OAuthRefreshToken(Base):
    """Hashed refresh tokens issued by the bond-mcps Authorization Server."""

    __tablename__ = "oauth_refresh_tokens"

    token_hash = Column(String, primary_key=True)
    client_id = Column(String, nullable=False)
    user_key = Column(String, nullable=False)
    resource = Column(String, nullable=True)
    scope = Column(String, nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)


class ConnectTicket(Base):
    """Short-lived per-user tickets that gate per-MCP /connect/<provider>.

    Minted by a tool path that catches ``MissingProviderConnection`` and
    embedded in the connect URL surfaced to the agent. Consumed exactly
    once by the corresponding GET /connect/<provider>?ticket=... route.
    """

    __tablename__ = "connect_tickets"

    ticket_hash = Column(String, primary_key=True)
    user_key = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
