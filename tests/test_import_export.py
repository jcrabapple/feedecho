"""Import/export: round-trip, id remapping, dedup, quota, and validation."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import database
from database import get_db, init_db
import import_export

TEST_PG_URL = os.environ.get("FEEDECHO_TEST_PG_URL", "")
requires_pg = pytest.mark.skipif(
    not TEST_PG_URL, reason="FEEDECHO_TEST_PG_URL not set; PG tests are CI-gated"
)


@pytest.fixture
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        monkeypatch.setattr("database.DB_PATH", db_path)
        init_db()
        yield db_path


@pytest.fixture
def client(temp_db, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(app_module.settings, "MULTI", False)
    return TestClient(app_module.app)


def _seed(db, feed_url="https://example.com/feed.xml"):
    db.execute(
        "INSERT INTO feeds (name, url) VALUES (?, ?)",
        ("Test Feed", feed_url),
    )
    db.execute(
        "INSERT INTO accounts (name, username, instance, access_token)"
        " VALUES (?, ?, ?, ?)",
        ("Mastodon", "user", "https://mastodon.example", "access-token"),
    )
    db.execute(
        "INSERT INTO bluesky_accounts (name, handle, app_password)"
        " VALUES (?, ?, ?)",
        ("Bluesky", "user.bsky.social", "app-pw"),
    )
    db.execute(
        "INSERT INTO echoes (feed_id, destination_type, destination_id, template)"
        " VALUES (?, ?, ?, ?)",
        (1, "mastodon", 1, "{{ title }} {{ link }}"),
    )
    db.execute(
        "INSERT INTO echoes (feed_id, destination_type, destination_id, template)"
        " VALUES (?, ?, ?, ?)",
        (1, "bluesky", 1, "{{ title }}"),
    )


def _payload(**overrides):
    payload = {
        "format": "feedecho-export",
        "version": 1,
        "feeds": [{"id": 1, "name": "F", "url": "https://a.example/rss"}],
        "accounts": {},
        "echoes": [],
    }
    payload.update(overrides)
    return payload


class TestExport:
    def test_export_document_shape(self, client):
        with get_db() as db:
            _seed(db)
        resp = client.get("/api/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert data["format"] == "feedecho-export"
        assert data["version"] == 1
        assert data["app_version"]
        assert [f["url"] for f in data["feeds"]] == ["https://example.com/feed.xml"]
        assert data["accounts"]["mastodon"][0]["access_token"] == "access-token"
        assert data["accounts"]["bluesky"][0]["app_password"] == "app-pw"
        assert len(data["echoes"]) == 2

    def test_export_excludes_soft_deleted(self, client):
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url) VALUES (?, ?)",
                ("Live", "https://live.example/rss"),
            )
            db.execute(
                "INSERT INTO feeds (name, url, deleted_at) VALUES (?, ?, ?)",
                ("Dead", "https://dead.example/rss", "2026-01-01 00:00:00"),
            )
        data = client.get("/api/export").json()
        urls = [f["url"] for f in data["feeds"]]
        assert "https://live.example/rss" in urls
        assert "https://dead.example/rss" not in urls


class TestImportEndpoint:
    def test_import_success(self, client):
        payload = _payload()
        resp = client.post(
            "/api/import",
            files={"file": ("export.json", json.dumps(payload), "application/json")},
        )
        assert resp.status_code == 200
        assert "Imported 1 feed" in resp.text
        with get_db() as db:
            count = db.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
            assert count == 1

    def test_import_rejects_non_json(self, client):
        resp = client.post(
            "/api/import",
            files={"file": ("export.txt", "this is not json", "text/plain")},
        )
        assert resp.status_code == 200
        assert "not valid JSON" in resp.text

    def test_import_rejects_wrong_format(self, client):
        resp = client.post(
            "/api/import",
            files={"file": ("export.json", json.dumps({"format": "other", "version": 1}), "application/json")},
        )
        assert resp.status_code == 200
        assert "not a FeedEcho export" in resp.text

    def test_import_rejects_newer_version(self, client):
        resp = client.post(
            "/api/import",
            files={"file": ("export.json", json.dumps({"format": "feedecho-export", "version": 2}), "application/json")},
        )
        assert resp.status_code == 200
        assert "newer FeedEcho" in resp.text


class TestRoundTrip:
    def _fresh(self, monkeypatch, tmp_path, name):
        db_path = tmp_path / name
        monkeypatch.setattr("database.DB_PATH", db_path)
        init_db()
        return db_path

    def test_round_trip_remaps_ids(self, monkeypatch, tmp_path):
        self._fresh(monkeypatch, tmp_path, "src.db")
        with get_db() as db:
            _seed(db)
        with get_db() as db:
            payload = import_export.build_export(db, 1)

        # Target already has a feed + account occupying ids 1, so the imported
        # rows must land on new ids and the echoes must follow them.
        self._fresh(monkeypatch, tmp_path, "dst.db")
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url) VALUES (?, ?)",
                ("Decoy", "https://decoy.example/rss"),
            )
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token)"
                " VALUES (?, ?, ?, ?)",
                ("Decoy", "decoy", "https://decoy.example", "x"),
            )
            summary = import_export.import_data(db, 1, payload)

        assert summary["added_feeds"] == 1
        assert summary["added_accounts"] == 2
        assert summary["added_echoes"] == 2

        with get_db() as db:
            echo = db.execute(
                "SELECT * FROM echoes WHERE destination_type = 'mastodon'"
            ).fetchone()
            feed = db.execute("SELECT id FROM feeds WHERE url = ?", ("https://example.com/feed.xml",)).fetchone()
            account = db.execute("SELECT id FROM accounts WHERE username = ?", ("user",)).fetchone()
            assert echo["feed_id"] == feed["id"]
            assert echo["destination_id"] == account["id"]
            assert feed["id"] != 1
            assert account["id"] != 1

    def test_import_is_idempotent(self, monkeypatch, tmp_path):
        self._fresh(monkeypatch, tmp_path, "db.db")
        with get_db() as db:
            _seed(db)
        with get_db() as db:
            payload = import_export.build_export(db, 1)

        # Re-import the same document: everything is already present.
        with get_db() as db:
            summary = import_export.import_data(db, 1, payload)
        assert summary["added_feeds"] == 0
        assert summary["added_accounts"] == 0
        assert summary["added_echoes"] == 0
        assert summary["existing_feeds"] == 1
        assert summary["existing_accounts"] == 2
        assert summary["existing_echoes"] == 2
        with get_db() as db:
            assert db.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"] == 1
            assert db.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 1
            assert db.execute("SELECT COUNT(*) AS c FROM echoes").fetchone()["c"] == 2

    def test_mastodon_empty_username_normalizes_and_dedups(self, monkeypatch, tmp_path):
        self._fresh(monkeypatch, tmp_path, "db.db")
        payload = _payload(accounts={
            "mastodon": [{
                "id": 1, "name": "Foo", "username": "",
                "instance": "https://m.example", "access_token": "t",
            }],
        })
        with get_db() as db:
            first = import_export.import_data(db, 1, payload)
            assert first["added_accounts"] == 1
        with get_db() as db:
            row = db.execute("SELECT username FROM accounts").fetchone()
            assert row["username"] == "Foo"  # fell back to name
        with get_db() as db:
            second = import_export.import_data(db, 1, payload)
            assert second["added_accounts"] == 0  # dedups by the normalized key
            assert second["existing_accounts"] == 1


class TestQuota:
    def _setup_multi(self, monkeypatch, tmp_path, plan_limits):
        db_path = tmp_path / "multi.db"
        monkeypatch.setattr("database.DB_PATH", db_path)
        import settings as settings_mod

        monkeypatch.setattr(settings_mod, "MULTI", True)
        monkeypatch.setattr(settings_mod, "PLAN_LIMITS", plan_limits)
        init_db()
        with get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, plan) VALUES (?, ?, ?)",
                (7, "tenant@example.com", "trial"),
            )
        return db_path

    def test_feed_quota_rejects_overrun(self, monkeypatch, tmp_path):
        self._setup_multi(monkeypatch, tmp_path, {
            "trial": {"max_feeds": 1, "max_destinations": 0, "min_poll_interval": 15, "max_posts_per_hour": 60},
        })
        payload = _payload(feeds=[
            {"id": 1, "name": "A", "url": "https://a.example/rss"},
            {"id": 2, "name": "B", "url": "https://b.example/rss"},
        ])
        with get_db() as db:
            with pytest.raises(import_export.ExportError):
                import_export.import_data(db, 7, payload)

    def test_destination_quota_rejects_overrun(self, monkeypatch, tmp_path):
        self._setup_multi(monkeypatch, tmp_path, {
            "trial": {"max_feeds": 0, "max_destinations": 1, "min_poll_interval": 15, "max_posts_per_hour": 60},
        })
        payload = _payload(accounts={
            "email": [
                {"id": 1, "name": "E1", "email": "one@example.com"},
                {"id": 2, "name": "E2", "email": "two@example.com"},
            ]
        })
        with get_db() as db:
            with pytest.raises(import_export.ExportError):
                import_export.import_data(db, 7, payload)

    def test_quota_counts_only_new_rows(self, monkeypatch, tmp_path):
        self._setup_multi(monkeypatch, tmp_path, {
            "trial": {"max_feeds": 1, "max_destinations": 0, "min_poll_interval": 15, "max_posts_per_hour": 60},
        })
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("Existing", "https://a.example/rss", 7),
            )
        # Same url as the existing feed → dedup reuses it, no overrun.
        payload = _payload(feeds=[{"id": 1, "name": "A", "url": "https://a.example/rss"}])
        with get_db() as db:
            summary = import_export.import_data(db, 7, payload)
        assert summary["added_feeds"] == 0
        assert summary["existing_feeds"] == 1


@pytest.mark.pg
@requires_pg
class TestImportExportPostgres:
    """Run the export/import round-trip against real Postgres.

    The SQL is dual-dialect (``?`` placeholders, dict rows, natural-key
    re-selects); the sqlite suite alone would not catch a PG-only bug.
    """

    def _setup(self, monkeypatch):
        import settings as settings_mod

        monkeypatch.setattr(settings_mod, "MULTI", True)
        monkeypatch.setattr(settings_mod, "DATABASE_URL", TEST_PG_URL)
        monkeypatch.setattr(settings_mod, "ALLOW_SQLITE_FALLBACK", False)

        with database.get_db() as db:
            db.execute("DROP SCHEMA public CASCADE")
            db.execute("CREATE SCHEMA public")
            db.execute("GRANT ALL ON SCHEMA public TO public")
        database.init_db()

    def _uid(self, email):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (email, plan) VALUES (?, ?)",
                (email, "trial"),
            )
            return db.execute(
                "SELECT id FROM users WHERE email = ?", (email,)
            ).fetchone()["id"]

    def test_round_trip_and_dedup_on_postgres(self, monkeypatch):
        self._setup(monkeypatch)
        uid = self._uid("pg@example.com")
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("PG Feed", "https://pg.example/rss", uid),
            )
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token, user_id)"
                " VALUES (?, ?, ?, ?, ?)",
                ("Mastodon", "pguser", "https://mastodon.example", "tok", uid),
            )
            feed_id = db.execute(
                "SELECT id FROM feeds WHERE url = ?", ("https://pg.example/rss",)
            ).fetchone()["id"]
            account_id = db.execute(
                "SELECT id FROM accounts WHERE username = ?", ("pguser",)
            ).fetchone()["id"]
            db.execute(
                "INSERT INTO echoes (feed_id, destination_type, destination_id, template, user_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (feed_id, "mastodon", account_id, "{{ title }}", uid),
            )

        with database.get_db() as db:
            payload = import_export.build_export(db, uid)

        # Round-trip: re-import the same document → every record dedups.
        with database.get_db() as db:
            summary = import_export.import_data(db, uid, payload)
        assert summary["added_feeds"] == 0
        assert summary["added_accounts"] == 0
        assert summary["existing_feeds"] == 1
        assert summary["existing_accounts"] == 1

    def test_insert_new_rows_on_postgres(self, monkeypatch):
        """Exercise the INSERT + natural-key re-select path on PG (the dedup
        path above never runs it)."""
        self._setup(monkeypatch)
        uid = self._uid("pg-insert@example.com")
        payload = {
            "format": "feedecho-export",
            "version": 1,
            "feeds": [{"id": 1, "name": "F", "url": "https://new.example/rss"}],
            "accounts": {
                "mastodon": [{
                    "id": 9, "name": "M", "username": "newuser",
                    "instance": "https://mastodon.example", "access_token": "tok",
                }],
            },
            "echoes": [{
                "id": 1, "feed_id": 1, "destination_type": "mastodon",
                "destination_id": 9, "template": "{{ title }}",
            }],
        }
        with database.get_db() as db:
            summary = import_export.import_data(db, uid, payload)
        assert summary["added_feeds"] == 1
        assert summary["added_accounts"] == 1
        assert summary["added_echoes"] == 1
        with database.get_db() as db:
            feed = db.execute(
                "SELECT id FROM feeds WHERE url = ?", ("https://new.example/rss",)
            ).fetchone()
            account = db.execute(
                "SELECT id FROM accounts WHERE username = ?", ("newuser",)
            ).fetchone()
            echo = db.execute("SELECT * FROM echoes WHERE user_id = ?", (uid,)).fetchone()
            assert echo["feed_id"] == feed["id"]
            assert echo["destination_id"] == account["id"]



class TestReaderImportExport:
    def test_export_includes_read_enabled_not_feed_items(self, temp_db):
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)",
                ("F", "https://e.com/f"),
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title) VALUES (1, 'a', 'SECRET_ITEM_CONTENT')"
            )
            doc = import_export.build_export(db, 1)
        assert doc["feeds"][0]["read_enabled"] == 1
        assert "feed_items" not in doc
        # Derived reader content must never leak into the export document.
        assert "SECRET_ITEM_CONTENT" not in json.dumps(doc)

    def test_import_roundtrips_read_enabled(self, temp_db):
        payload = {
            "format": "feedecho-export",
            "version": 1,
            "feeds": [{"id": 1, "name": "F", "url": "https://e.com/f", "read_enabled": 1}],
            "accounts": {section: [] for section in import_export.ACCOUNT_TYPES},
            "echoes": [],
        }
        with get_db() as db:
            import_export.import_data(db, 1, payload)
            row = db.execute(
                "SELECT read_enabled FROM feeds WHERE url = ?", ("https://e.com/f",)
            ).fetchone()
        assert row["read_enabled"] == 1

    def test_import_defaults_read_enabled_off_when_absent(self, temp_db):
        payload = {
            "format": "feedecho-export",
            "version": 1,
            "feeds": [{"id": 1, "name": "F", "url": "https://e.com/f"}],
            "accounts": {section: [] for section in import_export.ACCOUNT_TYPES},
            "echoes": [],
        }
        with get_db() as db:
            import_export.import_data(db, 1, payload)
            row = db.execute(
                "SELECT read_enabled FROM feeds WHERE url = ?", ("https://e.com/f",)
            ).fetchone()
        assert row["read_enabled"] == 0

