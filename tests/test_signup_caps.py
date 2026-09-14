"""Deployment-wide registration brake (auth._register_global_capped).

The per-IP bucket in the register flow bounds one source; these ceilings
bound the deployment as a whole so a rotating-IP spray cannot turn the
public signup form into an unbounded scrypt + email-send engine.
"""

import time

import auth
import settings


def setup_function():
    auth._register_global.clear()


def test_hourly_cap_trips_after_the_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "REGISTER_HOURLY_CAP", 3)
    monkeypatch.setattr(settings, "REGISTER_DAILY_CAP", 100)
    assert auth._register_global_capped() is False
    for _ in range(3):
        auth._record_register_global()
    assert auth._register_global_capped() is True


def test_daily_cap_trips_even_when_the_hour_has_room(monkeypatch):
    monkeypatch.setattr(settings, "REGISTER_HOURLY_CAP", 100)
    monkeypatch.setattr(settings, "REGISTER_DAILY_CAP", 3)
    now = time.monotonic()
    auth._register_global.extend([now - 7200, now - 3660, now - 30])
    assert auth._register_global_capped() is True


def test_entries_older_than_a_day_are_pruned(monkeypatch):
    monkeypatch.setattr(settings, "REGISTER_HOURLY_CAP", 100)
    monkeypatch.setattr(settings, "REGISTER_DAILY_CAP", 2)
    now = time.monotonic()
    auth._register_global.extend([now - 90000, now - 30])
    assert auth._register_global_capped() is False
    # The day-old entry is dropped, so the bucket cannot grow without bound.
    assert len(auth._register_global) == 1
