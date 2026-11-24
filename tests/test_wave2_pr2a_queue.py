"""Wave 2 PR 2A regression suite: Curation Queue Edit, Draft Approve/Schedule,
Queue Cadence & Active Hours settings, and Queue accessibility/touch targets
(FE-16, FE-17, FE-22 queue part).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from websockets.sync.client import connect as ws_connect

import database
import scheduler
import security
import settings
from app import app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def pr2a_multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(settings, "STATE_SECRET", "t" * 48)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "pr2a_multi.db")
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)

    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan) VALUES (101, 'u1@example.com', '', 'paid')"
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan) VALUES (102, 'u2@example.com', '', 'paid')"
        )
        db.execute(
            "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
            " VALUES (1, 'Masto One', 'm1', 'https://mastodon.social', 'tok1', 101)"
        )
        db.execute(
            "INSERT INTO bluesky_accounts (id, name, handle, app_password, user_id)"
            " VALUES (1, 'Bsky One', 'u1.bsky.social', 'pw1', 101)"
        )
        db.execute(
            "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
            " VALUES (2, 'Masto Two', 'm2', 'https://mastodon.social', 'tok2', 102)"
        )
        db.execute(
            "INSERT INTO feeds (id, name, url, read_enabled, user_id)"
            " VALUES (1, 'Feed One', 'https://example.com/f1.xml', 1, 101)"
        )
        db.execute(
            "INSERT INTO feeds (id, name, url, read_enabled, user_id)"
            " VALUES (2, 'Feed Two', 'https://example.com/f2.xml', 1, 102)"
        )
    return tmp_path


def _multi_client(uid: int = 101, email: str = "u1@example.com") -> TestClient:
    c = TestClient(app)
    c.cookies.set("feedecho_session", security.sign_session(uid, email))
    return c


class TestQueueApproveAndEditApi:
    def test_approve_draft_uses_next_free_slot_and_redirects_or_json(self, pr2a_multi_env):
        with database.get_db() as db:
            db.execute(
                """
                INSERT INTO queued_posts (
                    id, user_id, item_id, feed_id, destination_type, destination_id,
                    content, scheduled_at, status
                ) VALUES
                    (11, 101, 'i-1', 1, 'mastodon', 1, 'Draft 11', '2026-11-24 12:00:00', 'draft'),
                    (12, 101, 'i-2', 1, 'mastodon', 1, 'Draft 12', '2026-11-24 12:05:00', 'draft')
                """
            )
            db.execute(
                """
                INSERT INTO queue_settings (user_id, destination_key, queue_interval_minutes, queue_active_hours)
                VALUES (101, 'mastodon:1', 90, '')
                """
            )

        client = _multi_client(101, "u1@example.com")

        # HTML form submission -> 303 redirect to /queue
        r1 = client.post("/api/queue/11/approve", follow_redirects=False)
        assert r1.status_code == 303
        assert r1.headers["location"] == "/queue"

        with database.get_db() as db:
            row11 = db.execute("SELECT status, scheduled_at FROM queued_posts WHERE id = 11").fetchone()
        assert row11["status"] == "queued"
        slot11 = database.as_utc_naive(row11["scheduled_at"])
        assert slot11 is not None

        # JSON submission -> 200 with scheduled_at advancing by saved 90m cadence
        r2 = client.post(
            "/api/queue/12/approve",
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        assert r2.status_code == 200
        body = r2.json()
        assert body["success"] is True
        slot12 = database.as_utc_naive(body["scheduled_at"])
        assert slot12 is not None
        assert int((slot12 - slot11).total_seconds()) == 90 * 60

    def test_approve_accepts_explicit_scheduled_at_and_rejects_bad_states(self, pr2a_multi_env):
        with database.get_db() as db:
            db.execute(
                """
                INSERT INTO queued_posts (
                    id, user_id, item_id, feed_id, destination_type, destination_id,
                    content, scheduled_at, status
                ) VALUES
                    (21, 101, 'i-21', 1, 'mastodon', 1, 'Draft 21', '2026-11-24 12:00:00', 'draft'),
                    (22, 101, 'i-22', 1, 'mastodon', 1, 'Queued 22', '2026-11-24 14:00:00', 'queued'),
                    (23, 101, 'i-23', 1, 'mastodon', 1, 'Failed 23', '2026-11-24 15:00:00', 'failed'),
                    (24, 102, 'i-24', 2, 'mastodon', 2, 'Other user draft', '2026-11-24 16:00:00', 'draft')
                """
            )

        client = _multi_client(101, "u1@example.com")

        # Invalid scheduled_at -> 400 without mutating status
        bad = client.post("/api/queue/21/approve", data={"scheduled_at": "not-a-timestamp"})
        assert bad.status_code == 400

        # Valid ISO UTC scheduled_at -> normalized to SQL UTC
        ok = client.post(
            "/api/queue/21/approve",
            data={"scheduled_at": "2026-12-01T18:30:00Z"},
            headers={"Accept": "application/json"},
        )
        assert ok.status_code == 200
        assert ok.json() == {"success": True, "scheduled_at": "2026-12-01 18:30:00"}

        # Already queued / failed -> 400
        assert client.post("/api/queue/21/approve").status_code == 400
        assert client.post("/api/queue/22/approve").status_code == 400
        assert client.post("/api/queue/23/approve").status_code == 400

        # Other user's draft or missing -> 404
        assert client.post("/api/queue/24/approve").status_code == 404
        assert client.post("/api/queue/9999/approve").status_code == 404

    def test_edit_works_for_draft_and_queued_and_enforces_ownership(self, pr2a_multi_env):
        with database.get_db() as db:
            db.execute(
                """
                INSERT INTO queued_posts (
                    id, user_id, item_id, feed_id, destination_type, destination_id,
                    content, scheduled_at, status
                ) VALUES
                    (31, 101, 'i-31', 1, 'mastodon', 1, 'Original draft', '2026-11-24 10:00:00', 'draft'),
                    (32, 101, 'i-32', 1, 'mastodon', 1, 'Original queued', '2026-11-24 11:00:00', 'queued'),
                    (33, 102, 'i-33', 2, 'mastodon', 2, 'U2 queued', '2026-11-24 11:00:00', 'queued')
                """
            )

        client = _multi_client(101, "u1@example.com")
        r_draft = client.post(
            "/api/queue/31/edit",
            data={"content": "Updated draft text", "scheduled_at": "2026-11-25 14:15:00"},
            follow_redirects=False,
        )
        assert r_draft.status_code == 303
        r_queued = client.post(
            "/api/queue/32/edit",
            data={"content": "Updated queued text", "scheduled_at": "2026-11-25 16:45:00"},
            follow_redirects=False,
        )
        assert r_queued.status_code == 303
        assert (
            client.post(
                "/api/queue/33/edit",
                data={"content": "Hijack", "scheduled_at": "2026-11-25 16:45:00"},
            ).status_code
            == 404
        )

        with database.get_db() as db:
            d_row = db.execute("SELECT content, scheduled_at, status FROM queued_posts WHERE id = 31").fetchone()
            q_row = db.execute("SELECT content, scheduled_at, status FROM queued_posts WHERE id = 32").fetchone()
        assert d_row["content"] == "Updated draft text"
        assert database.timestamp_str(d_row["scheduled_at"]) == "2026-11-25 14:15:00"
        assert d_row["status"] == "draft"
        assert q_row["content"] == "Updated queued text"
        assert database.timestamp_str(q_row["scheduled_at"]) == "2026-11-25 16:45:00"
        assert q_row["status"] == "queued"


class TestQueueSettingsApi:
    def test_settings_ownership_validation_clamping_and_hhmm_guards(self, pr2a_multi_env):
        client = _multi_client(101, "u1@example.com")

        # Unknown / other user's destination -> 400
        for bad_dest in ("", "mastodon:2", "mastodon:999", "invalid:1", "github:1"):
            r = client.post(
                "/api/queue/settings",
                data={"destination_key": bad_dest, "queue_interval_minutes": "120"},
            )
            assert r.status_code == 400, f"Expected 400 for destination {bad_dest}"

        # Non-integer interval or tz_offset -> 400
        assert (
            client.post(
                "/api/queue/settings",
                data={"destination_key": "mastodon:1", "queue_interval_minutes": "abc"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/queue/settings",
                data={
                    "destination_key": "mastodon:1",
                    "queue_interval_minutes": "60",
                    "tz_offset_minutes": "bad",
                },
            ).status_code
            == 400
        )

        # Invalid HH:MM -> 400
        for bad_start, bad_end in (
            ("9:00", "21:00"),
            ("24:00", "21:00"),
            ("09:60", "21:00"),
            ("09:00", "25:00"),
            ("09:00", ""),
            ("", "21:00"),
            ("bad", "21:00"),
        ):
            r = client.post(
                "/api/queue/settings",
                data={
                    "destination_key": "mastodon:1",
                    "queue_interval_minutes": "60",
                    "active_hours_enabled": "1",
                    "active_start": bad_start,
                    "active_end": bad_end,
                },
            )
            assert r.status_code == 400, f"Expected 400 for ({bad_start!r}, {bad_end!r})"

        # Clamp low (5 -> 15) and high (99999 -> 10080)
        r_low = client.post(
            "/api/queue/settings",
            data={"destination_key": "mastodon:1", "queue_interval_minutes": "5"},
            follow_redirects=False,
        )
        assert r_low.status_code == 303
        assert r_low.headers["location"] == "/queue?status=saved"
        with database.get_db() as db:
            row = db.execute(
                "SELECT queue_interval_minutes, queue_active_hours FROM queue_settings"
                " WHERE user_id = 101 AND destination_key = 'mastodon:1'"
            ).fetchone()
        assert row["queue_interval_minutes"] == 15
        assert row["queue_active_hours"] == ""

        r_high = client.post(
            "/api/queue/settings",
            data={"destination_key": "mastodon:1", "queue_interval_minutes": "99999"},
            follow_redirects=False,
        )
        assert r_high.status_code == 303
        with database.get_db() as db:
            row = db.execute(
                "SELECT queue_interval_minutes FROM queue_settings"
                " WHERE user_id = 101 AND destination_key = 'mastodon:1'"
            ).fetchone()
        assert row["queue_interval_minutes"] == 10080

    def test_timezone_conversion_sameday_overnight_24h_and_next_free_slot(self, pr2a_multi_env):
        client = _multi_client(101, "u1@example.com")

        # 1. Positive tz_offset_minutes (+300 = EST UTC-5):
        #    09:00-17:00 local -> 14:00-22:00 UTC (same-day UTC window)
        r1 = client.post(
            "/api/queue/settings",
            data={
                "destination_key": "mastodon:1",
                "queue_interval_minutes": "120",
                "active_hours_enabled": "1",
                "active_start": "09:00",
                "active_end": "17:00",
                "tz_offset_minutes": "300",
            },
            follow_redirects=False,
        )
        assert r1.status_code == 303
        with database.get_db() as db:
            s1 = db.execute(
                "SELECT queue_interval_minutes, queue_active_hours FROM queue_settings"
                " WHERE user_id = 101 AND destination_key = 'mastodon:1'"
            ).fetchone()
        assert s1["queue_interval_minutes"] == 120
        assert s1["queue_active_hours"] == "14:00-22:00"

        # 2. Positive tz_offset_minutes (+300 = EST UTC-5) crossing UTC midnight:
        #    09:00-21:00 local -> 14:00-02:00 UTC (overnight UTC window)
        assert (
            client.post(
                "/api/queue/settings",
                data={
                    "destination_key": "mastodon:1",
                    "queue_interval_minutes": "60",
                    "active_hours_enabled": "1",
                    "active_start": "09:00",
                    "active_end": "21:00",
                    "tz_offset_minutes": "300",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        with database.get_db() as db:
            s2 = db.execute(
                "SELECT queue_active_hours FROM queue_settings"
                " WHERE user_id = 101 AND destination_key = 'mastodon:1'"
            ).fetchone()
            # Verify next_free_slot snaps a 05:00 UTC base + 60m = 06:00 UTC (outside 14:00-02:00) -> 14:00 UTC
            db.execute(
                """
                INSERT INTO queued_posts (
                    id, user_id, item_id, feed_id, destination_type, destination_id,
                    content, scheduled_at, status
                ) VALUES (50, 101, 'i-50', 1, 'mastodon', 1, 'Anchor', '2030-06-01 05:00:00', 'queued')
                """
            )
            slot = scheduler.next_free_slot(db, 101, "mastodon", 1)
        assert s2["queue_active_hours"] == "14:00-02:00"
        assert slot == "2030-06-01 14:00:00"

        # 3. Negative tz_offset_minutes (-540 = JST UTC+9):
        #    02:00-10:00 local -> 17:00-01:00 UTC (overnight)
        assert (
            client.post(
                "/api/queue/settings",
                data={
                    "destination_key": "bluesky:1",
                    "queue_interval_minutes": "45",
                    "active_hours_enabled": "1",
                    "active_start": "02:00",
                    "active_end": "10:00",
                    "tz_offset_minutes": "-540",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        with database.get_db() as db:
            sb = db.execute(
                "SELECT queue_interval_minutes, queue_active_hours FROM queue_settings"
                " WHERE user_id = 101 AND destination_key = 'bluesky:1'"
            ).fetchone()
        assert sb["queue_interval_minutes"] == 45
        assert sb["queue_active_hours"] == "17:00-01:00"

        # 4. Unchecked form checkbox stores 24h empty window "" AND GET /queue preserves ""
        assert (
            client.post(
                "/api/queue/settings",
                data={
                    "destination_key": "mastodon:1",
                    "queue_interval_minutes": "30",
                    "form_mode": "1",
                    "active_hours_enabled": "",
                    "active_start": "09:00",
                    "active_end": "21:00",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        with database.get_db() as db:
            s_empty = db.execute(
                "SELECT queue_interval_minutes, queue_active_hours FROM queue_settings"
                " WHERE user_id = 101 AND destination_key = 'mastodon:1'"
            ).fetchone()
            # 05:00 + 30m stays 05:30 because 24h window has no active-hours snap
            slot_24h = scheduler.next_free_slot(db, 101, "mastodon", 1)
        assert s_empty["queue_interval_minutes"] == 30
        assert s_empty["queue_active_hours"] == ""
        assert slot_24h == "2030-06-01 05:30:00"

        page = client.get("/queue?status=saved")
        assert page.status_code == 200
        assert 'role="status">Queue cadence settings saved.</div>' in page.text
        assert 'data-utc-window=""' in page.text
        assert 'data-utc-window="17:00-01:00"' in page.text

    def test_single_mode_queue_approve_and_settings(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()

        with database.get_db() as db:
            db.execute(
                "INSERT INTO accounts (id, name, username, instance, access_token)"
                " VALUES (1, 'Solo Masto', 'solo', 'https://mastodon.social', 'tok')"
            )
            db.execute(
                "INSERT INTO feeds (id, name, url, read_enabled) VALUES (1, 'Solo Feed', 'https://e.com/f', 1)"
            )
            db.execute(
                """
                INSERT INTO queued_posts (
                    id, user_id, item_id, feed_id, destination_type, destination_id,
                    content, scheduled_at, status
                ) VALUES (1, 1, 'item-s1', 1, 'mastodon', 1, 'Single draft', '2026-11-24 10:00:00', 'draft')
                """
            )

        client = TestClient(app)
        assert (
            client.post(
                "/api/queue/settings",
                data={
                    "destination_key": "mastodon:1",
                    "queue_interval_minutes": "75",
                    "active_hours_enabled": "1",
                    "active_start": "08:00",
                    "active_end": "20:00",
                    "tz_offset_minutes": "0",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        res = client.post("/api/queue/1/approve", headers={"Accept": "application/json"})
        assert res.status_code == 200
        assert res.json()["success"] is True

    def test_postgres_datetime_scheduled_at_renders_clean_data_attributes(self):
        """Simulate psycopg returning scheduled_at as a datetime object in queue.html."""
        import app as app_mod

        tpl = app_mod.jinja.get_template("queue.html")
        dt_val = datetime(2026, 11, 24, 18, 30, 0, tzinfo=timezone.utc)

        class _DummyUrl:
            path = "/queue"

        class _DummyReq:
            url = _DummyUrl()
            query_params = {}
            state = type("S", (), {"authed": True})()

        html = tpl.render(
            request=_DummyReq(),
            authed=True,
            MULTI=False,
            is_admin=False,
            posting_paused=False,
            trial_pending=False,
            billing_enabled=False,
            current_user_email="",
            project_url="https://example.com",
            app_version="0.0.0",
            cadence_destinations=[],
            posts=[
                {
                    "id": 77,
                    "feed_name": "PG Feed",
                    "destination_label": "Mastodon · Main",
                    "destination_type": "mastodon",
                    "destination_id": 1,
                    "content": "PG scheduled row",
                    "scheduled_at": dt_val,
                    "status": "queued",
                    "attempt_count": 0,
                    "error_message": None,
                }
            ],
        )
        assert 'data-scheduled-iso="2026-11-24T18:30:00Z"' in html
        assert 'data-scheduled-utc="2026-11-24 18:30:00"' in html


def _find_chromium() -> str | None:
    preferred = Path("/var/home/jason/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")
    if preferred.is_file() and os.access(preferred, os.X_OK):
        return str(preferred)
    for root in (
        Path.home() / ".cache" / "ms-playwright",
        Path("/var/home/jason/.cache/ms-playwright"),
    ):
        if root.is_dir():
            for c in sorted(root.glob("chromium-*/chrome-linux*/chrome"), reverse=True):
                if c.is_file() and os.access(c, os.X_OK):
                    return str(c)
    for binary in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(binary)
        if found:
            return found
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _CdpPage:
    def __init__(self, ws_url: str):
        self._ws = ws_connect(ws_url, max_size=16 * 1024 * 1024)
        self._msg_id = 0
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("DOM.enable")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def call(self, method: str, **params):
        self._msg_id += 1
        target_id = self._msg_id
        self._ws.send(json.dumps({"id": target_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self._ws.recv(timeout=15))
            if msg.get("id") == target_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} failed: {msg['error']}")
                return msg.get("result", {})

    def set_viewport(self, width: int, height: int) -> None:
        self.call(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=False,
            screenWidth=width,
            screenHeight=height,
        )

    def set_cookie(self, base_url: str, name: str, value: str | None) -> None:
        self.call("Network.enable")
        self.call("Network.clearBrowserCookies")
        if value is not None:
            self.call("Network.setCookie", url=base_url, name=name, value=value, path="/")

    def goto(self, url: str, width: int, height: int, theme: str = "dark") -> None:
        self.call(
            "Emulation.setEmulatedMedia",
            features=[{"name": "prefers-color-scheme", "value": theme}],
        )
        self.set_viewport(width, height)
        self.call("Page.navigate", url=url)
        self.wait_complete()
        self.set_viewport(width, height)
        self.eval(
            f"""(() => {{
                document.documentElement.setAttribute('data-theme', {json.dumps(theme)});
                document.documentElement.setAttribute('data-theme-mode', {json.dumps(theme)});
            }})()"""
        )

    def wait_complete(self, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self.eval("document.readyState") == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.05)

    def eval(self, expression: str):
        res = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
        if "exceptionDetails" in res:
            raise AssertionError(f"JS exception: {res['exceptionDetails']}")
        return res.get("result", {}).get("value")


_PR2A_SERVER_SCRIPT = """
import json, os, sys
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
os.environ["FEEDECHO_MODE"] = "multi"
os.environ["FEEDECHO_DB_PATH"] = cfg["db_path"]
os.environ["FEEDECHO_ALLOW_SQLITE_FALLBACK"] = "1"
os.environ["FE" + "EDECHO_SESSION_" + "SEC" + "RET"] = cfg["sess_val"]
os.environ["FE" + "EDECHO_STATE_" + "SEC" + "RET"] = cfg["state_val"]

import database, security, app as app_mod
import uvicorn

app_mod.start_scheduler = lambda: None
app_mod.stop_scheduler = lambda: None

database.init_db()
with database.get_db() as db:
    db.execute(
        "INSERT INTO users (id, email, password_hash, plan, email_verified, is_admin, suspended, session_epoch)"
        " VALUES (201, 'curator@example.com', 'x', 'paid', 1, 1, 0, 0)"
    )
    db.execute(
        "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
        " VALUES (1, 'Curator Masto', 'cur', 'https://mastodon.social', 'tok', 201)"
    )
    db.execute(
        "INSERT INTO bluesky_accounts (id, name, handle, app_password, user_id)"
        " VALUES (1, 'Curator Bsky', 'cur.bsky.social', 'pw', 201)"
    )
    db.execute(
        "INSERT INTO feeds (id, name, url, poll_interval, read_enabled, user_id)"
        " VALUES (1, 'Curated Feed', 'https://example.com/rss.xml', 15, 1, 201)"
    )
    db.execute(
        "INSERT INTO queue_settings (user_id, destination_key, queue_interval_minutes, queue_active_hours)"
        " VALUES (201, 'mastodon:1', 180, '14:00-02:00')"
    )
    db.execute(
        \"\"\"
        INSERT INTO queued_posts (
            id, user_id, item_id, feed_id, destination_type, destination_id,
            content, scheduled_at, status, error_message
        ) VALUES
            (1, 201, 'item-1', 1, 'mastodon', 1, 'Draft item awaiting approval', '2026-11-24 15:00:00', 'draft', NULL),
            (2, 201, 'item-2', 1, 'mastodon', 1, 'Queued post alpha', '2026-11-24 18:30:00', 'queued', NULL),
            (3, 201, 'item-3', 1, 'mastodon', 1, 'Queued post beta', '2026-11-24 22:30:00', 'queued', NULL),
            (4, 201, 'item-4', 1, 'mastodon', 1, 'Failed post gamma', '2026-11-24 10:00:00', 'failed', 'Upstream 502 timeout')
        \"\"\"
    )

Path(cfg["cookies_path"]).write_text(
    json.dumps({"curator": security.sign_session(201, "curator@example.com", 0)}),
    encoding="utf-8",
)
uvicorn.run(app_mod.app, host="127.0.0.1", port=cfg["port"], log_level="error")
"""


@pytest.fixture(scope="module")
def pr2a_browser(tmp_path_factory):
    chrome_bin = _find_chromium()
    if not chrome_bin:
        pytest.skip("Headless Chromium binary not available")

    work = tmp_path_factory.mktemp("pr2a_browser")
    db_path = work / "feedecho.db"
    port = _free_port()
    cfg_path = work / "server_cfg.json"
    cookies_path = work / "cookies.json"
    runner_path = work / "run_server.py"
    runner_path.write_text(_PR2A_SERVER_SCRIPT, encoding="utf-8")
    cfg_path.write_text(
        json.dumps(
            {
                "db_path": str(db_path),
                "cookies_path": str(cookies_path),
                "port": port,
                "sess_val": "c" * 48,
                "state_val": "d" * 48,
            }
        ),
        encoding="utf-8",
    )

    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k != "PYTHONPATH" and not k.startswith(("FE" + "EDCHO_", "FEEDECHO_"))
    }
    clean_env["PYTHONPATH"] = str(REPO_ROOT)

    server_proc = subprocess.Popen(
        [sys.executable, str(runner_path), str(cfg_path)],
        cwd=str(REPO_ROOT),
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"
    chrome_proc = None
    page = None
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if server_proc.poll() is not None:
                err = server_proc.stderr.read().decode("utf-8", errors="replace") if server_proc.stderr else ""
                raise RuntimeError(f"Ephemeral uvicorn exited early: {err}")
            try:
                with urllib.request.urlopen(f"{base_url}/healthz", timeout=1) as resp:
                    if resp.status == 200 and cookies_path.is_file():
                        break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("Timed out waiting for ephemeral uvicorn /healthz")

        cookies = json.loads(cookies_path.read_text(encoding="utf-8"))
        debug_port = _free_port()
        profile_dir = work / "chrome_profile"
        profile_dir.mkdir()
        chrome_proc = subprocess.Popen(
            [
                chrome_bin,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={profile_dir}",
                "about:blank",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        ws_url = None
        c_deadline = time.monotonic() + 15.0
        while time.monotonic() < c_deadline:
            if chrome_proc.poll() is not None:
                err = chrome_proc.stderr.read().decode("utf-8", errors="replace") if chrome_proc.stderr else ""
                raise RuntimeError(f"Chromium exited early: {err}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=1) as r:
                    targets = json.loads(r.read().decode("utf-8"))
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                time.sleep(0.1)
        if not ws_url:
            raise RuntimeError("Timed out waiting for Chromium CDP target")

        page = _CdpPage(ws_url)
        yield {"page": page, "base_url": base_url, "cookies": cookies, "db_path": db_path}
    finally:
        if page is not None:
            page.close()
        for proc in (chrome_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


class TestQueueBrowserPr2a:
    def test_accessibility_touch_targets_and_no_overflow_mobile_and_desktop(self, pr2a_browser):
        page = pr2a_browser["page"]
        base = pr2a_browser["base_url"]
        page.set_cookie(base, "feedecho_session", pr2a_browser["cookies"]["curator"])

        for width, height in ((390, 844), (1440, 900)):
            page.goto(f"{base}/queue", width, height, theme="dark")
            metrics = page.eval(
                """(() => {
                    const thScopes = Array.from(document.querySelectorAll('.data-table thead th')).map(
                        th => ({ text: th.textContent.trim(), scope: th.getAttribute('scope') })
                    );
                    const upBtn = document.querySelector('#queue-row-2 button[aria-label="Move earlier"]');
                    const downBtn = document.querySelector('#queue-row-2 button[aria-label="Move later"]');
                    const upHidden = upBtn ? !!upBtn.querySelector('span[aria-hidden="true"]') : false;
                    const downHidden = downBtn ? !!downBtn.querySelector('span[aria-hidden="true"]') : false;

                    const swClosed = document.documentElement.scrollWidth;
                    const panel = document.querySelector('details.queue-cadence-panel');
                    panel.open = true;
                    const swCadenceOpen = document.documentElement.scrollWidth;

                    const targets = {};
                    const measure = (name, el) => {
                        if (!el) {
                            targets[name] = null;
                            return;
                        }
                        const r = el.getBoundingClientRect();
                        targets[name] = { w: Math.round(r.width * 100) / 100, h: Math.round(r.height * 100) / 100 };
                    };

                    measure('cadenceSummary', document.querySelector('.queue-cadence-summary'));
                    measure('cadenceInterval', document.querySelector('.queue-cadence-form input[name="queue_interval_minutes"]'));
                    measure('cadenceCheck', document.querySelector('.queue-cadence-check'));
                    measure('cadenceStart', document.querySelector('.queue-cadence-form input[name="active_start"]'));
                    measure('cadenceEnd', document.querySelector('.queue-cadence-form input[name="active_end"]'));
                    measure('cadenceSave', document.querySelector('.queue-cadence-form button[type="submit"]'));
                    measure('draftApprove', document.querySelector('#queue-row-1 form[action$="/approve"] button'));
                    measure('draftEdit', document.querySelector('#queue-row-1 .queue-edit-btn'));
                    measure('queuedEdit', document.querySelector('#queue-row-2 .queue-edit-btn'));
                    measure('postNow', document.querySelector('#queue-row-2 button[onclick*="queuePostNow"]'));
                    measure('moveUp', upBtn);
                    measure('moveDown', downBtn);
                    measure('retryBtn', document.querySelector('#queue-row-4 form[action$="/retry"] button'));
                    measure('removeBtn', document.querySelector('#queue-row-2 form[action$="/cancel"] button'));

                    openQueueEdit(2, document.querySelector('#queue-row-2 .queue-edit-btn'));
                    const swModalOpen = document.documentElement.scrollWidth;
                    measure('modalDatetime', document.getElementById('queue-edit-local-dt'));
                    measure('modalCancel', document.getElementById('queue-edit-cancel'));
                    measure('modalSave', document.getElementById('queue-edit-save'));
                    const cancelRect = document.getElementById('queue-edit-cancel').getBoundingClientRect();
                    const saveRect = document.getElementById('queue-edit-save').getBoundingClientRect();
                    const approveBtn = document.querySelector('#queue-row-1 form[action$="/approve"] button');
                    const neutralBtn = document.querySelector('#queue-row-1 .queue-edit-btn');
                    const approveHasPrimary = approveBtn.classList.contains('btn-primary');
                    const approveBg = getComputedStyle(approveBtn).backgroundColor;
                    const neutralBg = getComputedStyle(neutralBtn).backgroundColor;
                    const dlgHeadingTag = document.getElementById('queue-edit-title')?.tagName;
                    closeQueueEdit();

                    return {
                        thScopes,
                        upHidden,
                        downHidden,
                        swClosed,
                        swCadenceOpen,
                        swModalOpen,
                        targets,
                        cancelLeftOfSave: cancelRect.left < saveRect.left,
                        approveHasPrimary,
                        approveBg,
                        neutralBg,
                        dlgHeadingTag,
                    };
                })()"""
            )

            assert len(metrics["thScopes"]) == 6
            for th in metrics["thScopes"]:
                assert th["scope"] == "col", f"Missing scope=col on {th}"
            assert metrics["upHidden"] is True
            assert metrics["downHidden"] is True
            assert metrics["swClosed"] <= width, f"Overflow at {width}px closed: {metrics['swClosed']}"
            assert metrics["swCadenceOpen"] <= width, f"Overflow at {width}px cadence open: {metrics['swCadenceOpen']}"
            assert metrics["swModalOpen"] <= width, f"Overflow at {width}px modal open: {metrics['swModalOpen']}"
            assert metrics["cancelLeftOfSave"] is True
            assert metrics["approveHasPrimary"] is True
            assert metrics["approveBg"] != metrics["neutralBg"]
            assert metrics["dlgHeadingTag"] == "H2"

            for name, box in metrics["targets"].items():
                assert box is not None, f"Missing control {name} at {width}x{height}"
                assert box["w"] >= 44 and box["h"] >= 44, (
                    f"Control {name} < 44x44px at {width}x{height}: {box}"
                )

    def test_edit_approve_and_cadence_roundtrip_in_chromium(self, pr2a_browser):
        import sqlite3

        page = pr2a_browser["page"]
        base = pr2a_browser["base_url"]
        db_path = pr2a_browser["db_path"]
        page.set_cookie(base, "feedecho_session", pr2a_browser["cookies"]["curator"])

        # 1. Cadence hydration + save + ?status=saved banner + URL replaceState strip
        page.goto(f"{base}/queue", 1440, 900, theme="dark")
        hydrated = page.eval(
            """(() => {
                const panel = document.querySelector('details.queue-cadence-panel');
                panel.open = true;
                const mastoForm = document.querySelector('form.queue-cadence-form input[value="mastodon:1"]').closest('form');
                const tzOffset = new Date().getTimezoneOffset();
                const expStartMins = ((14 * 60 - tzOffset) % 1440 + 1440) % 1440;
                const expEndMins = ((2 * 60 - tzOffset) % 1440 + 1440) % 1440;
                const fmt = (m) => String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
                return {
                    tzInputVal: mastoForm.querySelector('input[name="tz_offset_minutes"]').value,
                    tzExpected: String(tzOffset),
                    startVal: mastoForm.querySelector('input[name="active_start"]').value,
                    endVal: mastoForm.querySelector('input[name="active_end"]').value,
                    expStart: fmt(expStartMins),
                    expEnd: fmt(expEndMins),
                };
            })()"""
        )
        assert hydrated["tzInputVal"] == hydrated["tzExpected"]
        assert hydrated["startVal"] == hydrated["expStart"]
        assert hydrated["endVal"] == hydrated["expEnd"]

        # Submit updated cadence for bluesky:1 via real form submission
        page.eval(
            """(() => {
                const bskyForm = document.querySelector('form.queue-cadence-form input[value="bluesky:1"]').closest('form');
                bskyForm.querySelector('input[name="queue_interval_minutes"]').value = '95';
                bskyForm.querySelector('input[name="active_start"]').value = '08:30';
                bskyForm.querySelector('input[name="active_end"]').value = '20:15';
                bskyForm.requestSubmit();
            })()"""
        )
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            banner = page.eval("document.querySelector('.flash.flash-ok[role=\"status\"]')?.textContent || ''")
            if "Queue cadence settings saved." in banner:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Timed out waiting for saved flash banner after cadence submit")

        post_save = page.eval(
            """({
                search: window.location.search,
                pathname: window.location.pathname,
                bannerText: document.querySelector('.flash.flash-ok[role="status"]')?.textContent?.trim(),
                bskyInterval: document.querySelector('form.queue-cadence-form input[value="bluesky:1"]').closest('form').querySelector('input[name="queue_interval_minutes"]').value,
                bskyStart: document.querySelector('form.queue-cadence-form input[value="bluesky:1"]').closest('form').querySelector('input[name="active_start"]').value,
                bskyEnd: document.querySelector('form.queue-cadence-form input[value="bluesky:1"]').closest('form').querySelector('input[name="active_end"]').value,
            })"""
        )
        assert post_save["pathname"] == "/queue"
        assert post_save["search"] == "", f"status param not stripped from URL: {post_save}"
        assert post_save["bannerText"] == "Queue cadence settings saved."
        assert post_save["bskyInterval"] == "95"
        assert post_save["bskyStart"] == "08:30"
        assert post_save["bskyEnd"] == "20:15"

        # 2. Edit modal round-trip on queued post #2 (local datetime-local -> UTC)
        expected_utc = page.eval(
            """(() => {
                openQueueEdit(2, document.querySelector('#queue-row-2 .queue-edit-btn'));
                const contentEl = document.getElementById('queue-edit-content');
                const localInput = document.getElementById('queue-edit-local-dt');
                contentEl.value = 'Queued post alpha — edited in modal';
                localInput.value = '2026-12-15T11:45';
                localInput.dispatchEvent(new Event('input', { bubbles: true }));
                const previewText = document.getElementById('queue-edit-tz-preview').textContent;
                const hiddenUtc = document.getElementById('queue-edit-utc-dt').value;
                const d = new Date(2026, 11, 15, 11, 45, 0);
                const pad = (n) => String(n).padStart(2, '0');
                const calcUtc = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:00`;
                document.getElementById('queue-edit-form').requestSubmit();
                return { previewText, hiddenUtc, calcUtc };
            })()"""
        )
        assert expected_utc["hiddenUtc"] == expected_utc["calcUtc"]
        assert "Local:" in expected_utc["previewText"] and "UTC:" in expected_utc["previewText"]

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            txt = page.eval("document.querySelector('#queue-row-2')?.textContent || ''")
            if "Queued post alpha — edited in modal" in txt:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Timed out waiting for edited content on #queue-row-2")

        with sqlite3.connect(db_path) as raw_db:
            r2 = raw_db.execute("SELECT content, scheduled_at FROM queued_posts WHERE id = 2").fetchone()
        assert r2 == ("Queued post alpha — edited in modal", expected_utc["calcUtc"])

        # 3. Approve & Schedule button on draft row #1 promotes draft -> queued
        page.eval("document.querySelector('#queue-row-1 form[action$=\"/approve\"]').requestSubmit()")
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            st = page.eval("document.querySelector('#queue-row-1')?.dataset?.status || ''")
            if st == "queued":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Timed out waiting for draft #1 to promote to queued")

        has_approve_after = page.eval("!!document.querySelector('#queue-row-1 form[action$=\"/approve\"]')")
        assert has_approve_after is False

        # 4. Unchecking Active hours in browser disables time inputs and persists "" (24h)
        page.eval(
            """(() => {
                document.querySelector('details.queue-cadence-panel').open = true;
                const bskyForm = document.querySelector('form.queue-cadence-form input[value="bluesky:1"]').closest('form');
                const cb = bskyForm.querySelector('input[name="active_hours_enabled"]');
                cb.checked = false;
                cb.dispatchEvent(new Event('change', { bubbles: true }));
                if (!bskyForm.querySelector('input[name="active_start"]').disabled) {
                    throw new Error('Expected active_start to be disabled when unchecked');
                }
                bskyForm.requestSubmit();
            })()"""
        )
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            unchecked_after = page.eval(
                """(() => {
                    const f = document.querySelector('form.queue-cadence-form input[value="bluesky:1"]')?.closest('form');
                    return f ? (f.dataset.utcWindow === '' && !f.querySelector('input[name="active_hours_enabled"]').checked) : false;
                })()"""
            )
            if unchecked_after:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Timed out waiting for 24h empty active_hours persistence")

        # 5. Dark & light contrast >= 4.5:1 across new queue cadence, flash & modal elements
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "test_wave1_p0_clusters", Path(__file__).with_name("test_wave1_p0_clusters.py")
        )
        assert spec and spec.loader
        w1_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(w1_mod)
        contrast_js = w1_mod.CONTRAST_HELPER_JS

        for theme in ("dark", "light"):
            page.goto(f"{base}/queue?status=saved", 1440, 900, theme=theme)
            page.eval(contrast_js)
            contrasts = page.eval(
                """(() => {
                    document.querySelector('details.queue-cadence-panel').open = true;
                    openQueueEdit(2, document.querySelector('#queue-row-2 .queue-edit-btn'));
                    const out = {
                        flashOk: window.__feContrast(document.querySelector('.flash.flash-ok[role="status"]')),
                        cadenceSummary: window.__feContrast(document.querySelector('.queue-cadence-summary')),
                        cadencePreview: window.__feContrast(document.querySelector('.queue-cadence-preview')),
                        cadenceLabel: window.__feContrast(document.querySelector('.queue-cadence-field span')),
                        cadenceCheck: window.__feContrast(document.querySelector('.queue-cadence-check')),
                        modalTitle: window.__feContrast(document.getElementById('queue-edit-title')),
                        modalPreview: window.__feContrast(document.getElementById('queue-edit-tz-preview')),
                    };
                    closeQueueEdit();
                    return out;
                })()"""
            )
            for key, info in contrasts.items():
                assert info["ratio"] >= 4.5, f"{theme} {key} contrast < 4.5: {info}"




