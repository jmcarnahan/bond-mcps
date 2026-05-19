"""TokenRepository: CRUD over encrypted token rows.

This is the single boundary where plaintext tokens cross into / out of the DB.
Callers (TokenStore, MSAL adapter, importer) never touch the encryption
primitives directly.

Token "data" dicts on save_token / get_token use the same shape as the
historical file-based format:
  {access_token, refresh_token?, expires_at?, scopes?, **extras}
where `extras` lands in the extra_metadata JSON column (NOT encrypted —
treated as low-sensitivity reference data, e.g., cloud_id).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from auth import encryption
from auth.db.models import MsalTokenCache, ProviderToken
from auth.db.session import get_session_factory

logger = logging.getLogger(__name__)

# Fields we lift out of `data` into typed columns. Anything else lives in
# extra_metadata.
_RESERVED_FIELDS = {
    "access_token",
    "refresh_token",
    "expires_at",
    "scopes",
}


def build_default_resolver(url: str = "") -> encryption.EncryptionKeyResolver:
    """Build the standard resolver for a given DB URL.

    Postgres URLs require an env-var key (no file fallback). SQLite URLs
    may fall back to ~/.bond_mcps/encryption_key. ``url`` defaults to the
    BOND_MCPS_DB_URL env var when empty.
    """
    if not url:
        url = os.environ.get("BOND_MCPS_DB_URL", "")
    is_postgres = url.startswith("postgres")  # matches postgres:// and postgresql://
    return encryption.EncryptionKeyResolver(allow_file_fallback=not is_postgres)


# Legacy private alias — kept so existing callers keep working until they're
# migrated to the public name. Both names produce identical objects.
_default_resolver = build_default_resolver


class TokenRepository:
    """Sole CRUD entry point for encrypted token rows.

    Sessions are short-lived: open one per operation, commit, close. Never
    hold a session across an `await` boundary in async code.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        resolver: encryption.EncryptionKeyResolver | None = None,
    ):
        # Resolve the URL once and reuse it for both the session factory
        # and the resolver so they can't drift (e.g., explicit Postgres url
        # while env still points at sqlite).
        from auth.db.session import _resolve_url

        resolved = _resolve_url(url)
        self._session_factory = get_session_factory(resolved)
        self._resolver = resolver or _default_resolver(resolved)

    @contextmanager
    def _read_session(self) -> Iterator[Session]:
        """Open a session for read-only operations."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def _write_session(self) -> Iterator[Session]:
        """Open a session for write operations.

        On SQLite, promotes to a write lock immediately (BEGIN IMMEDIATE)
        so concurrent writers serialize cleanly. On Postgres this is a
        no-op — concurrency safety there comes from the dialect-aware
        upsert helpers and from row-level locks in ``locked_token`` /
        ``locked_msal_cache``.
        """
        session = self._session_factory()
        try:
            self._begin_immediate(session)
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _begin_immediate(session: Session) -> None:
        """SQLite-only: promote the implicit transaction to a write lock."""
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))

    @staticmethod
    def _dialect_insert(session: Session, table_cls):
        """Return a dialect-aware INSERT statement supporting ON CONFLICT.

        Both SQLite (3.24+) and Postgres support INSERT ... ON CONFLICT DO
        UPDATE. The two dialects' SQLAlchemy modules expose this differently,
        so we pick the right one per dialect.
        """
        name = session.get_bind().dialect.name
        if name == "sqlite":
            return sqlite_insert(table_cls)
        if name in ("postgresql", "postgres"):
            return pg_insert(table_cls)
        raise NotImplementedError(
            f"Upsert is not implemented for dialect: {name}. "
            "Supported dialects: sqlite, postgresql."
        )

    # ---- ProviderToken CRUD --------------------------------------------------

    def get_token(self, user_key: str, provider: str) -> dict | None:
        """Return decrypted token data dict, or None if no row exists.

        Caller is responsible for expiry semantics (mirrors the old TokenStore
        API — refresh_if_needed uses raw load too).
        """
        with self._read_session() as s:
            row = s.get(ProviderToken, (user_key, provider))
            if row is None:
                return None
            return self._decode_row(row)

    def save_token(self, user_key: str, provider: str, data: dict) -> None:
        """Upsert a token row using dialect-aware ON CONFLICT DO UPDATE.

        Concurrency-safe on both SQLite (atomic INSERT-or-REPLACE under the
        IMMEDIATE write lock) and Postgres (atomic INSERT ... ON CONFLICT
        DO UPDATE — no IntegrityError race).
        """
        access_token = data.get("access_token")
        if not access_token:
            raise ValueError("save_token requires 'access_token' in data")

        values = self._encode_provider_token_values(user_key, provider, data)
        with self._write_session() as s:
            self._upsert_provider_token(s, user_key, provider, values)

    def clear_token(self, user_key: str, provider: str) -> None:
        """Delete the token row if it exists. Idempotent under concurrency
        (a concurrent DELETE either races and finds the row, or finds it
        already gone — both return successfully)."""
        with self._write_session() as s:
            row = s.get(ProviderToken, (user_key, provider))
            if row is not None:
                s.delete(row)

    def _encode_provider_token_values(self, user_key: str, provider: str, data: dict) -> dict:
        """Encrypt and shape a token-data dict into column values for upsert."""
        access_blob, key_version = encryption.encrypt(
            _to_bytes(data["access_token"]),
            user_key=user_key,
            provider=provider,
            field="access_token",
            resolver=self._resolver,
        )
        refresh_blob, refresh_version = encryption.encrypt_optional(
            _to_bytes(data.get("refresh_token")),
            user_key=user_key,
            provider=provider,
            field="refresh_token",
            resolver=self._resolver,
        )
        scopes = data.get("scopes")
        scopes_str = scopes if isinstance(scopes, str) or scopes is None else " ".join(scopes)
        extras = {k: v for k, v in data.items() if k not in _RESERVED_FIELDS}
        return {
            "access_token_encrypted": access_blob,
            "refresh_token_encrypted": refresh_blob,
            "refresh_token_key_version": refresh_version,
            "expires_at": _coerce_expires_at(data.get("expires_at")),
            "scopes": scopes_str,
            "extra_metadata": extras,
            "key_version": key_version,
        }

    def _upsert_provider_token(
        self, session: Session, user_key: str, provider: str, values: dict
    ) -> None:
        """Dialect-aware INSERT ... ON CONFLICT (user_key, provider) DO UPDATE.

        Sets updated_at via func.now() so the column reflects the upsert
        time, since ORM ``onupdate`` only fires for ORM-style mutations.
        """
        stmt = self._dialect_insert(session, ProviderToken).values(
            user_key=user_key, provider=provider, **values
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_key", "provider"],
            set_={**values, "updated_at": func.now()},
        )
        session.execute(stmt)

    def _upsert_msal_cache(
        self, session: Session, user_key: str, cache_blob: bytes, key_version: int
    ) -> None:
        stmt = self._dialect_insert(session, MsalTokenCache).values(
            user_key=user_key,
            cache_data_encrypted=cache_blob,
            key_version=key_version,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_key"],
            set_={
                "cache_data_encrypted": cache_blob,
                "key_version": key_version,
                "updated_at": func.now(),
            },
        )
        session.execute(stmt)

    @contextmanager
    def locked_token(self, user_key: str, provider: str) -> Iterator[LockedToken]:
        """Open a write-locked context for the read-modify-write refresh path.

        SQLite: BEGIN IMMEDIATE so concurrent processes block on the lock.
        Postgres: SELECT ... FOR UPDATE so the row is locked.
        """
        session = self._session_factory()
        try:
            self._begin_immediate(session)
            bind = session.get_bind()
            if bind.dialect.name in ("postgresql", "postgres"):
                stmt = (
                    select(ProviderToken)
                    .where(
                        ProviderToken.user_key == user_key,
                        ProviderToken.provider == provider,
                    )
                    .with_for_update()
                )
                row = session.execute(stmt).scalar_one_or_none()
            else:
                row = session.get(ProviderToken, (user_key, provider))
            yield LockedToken(self, session, user_key, provider, row)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---- MSAL cache CRUD -----------------------------------------------------

    def get_msal_cache(self, user_key: str) -> str | None:
        with self._read_session() as s:
            row = s.get(MsalTokenCache, user_key)
            if row is None:
                return None
            return encryption.decrypt(
                row.cache_data_encrypted,
                user_key=user_key,
                provider="__msal__",
                field="cache_data",
                key_version=row.key_version,
                resolver=self._resolver,
            ).decode("utf-8")

    def save_msal_cache(self, user_key: str, cache_json: str) -> None:
        """Upsert the MSAL serialized cache blob for a user."""
        blob, version = encryption.encrypt(
            cache_json.encode("utf-8"),
            user_key=user_key,
            provider="__msal__",
            field="cache_data",
            resolver=self._resolver,
        )
        with self._write_session() as s:
            self._upsert_msal_cache(s, user_key, blob, version)

    def clear_msal_cache(self, user_key: str) -> None:
        with self._write_session() as s:
            row = s.get(MsalTokenCache, user_key)
            if row is not None:
                s.delete(row)

    @contextmanager
    def locked_msal_cache(self, user_key: str) -> Iterator[LockedMsalCache]:
        """Open a write-locked context spanning the entire MSAL R-M-W cycle.

        MSAL's silent acquisition rotates the refresh_token on every call.
        Without locking the read AND the write under one transaction, two
        processes can both load the same blob, both call Microsoft with the
        same refresh_token, and the second call fails with 'invalid_grant'
        because the first call invalidated the token on use.

        SQLite: BEGIN IMMEDIATE serializes writers; readers in WAL mode
        continue uninterrupted.
        Postgres: SELECT ... FOR UPDATE locks the single row.
        """
        session = self._session_factory()
        try:
            self._begin_immediate(session)
            bind = session.get_bind()
            if bind.dialect.name in ("postgresql", "postgres"):
                stmt = (
                    select(MsalTokenCache)
                    .where(MsalTokenCache.user_key == user_key)
                    .with_for_update()
                )
                row = session.execute(stmt).scalar_one_or_none()
            else:
                row = session.get(MsalTokenCache, user_key)

            if row is not None:
                blob = encryption.decrypt(
                    row.cache_data_encrypted,
                    user_key=user_key,
                    provider="__msal__",
                    field="cache_data",
                    key_version=row.key_version,
                    resolver=self._resolver,
                ).decode("utf-8")
            else:
                blob = None

            handle = LockedMsalCache(self, session, user_key, row, blob)
            yield handle
            if handle._dirty:
                handle._persist()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---- internals -----------------------------------------------------------

    def _decode_row(self, row: ProviderToken) -> dict:
        access = encryption.decrypt(
            row.access_token_encrypted,
            user_key=row.user_key,
            provider=row.provider,
            field="access_token",
            key_version=row.key_version,
            resolver=self._resolver,
        ).decode("utf-8")
        refresh = encryption.decrypt_optional(
            row.refresh_token_encrypted,
            row.refresh_token_key_version,
            user_key=row.user_key,
            provider=row.provider,
            field="refresh_token",
            resolver=self._resolver,
        )
        out: dict = {"access_token": access}
        if refresh is not None:
            out["refresh_token"] = refresh.decode("utf-8")
        if row.expires_at is not None:
            out["expires_at"] = _to_epoch(row.expires_at)
        if row.scopes:
            out["scopes"] = row.scopes
        if row.extra_metadata:
            for k, v in row.extra_metadata.items():
                out[k] = v
        return out


class LockedToken:
    """Handle for a row-locked refresh transaction.

    Use:
        with repo.locked_token(user_key, provider) as locked:
            data = locked.data
            if data and locked.is_expired():
                new_data = call_refresh_endpoint(data["refresh_token"])
                locked.update(new_data)
    """

    def __init__(
        self,
        repo: TokenRepository,
        session: Session,
        user_key: str,
        provider: str,
        row: ProviderToken | None,
    ):
        self._repo = repo
        self._session = session
        self._user_key = user_key
        self._provider = provider
        self._row = row
        self._data: dict | None = repo._decode_row(row) if row else None

    @property
    def data(self) -> dict | None:
        return self._data

    def is_expired(self, *, buffer_seconds: float = 60.0) -> bool:
        if self._data is None:
            return False
        expires_at = self._data.get("expires_at")
        if expires_at is None:
            return False
        return time.time() >= (expires_at - buffer_seconds)

    def update(self, new_data: dict) -> None:
        """Persist refreshed token data while still inside the lock.

        Uses the same upsert helper as save_token so it's safe even on
        Postgres if the row didn't exist at lock acquisition time (which
        would defeat SELECT...FOR UPDATE — a non-existent row can't be
        row-locked).
        """
        if not new_data.get("access_token"):
            raise ValueError("update() requires 'access_token' in new_data")

        # Preserve extras (e.g., Atlassian cloud_id) from the row being
        # refreshed; new_data wins on any keys it explicitly supplies.
        # _encode_provider_token_values filters reserved keys out into
        # `extras` itself, so merging the full new_data is safe.
        prior_extras = dict(self._row.extra_metadata or {}) if self._row else {}
        merged = {**prior_extras, **new_data}

        values = self._repo._encode_provider_token_values(self._user_key, self._provider, merged)
        self._repo._upsert_provider_token(self._session, self._user_key, self._provider, values)
        # The upsert bypasses the ORM, so the session's identity map still
        # holds the stale snapshot of self._row. Expire it (if cached) and
        # re-read from the DB so locked.data reflects the just-written row.
        if self._row is not None:
            self._session.expire(self._row)
        self._row = self._session.get(ProviderToken, (self._user_key, self._provider))
        self._data = self._repo._decode_row(self._row)


class LockedMsalCache:
    """Handle for a row-locked MSAL cache R-M-W transaction.

    Use:
        with repo.locked_msal_cache(user_key) as handle:
            cache = msal.SerializableTokenCache()
            if handle.blob:
                cache.deserialize(handle.blob)
            # ... run MSAL silent acquire, which may mutate cache ...
            if cache.has_state_changed:
                handle.set_blob(cache.serialize())
    """

    def __init__(
        self,
        repo: TokenRepository,
        session: Session,
        user_key: str,
        row: MsalTokenCache | None,
        blob: str | None,
    ):
        self._repo = repo
        self._session = session
        self._user_key = user_key
        self._row = row
        self._blob = blob
        self._dirty = False

    @property
    def blob(self) -> str | None:
        return self._blob

    def set_blob(self, new_blob: str) -> None:
        """Mark the cache as dirty with the new serialized blob.

        The blob will be encrypted and persisted on context exit (commit).
        """
        if new_blob is None:
            raise ValueError("set_blob requires a non-None string")
        self._blob = new_blob
        self._dirty = True

    def _persist(self) -> None:
        blob_bytes, version = encryption.encrypt(
            self._blob.encode("utf-8"),
            user_key=self._user_key,
            provider="__msal__",
            field="cache_data",
            resolver=self._repo._resolver,
        )
        self._repo._upsert_msal_cache(self._session, self._user_key, blob_bytes, version)


def _to_bytes(s: str | bytes | None) -> bytes | None:
    """UTF-8-encode a str, pass bytes through, propagate None."""
    if s is None:
        return None
    return s if isinstance(s, bytes) else s.encode("utf-8")


def _coerce_expires_at(value) -> _dt.datetime | None:
    """Accept a unix-epoch float, an ISO datetime string, or a datetime; return tz-aware UTC datetime.

    All DB columns are TIMESTAMP WITH TIME ZONE, so we always write tz-aware
    values. Naive inputs are interpreted as UTC.
    """
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=_dt.timezone.utc)
        return value.astimezone(_dt.timezone.utc)
    if isinstance(value, (int, float)):
        return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
    if isinstance(value, str):
        try:
            dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc)
    return None


def _to_epoch(dt: _dt.datetime) -> float:
    """Convert a datetime (tz-aware or naive) to a unix-epoch float.

    Naive datetimes are interpreted as UTC for backward compatibility with
    SQLite TEXT-stored timestamps that may come back naive on some drivers.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()
