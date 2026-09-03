"""Tests for D2 scheduler job leases (flush_digests, flush_drips)."""

import time

import pytest

import database
import scheduler
import settings


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "sched.db")
    database.init_db()
    yield


class TestJobLease:
    def test_acquire_and_release(self, env):
        assert scheduler._acquire_job_lease("test_job", 60)
        scheduler._release_job_lease("test_job")

    def test_no_concurrent_acquisition(self, env):
        assert scheduler._acquire_job_lease("test_job", 60)
        # Same instance (renewal) should succeed.
        assert scheduler._acquire_job_lease("test_job", 60)
        # A different instance must fail to acquire.
        orig = scheduler._instance_id
        try:
            scheduler._instance_id = "other-instance"
            assert not scheduler._acquire_job_lease("test_job", 60)
        finally:
            scheduler._instance_id = orig
        scheduler._release_job_lease("test_job")

    def test_expired_lease_can_be_stolen(self, env):
        assert scheduler._acquire_job_lease("test_job", 0)  # TTL=0 → already expired
        orig = scheduler._instance_id
        try:
            scheduler._instance_id = "other-instance"
            # A tiny sleep ensures the timestamp advances past the expired TTL.
            time.sleep(0.1)
            assert scheduler._acquire_job_lease("test_job", 60)
            scheduler._release_job_lease("test_job")
        finally:
            scheduler._instance_id = orig

    def test_release_only_clears_own_lease(self, env):
        assert scheduler._acquire_job_lease("test_job", 60)
        orig = scheduler._instance_id
        try:
            scheduler._instance_id = "other-instance"
            # Releasing as a non-owner must be a no-op...
            scheduler._release_job_lease("test_job")
            # ...so the other instance still cannot acquire.
            assert not scheduler._acquire_job_lease("test_job", 60)
            scheduler._instance_id = orig
            # The original owner can still renew its own lease.
            assert scheduler._acquire_job_lease("test_job", 60)
        finally:
            scheduler._instance_id = orig
        scheduler._release_job_lease("test_job")

    def test_flush_drips_skips_when_lease_held(self, env, monkeypatch):
        held = [False]

        def _fake_acquire(job_name, ttl):
            if held[0]:
                return False
            held[0] = True
            return True

        monkeypatch.setattr(scheduler, "_acquire_job_lease", _fake_acquire)
        monkeypatch.setattr(scheduler, "_release_job_lease", lambda _: None)
        monkeypatch.setattr(scheduler, "_flush_drips", lambda: None)
        # First call: acquires, runs, releases.
        scheduler.flush_drips()
        assert held[0]
        # Second call: lease held, skips.
        scheduler.flush_drips()

    def test_flush_digests_skips_when_lease_held(self, env, monkeypatch):
        held = [False]

        def _fake_acquire(job_name, ttl):
            if held[0]:
                return False
            held[0] = True
            return True

        monkeypatch.setattr(scheduler, "_acquire_job_lease", _fake_acquire)
        monkeypatch.setattr(scheduler, "_release_job_lease", lambda _: None)
        monkeypatch.setattr(scheduler, "_flush_digests", lambda: None)
        scheduler.flush_digests()
        assert held[0]
        scheduler.flush_digests()  # skips


@pytest.mark.pg
@pytest.mark.skipif(
    "not os.environ.get('FEEDECHO_TEST_PG_URL')",
    reason="FEEDECHO_TEST_PG_URL not set",
)
class TestJobLeasePG:
    def test_acquire_and_release_pg(self, env):
        assert scheduler._acquire_job_lease("test_job_pg", 60)
        scheduler._release_job_lease("test_job_pg")

    def test_no_concurrent_pg(self, env):
        assert scheduler._acquire_job_lease("test_job_pg", 60)
        orig = scheduler._instance_id
        try:
            scheduler._instance_id = "other"
            assert not scheduler._acquire_job_lease("test_job_pg", 60)
        finally:
            scheduler._instance_id = orig
        scheduler._release_job_lease("test_job_pg")