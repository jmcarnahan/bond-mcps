"""Database engine, session factory, and schema-currency check.

URL resolution: BOND_MCPS_DB_URL env var → default sqlite at <repo_root>/tokens.db.
URL validation: refuse Postgres URLs without a strict sslmode setting.
SQLite: enable WAL mode + busy_timeout via a connect-event listener; chmod
the DB file 0600 on first creation.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import scoped_session, sessionmaker

logger = logging.getLogger(__name__)


class SchemaOutOfDateError(RuntimeError):
    """Raised when the DB schema is not at Alembic head."""


_engine_lock = threading.Lock()
_state: dict = {"engine": None, "session_factory": None, "url": None}

_STRICT_SSLMODES = {"require", "verify-ca", "verify-full"}


def _repo_root() -> Path:
    """Return repo root: <auth-package>/../.. → bond-mcps repo root."""
    return Path(__file__).resolve().parents[3]


def default_db_url() -> str:
    db_path = _repo_root() / "tokens.db"
    return f"sqlite:///{db_path}"


def validate_db_url(url: str) -> None:
    """Refuse non-TLS Postgres URLs. SQLite passes through.

    Raises ValueError with a remediation hint on misconfigured URLs.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.split("+")[0]
    if scheme in ("postgresql", "postgres"):
        qs = parse_qs(parts.query)
        sslmode = (qs.get("sslmode", [""])[0] or "").lower()
        if sslmode not in _STRICT_SSLMODES:
            raise ValueError(
                f"Postgres URL must set sslmode to one of {sorted(_STRICT_SSLMODES)}; "
                f"got sslmode={sslmode!r}. For Aurora RDS, use 'verify-full' with "
                f"the RDS root CA bundle."
            )
    elif scheme == "sqlite":
        return
    else:
        # Other dialects — allow but warn (mysql etc are not officially supported here)
        logger.warning("Untested DB dialect: %s. SQLite and PostgreSQL are tested.", scheme)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite:")


def _sqlite_path(url: str) -> Path | None:
    """Extract the on-disk path from a sqlite URL, or None for :memory:."""
    parts = urlsplit(url)
    if parts.scheme != "sqlite":
        return None
    # sqlite:///abs/path → path = "/abs/path"; sqlite:///:memory: → ":memory:"
    raw = parts.path
    if not raw or raw == "/:memory:":
        return None
    return Path(raw)


def _make_engine(url: str) -> Engine:
    if _is_sqlite(url):
        engine = create_engine(
            url,
            echo=False,
            connect_args={"check_same_thread": False},
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            try:
                # busy_timeout must be set FIRST so subsequent statements
                # wait for the lock instead of failing immediately.
                cursor.execute("PRAGMA busy_timeout=5000")
                # WAL mode is persistent. Only switch if not already WAL,
                # to avoid an EXCLUSIVE-lock race when N processes connect
                # to a brand-new DB simultaneously.
                current = cursor.execute("PRAGMA journal_mode").fetchone()
                if current and (current[0] or "").lower() != "wal":
                    try:
                        cursor.execute("PRAGMA journal_mode=WAL")
                    except Exception:
                        # Another process is currently setting WAL mode.
                        # Their change will be visible to us shortly.
                        pass
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        path = _sqlite_path(url)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Touch and chmod to 0600 if newly created. SQLite inherits perms
            # for WAL/SHM sidecars from the main DB file's mode.
            if not path.exists():
                path.touch(mode=0o600)
            else:
                try:
                    os.chmod(path, 0o600)
                except OSError as e:
                    logger.warning("Could not chmod %s to 0600: %s", path, e)
        return engine

    # Non-SQLite (Postgres / Aurora)
    return create_engine(
        url,
        echo=False,
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
    )


def _resolve_url(url: str | None) -> str:
    if url:
        return url
    return os.environ.get("BOND_MCPS_DB_URL") or default_db_url()


def get_engine(url: str | None = None) -> Engine:
    """Return a cached engine for the resolved URL. Validates the URL first."""
    resolved = _resolve_url(url)
    with _engine_lock:
        if _state["engine"] is not None and _state["url"] == resolved:
            return _state["engine"]
        validate_db_url(resolved)
        engine = _make_engine(resolved)
        _state["engine"] = engine
        _state["session_factory"] = scoped_session(
            sessionmaker(bind=engine, autoflush=False, future=True)
        )
        _state["url"] = resolved
        logger.info("DB engine initialized for %s", _scrub_url(resolved))
        return engine


def get_session_factory(url: str | None = None):
    get_engine(url)
    return _state["session_factory"]


def reset_for_tests() -> None:
    """Drop cached engine + session factory. Call between tests."""
    with _engine_lock:
        sf = _state["session_factory"]
        if sf is not None:
            sf.remove()
        engine = _state["engine"]
        if engine is not None:
            engine.dispose()
        _state["engine"] = None
        _state["session_factory"] = None
        _state["url"] = None


def ensure_schema_current(url: str | None = None) -> None:
    """Verify alembic_version is at head. Raises SchemaOutOfDateError if not.

    Lazily imports the alembic_config module to avoid pulling Alembic at
    auth-package import time.
    """
    from auth.alembic_config import get_head_revision

    engine = get_engine(url)
    head = get_head_revision()

    with engine.connect() as conn:
        try:
            current = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()
        except Exception:
            current = None

    if current is None:
        raise SchemaOutOfDateError(
            "DB schema is empty. Run `bond-mcps migrate-db` (or `make migrate-db`) "
            "to create the token database."
        )
    if current != head:
        raise SchemaOutOfDateError(
            f"DB schema at revision {current!r}; expected {head!r}. "
            "Run `bond-mcps migrate-db` (or `make migrate-db`) to upgrade."
        )


def _scrub_url(url: str) -> str:
    """Strip user:password from a URL for logging."""
    parts = urlsplit(url)
    if parts.password:
        scrubbed = parts._replace(
            netloc=f"{parts.username}:***@{parts.hostname}"
            + (f":{parts.port}" if parts.port else "")
        )
        return scrubbed.geturl()
    return url
