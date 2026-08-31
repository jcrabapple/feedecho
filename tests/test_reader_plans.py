"""Phase 6: reader plan gating (issue #11)."""

import pytest
from fastapi.testclient import TestClient

import database
import scheduler
import security
import settings
from app import app


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reader-plans.db")
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
    with database.get_db() as db:
        db.execute("INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, '', 'trial')", (11, "trial@example.com"))
        db.execute("INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, '', 'paid')", (12, "paid@example.com"))
        db.execute("INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)", ("F", "u", 12))
        db.execute("INSERT INTO feed_items (feed_id, item_id, title) VALUES (1, 'a', 'Item A')")
    return settings


def _as(client, uid, email):
    client.cookies.set("feedecho_session", security.sign_session(uid, email))
    return client


class TestReaderPlanGating:
    def test_trial_user_cannot_open_reader(self, multi_env):
        with TestClient(app) as c:
            _as(c, 11, "trial@example.com")
            assert c.get("/reader").status_code == 402

    def test_beta_user_cannot_open_reader(self, multi_env):
        with database.get_db() as db:
            db.execute("UPDATE users SET plan = 'beta' WHERE id = 11")
        with TestClient(app) as c:
            _as(c, 11, "trial@example.com")
            assert c.get("/reader").status_code == 402

    def test_paid_user_opens_reader(self, multi_env):
        with TestClient(app) as c:
            _as(c, 12, "paid@example.com")
            resp = c.get("/reader")
        assert resp.status_code == 200
        assert "Item A" in resp.text

    def test_trial_user_cannot_shout(self, multi_env):
        with TestClient(app) as c:
            _as(c, 11, "trial@example.com")
            r = c.post("/api/reader/1/shout", data={"destination": "mastodon:1"})
        assert r.status_code == 402

    def test_single_mode_reader_not_gated(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "reader-single.db")
        database.init_db()
        monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
        with TestClient(app) as c:
            assert c.get("/reader").status_code == 200

class TestReaderEnabled:
    def test_reader_enabled_by_plan(self):
        import plans

        assert plans.reader_enabled("paid") is True
        assert plans.reader_enabled("trial") is False
        assert plans.reader_enabled("beta") is False
        assert plans.reader_enabled("unknown-plan") is False



class TestReaderImportEntitlement:
    def test_trial_import_clamps_read_enabled(self, multi_env):
        import import_export

        payload = {
            "format": "feedecho-export",
            "version": 1,
            "feeds": [{"id": 1, "name": "F", "url": "https://e.com/f", "read_enabled": 1}],
            "accounts": {section: [] for section in import_export.ACCOUNT_TYPES},
            "echoes": [],
        }
        with database.get_db() as db:
            import_export.import_data(db, 11, payload)  # user 11 = trial
            row = db.execute(
                "SELECT read_enabled FROM feeds WHERE url = ?", ("https://e.com/f",)
            ).fetchone()
        assert row["read_enabled"] == 0

    def test_paid_import_keeps_read_enabled(self, multi_env):
        import import_export

        payload = {
            "format": "feedecho-export",
            "version": 1,
            "feeds": [{"id": 1, "name": "F", "url": "https://e.com/f", "read_enabled": 1}],
            "accounts": {section: [] for section in import_export.ACCOUNT_TYPES},
            "echoes": [],
        }
        with database.get_db() as db:
            import_export.import_data(db, 12, payload)  # user 12 = paid
            row = db.execute(
                "SELECT read_enabled FROM feeds WHERE url = ?", ("https://e.com/f",)
            ).fetchone()
        assert row["read_enabled"] == 1

