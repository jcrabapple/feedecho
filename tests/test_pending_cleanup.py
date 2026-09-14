"""Abandoned card-pending signup cleanup (scheduler.cleanup_pending_accounts).

A card-pending account (registered while billing is on, Stripe Checkout
never completed) cannot reach the app at all; past the TTL it is deleted
through the same path as self-serve account deletion.
"""

from datetime import datetime, timedelta, timezone

import pytest

import app as app_module
import database
import plans
import scheduler
import settings

pytestmark = pytest.mark.multi


@pytest.fixture(autouse=True)
def _restore_guards():
    """Leave the global guard list exactly as it was found."""
    before = list(app_module._pending_cleanup_guards)
    yield
    app_module._pending_cleanup_guards[:] = before


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _setup(monkeypatch, tmp_path, *, billing=True, ttl=3):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(settings, "BILLING_ENABLED", billing)
    monkeypatch.setattr(settings, "PENDING_ACCOUNT_TTL_DAYS", ttl)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "cleanup.db")
    database.init_db()


def _insert(
    email,
    *,
    plan="trial",
    ends: str | None = plans.TRIAL_PENDING,
    days_old: float = 0,
    sub="",
):
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (email, password_hash, plan, trial_ends_at,"
            " email_verified, stripe_subscription_id, created_at)"
            " VALUES (?, '', ?, ?, 0, ?, ?)",
            (email, plan, ends, sub, _ts(days_old)),
        )


def _user_id(email) -> int:
    with database.get_db() as db:
        return db.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()["id"]


def _count(email) -> int:
    with database.get_db() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE email = ?", (email,)
        ).fetchone()["n"]


class TestPendingCleanup:
    def test_deletes_only_old_pending_accounts(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _insert("old-pending@example.com", days_old=5)
        _insert("young-pending@example.com", days_old=1)
        # An ACTIVE trial (future end date) is not pending, however old.
        _insert("active-trial@example.com", ends=_ts(-30), days_old=5)
        _insert("paid@example.com", plan="paid", ends=None, days_old=10)

        assert scheduler.cleanup_pending_accounts() == 1
        assert _count("old-pending@example.com") == 0
        assert _count("young-pending@example.com") == 1
        assert _count("active-trial@example.com") == 1
        assert _count("paid@example.com") == 1

    def test_removes_child_rows_too(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _insert("old-pending@example.com", days_old=5)
        uid = _user_id("old-pending@example.com")
        with database.get_db() as db:
            db.execute(
                "INSERT INTO email_tokens (user_id, token_hash, purpose, expires_at)"
                " VALUES (?, 'hash', 'verify', ?)",
                (uid, _ts(-1)),
            )
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (?, 'smtp_host', 'x')",
                (uid,),
            )

        assert scheduler.cleanup_pending_accounts() == 1
        with database.get_db() as db:
            tokens = db.execute(
                "SELECT COUNT(*) AS n FROM email_tokens WHERE user_id = ?", (uid,)
            ).fetchone()["n"]
            prefs = db.execute(
                "SELECT COUNT(*) AS n FROM settings WHERE user_id = ?", (uid,)
            ).fetchone()["n"]
        assert tokens == 0
        assert prefs == 0

    def test_skips_pending_account_with_subscription_id(self, monkeypatch, tmp_path):
        # A webhook that failed mid-flight can leave a real payer on the
        # pending sentinel; that row must never be deleted.
        _setup(monkeypatch, tmp_path)
        _insert("webhook-victim@example.com", days_old=9, sub="sub_live_123")

        assert scheduler.cleanup_pending_accounts() == 0
        assert _count("webhook-victim@example.com") == 1

    def test_noop_when_billing_disabled(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path, billing=False)
        _insert("old-pending@example.com", days_old=5)

        assert scheduler.cleanup_pending_accounts() == 0
        assert _count("old-pending@example.com") == 1

    def test_ttl_boundary(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path, ttl=3)
        _insert("just-under@example.com", days_old=2.9)
        _insert("just-over@example.com", days_old=3.1)

        assert scheduler.cleanup_pending_accounts() == 1
        assert _count("just-under@example.com") == 1
        assert _count("just-over@example.com") == 0

    def test_guard_veto_preserves_the_account(self, monkeypatch, tmp_path):
        # The hosted billing guard vetoes when Stripe still shows a live
        # subscription (webhook never landed): a payer must not be deleted.
        _setup(monkeypatch, tmp_path)
        _insert("maybe-paying@example.com", days_old=5)

        def veto(uid):
            raise app_module.AccountDeletionAbort("stripe shows a live subscription")

        app_module.register_pending_cleanup_guard(veto)
        assert scheduler.cleanup_pending_accounts() == 0
        assert _count("maybe-paying@example.com") == 1

    def test_guard_failure_skips_the_account(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _insert("maybe-paying@example.com", days_old=5)

        def boom(uid):
            raise RuntimeError("stripe unreachable")

        app_module.register_pending_cleanup_guard(boom)
        assert scheduler.cleanup_pending_accounts() == 0
        assert _count("maybe-paying@example.com") == 1

    def test_one_failed_delete_does_not_stop_the_batch(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _insert("first@example.com", days_old=6)
        _insert("second@example.com", days_old=5)
        original = app_module._hard_delete_user
        calls = []

        def flaky(db, uid):
            calls.append(uid)
            if len(calls) == 1:
                raise RuntimeError("db hiccup")
            return original(db, uid)

        monkeypatch.setattr(app_module, "_hard_delete_user", flaky)
        assert scheduler.cleanup_pending_accounts() == 1
        # Order-independent: whichever row failed stays, the other is gone.
        survivors = [
            email
            for email in ("first@example.com", "second@example.com")
            if _count(email) == 1
        ]
        assert len(survivors) == 1
