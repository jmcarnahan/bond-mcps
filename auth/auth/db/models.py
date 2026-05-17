"""SQLAlchemy models for the encrypted token store.

Two tables. Composite primary keys. No surrogate IDs. No users table —
`user_key` is the identity column, set from BOND_MCPS_USER_ID env var or
getpass.getuser().

LargeBinary columns hold raw `nonce || ciphertext`. Encryption is done in
the repository layer; the model is unaware of encryption semantics.
"""

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    JSON,
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
    expires_at = Column(DateTime, nullable=True)
    scopes = Column(String, nullable=True)
    extra_metadata = Column(JSON, nullable=False, default=dict)
    key_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
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
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<MsalTokenCache user_key={self.user_key!r} "
            f"key_version={self.key_version}>"
        )
