"""refresh token successor pointer

Adds ``oauth_refresh_tokens.replaced_by_hash``: the SHA-256 base64url
fingerprint of the successor minted when a refresh token was rotated.

A refresh whose response never reaches the client leaves the AS holding a
revoked token and an unused successor nobody has. Retrying with the only
token the client still has is a replay, and today that signs the user out.
With this column the AS can tell the two apart: a revoked row whose recorded
successor has never been used was a lost response, not a stolen token, so
the presentation is honoured again (the unused successor is retired and a
fresh one minted). NULL on a live row and on a row the grace path retired.

Revision ID: 0003_refresh_token_successor
Revises: 0002_oauth_authorization_server
Create Date: 2026-09-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_refresh_token_successor"
down_revision: Union[str, None] = "0002_oauth_authorization_server"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode for SQLite parity with downgrade (SQLite has no ALTER TABLE
    # DROP COLUMN before 3.35, so the pair is written the same way).
    with op.batch_alter_table("oauth_refresh_tokens") as batch:
        batch.add_column(sa.Column("replaced_by_hash", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("oauth_refresh_tokens") as batch:
        batch.drop_column("replaced_by_hash")
