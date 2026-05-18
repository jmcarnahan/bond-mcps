"""Verify timestamps round-trip correctly with timezone-aware columns.

The DB columns are TIMESTAMP WITH TIME ZONE. We always write tz-aware
UTC values and read them back as tz-aware (or as epoch floats for the
public API).
"""

import datetime as _dt
import time

from auth.db.repository import _coerce_expires_at, _to_epoch


def test_coerce_epoch_float_to_utc_aware():
    epoch = 1716234567.89
    got = _coerce_expires_at(epoch)
    assert got.tzinfo is not None
    assert got.tzinfo == _dt.timezone.utc
    assert abs(got.timestamp() - epoch) < 1e-3


def test_coerce_naive_datetime_assumes_utc():
    naive = _dt.datetime(2026, 5, 17, 12, 0, 0)
    got = _coerce_expires_at(naive)
    assert got.tzinfo == _dt.timezone.utc
    assert got.year == 2026 and got.hour == 12


def test_coerce_aware_non_utc_converts_to_utc():
    """Eastern time should become UTC."""
    eastern = _dt.timezone(_dt.timedelta(hours=-5))
    aware = _dt.datetime(2026, 5, 17, 12, 0, 0, tzinfo=eastern)
    got = _coerce_expires_at(aware)
    assert got.tzinfo == _dt.timezone.utc
    assert got.hour == 17  # 12 EST = 17 UTC


def test_coerce_iso_string_with_z():
    got = _coerce_expires_at("2026-05-17T12:00:00Z")
    assert got.tzinfo == _dt.timezone.utc
    assert got.hour == 12


def test_coerce_iso_string_naive():
    got = _coerce_expires_at("2026-05-17T12:00:00")
    assert got.tzinfo == _dt.timezone.utc  # interpreted as UTC


def test_to_epoch_handles_aware_and_naive():
    epoch = 1716234567.0
    aware = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)
    assert abs(_to_epoch(aware) - epoch) < 1e-6

    naive = aware.replace(tzinfo=None)
    assert abs(_to_epoch(naive) - epoch) < 1e-6


def test_expires_at_round_trip_through_repository(repo):
    """Save with epoch float; read back as epoch float; values agree."""
    in_epoch = time.time() + 3600
    repo.save_token("alice", "github", {
        "access_token": "x",
        "expires_at": in_epoch,
    })
    got = repo.get_token("alice", "github")
    assert abs(got["expires_at"] - in_epoch) < 1.0


def test_expires_at_with_naive_datetime_interpreted_as_utc(repo):
    """A naive datetime passed to save_token must NOT shift by local TZ."""
    naive_dt = _dt.datetime(2026, 5, 17, 12, 0, 0)
    expected_epoch = naive_dt.replace(tzinfo=_dt.timezone.utc).timestamp()

    repo.save_token("alice", "github", {
        "access_token": "x",
        "expires_at": naive_dt,
    })
    got = repo.get_token("alice", "github")
    assert abs(got["expires_at"] - expected_epoch) < 1.0
