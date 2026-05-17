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
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import select
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


def _default_resolver() -> encryption.EncryptionKeyResolver:
    # Postgres URLs require a strict env-var key; SQLite may use file fallback.
    url = os.environ.get("BOND_MCPS_DB_URL", "")
    is_postgres = url.startswith("postgres") or url.startswith("postgresql")
    return encryption.EncryptionKeyResolver(allow_file_fallback=not is_postgres)


class TokenRepository:
    """Sole CRUD entry point for encrypted token rows.

    Sessions are short-lived: open one per operation, commit, close. Never
    hold a session across an `await` boundary in async code.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        resolver: encryption.KeyResolver | None = None,
    ):
        self._session_factory = get_session_factory(url)
        self._resolver = resolver or _default_resolver()

    @contextmanager
    def _session(self, *, lock_for_write: bool = False) -> Iterator[Session]:
        session = self._session_factory()
        try:
            if lock_for_write:
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
        """Promote the implicit transaction to a write lock when possible.

        SQLite: BEGIN IMMEDIATE acquires RESERVED, serializing concurrent
        writers and preventing two refreshers from racing on the same row.
        Postgres: no-op here; per-row locking happens via SELECT...FOR UPDATE
        inside the body of the locked operation.
        """
        bind = session.get_bind()
        if bind.dialect.name == "sqlite":
            session.execute(__import__("sqlalchemy").text("BEGIN IMMEDIATE"))

    # ---- ProviderToken CRUD --------------------------------------------------

    def get_token(self, user_key: str, provider: str) -> dict | None:
        """Return decrypted token data dict, or None if no row exists.

        Caller is responsible for expiry semantics (mirrors the old TokenStore
        API — refresh_if_needed uses raw load too).
        """
        with self._session() as s:
            row = s.get(ProviderToken, (user_key, provider))
            if row is None:
                return None
            return self._decode_row(row)

    def save_token(self, user_key: str, provider: str, data: dict) -> None:
        """Upsert a token row. `data` matches the historical dict shape."""
        access_token = data.get("access_token")
        if not access_token:
            raise ValueError("save_token requires 'access_token' in data")

        access_pt = _to_bytes(access_token)
        access_blob, key_version = encryption.encrypt(
            access_pt,
            user_key=user_key,
            provider=provider,
            field="access_token",
            resolver=self._resolver,
        )

        refresh_token = data.get("refresh_token")
        refresh_blob, refresh_version = encryption.encrypt_optional(
            _to_bytes_or_none(refresh_token),
            user_key=user_key,
            provider=provider,
            field="refresh_token",
            resolver=self._resolver,
        )

        expires_at = _coerce_expires_at(data.get("expires_at"))
        scopes = data.get("scopes")
        scopes_str = scopes if isinstance(scopes, str) or scopes is None else " ".join(scopes)
        extras = {k: v for k, v in data.items() if k not in _RESERVED_FIELDS}

        with self._session(lock_for_write=True) as s:
            row = s.get(ProviderToken, (user_key, provider))
            if row is None:
                row = ProviderToken(user_key=user_key, provider=provider)
                s.add(row)
            row.access_token_encrypted = access_blob
            row.refresh_token_encrypted = refresh_blob
            row.refresh_token_key_version = refresh_version
            row.expires_at = expires_at
            row.scopes = scopes_str
            row.extra_metadata = extras
            row.key_version = key_version

    def clear_token(self, user_key: str, provider: str) -> None:
        with self._session(lock_for_write=True) as s:
            row = s.get(ProviderToken, (user_key, provider))
            if row is not None:
                s.delete(row)

    @contextmanager
    def locked_token(self, user_key: str, provider: str) -> Iterator["LockedToken"]:
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
        with self._session() as s:
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
        blob, version = encryption.encrypt(
            cache_json.encode("utf-8"),
            user_key=user_key,
            provider="__msal__",
            field="cache_data",
            resolver=self._resolver,
        )
        with self._session(lock_for_write=True) as s:
            row = s.get(MsalTokenCache, user_key)
            if row is None:
                row = MsalTokenCache(user_key=user_key)
                s.add(row)
            row.cache_data_encrypted = blob
            row.key_version = version

    def clear_msal_cache(self, user_key: str) -> None:
        with self._session(lock_for_write=True) as s:
            row = s.get(MsalTokenCache, user_key)
            if row is not None:
                s.delete(row)

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
            out["expires_at"] = row.expires_at.replace(
                tzinfo=_dt.timezone.utc
            ).timestamp() if row.expires_at.tzinfo is None else row.expires_at.timestamp()
            # Use the simpler representation: a float epoch.
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

    def __init__(self, repo: TokenRepository, session: Session, user_key: str, provider: str, row: ProviderToken | None):
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
        import time
        return time.time() >= (expires_at - buffer_seconds)

    def update(self, new_data: dict) -> None:
        """Persist refreshed token data while still inside the lock."""
        access_token = new_data.get("access_token")
        if not access_token:
            raise ValueError("update() requires 'access_token' in new_data")

        access_blob, key_version = encryption.encrypt(
            _to_bytes(access_token),
            user_key=self._user_key,
            provider=self._provider,
            field="access_token",
            resolver=self._repo._resolver,
        )
        refresh_blob, refresh_version = encryption.encrypt_optional(
            _to_bytes_or_none(new_data.get("refresh_token")),
            user_key=self._user_key,
            provider=self._provider,
            field="refresh_token",
            resolver=self._repo._resolver,
        )
        expires_at = _coerce_expires_at(new_data.get("expires_at"))
        scopes = new_data.get("scopes")
        scopes_str = scopes if isinstance(scopes, str) or scopes is None else " ".join(scopes)
        extras = {k: v for k, v in new_data.items() if k not in _RESERVED_FIELDS}

        if self._row is None:
            self._row = ProviderToken(user_key=self._user_key, provider=self._provider)
            self._session.add(self._row)
        self._row.access_token_encrypted = access_blob
        self._row.refresh_token_encrypted = refresh_blob
        self._row.refresh_token_key_version = refresh_version
        self._row.expires_at = expires_at
        self._row.scopes = scopes_str
        # Preserve old extras keys not in new_data unless new_data overrides them
        merged_extras = dict(self._row.extra_metadata or {})
        merged_extras.update(extras)
        self._row.extra_metadata = merged_extras
        self._row.key_version = key_version
        self._data = self._repo._decode_row(self._row)


def _to_bytes(s: str | bytes) -> bytes:
    if isinstance(s, bytes):
        return s
    return s.encode("utf-8")


def _to_bytes_or_none(s: str | bytes | None) -> bytes | None:
    if s is None:
        return None
    return _to_bytes(s)


def _coerce_expires_at(value) -> _dt.datetime | None:
    """Accept a unix-epoch float, an ISO datetime string, or a datetime; return naive UTC datetime."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.astimezone(_dt.timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)):
        return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc).replace(tzinfo=None)
    if isinstance(value, str):
        try:
            dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.astimezone(_dt.timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    return None


def _to_epoch(dt: _dt.datetime) -> float:
    """Convert a (possibly naive) UTC datetime back to a unix-epoch float."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()
