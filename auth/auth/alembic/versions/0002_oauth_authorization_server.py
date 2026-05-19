"""oauth authorization server tables + connect tickets

Adds five tables that support the bond-mcps Authorization Server:

* ``oauth_clients``     -- RFC 7591 dynamic client registry + statically
                           configured clients (e.g. the Claude Code public
                           client when DCR is disabled in a deployment).
* ``oauth_pending_auth`` -- in-flight ``/oauth/authorize`` requests waiting
                           on the upstream IdP (Cognito/Okta) redirect.
* ``oauth_auth_codes``  -- one-shot codes the AS issues to clients after a
                           successful upstream login; consumed at
                           ``/oauth/token`` against the original PKCE
                           challenge.
* ``oauth_refresh_tokens`` -- hashed refresh tokens issued by the AS,
                           scoped to (client_id, user_key, resource).
* ``connect_tickets``   -- short-lived opaque tickets bound to a user_key,
                           consumed by per-MCP ``/connect/<provider>``
                           flows after a ``MissingProviderConnection``
                           error.

Sensitive secret material (auth codes, refresh tokens) is stored hashed
or AEAD-encrypted with the existing key in ``auth.encryption``; no plain-
text token ever appears in the DB.

Revision ID: 0002_oauth_authorization_server
Revises: 0001_initial_schema
Create Date: 2026-05-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_oauth_authorization_server"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("client_name", sa.String(), nullable=True),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column(
            "token_endpoint_auth_method",
            sa.String(),
            nullable=False,
            server_default="none",
        ),
        sa.Column("grant_types", sa.JSON(), nullable=False),
        sa.Column("response_types", sa.JSON(), nullable=False),
        sa.Column("scope", sa.String(), nullable=True),
        sa.Column("is_static", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("client_id"),
    )

    op.create_table(
        "oauth_pending_auth",
        sa.Column("bond_state", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("client_state", sa.String(), nullable=True),
        sa.Column("code_challenge", sa.String(), nullable=False),
        sa.Column(
            "code_challenge_method",
            sa.String(),
            nullable=False,
            server_default="S256",
        ),
        sa.Column("resource", sa.String(), nullable=True),
        sa.Column("scope", sa.String(), nullable=True),
        # Upstream PKCE leg (AS → Cognito/Okta). AEAD-encrypted at rest so
        # a leaked DB snapshot can't be replayed against the upstream IdP.
        sa.Column("upstream_code_verifier_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("bond_state"),
    )
    op.create_index(
        "ix_oauth_pending_auth_expires_at",
        "oauth_pending_auth",
        ["expires_at"],
    )

    op.create_table(
        "oauth_auth_codes",
        # The opaque code value is hashed (SHA-256, base64url) on the way
        # in so a leaked snapshot doesn't let an attacker complete the
        # exchange — see auth.auth_server.codes.
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("user_key", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("code_challenge", sa.String(), nullable=False),
        sa.Column(
            "code_challenge_method",
            sa.String(),
            nullable=False,
            server_default="S256",
        ),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=True),
        sa.Column("scope", sa.String(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    op.create_index(
        "ix_oauth_auth_codes_expires_at",
        "oauth_auth_codes",
        ["expires_at"],
    )

    op.create_table(
        "oauth_refresh_tokens",
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("user_key", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=True),
        sa.Column("scope", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index(
        "ix_oauth_refresh_tokens_user_key",
        "oauth_refresh_tokens",
        ["user_key"],
    )

    op.create_table(
        "connect_tickets",
        sa.Column("ticket_hash", sa.String(), nullable=False),
        sa.Column("user_key", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("ticket_hash"),
    )
    op.create_index(
        "ix_connect_tickets_expires_at",
        "connect_tickets",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_connect_tickets_expires_at", table_name="connect_tickets")
    op.drop_table("connect_tickets")
    op.drop_index("ix_oauth_refresh_tokens_user_key", table_name="oauth_refresh_tokens")
    op.drop_table("oauth_refresh_tokens")
    op.drop_index("ix_oauth_auth_codes_expires_at", table_name="oauth_auth_codes")
    op.drop_table("oauth_auth_codes")
    op.drop_index("ix_oauth_pending_auth_expires_at", table_name="oauth_pending_auth")
    op.drop_table("oauth_pending_auth")
    op.drop_table("oauth_clients")
