"""Tests for template preview endpoint and echo form template validation."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from database import get_db, init_db
from feed_parser import SSRFError


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

    monkeypatch.setattr(app_module, "_AUTH_TOKEN", None)
    return TestClient(app_module.app)


def _seed_feed(name="Example Feed", url="https://example.com/feed.xml"):
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)", (name, url)
        )
        return cursor.lastrowid


def _seed_account():
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO accounts (name, username, instance, access_token)"
            " VALUES ('Test', 'test', 'https://example.com', 'tok')"
        )
        return cursor.lastrowid


def _seed_echo(feed_id, template="{{ title }} {{ link }}", destination_type="mastodon"):
    account_id = _seed_account()
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO echoes (feed_id, destination_type, destination_id, template)"
            " VALUES (?, ?, ?, ?)",
            (feed_id, destination_type, account_id, template),
        )
        return cursor.lastrowid


def _fake_feed_data(n=5):
    return {
        "title": "Example",
        "type": "rss",
        "items": [
            {
                "id": f"item-{i}",
                "title": f"Post {i}",
                "link": f"https://example.com/{i}",
                "summary": f"Summary {i}",
                "author": "",
                "date": "2024-01-15T09:30:00Z",
                "tags": [],
                "image_url": "",
            }
            for i in range(n)
        ],
    }


class TestPreviewEndpoint:
    def test_preview_renders_latest_items(self, client, monkeypatch):
        import app as app_module

        feed_id = _seed_feed()
        monkeypatch.setattr(app_module, "fetch_feed", lambda url: _fake_feed_data(2))

        resp = client.post(
            "/api/preview",
            data={"template": "{{ title }} {{ link }}", "feed_id": str(feed_id)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert len(body["items"]) == 2
        assert body["items"][0]["title"] == "Post 0"
        assert body["items"][0]["rendered"] == "Post 0 https://example.com/0"

    def test_preview_caps_at_three_items(self, client, monkeypatch):
        import app as app_module

        feed_id = _seed_feed()
        monkeypatch.setattr(app_module, "fetch_feed", lambda url: _fake_feed_data(5))

        resp = client.post(
            "/api/preview",
            data={"template": "{{ title }}", "feed_id": str(feed_id)},
        )
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 3

    def test_preview_includes_feed_name(self, client, monkeypatch):
        import app as app_module

        feed_id = _seed_feed(name="Named Feed")
        monkeypatch.setattr(app_module, "fetch_feed", lambda url: _fake_feed_data(1))

        resp = client.post(
            "/api/preview",
            data={"template": "{{ feed_name }}: {{ title }}", "feed_id": str(feed_id)},
        )
        assert resp.json()["items"][0]["rendered"] == "Named Feed: Post 0"

    def test_preview_bad_template_returns_400(self, client):
        feed_id = _seed_feed()
        resp = client.post(
            "/api/preview",
            data={"template": "{% if summary %}broken", "feed_id": str(feed_id)},
        )
        assert resp.status_code == 400
        assert "Template syntax error" in resp.json()["error"]

    def test_preview_sandbox_violation_returns_400(self, client, monkeypatch):
        import app as app_module

        feed_id = _seed_feed()
        monkeypatch.setattr(app_module, "fetch_feed", lambda url: _fake_feed_data(1))

        resp = client.post(
            "/api/preview",
            data={
                "template": "{{ item.__class__.__mro__ }}",
                "feed_id": str(feed_id),
            },
        )
        assert resp.status_code == 400
        assert "Render error" in resp.json()["error"]

    def test_preview_unknown_feed_404(self, client):
        resp = client.post(
            "/api/preview",
            data={"template": "{{ title }}", "feed_id": "9999"},
        )
        assert resp.status_code == 404

    def test_preview_feed_fetch_failure(self, client, monkeypatch):
        import app as app_module

        feed_id = _seed_feed()

        def boom(url):
            raise SSRFError("blocked")

        monkeypatch.setattr(app_module, "fetch_feed", boom)

        resp = client.post(
            "/api/preview",
            data={"template": "{{ title }}", "feed_id": str(feed_id)},
        )
        assert resp.status_code == 400

    def test_preview_empty_feed(self, client, monkeypatch):
        import app as app_module

        feed_id = _seed_feed()
        monkeypatch.setattr(app_module, "fetch_feed", lambda url: {"items": []})

        resp = client.post(
            "/api/preview",
            data={"template": "{{ title }}", "feed_id": str(feed_id)},
        )
        assert resp.status_code == 200
        assert resp.json()["items"] == []


class TestEchoFormValidation:
    def _mastodon_form(self, feed_id, account_id, template):
        return {
            "feed_id": str(feed_id),
            "destination_type": "mastodon",
            "account_id": str(account_id),
            "template": template,
        }

    def test_add_echo_rejects_bad_template(self, client):
        feed_id = _seed_feed()
        account_id = _seed_account()
        resp = client.post(
            "/api/echoes",
            data=self._mastodon_form(feed_id, account_id, "{% if summary %}broken"),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "Template error" in resp.json()["detail"]

    def test_add_echo_accepts_valid_template(self, client):
        feed_id = _seed_feed()
        account_id = _seed_account()
        resp = client.post(
            "/api/echoes",
            data=self._mastodon_form(feed_id, account_id, "{{ title }} - {{ link }}"),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with get_db() as db:
            row = db.execute(
                "SELECT template FROM echoes WHERE feed_id = ?", (feed_id,)
            ).fetchone()
        assert row["template"] == "{{ title }} - {{ link }}"

    def test_add_echo_bad_template_inserts_nothing(self, client):
        feed_id = _seed_feed()
        account_id = _seed_account()
        client.post(
            "/api/echoes",
            data=self._mastodon_form(feed_id, account_id, "{% if summary %}broken"),
            follow_redirects=False,
        )
        with get_db() as db:
            rows = db.execute(
                "SELECT COUNT(*) AS n FROM echoes WHERE feed_id = ?", (feed_id,)
            ).fetchone()
        assert rows["n"] == 0

    def test_edit_echo_rejects_bad_template(self, client):
        feed_id = _seed_feed()
        echo_id = _seed_echo(feed_id)
        account_id = _seed_account()
        form = self._mastodon_form(feed_id, account_id, "{% if summary %}broken")
        form["echo_id"] = str(echo_id)
        resp = client.post(
            f"/api/echoes/{echo_id}/edit",
            data=form,
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "Template error" in resp.json()["detail"]


class TestStartupRevalidation:
    def test_logs_warning_for_broken_stored_template(self, client, caplog):
        import logging

        import app as app_module

        feed_id = _seed_feed()
        with get_db() as db:
            db.execute(
                "INSERT INTO echoes (feed_id, destination_type, destination_id, template)"
                " VALUES (?, 'mastodon', ?, ?)",
                (feed_id, _seed_account(), "{% if summary %}broken"),
            )

        with caplog.at_level(logging.WARNING):
            app_module._revalidate_stored_templates()

        assert any(
            "no longer parses" in record.message for record in caplog.records
        )

    def test_no_warning_for_valid_stored_template(self, client, caplog):
        import logging

        import app as app_module

        _seed_echo(_seed_feed())

        with caplog.at_level(logging.WARNING):
            app_module._revalidate_stored_templates()

        assert not any(
            "no longer parses" in record.message for record in caplog.records
        )
