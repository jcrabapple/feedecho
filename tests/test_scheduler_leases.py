"""Tests for D2 scheduler job leases (flush_digests, flush_drips)."""

import os
import time

import pytest

import database
import scheduler
import settings

TEST_PG_URL = os.environ.get("FEEDECHO_TEST_PG_URL", "")

requires_pg = pytest.mark.skipif(
    not TEST_PG_URL, reason="FEEDECHO_TEST_PG_URL not set; PG tests are CI-gated"
)


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
        calls = []

        def _fake_acquire(job_name, ttl):
            if held[0]:
                return False
            held[0] = True
            return True

        monkeypatch.setattr(scheduler, "_acquire_job_lease", _fake_acquire)
        monkeypatch.setattr(scheduler, "_release_job_lease", lambda _: None)
        monkeypatch.setattr(scheduler, "_flush_drips", lambda: calls.append(1))
        scheduler.flush_drips()  # acquires, runs
        scheduler.flush_drips()  # lease held, must skip
        assert len(calls) == 1

    def test_flush_digests_skips_when_lease_held(self, env, monkeypatch):
        held = [False]
        calls = []

        def _fake_acquire(job_name, ttl):
            if held[0]:
                return False
            held[0] = True
            return True

        monkeypatch.setattr(scheduler, "_acquire_job_lease", _fake_acquire)
        monkeypatch.setattr(scheduler, "_release_job_lease", lambda _: None)
        monkeypatch.setattr(scheduler, "_flush_digests", lambda: calls.append(1))
        scheduler.flush_digests()
        scheduler.flush_digests()  # skips
        assert len(calls) == 1


@pytest.mark.pg
@requires_pg
class TestJobLeasePG:
    @pytest.fixture(autouse=True)
    def pg_env(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "DATABASE_URL", TEST_PG_URL)
        monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", False)
        with database.get_db() as db:
            db.execute("DROP SCHEMA public CASCADE")
            db.execute("CREATE SCHEMA public")
            db.execute("GRANT ALL ON SCHEMA public TO public")
        database.init_db()

    def test_acquire_and_release_pg(self, pg_env):
        assert scheduler._acquire_job_lease("test_job_pg", 60)
        scheduler._release_job_lease("test_job_pg")

    def test_no_concurrent_pg(self, pg_env):
        assert scheduler._acquire_job_lease("test_job_pg", 60)
        orig = scheduler._instance_id
        try:
            scheduler._instance_id = "other"
            assert not scheduler._acquire_job_lease("test_job_pg", 60)
        finally:
            scheduler._instance_id = orig
        scheduler._release_job_lease("test_job_pg")