"""initial schema: provider_tokens and msal_token_caches

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provider_tokens",
        sa.Column("user_key", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("access_token_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("refresh_token_key_version", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("scopes", sa.String(), nullable=True),
        sa.Column("extra_metadata", sa.JSON(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("user_key", "provider"),
    )
    op.create_table(
        "msal_token_caches",
        sa.Column("user_key", sa.String(), nullable=False),
        sa.Column("cache_data_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("user_key"),
    )


def downgrade() -> None:
    op.drop_table("msal_token_caches")
    op.drop_table("provider_tokens")
