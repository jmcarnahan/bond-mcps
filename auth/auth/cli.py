"""bond-mcps CLI: key management, DB migrations, file import, health check."""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def cmd_generate_key(_args) -> int:
    import os as _os

    from auth.encryption import generate_key

    if _os.environ.get("BOND_MCPS_ENCRYPTION_KEY"):
        print(
            "WARNING: BOND_MCPS_ENCRYPTION_KEY is already set in this shell. "
            "If you replace it with the value below, any data encrypted under "
            "the previous key will become unreadable.",
            file=sys.stderr,
        )
    print(generate_key())
    print(
        "\nSet this in your environment as BOND_MCPS_ENCRYPTION_KEY before "
        "starting MCP servers.\n"
        "Recommended: store via your shell rc or a secrets manager — not in "
        "a checked-in file.",
        file=sys.stderr,
    )
    return 0


def cmd_migrate_db(_args) -> int:
    from auth.alembic_config import upgrade_head

    upgrade_head()
    print("DB schema is at head.")
    return 0


def cmd_import_files(args) -> int:
    from auth.db.importer import import_legacy_files
    from auth.token_store import current_user_key

    user_key = args.user or current_user_key()
    result = import_legacy_files(user_key=user_key)

    if result.imported:
        print(f"Imported: {', '.join(result.imported)}")
    if result.skipped_existing:
        print(f"Skipped (already in DB): {', '.join(result.skipped_existing)}")
    if result.archived:
        print(f"Archived {len(result.archived)} file(s) to legacy_imported/")
    if result.errors:
        for provider, msg in result.errors:
            print(f"ERROR importing {provider}: {msg}", file=sys.stderr)
        return 1
    if not result.imported and not result.skipped_existing:
        print("Nothing to import.")
    return 0


def cmd_clear(args) -> int:
    from auth.db.repository import TokenRepository
    from auth.token_store import current_user_key

    user_key = args.user or current_user_key()
    repo = TokenRepository()
    if args.provider == "microsoft":
        repo.clear_msal_cache(user_key)
        print(f"Cleared Microsoft MSAL cache for user {user_key!r}.")
    else:
        repo.clear_token(user_key, args.provider)
        print(f"Cleared {args.provider} token for user {user_key!r}.")
    return 0


def cmd_doctor(_args) -> int:
    """Health check: validate config, DB connection, schema, encryption."""
    import os

    from auth.db.repository import _default_resolver
    from auth.db.session import (
        DeploymentConfigError,
        SchemaOutOfDateError,
        default_db_url,
        ensure_schema_current,
        get_engine,
        validate_db_url,
    )
    from auth.encryption import TokenEncryptionError, verify_encryption_setup

    from auth.token_store import current_user_key

    env_url = os.environ.get("BOND_MCPS_DB_URL")
    if env_url:
        url = env_url
    else:
        try:
            url = default_db_url()
        except DeploymentConfigError as e:
            print(f"  URL resolution: FAIL — {e}", file=sys.stderr)
            return 1
    print(f"DB URL:     {url}")
    print(f"User key:   {current_user_key()}")

    try:
        validate_db_url(url)
    except ValueError as e:
        print(f"  URL validation: FAIL — {e}", file=sys.stderr)
        return 1
    print("  URL validation: OK")

    try:
        get_engine(url)
        print("  Engine:         OK")
    except Exception as e:
        print(f"  Engine:         FAIL — {e}", file=sys.stderr)
        return 1

    try:
        ensure_schema_current(url)
        print("  Schema:         at head")
    except SchemaOutOfDateError as e:
        print(f"  Schema:         FAIL — {e}", file=sys.stderr)
        return 1

    try:
        resolver = _default_resolver()
        verify_encryption_setup(resolver)
        print("  Encryption:     round-trip OK")
    except TokenEncryptionError as e:
        print(f"  Encryption:     FAIL — {e}", file=sys.stderr)
        return 1

    print("\nAll checks passed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bond-mcps",
        description="bond-mcps token database management.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate-key", help="Print a fresh base64 AES-256 key").set_defaults(
        func=cmd_generate_key
    )
    sub.add_parser("migrate-db", help="Run Alembic migrations to head").set_defaults(
        func=cmd_migrate_db
    )
    sub.add_parser("doctor", help="Validate config, DB, and encryption setup").set_defaults(
        func=cmd_doctor
    )

    p_import = sub.add_parser(
        "import-files",
        help="One-time import of ~/.bond_mcps/*.json into the encrypted DB",
    )
    p_import.add_argument("--user", help="Override BOND_MCPS_USER_ID for this import")
    p_import.set_defaults(func=cmd_import_files)

    p_clear = sub.add_parser(
        "clear",
        help="Delete the cached token for a provider (logout)",
    )
    p_clear.add_argument(
        "--provider", required=True,
        choices=("github", "atlassian", "microsoft"),
        help="Which provider's cached token to delete",
    )
    p_clear.add_argument("--user", help="Override BOND_MCPS_USER_ID")
    p_clear.set_defaults(func=cmd_clear)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
