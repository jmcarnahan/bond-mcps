"""Database layer for the bond-mcps token store.

Imports here are kept lightweight; SQLAlchemy/cryptography are pulled in by
sub-modules on demand so importing the `auth` package alone (e.g., for the
proxy server) does not pay the DB-stack startup cost.
"""

from auth.db.repository import TokenRepository
from auth.db.session import (
    SchemaOutOfDateError,
    default_db_url,
    ensure_schema_current,
    get_engine,
    get_session_factory,
    reset_for_tests,
    validate_db_url,
)

__all__ = [
    "SchemaOutOfDateError",
    "TokenRepository",
    "default_db_url",
    "ensure_schema_current",
    "get_engine",
    "get_session_factory",
    "reset_for_tests",
    "validate_db_url",
]
