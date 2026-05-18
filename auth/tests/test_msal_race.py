"""Two threads racing on the same MSAL cache should be serialized by locked_msal_cache.

Critical property under test: when two processes both want to refresh the
MSAL cache simultaneously, the second one must see the first one's writes
(not stomp on them). Without locked_msal_cache, both processes could
"silent acquire" with the same refresh_token and the second call would
fail because Microsoft invalidates refresh tokens on use.
"""

import threading
import time

from auth.db.repository import TokenRepository


def test_locked_msal_cache_serializes_writers(repo):
    """Two threads each do a R-M-W under locked_msal_cache.

    Verify: (a) each thread's "increment counter" write is preserved (no
    lost updates from interleaved R-M-W), (b) the threads were actually
    serialized (one fully completed before the other began).
    """
    # Seed initial state.
    repo.save_msal_cache("alice", '{"counter": 0}')

    enter_times = []
    exit_times = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        import json
        with repo.locked_msal_cache("alice") as handle:
            enter_times.append(time.monotonic())
            data = json.loads(handle.blob or '{"counter": 0}')
            time.sleep(0.05)  # widen the race window
            data["counter"] += 1
            handle.set_blob(json.dumps(data))
            exit_times.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # Both increments must be preserved → final counter is 2.
    import json
    final = json.loads(repo.get_msal_cache("alice"))
    assert final["counter"] == 2, (
        f"Lost update: counter={final['counter']}, expected 2"
    )

    # The two enter timestamps must not overlap with the prior exit —
    # i.e., second enter happens after first exit. This is the
    # "actually serialized" assertion.
    assert sorted(enter_times) and sorted(exit_times)
    first_exit = min(exit_times)
    later_enter = max(enter_times)
    assert later_enter >= first_exit, (
        f"Locks did not serialize: later_enter={later_enter}, "
        f"first_exit={first_exit}"
    )


def test_locked_msal_cache_initial_creation(repo):
    """If no row exists, locked_msal_cache yields blob=None and persists on set_blob."""
    with repo.locked_msal_cache("nobody") as handle:
        assert handle.blob is None
        handle.set_blob('{"v": 1}')

    assert repo.get_msal_cache("nobody") == '{"v": 1}'


def test_locked_msal_cache_no_write_when_not_dirty(repo):
    """If set_blob is never called, the existing row is not touched."""
    repo.save_msal_cache("alice", '{"v": 1}')

    with repo.locked_msal_cache("alice") as handle:
        # Read but don't write.
        assert handle.blob == '{"v": 1}'

    # Row unchanged
    assert repo.get_msal_cache("alice") == '{"v": 1}'


def test_locked_msal_cache_rollback_on_exception(repo):
    """An exception inside the context discards uncommitted writes."""
    repo.save_msal_cache("alice", '{"v": 1}')

    try:
        with repo.locked_msal_cache("alice") as handle:
            handle.set_blob('{"v": 2}')
            raise RuntimeError("oops")
    except RuntimeError:
        pass

    # The set_blob should be rolled back; the original value persists.
    assert repo.get_msal_cache("alice") == '{"v": 1}'


def test_locked_msal_cache_set_blob_rejects_none(repo):
    """set_blob(None) is a bug; reject loudly rather than write garbage."""
    import pytest

    with repo.locked_msal_cache("alice") as handle:
        with pytest.raises(ValueError, match="non-None"):
            handle.set_blob(None)  # type: ignore[arg-type]
