"""bond-mcps CLI: key management, DB migrations, file import, health check."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from auth.token_store import current_user_key

logger = logging.getLogger(__name__)


def cmd_generate_key(_args) -> int:
    from auth.encryption import generate_key

    if os.environ.get("BOND_MCPS_ENCRYPTION_KEY"):
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

    is_postgres = url.startswith("postgres")

    # User-key resolution. For Postgres deployments current_user_key()
    # raises DeploymentConfigError if BOND_MCPS_USER_ID is unset — that
    # turns into a doctor FAIL since silently defaulting to "root" in a
    # container would collide across tenants.
    try:
        print(f"User key:   {current_user_key()}")
    except DeploymentConfigError as e:
        print(f"  User key:       FAIL — {e}", file=sys.stderr)
        return 1

    try:
        validate_db_url(url)
    except ValueError as e:
        print(f"  URL validation: FAIL — {e}", file=sys.stderr)
        return 1
    print("  URL validation: OK")

    # For sslmode=verify-ca / verify-full, the operator must supply a root
    # CA bundle via sslrootcert. A missing file would surface as a confusing
    # psycopg error at first connection; check it here at boot.
    if is_postgres:
        qs = parse_qs(urlsplit(url).query)
        sslmode = (qs.get("sslmode", [""])[0] or "").lower()
        sslrootcert = qs.get("sslrootcert", [""])[0]
        if sslmode in ("verify-ca", "verify-full"):
            if not sslrootcert:
                print(
                    f"  TLS cert path: WARN — sslmode={sslmode} without sslrootcert "
                    f"in URL; psycopg will fall back to system trust store. "
                    f"For Aurora, set sslrootcert=/path/to/rds-global-bundle.pem.",
                    file=sys.stderr,
                )
            elif not Path(sslrootcert).exists():
                print(
                    f"  TLS cert path: FAIL — sslrootcert={sslrootcert!r} does not "
                    f"exist. Mount the RDS root CA bundle into the container or "
                    f"point at an installed system bundle.",
                    file=sys.stderr,
                )
                return 1
            else:
                print(f"  TLS cert path: {sslrootcert} present")

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


def cmd_prune_oauth(args) -> int:
    """Delete OAuth AS rows no longer needed.

    Designed to be run on a cron schedule (or once at AS startup). All
    deletions are idempotent and safe to retry.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from auth.db.models import (
        ConnectTicket,
        OAuthAuthCode,
        OAuthClient,
        OAuthPendingAuth,
        OAuthRefreshToken,
    )
    from auth.db.session import get_session

    now = datetime.now(timezone.utc)
    client_idle_cutoff = now - timedelta(days=args.client_idle_days)
    revoked_cutoff = now - timedelta(days=args.revoked_grace_days)
    pending_cutoff = now - timedelta(minutes=10)
    code_cutoff = now - timedelta(minutes=10)
    ticket_cutoff = now - timedelta(minutes=10)

    counts: dict[str, int] = {}
    with get_session() as session:
        # 1. Expired one-shot artifacts (these have INSERT-time sweeps but
        # may accumulate when traffic dries up).
        counts["pending_auth"] = (
            session.query(OAuthPendingAuth)
            .filter(OAuthPendingAuth.expires_at < pending_cutoff)
            .count()
        )
        counts["auth_codes"] = (
            session.query(OAuthAuthCode).filter(OAuthAuthCode.expires_at < code_cutoff).count()
        )
        counts["connect_tickets"] = (
            session.query(ConnectTicket).filter(ConnectTicket.expires_at < ticket_cutoff).count()
        )

        # 2. Long-revoked refresh tokens.
        counts["revoked_refresh_tokens"] = (
            session.query(OAuthRefreshToken)
            .filter(
                OAuthRefreshToken.revoked_at != None,  # noqa: E711
                OAuthRefreshToken.revoked_at < revoked_cutoff,
            )
            .count()
        )

        # 3. DCR clients with no recent activity. "Recent activity" = a
        # refresh_token issued in the last `client_idle_days`. Static
        # clients (is_static=True) are exempt — operators registered them
        # deliberately.
        active_client_ids = (
            session.execute(
                select(OAuthRefreshToken.client_id)
                .where(OAuthRefreshToken.created_at >= client_idle_cutoff)
                .distinct()
            )
            .scalars()
            .all()
        )
        idle_clients = session.query(OAuthClient).filter(
            OAuthClient.is_static.is_(False),
            OAuthClient.created_at < client_idle_cutoff,
            ~OAuthClient.client_id.in_(active_client_ids),
        )
        counts["idle_dcr_clients"] = idle_clients.count()

        if args.dry_run:
            print("dry-run; would delete:")
            for k, v in counts.items():
                print(f"  {k:25s} {v}")
            return 0

        # Execute deletions in dependency-safe order (refresh tokens before
        # clients, since RT references client_id).
        session.query(OAuthPendingAuth).filter(OAuthPendingAuth.expires_at < pending_cutoff).delete(
            synchronize_session=False
        )
        session.query(OAuthAuthCode).filter(OAuthAuthCode.expires_at < code_cutoff).delete(
            synchronize_session=False
        )
        session.query(ConnectTicket).filter(ConnectTicket.expires_at < ticket_cutoff).delete(
            synchronize_session=False
        )
        session.query(OAuthRefreshToken).filter(
            OAuthRefreshToken.revoked_at != None,  # noqa: E711
            OAuthRefreshToken.revoked_at < revoked_cutoff,
        ).delete(synchronize_session=False)
        idle_clients.delete(synchronize_session=False)
        session.commit()

    print("pruned:")
    for k, v in counts.items():
        print(f"  {k:25s} {v}")
    return 0


def cmd_revoke_tokens(args) -> int:
    """Revoke every non-revoked refresh token for a given user_key.

    Use case: an end user reports a workstation lost/stolen. Setting
    ``revoked_at`` invalidates the refresh tokens; combined with short
    access-token TTL this kicks the user out within at most one TTL.
    """
    from datetime import datetime, timezone

    from sqlalchemy import update

    from auth.db.models import OAuthRefreshToken
    from auth.db.session import get_session

    now = datetime.now(timezone.utc)
    with get_session() as session:
        result = session.execute(
            update(OAuthRefreshToken)
            .where(
                OAuthRefreshToken.user_key == args.user_key,
                OAuthRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        session.commit()
        print(f"Revoked {result.rowcount} refresh token(s) for user_key={args.user_key!r}.")
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
        "--provider",
        required=True,
        choices=(
            "github",
            "atlassian",
            "microsoft",
            "microsoft_powerbi",
            "databricks",
            "databricks_octo",
            "workday",
            "figma",
            "omnea",
            "aws",
            "bond_ai",
        ),
        help="Which provider's cached token to delete",
    )
    p_clear.add_argument("--user", help="Override BOND_MCPS_USER_ID")
    p_clear.set_defaults(func=cmd_clear)

    p_prune = sub.add_parser(
        "prune-oauth",
        help="Delete stale OAuth AS rows (expired codes, idle DCR clients, "
        "long-revoked refresh tokens). Safe to run on a schedule.",
    )
    p_prune.add_argument(
        "--client-idle-days",
        type=int,
        default=30,
        help="DCR clients with no recent activity in this many days are deleted. " "Default: 30.",
    )
    p_prune.add_argument(
        "--revoked-grace-days",
        type=int,
        default=7,
        help="Refresh tokens revoked more than this many days ago are deleted. " "Default: 7.",
    )
    p_prune.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts of what would be deleted, but don't delete.",
    )
    p_prune.set_defaults(func=cmd_prune_oauth)

    p_revoke = sub.add_parser(
        "revoke-tokens",
        help="Revoke all refresh tokens for a given user_key (emergency invalidation).",
    )
    p_revoke.add_argument(
        "--user-key",
        required=True,
        help="Cognito sub (or JWT sub claim) whose sessions should be invalidated.",
    )
    p_revoke.set_defaults(func=cmd_revoke_tokens)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
