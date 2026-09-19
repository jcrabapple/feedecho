"""Tests for the email destination's render_html mode (Phase 2).

An email echo with render_html set sends the rendered template output AS
the HTML alternative (the template embeds ingest-sanitized {{ content_html }}).
Default behavior (escape, plain-only) is unchanged. Covers the sanitizer
allowlist extension, URL absolutization, the route clamping (email + instant
only), the dispatch wiring, and the MIME structure.
"""

import json
from email import parser as email_parser
from email import policy
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import database
import feed_parser
import scheduler
import template_engine
import webhook


@pytest.fixture
def client(db_tmp, monkeypatch):
    """Single-mode TestClient (no auth gate): mirrors test_import_export."""
    import app as app_module

    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(app_module.settings, "MULTI", False)
    return TestClient(app_module.app)


# ── Sanitizer: email-grade allowlist + URL absolutization ───────────────────

class TestSanitizeHtmlEmailGrade:
    def test_headings_and_tables_survive(self):
        dirty = "<h2>Title</h2><table><tr><td>cell</td></tr></table><hr>"
        out = feed_parser.sanitize_html(dirty)
        assert "<h2>Title</h2>" in out
        assert "<td>cell</td>" in out
        assert "<hr" in out

    def test_form_inside_table_still_stripped(self):
        out = feed_parser.sanitize_html("<table><tr><form action='x'></form></tr></table>")
        assert "form" not in out

    def test_relative_urls_absoluteized(self):
        out = feed_parser.prepare_content_html(
            '<p><a href="/post/1">x</a><img src="/img.png"></p>',
            "https://blog.example/posts/",
        )
        assert 'href="https://blog.example/post/1"' in out
        assert 'src="https://blog.example/img.png"' in out

    def test_absolute_urls_untouched(self):
        out = feed_parser.prepare_content_html(
            '<a href="https://other.example/x">x</a>', "https://blog.example/"
        )
        assert 'href="https://other.example/x"' in out

    def test_fragment_mailto_and_data_untouched_or_stripped(self):
        out = feed_parser.prepare_content_html(
            '<a href="#section">s</a><a href="mailto:a@b.c">m</a>', "https://e.com/x"
        )
        assert 'href="#section"' in out
        # mailto is in the scheme allowlist (newsletter author links).
        assert 'href="mailto:a@b.c"' in out

    def test_tfoot_and_caption_survive(self):
        out = feed_parser.sanitize_html(
            "<table><caption>c</caption><tfoot><tr><td>f</td></tr></tfoot></table>"
        )
        assert "<caption>c</caption>" in out
        assert "tfoot" in out

    def test_code_block_prose_not_absoluteized(self):
        """`href="..."` inside code/text must NOT be rewritten — the regex is
        anchored to <a>/<img> opening tags only."""
        out = feed_parser.prepare_content_html(
            '<pre><code>&lt;img src="/x.png"&gt;</code></pre>'
            '<p>Use href="/api/v1" in requests</p>',
            "https://e.com/",
        )
        assert 'src="https://e.com/x.png"' not in out
        assert 'href="https://e.com/api/v1"' not in out

    def test_hostile_base_scheme_disables_absolutization(self):
        """urljoin against a javascript: base could smuggle schemes past the
        allowlist; a non-http base disables rewriting entirely."""
        out = feed_parser.prepare_content_html(
            '<a href="/p">x</a>', "javascript:alert(1)"
        )
        assert 'href="/p"' in out
        assert "javascript" not in out

    def test_no_base_leaves_relative_urls(self):
        out = feed_parser.prepare_content_html('<a href="/p">x</a>', "")
        assert 'href="/p"' in out


# ── Template context unchanged (content_html already exposed in Phase 1) ────

def test_template_renders_content_html_for_email_template():
    item = {
        "id": "1", "title": "T", "link": "https://e.com/1", "summary": "",
        "content": "Hi", "content_html": '<p>Hi <a href="https://e.com/x">x</a></p>',
        "content_link": "", "author": "", "date": "", "tags": [], "image_url": "",
    }
    out = template_engine.render_template("{{ content_html }}", item, feed_name="f")
    assert '<a href="https://e.com/x">x</a>' in out


# ── MIME structure: render_html sends raw HTML + derived plain part ─────────

def _parse_message(raw: str):
    return email_parser.Parser(policy=policy.default).parsestr(raw)


class TestSendViaRenderHtml:
    def _smtp_cfg(self):
        return {
            "host": "smtp.example.com", "port": 587, "username": "u",
            "password": "p", "from_email": "from@example.com",
            "from_name": "FeedEcho", "use_tls": True,
        }

    def test_render_html_body_is_the_html_part(self):
        from email_sender import _send_via

        sent = []
        with mock.patch("email_sender.smtplib.SMTP") as smtp_cls:
            smtp = smtp_cls.return_value.__enter__.return_value
            smtp.sendmail.side_effect = (
                lambda from_addr, to_addrs, raw: sent.append(raw)
            )
            html = '<p>Hi <a href="https://e.com/x">there</a></p>'
            _send_via(self._smtp_cfg(), "to@example.com", "Subject", html, render_html=True)

        msg = _parse_message(sent[0])
        parts = msg.get_payload()
        plain = next(p for p in parts if p.get_content_type() == "text/plain")
        html_part = next(p for p in parts if p.get_content_type() == "text/html")
        # Plain part is the structure-preserving text conversion.
        assert "Hi there" in plain.get_content()
        assert "<" not in plain.get_content()
        # HTML part is the body (re-sanitized at the boundary; nh3 annotates
        # links with rel="noopener noreferrer").
        assert 'href="https://e.com/x"' in html_part.get_content()
        assert "there</a>" in html_part.get_content()

    def test_default_mode_still_escapes(self):
        from email_sender import _send_via

        sent = []
        with mock.patch("email_sender.smtplib.SMTP") as smtp_cls:
            smtp = smtp_cls.return_value.__enter__.return_value
            smtp.sendmail.side_effect = (
                lambda from_addr, to_addrs, raw: sent.append(raw)
            )
            _send_via(
                self._smtp_cfg(), "to@example.com", "Subject",
                '<p>not rendered</p>', render_html=False,
            )

        msg = _parse_message(sent[0])
        types = [p.get_content_type() for p in msg.get_payload()]
        # No HTML part exists in the default path.
        assert "text/html" not in types
        plain = next(p for p in msg.get_payload() if p.get_content_type() == "text/plain")
        assert "<p>not rendered</p>" in plain.get_content()

    def test_send_boundary_sanitizes_hostile_interpolations(self):
        """Templates mix raw-text variables ({{ title }}, {{ author }},
        entity-unescaped by clean_text) into the rendered body; the send
        boundary must sanitize the FINAL markup, not trust the template."""
        from email_sender import _send_via

        sent = []
        with mock.patch("email_sender.smtplib.SMTP") as smtp_cls:
            smtp = smtp_cls.return_value.__enter__.return_value
            smtp.sendmail.side_effect = (
                lambda from_addr, to_addrs, raw: sent.append(raw)
            )
            hostile = (
                '<p>{{ content_html }}</p>'.replace("{{ content_html }}", "<p>ok</p>")
                + '<img src=x onerror=alert(1)>'
                + '<form action="/phish"><input name="pw"></form>'
            )
            _send_via(
                self._smtp_cfg(), "to@example.com", "Subject", hostile, render_html=True
            )

        msg = _parse_message(sent[0])
        html_part = next(
            p for p in msg.get_payload() if p.get_content_type() == "text/html"
        )
        body = html_part.get_content()
        assert "onerror" not in body
        assert "<form" not in body
        assert "<p>ok</p>" in body

    def test_render_html_with_images_full_mime(self):
        """render_html + attach_image: multipart/related with the raw body as
        the HTML alternative and the images as inline cid parts."""
        from email_sender import _send_via

        sent = []
        with mock.patch("email_sender.smtplib.SMTP") as smtp_cls:
            smtp = smtp_cls.return_value.__enter__.return_value
            smtp.sendmail.side_effect = (
                lambda from_addr, to_addrs, raw: sent.append(raw)
            )
            _send_via(
                self._smtp_cfg(), "to@example.com", "Subject",
                "<p>article</p>", images=[{"data": b"fake", "content_type": "image/png", "alt": "p"}],
                render_html=True,
            )

        msg = _parse_message(sent[0])
        assert msg.get_content_type() == "multipart/related"
        html_part = next(
            p for p in msg.walk() if p.get_content_type() == "text/html"
        )
        assert "<p>article</p>" in html_part.get_content()
        image_parts = [p for p in msg.walk() if p.get_content_type().startswith("image/")]
        assert len(image_parts) == 1
        assert image_parts[0]["Content-ID"] == "<image0@feedecho>"

    def test_render_html_with_images_keeps_cid_references(self):
        from email_sender import _render_html_body

        html = '<p>article</p>'
        out = _render_html_body(html, [{"alt": "pic"}], escape_body=False)
        assert html in out
        assert 'src="cid:image0@feedecho"' in out


# ── Dispatch wiring + route clamping ────────────────────────────────────────

def _setup_email_echo(db_tmp, echo_overrides=None):
    echo_kwargs = {
        "destination_type": "email",
        "destination_id": 1,
        "template": "{{ content_html }}",
        "visibility": "public",
        "filter_keywords": "",
        "filter_mode": "exclude",
        "content_warning": "",
        "attach_image": 0,
        "render_html": 0,
        "delivery_mode": "instant",
        "enabled": 1,
    }
    if echo_overrides:
        echo_kwargs.update(echo_overrides)

    with db_tmp.get_db() as db:
        db.execute("INSERT INTO email_accounts (name, email) VALUES ('Test', 'test@example.com')")
        db.execute("INSERT INTO feeds (name, url) VALUES ('f', 'https://example.com/feed')")
        db.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                   visibility, filter_keywords, filter_mode,
                                   content_warning, attach_image, render_html,
                                   delivery_mode, enabled)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                echo_kwargs["destination_type"], echo_kwargs["destination_id"],
                echo_kwargs["template"], echo_kwargs["visibility"],
                echo_kwargs["filter_keywords"], echo_kwargs["filter_mode"],
                echo_kwargs["content_warning"], echo_kwargs["attach_image"],
                echo_kwargs["render_html"], echo_kwargs["delivery_mode"],
                echo_kwargs["enabled"],
            ),
        )
    with db_tmp.get_db() as db:
        return db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()


class TestDispatchWiring:
    def test_render_html_flag_reaches_send_email(self, db_tmp, monkeypatch):
        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )
        echo = _setup_email_echo(db_tmp, {"render_html": 1})
        item = {
            "id": "i1", "title": "T", "link": "https://e.com/1", "summary": "",
            "content": "Hi", "content_html": "<p>Hi</p>", "date": "",
            "image_url": "", "tags": [],
        }
        assert scheduler.process_echo(echo, item) is True
        assert len(sent) == 1
        assert sent[0]["render_html"] is True
        assert sent[0]["body"] == "<p>Hi</p>"

    def test_flag_off_omits_render_html(self, db_tmp, monkeypatch):
        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )
        echo = _setup_email_echo(db_tmp, {"render_html": 0})
        item = {"id": "i1", "title": "T", "link": "https://e.com/1", "summary": "",
                "content": "Hi", "content_html": "<p>Hi</p>", "date": "",
                "image_url": "", "tags": []}
        assert scheduler.process_echo(echo, item) is True
        assert sent[0]["render_html"] is False


class TestRouteClamping:
    def test_add_echo_clamps_render_html_to_email_instant(self, client):
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES ('f', 'https://e.com/feed')")
            db.execute("INSERT INTO email_accounts (name, email) VALUES ('e', 't@example.com')")
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token)"
                " VALUES ('m', 'u', 'https://mastodon.example', 'tok')"
            )
        # mastodon destination: flag clamps off
        r = client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "mastodon", "account_id": "1",
            "template": "t", "render_html": "true",
        }, follow_redirects=False)
        assert r.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert row["render_html"] == 0

    def test_add_echo_keeps_flag_for_email_instant(self, client):
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES ('f', 'https://e.com/feed')")
            db.execute("INSERT INTO email_accounts (name, email) VALUES ('e', 't@example.com')")
        client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "email", "email_account_id": "1",
            "template": "t", "render_html": "true", "delivery_mode": "instant",
        })
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert row["render_html"] == 1

    def test_add_echo_clamps_flag_for_digest(self, client):
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES ('f', 'https://e.com/feed')")
            db.execute("INSERT INTO email_accounts (name, email) VALUES ('e', 't@example.com')")
        client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "email", "email_account_id": "1",
            "template": "t", "render_html": "true", "delivery_mode": "digest",
        })
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert row["render_html"] == 0

    def test_edit_echo_sets_and_clears_flag(self, client):
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES ('f', 'https://e.com/feed')")
            db.execute("INSERT INTO email_accounts (name, email) VALUES ('e', 't@example.com')")
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token)"
                " VALUES ('m', 'u', 'https://mastodon.example', 'tok')"
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                       visibility, filter_keywords, filter_mode,
                                       content_warning, attach_image, render_html,
                                       delivery_mode, enabled)
                   VALUES (1, 'email', 1, 't', 'public', '', 'exclude', '', 0, 0,
                           'instant', 1)"""
            )
        base = {
            "feed_id": "1", "destination_type": "email", "email_account_id": "1",
            "template": "t", "delivery_mode": "instant",
        }
        client.post("/api/echoes/1/edit", data={**base, "render_html": "true"})
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert row["render_html"] == 1
        # Turning the checkbox off clears it.
        client.post("/api/echoes/1/edit", data={**base})
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert row["render_html"] == 0
        # Switching the destination to mastodon clamps the flag off.
        client.post("/api/echoes/1/edit", data={
            **base, "render_html": "true", "destination_type": "mastodon",
            "account_id": "1",
        })
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert row["destination_type"] == "mastodon"
        assert row["render_html"] == 0
