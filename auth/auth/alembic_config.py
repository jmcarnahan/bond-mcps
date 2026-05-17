"""Programmatic Alembic configuration.

We deliberately avoid an alembic.ini file — the config is constructed in
Python and points at auth/auth/alembic/.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic.config import Config


def _alembic_dir() -> Path:
    return Path(__file__).resolve().parent / "alembic"


def get_alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_alembic_dir()))
    resolved = url or os.environ.get("BOND_MCPS_DB_URL")
    if resolved is None:
        from auth.db.session import default_db_url

        resolved = default_db_url()
    cfg.set_main_option("sqlalchemy.url", resolved)
    return cfg


def get_head_revision() -> str:
    from alembic.script import ScriptDirectory

    cfg = get_alembic_config()
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("Alembic has no revisions — this should never happen")
    return head


def upgrade_head(url: str | None = None) -> None:
    from alembic import command

    cfg = get_alembic_config(url)
    command.upgrade(cfg, "head")
