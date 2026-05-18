"""One-time importer: migrate ~/.bond_mcps/*.json into the encrypted DB.

Per-provider strategy: GitHub and Atlassian use the generic token-dict
shape; Microsoft is an opaque MSAL SerializableTokenCache blob and goes
into msal_token_caches instead. A generic "treat any JSON as a token
dict" importer would silently corrupt MSAL state — don't do that.

Idempotent: if a DB row already exists for (user_key, provider), the
file is left alone. Imported files are MOVED to ~/.bond_mcps/legacy_imported/
with a timestamp suffix; nothing is deleted.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from auth.db.repository import TokenRepository

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".bond_mcps"
LEGACY_DIR_NAME = "legacy_imported"

TOKEN_PROVIDERS = ("github", "atlassian")
MSAL_PROVIDERS = ("microsoft",)


@dataclass
class ImportResult:
    imported: list[str]
    skipped_existing: list[str]
    archived: list[Path]
    errors: list[tuple[str, str]]


def import_legacy_files(
    *,
    user_key: str,
    cache_dir: Path | None = None,
    repo: TokenRepository | None = None,
) -> ImportResult:
    """Import any ~/.bond_mcps/*.json files for the given user_key."""
    cache_dir = cache_dir or DEFAULT_CACHE_DIR
    repo = repo or TokenRepository()
    result = ImportResult(imported=[], skipped_existing=[], archived=[], errors=[])

    if not cache_dir.exists():
        return result

    for provider in TOKEN_PROVIDERS:
        path = cache_dir / f"{provider}.json"
        if not path.exists():
            continue
        try:
            if repo.get_token(user_key, provider) is not None:
                result.skipped_existing.append(provider)
                continue
            data = json.loads(path.read_text())
            repo.save_token(user_key, provider, data)
            archived = _archive(path, cache_dir)
            result.imported.append(provider)
            result.archived.append(archived)
        except Exception as e:
            logger.exception("Failed to import %s", path)
            result.errors.append((provider, str(e)))

    for provider in MSAL_PROVIDERS:
        path = cache_dir / f"{provider}.json"
        if not path.exists():
            continue
        try:
            if repo.get_msal_cache(user_key) is not None:
                result.skipped_existing.append(provider)
                continue
            blob = path.read_text()
            # Validate that it parses as JSON (sanity check) before encrypting.
            json.loads(blob)
            repo.save_msal_cache(user_key, blob)
            archived = _archive(path, cache_dir)
            result.imported.append(provider)
            result.archived.append(archived)
        except Exception as e:
            logger.exception("Failed to import %s", path)
            result.errors.append((provider, str(e)))

    return result


def _archive(path: Path, cache_dir: Path) -> Path:
    legacy_dir = cache_dir / LEGACY_DIR_NAME
    legacy_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = legacy_dir / f"{path.name}.{ts}"
    shutil.move(str(path), str(dest))
    return dest
