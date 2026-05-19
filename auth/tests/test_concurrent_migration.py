"""Three concurrent processes hitting a fresh DB should all fail with the
same helpful 'run migrate-db' error rather than racing the schema upgrade.

This protects against the SQLite-has-no-advisory-lock footgun: bond-mcps
launches three MCP processes simultaneously from `make dev`. If any of
them tried to auto-upgrade on first session, we'd risk partial schema
state on SQLite.
"""

import multiprocessing as mp
import os


def _worker(db_path: str, encryption_key: str, result_q: mp.Queue) -> None:
    """Run inside a fresh subprocess. Tries to use the empty DB."""
    os.environ["BOND_MCPS_DB_URL"] = f"sqlite:///{db_path}"
    os.environ["BOND_MCPS_ENCRYPTION_KEY"] = encryption_key

    from auth.db import reset_for_tests
    from auth.db.session import SchemaOutOfDateError, ensure_schema_current

    reset_for_tests()  # discard any state from before the fork
    try:
        ensure_schema_current()
        result_q.put(("ok", None))
    except SchemaOutOfDateError as e:
        result_q.put(("schema_out_of_date", str(e)))
    except Exception as e:
        result_q.put(("error", repr(e)))


def test_three_processes_all_fail_with_helpful_error(tmp_path):
    db_path = tmp_path / "tokens.db"

    from auth.encryption import generate_key

    key = generate_key()

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(str(db_path), key, q)) for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    results = []
    while not q.empty():
        results.append(q.get_nowait())

    assert len(results) == 3
    # All three should signal the same kind of failure with a helpful message.
    for status, msg in results:
        assert status == "schema_out_of_date", f"unexpected status: {status} / {msg}"
        assert "migrate-db" in msg, f"error should point to migrate-db: {msg}"


def test_migrate_db_then_processes_succeed(tmp_path):
    db_path = tmp_path / "tokens.db"

    from auth.alembic_config import upgrade_head
    from auth.db import reset_for_tests
    from auth.encryption import generate_key

    key = generate_key()
    os.environ["BOND_MCPS_DB_URL"] = f"sqlite:///{db_path}"
    os.environ["BOND_MCPS_ENCRYPTION_KEY"] = key
    reset_for_tests()
    upgrade_head()
    reset_for_tests()

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(str(db_path), key, q)) for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    results = []
    while not q.empty():
        results.append(q.get_nowait())

    assert len(results) == 3
    for status, msg in results:
        assert status == "ok", f"expected success, got {status}: {msg}"
