"""Tests for Mastodon echo use_markdown → content_type=text/markdown."""

import pytest
from fastapi.testclient import TestClient

import database


@pytest.fixture
def client(db_tmp, monkeypatch):
    """Single-mode TestClient (no auth gate)."""
    import app as app_module

    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(app_module.settings, "MULTI", False)
    return TestClient(app_module.app)


class TestUseMarkdownClamping:
    def test_add_echo_keeps_flag_for_mastodon(self, client):
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES ('f', 'https://e.com/feed')")
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token)"
                " VALUES ('m', 'u', 'https://example.com', 'tok')"
            )
        r = client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "mastodon", "account_id": "1",
            "template": "**{{ title }}**", "use_markdown": "true",
        }, follow_redirects=False)
        assert r.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert row["use_markdown"] == 1

    def test_add_echo_clamps_flag_for_email(self, client):
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES ('f', 'https://e.com/feed')")
            db.execute("INSERT INTO email_accounts (name, email) VALUES ('e', 't@example.com')")
        client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "email", "email_account_id": "1",
            "template": "t", "use_markdown": "true", "delivery_mode": "instant",
        })
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert row["use_markdown"] == 0

    def test_edit_echo_sets_and_clears_flag(self, client):
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES ('f', 'https://e.com/feed')")
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token)"
                " VALUES ('m', 'u', 'https://example.com', 'tok')"
            )
            db.execute("INSERT INTO email_accounts (name, email) VALUES ('e', 't@example.com')")
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                       visibility, filter_keywords, filter_mode,
                                       content_warning, attach_image, render_html,
                                       use_markdown, delivery_mode, enabled)
                   VALUES (1, 'mastodon', 1, 't', 'public', '', 'exclude', '', 0, 0,
                           0, 'instant', 1)"""
            )
        base = {
            "feed_id": "1", "destination_type": "mastodon", "account_id": "1",
            "template": "t",
        }
        client.post("/api/echoes/1/edit", data={**base, "use_markdown": "true"})
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert row["use_markdown"] == 1
        client.post("/api/echoes/1/edit", data={**base})
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert row["use_markdown"] == 0
        # Switching destination away from mastodon clamps the flag off.
        client.post("/api/echoes/1/edit", data={
            "feed_id": "1", "destination_type": "email", "email_account_id": "1",
            "template": "t", "use_markdown": "true", "delivery_mode": "instant",
        })
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert row["destination_type"] == "email"
        assert row["use_markdown"] == 0


class TestSendPassesContentType:
    def test_send_mastodon_passes_markdown_content_type(
        self, db_tmp, monkeypatch, setup_echo
    ):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        echo = setup_echo({"use_markdown": 1}, attach_image=0)
        assert scheduler.process_echo(echo, {
            "id": "item-1",
            "title": "Test Post",
            "link": "https://example.com/post/1",
            "summary": "",
        }) is True
        assert len(sent) == 1
        assert sent[0]["content_type"] == "text/markdown"

    def test_send_mastodon_omits_content_type_when_flag_off(
        self, db_tmp, monkeypatch, setup_echo
    ):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        echo = setup_echo({"use_markdown": 0}, attach_image=0)
        assert scheduler.process_echo(echo, {
            "id": "item-1",
            "title": "Test Post",
            "link": "https://example.com/post/1",
            "summary": "",
        }) is True
        assert len(sent) == 1
        assert sent[0].get("content_type") is None


class TestUseMarkdownImportExport:
    """PR #47 build-out: the flag round-trips through import/export and the
    import clamp mirrors the form clamp (mastodon-only)."""

    def _payload(self, use_markdown_value, dest_type="mastodon", dest_id=1):
        return {
            "format": "feedecho-export",
            "version": 1,
            "feeds": [{"id": 1, "name": "F", "url": "https://a.example/rss"}],
            "accounts": {
                "mastodon": [{"id": 1, "name": "m", "username": "u", "instance": "https://example.com", "access_token": "tok"}],
                "email": [{"id": 1, "name": "e", "email": "t@example.com"}],
            },
            "echoes": [{
                "feed_id": 1, "destination_type": dest_type, "destination_id": dest_id,
                "template": "t", "visibility": "public", "enabled": 1,
                "attach_image": 0, "use_markdown": use_markdown_value,
                "delivery_mode": "instant",
            }],
        }

    def test_use_markdown_survives_import(self, db_tmp):
        import import_export

        with database.get_db() as db:
            summary = import_export.import_data(db, 1, self._payload(1))
            assert summary["added_echoes"] == 1
            row = db.execute("SELECT use_markdown FROM echoes").fetchone()
        assert row["use_markdown"] == 1

    def test_use_markdown_clamped_on_non_mastodon_import(self, db_tmp):
        import import_export

        with database.get_db() as db:
            summary = import_export.import_data(
                db, 1, self._payload(1, dest_type="email")
            )
            assert summary["added_echoes"] == 1
            row = db.execute("SELECT use_markdown FROM echoes").fetchone()
        assert row["use_markdown"] == 0

    def test_export_includes_use_markdown(self, db_tmp):
        import import_export

        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES ('f', 'https://e.com/feed')")
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token)"
                " VALUES ('m', 'u', 'https://example.com', 'tok')"
            )
            db.execute(
                "INSERT INTO echoes (feed_id, destination_type, destination_id, template,"
                " visibility, use_markdown, enabled)"
                " VALUES (1, 'mastodon', 1, 't', 'public', 1, 1)"
            )
            payload = import_export.build_export(db, 1)
        echo = payload["echoes"][0]
        assert echo["use_markdown"] == 1
