"""Two threads simultaneously refresh an expired token; only one network call should occur.

The TokenStore.refresh_if_needed path takes a row-level write lock around
read → check expiry → POST → write. On SQLite, BEGIN IMMEDIATE serializes
the threads; on Postgres, SELECT...FOR UPDATE does the same. The second
thread, after waiting for the lock, sees the row already refreshed and
returns without calling the OAuth endpoint again.
"""

import json
import threading
import time
from unittest.mock import MagicMock, patch

from auth.token_store import TokenStore


def test_only_one_refresh_call_under_concurrency(repo):
    store = TokenStore("github", user_key="alice")
    store.save_token(
        {
            "access_token": "old",
            "refresh_token": "rtk-1",
            "expires_at": time.time() - 100,  # expired
        }
    )

    call_count = 0
    call_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def fake_urlopen(req, timeout=None):
        nonlocal call_count
        with call_lock:
            call_count += 1
            seq = call_count
        # Force the threads to interleave: thread 1 holds the network call
        # open briefly so thread 2 has time to enter refresh_if_needed.
        time.sleep(0.1)
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {
                "access_token": f"new_tok_{seq}",
                "refresh_token": f"rtk_{seq + 1}",
                "expires_in": 3600,
            }
        ).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    results = [None, None]

    def worker(idx):
        barrier.wait()
        results[idx] = store.refresh_if_needed("cid", "secret", "https://token.url")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    with patch("auth.token_store.urllib.request.urlopen", side_effect=fake_urlopen):
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    # Exactly one network refresh happened.
    assert call_count == 1, f"Expected 1 refresh call, got {call_count}"
    # Both threads returned the same (refreshed) access token.
    assert results[0] is not None and results[1] is not None
    assert results[0] == results[1]
