"""Plan limits and trial-pause mechanics (hosted mode).

Covers the plans module itself, the route guards (feed cap, destination cap,
poll clamp, drip clamp), the admin plan/extend-trial controls, and the
scheduler's trial-pause gate (expired trial = feeds skipped, nothing deleted).
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import auth
import database
import plans
import scheduler
import security
import settings
from app import app


@pytest.fixture()
def db_tmp(monkeypatch):
    """Point the DB layer at a fresh temp file per test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()
    monkeypatch.setattr(scheduler, "get_db", database.get_db)
    yield database
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


UID = 5


@pytest.fixture()
def multi_client(monkeypatch, db_tmp):
    """Signed-in multi-mode TestClient over the temp DB."""
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (?, 'u@example.com', '', 1)",
            (UID,),
        )
    client = TestClient(app)
    client.cookies.set("feedecho_session", security.sign_session(UID, "u@example.com"))
    return client


def _set_plan(uid, plan, trial_ends_at=None):
    with database.get_db() as db:
        db.execute(
            "UPDATE users SET plan = ?, trial_ends_at = ? WHERE id = ?",
            (plan, trial_ends_at, uid),
        )


def _future(days=10):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _past(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ── plans module ────────────────────────────────────────────────────────────


class TestLimitFor:
    def test_trial_defaults(self):
        assert plans.limit_for("trial", "max_feeds") == 5
        assert plans.limit_for("trial", "min_poll_interval") == 15

    def test_unknown_plan_falls_back_to_trial(self):
        assert plans.limit_for("enterprise", "max_feeds") == 5

    def test_zero_means_unlimited(self):
        assert plans.limit_for("paid", "min_poll_interval") == 5  # floor, not cap
        assert plans.limit_for("paid", "max_feeds") == 50


class TestTrialState:
    def test_future_trial_is_active(self):
        assert plans.trial_state("trial", _future(5)) == "active"

    def test_past_trial_is_expired(self):
        assert plans.trial_state("trial", _past(1)) == "expired"

    def test_paid_plan_is_never_expired(self):
        assert plans.trial_state("paid", _past(30)) == "na"
        assert plans.posting_paused("paid", _past(30)) is False

    def test_missing_expiry_treated_as_active(self):
        assert plans.trial_state("trial", None) == "active"

    def test_both_dialect_timestamp_forms_parse(self):
        # sqlite TEXT form and ISO form must agree
        assert plans.trial_state("trial", "2020-01-01 00:00:00") == "expired"
        assert plans.trial_state("trial", "2020-01-01T00:00:00") == "expired"

    def test_datetime_object_form(self):
        # PG hands back datetime objects
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        assert plans.trial_state("trial", past) == "expired"

    def test_garbage_expiry_treated_as_active(self):
        # Never lock a user out on a data quirk
        assert plans.trial_state("trial", "not-a-date") == "active"


class TestClamps:
    def test_poll_clamped_up_to_plan_floor(self):
        assert plans.clamp_poll_interval(5, "trial") == 15

    def test_poll_untouched_when_above_floor(self):
        assert plans.clamp_poll_interval(60, "trial") == 60

    def test_poll_paid_floor_is_5(self):
        assert plans.clamp_poll_interval(1, "paid") == 5

    def test_drip_clamped_down_to_plan_ceiling(self):
        assert plans.clamp_drip_limit(500, "trial") == 60

    def test_drip_untouched_under_ceiling(self):
        assert plans.clamp_drip_limit(10, "trial") == 10

    def test_drip_zero_stays_zero(self):
        assert plans.clamp_drip_limit(0, "trial") == 0


class TestAllowances:
    def test_feed_allowance_raises_at_cap(self):
        with pytest.raises(plans.PlanError, match="5 feeds"):
            plans.check_feed_allowance(5, "trial")

    def test_feed_allowance_passes_under_cap(self):
        plans.check_feed_allowance(4, "trial")

    def test_destination_allowance_raises_at_cap(self):
        with pytest.raises(plans.PlanError, match="5 connected"):
            plans.check_destination_allowance(5, "trial")


# ── Route guards ────────────────────────────────────────────────────────────


class TestFeedCapRoute:
    def _fill_feeds(self, client, n):
        for i in range(n):
            r = client.post(
                "/api/feeds",
                data={"name": f"f{i}", "url": "https://example.com/feed.xml"},
                follow_redirects=False,
            )
            assert r.status_code == 303

    def test_add_feed_blocked_at_plan_cap(self, multi_client):
        self._fill_feeds(multi_client, 5)
        r = multi_client.post(
            "/api/feeds",
            data={"name": "one-too-many", "url": "https://example.com/x.xml"},
            follow_redirects=False,
        )
        assert r.status_code == 402
        assert "5 feeds" in r.json()["detail"]

    def test_add_feed_allowed_under_cap(self, multi_client):
        self._fill_feeds(multi_client, 4)
        r = multi_client.post(
            "/api/feeds",
            data={"name": "fifth", "url": "https://example.com/x.xml"},
            follow_redirects=False,
        )
        assert r.status_code == 303

    def test_paid_plan_higher_cap(self, multi_client):
        _set_plan(UID, "paid")
        self._fill_feeds(multi_client, 25)
        r = multi_client.post(
            "/api/feeds",
            data={"name": "26th", "url": "https://example.com/x.xml"},
            follow_redirects=False,
        )
        assert r.status_code == 303

    def test_deleted_feeds_do_not_count(self, multi_client):
        self._fill_feeds(multi_client, 5)
        with database.get_db() as db:
            db.execute(
                "UPDATE feeds SET deleted_at = '2026-01-01 00:00:00' WHERE user_id = ? AND id = 1",
                (UID,),
            )
        r = multi_client.post(
            "/api/feeds",
            data={"name": "after-delete", "url": "https://example.com/x.xml"},
            follow_redirects=False,
        )
        assert r.status_code == 303

    def test_poll_interval_clamped_to_plan_floor(self, multi_client):
        r = multi_client.post(
            "/api/feeds",
            data={"name": "fast", "url": "https://example.com/f.xml", "poll_interval": "2"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        with database.get_db() as db:
            row = db.execute(
                "SELECT poll_interval FROM feeds WHERE user_id = ?", (UID,)
            ).fetchone()
        assert row["poll_interval"] == 15  # trial floor

    def test_single_mode_untouched(self, monkeypatch, db_tmp):
        monkeypatch.setattr(settings, "MULTI", False)
        c = TestClient(app)
        c.headers["X-Auth-Token"] = "tok"
        monkeypatch.setattr(settings, "AUTH_TOKEN", "tok")
        r = c.post(
            "/api/feeds",
            data={"name": "solo", "url": "https://example.com/f.xml", "poll_interval": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT poll_interval FROM feeds").fetchone()
        assert row["poll_interval"] == 1  # no plan clamp in single mode


class TestDestinationCapRoute:
    def test_add_destination_blocked_at_cap(self, multi_client):
        for i in range(5):
            r = multi_client.post(
                "/api/email-accounts",
                data={"name": f"a{i}", "email": f"user{i}@example.com"},
                follow_redirects=False,
            )
            assert r.status_code == 303
        # Form route: renders the accounts page with an error banner (200),
        # unlike the JSON feeds route which raises a 402.
        r = multi_client.post(
            "/api/email-accounts",
            data={"name": "sixth", "email": "sixth@example.com"},
        )
        assert r.status_code == 200
        assert "5 connected" in r.text

    def test_cap_counts_across_destination_types(self, multi_client):
        # 4 email + 1 microblog = 5 total → next email is blocked
        for i in range(4):
            multi_client.post(
                "/api/email-accounts",
                data={"name": f"a{i}", "email": f"user{i}@example.com"},
                follow_redirects=False,
            )
        with mock.patch(
            "app.microblog_list_destinations",
            return_value=[{"uid": "https://a.micro.blog/", "name": "A"}],
        ):
            r = multi_client.post(
                "/api/microblog-accounts", data={"token": "t"}, follow_redirects=False
            )
        assert r.status_code == 303
        r = multi_client.post(
            "/api/email-accounts",
            data={"name": "over", "email": "over@example.com"},
        )
        assert r.status_code == 200
        assert "5 connected" in r.text


class TestEchoDripClampRoute:
    def _setup_feed(self, client):
        client.post(
            "/api/feeds",
            data={"name": "f", "url": "https://example.com/feed.xml"},
            follow_redirects=False,
        )

    def _add_email(self, client):
        r = client.post(
            "/api/email-accounts",
            data={"name": "a", "email": "dest@example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 303

    def test_drip_clamped_to_plan_ceiling(self, multi_client):
        self._setup_feed(multi_client)
        self._add_email(multi_client)
        r = multi_client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "email",
                "email_account_id": "1",
                "template": "{{ title }}",
                "drip_limit": "500",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT drip_limit FROM echoes WHERE user_id = ?", (UID,)).fetchone()
        assert row["drip_limit"] == 60  # trial ceiling


# ── Admin plan / extend-trial ───────────────────────────────────────────────


@pytest.fixture()
def admin_client(multi_client):
    with database.get_db() as db:
        db.execute("UPDATE users SET is_admin = 1, email = 'admin@example.com' WHERE id = ?", (UID,))
    return multi_client


class TestAdminPlanControls:
    def test_set_plan_to_paid_unpauses(self, admin_client):
        _set_plan(UID, "trial", _past(1))
        r = admin_client.post(
            "/admin/users/5/plan", data={"plan": "paid"}, follow_redirects=False
        )
        assert r.status_code == 302
        with database.get_db() as db:
            row = db.execute("SELECT plan FROM users WHERE id = ?", (UID,)).fetchone()
        assert row["plan"] == "paid"

    def test_set_plan_rejects_unknown_plan(self, admin_client):
        r = admin_client.post(
            "/admin/users/5/plan", data={"plan": "gold"}, follow_redirects=False
        )
        assert r.status_code == 400

    def test_extend_trial_sets_future_expiry(self, admin_client):
        _set_plan(UID, "trial", _past(30))
        r = admin_client.post(
            "/admin/users/5/extend-trial", data={"days": "14"}, follow_redirects=False
        )
        assert r.status_code == 302
        with database.get_db() as db:
            row = db.execute(
                "SELECT plan, trial_ends_at FROM users WHERE id = ?", (UID,)
            ).fetchone()
        assert row["plan"] == "trial"
        assert plans.trial_state(row["plan"], row["trial_ends_at"]) == "active"

    def test_extend_trial_rejects_out_of_range(self, admin_client):
        r = admin_client.post(
            "/admin/users/5/extend-trial", data={"days": "0"}, follow_redirects=False
        )
        assert r.status_code == 400

    def test_admin_routes_require_admin(self, multi_client):
        r = multi_client.post(
            "/admin/users/5/plan", data={"plan": "paid"}, follow_redirects=False
        )
        assert r.status_code == 403


# ── Scheduler trial-pause gate ──────────────────────────────────────────────


class TestTrialPauseScheduler:
    def _setup_feed_and_echo(self):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/f', ?)",
                (UID,),
            )
            db.execute(
                """INSERT INTO email_accounts (id, name, email, user_id)
                   VALUES (1, 'a', 'dest@example.com', ?)""",
                (UID,),
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id,
                                       template, user_id, enabled)
                   VALUES (1, 'email', 1, '{{ title }}', ?, 1)""",
                (UID,),
            )

    def test_expired_trial_feed_not_polled(self, multi_client, monkeypatch):
        _set_plan(UID, "trial", _past(1))
        self._setup_feed_and_echo()
        with mock.patch.object(scheduler, "check_feed", wraps=scheduler.check_feed) as spy, \
             mock.patch.object(scheduler, "fetch_feed", side_effect=AssertionError("must not fetch")):
            scheduler.check_all_feeds()
        assert not spy.called

    def test_active_trial_feed_polled(self, multi_client, monkeypatch):
        _set_plan(UID, "trial", _future(5))
        self._setup_feed_and_echo()
        with mock.patch.object(
            scheduler, "fetch_feed", return_value={"title": "t", "items": []}
        ) as spy:
            scheduler.check_all_feeds()
        assert spy.called

    def test_paid_plan_with_past_expiry_still_polled(self, multi_client):
        _set_plan(UID, "paid", _past(400))
        self._setup_feed_and_echo()
        with mock.patch.object(
            scheduler, "fetch_feed", return_value={"title": "t", "items": []}
        ) as spy:
            scheduler.check_all_feeds()
        assert spy.called

    def test_expired_trial_check_feed_skips_echoes(self, multi_client):
        """Direct check_feed: paused user's enabled echoes are invisible."""
        _set_plan(UID, "trial", _past(1))
        self._setup_feed_and_echo()
        with mock.patch.object(
            scheduler, "fetch_feed", side_effect=AssertionError("must not fetch")
        ):
            # _acquire_feed_lease uses the real DB; run the real path
            scheduler.check_feed(1)
        # No fetch attempt, no crash — the gate returned before fetching.

    def test_paused_user_nothing_deleted(self, multi_client):
        _set_plan(UID, "trial", _past(1))
        self._setup_feed_and_echo()
        scheduler.check_all_feeds()
        with database.get_db() as db:
            feeds = db.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
            echoes = db.execute("SELECT COUNT(*) AS c FROM echoes").fetchone()["c"]
            posted = db.execute("SELECT COUNT(*) AS c FROM posted_items").fetchone()["c"]
        assert feeds == 1 and echoes == 1 and posted == 0


# ── Kimi-gate fixes: regression pins ────────────────────────────────────────


class TestEchoRoutesNotCapBlocked:
    """F1: at-cap users must still be able to create/edit echoes."""

    def _fill_dests(self, client, n):
        for i in range(n):
            r = client.post(
                "/api/email-accounts",
                data={"name": f"a{i}", "email": f"user{i}@example.com"},
            )
            assert r.status_code == 200 or r.status_code == 303

    def test_echo_create_works_at_destination_cap(self, multi_client):
        self._fill_dests(multi_client, 5)
        multi_client.post(
            "/api/feeds",
            data={"name": "f", "url": "https://example.com/feed.xml"},
            follow_redirects=False,
        )
        r = multi_client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "email",
                "email_account_id": "1",
                "template": "{{ title }}",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

    def test_resaving_existing_email_at_cap_is_upsert_not_block(self, multi_client):
        """F9a: re-saving a connected address changes nothing the cap measures."""
        self._fill_dests(multi_client, 5)
        r = multi_client.post(
            "/api/email-accounts",
            data={"name": "renamed", "email": "user0@example.com"},
        )
        assert r.status_code == 200
        assert "plan allows" not in r.text
        with database.get_db() as db:
            row = db.execute(
                "SELECT name FROM email_accounts WHERE email = 'user0@example.com'"
            ).fetchone()
        assert row["name"] == "renamed"


class TestMicroblogMultiInsertCap:
    """F3: a token covering N blogs inserts N rows; the cap counts them all."""

    def test_connect_blocked_when_blogs_would_overflow(self, multi_client):
        for i in range(4):
            multi_client.post(
                "/api/email-accounts",
                data={"name": f"a{i}", "email": f"user{i}@example.com"},
            )
        # cap 5, current 4: a 2-blog token would make 6
        with mock.patch(
            "app.microblog_list_destinations",
            return_value=[
                {"uid": "https://a.micro.blog/", "name": "A"},
                {"uid": "https://b.micro.blog/", "name": "B"},
            ],
        ):
            r = multi_client.post("/api/microblog-accounts", data={"token": "t"})
        assert "would fit" in r.text
        with database.get_db() as db:
            count = db.execute("SELECT COUNT(*) AS c FROM microblog_accounts").fetchone()["c"]
        assert count == 0

    def test_connect_fits_when_room(self, multi_client):
        for i in range(3):
            multi_client.post(
                "/api/email-accounts",
                data={"name": f"a{i}", "email": f"user{i}@example.com"},
            )
        with mock.patch(
            "app.microblog_list_destinations",
            return_value=[
                {"uid": "https://a.micro.blog/", "name": "A"},
                {"uid": "https://b.micro.blog/", "name": "B"},
            ],
        ):
            r = multi_client.post(
                "/api/microblog-accounts", data={"token": "t"}, follow_redirects=False
            )
        assert r.status_code == 303
        with database.get_db() as db:
            count = db.execute("SELECT COUNT(*) AS c FROM microblog_accounts").fetchone()["c"]
        assert count == 2


class TestExtendTrialDowngradeGuard:
    """F4: extend-trial must not silently downgrade paid/beta users."""

    def test_extend_trial_refuses_paid_user(self, admin_client):
        _set_plan(UID, "paid", None)
        r = admin_client.post(
            "/admin/users/5/extend-trial", data={"days": "14"},
        )
        assert r.status_code == 400
        assert "no trial to extend" in r.text
        with database.get_db() as db:
            row = db.execute("SELECT plan, trial_ends_at FROM users WHERE id = ?", (UID,)).fetchone()
        assert row["plan"] == "paid"
        assert row["trial_ends_at"] is None

    def test_extend_trial_works_for_trial_user(self, admin_client):
        _set_plan(UID, "trial", _past(30))
        r = admin_client.post(
            "/admin/users/5/extend-trial", data={"days": "14"}, follow_redirects=False
        )
        assert r.status_code == 302
        with database.get_db() as db:
            row = db.execute(
                "SELECT plan, trial_ends_at FROM users WHERE id = ?", (UID,)
            ).fetchone()
        assert row["plan"] == "trial"
        assert plans.trial_state("trial", row["trial_ends_at"]) == "active"


class TestDigestGate:
    """F6: the digest sweep must skip paused/suspended owners."""

    def _setup_digest(self):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/f', ?)",
                (UID,),
            )
            db.execute(
                """INSERT INTO email_accounts (id, name, email, user_id)
                   VALUES (1, 'a', 'dest@example.com', ?)""",
                (UID,),
            )
            db.execute(
                """INSERT INTO echoes (id, feed_id, destination_type, destination_id,
                                       template, delivery_mode, user_id, enabled)
                   VALUES (1, 1, 'email', 1, '{{ title }}', 'digest', ?, 1)""",
                (UID,),
            )
            db.execute(
                """INSERT INTO digest_items (echo_id, item_id, item_title, item_url, rendered_content)
                   VALUES (1, 'item-1', 'T', 'https://example.com/1', 'rendered')"""
            )
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, status)
                   VALUES (1, 'item-1', 'queued')"""
            )

    def test_expired_trial_digest_items_not_sent(self, multi_client, monkeypatch):
        _set_plan(UID, "trial", _past(1))
        self._setup_digest()
        sent = []
        monkeypatch.setattr(
            scheduler, "send_email",
            lambda **kw: sent.append(kw) or {"success": True},
        )
        scheduler.flush_digests()
        assert sent == []
        # Items stay queued (not discarded, not sent): pause is reversible.
        with database.get_db() as db:
            row = db.execute("SELECT status FROM posted_items").fetchone()
        assert row["status"] == "queued"

    def test_active_trial_digest_items_sent(self, multi_client, monkeypatch):
        _set_plan(UID, "trial", _future(5))
        self._setup_digest()
        sent = []
        monkeypatch.setattr(
            scheduler, "send_email",
            lambda **kw: sent.append(kw) or {"success": True},
        )
        scheduler.flush_digests()
        assert len(sent) == 1


class TestDripClampSetBased:
    """F5/F7: drip clamp reads plan from the joined echo row, fails closed."""

    @pytest.fixture(autouse=True)
    def _multi(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)

    def test_missing_plan_key_fails_closed_to_trial_ceiling(self):
        echo = {"id": 1, "drip_limit": 500}  # no 'plan' key
        assert scheduler._drip_limit(echo) == 60

    def test_paid_plan_row_gets_paid_ceiling(self):
        echo = {"id": 1, "drip_limit": 500, "plan": "paid"}
        assert scheduler._drip_limit(echo) == 500

    def test_trial_plan_row_clamped(self):
        echo = {"id": 1, "drip_limit": 100, "plan": "trial"}
        assert scheduler._drip_limit(echo) == 60
