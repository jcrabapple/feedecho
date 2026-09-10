"""Tests for inline image embedding on the email destination.

attach_image was honored by Mastodon/Bluesky/Matrix/etc. but silently
ignored for email destinations, which were plain-text only. These tests
pin the new behavior: instant-mode email echoes embed fetched images as
inline MIME parts in an HTML alternative.
"""

import os
import tempfile
from email import parser as email_parser
from email import policy

import pytest

def _item(**overrides):
    item = {
        "id": "item-1",
        "title": "Test Post",
        "link": "https://example.com/post/1",
        "summary": "A summary.",
        "image_url": "",
    }
    item.update(overrides)
    return item

def _setup_email_echo(db_tmp, echo_overrides=None):
    """Create a test email account, feed, and instant email echo."""
    echo_kwargs = {
        "destination_type": "email",
        "destination_id": 1,
        "template": "{{ title }} — {{ link }}",
        "visibility": "public",
        "filter_keywords": "",
        "filter_mode": "exclude",
        "content_warning": "",
        "attach_image": 0,
        "delivery_mode": "instant",
        "enabled": 1,
    }
    if echo_overrides:
        echo_kwargs.update(echo_overrides)

    with db_tmp.get_db() as db:
        db.execute(
            "INSERT INTO email_accounts (name, email) VALUES (?, ?)",
            ("Test User", "test@example.com"),
        )
        db.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("f", "https://example.com/feed"),
        )
        db.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                   visibility, filter_keywords, filter_mode,
                                   content_warning, attach_image, delivery_mode, enabled)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                echo_kwargs["destination_type"],
                echo_kwargs["destination_id"],
                echo_kwargs["template"],
                echo_kwargs["visibility"],
                echo_kwargs["filter_keywords"],
                echo_kwargs["filter_mode"],
                echo_kwargs["content_warning"],
                echo_kwargs["attach_image"],
                echo_kwargs["delivery_mode"],
                echo_kwargs["enabled"],
            ),
        )
        return db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()

# ── Echo dispatch: image fetching and send_email payload ─────────────────────

class TestEmailEchoImages:
    def test_attach_image_embeds_fetched_image(self, db_tmp, monkeypatch):
        """attach_image=1 fetches the item image and passes it to send_email."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )

        echo = _setup_email_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/photo.jpg", image_alt="A photo")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0]["images"] == [
            {
                "data": b"fake-image-bytes",
                "content_type": "image/jpeg",
                "alt": "A photo",
            }
        ]

    def test_no_images_when_attach_image_disabled(self, db_tmp, monkeypatch):
        """attach_image=0 must not fetch or embed anything."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )
        fetch_calls = []
        monkeypatch.setattr(
            scheduler,
            "fetch_image",
            lambda url: fetch_calls.append(url) or (b"fake", "image/jpeg"),
        )

        echo = _setup_email_echo(db_tmp, {"attach_image": 0})
        item = _item(image_url="https://example.com/photo.jpg")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0]["images"] == []
        assert fetch_calls == []

    def test_attaches_up_to_four_images_with_alt(self, db_tmp, monkeypatch):
        """image_urls entries are embedded in order with their alt text."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-bytes", "image/png")
        )

        echo = _setup_email_echo(db_tmp, {"attach_image": 1})
        item = _item(
            image_urls=[
                {"url": "https://example.com/1.jpg", "alt": "one"},
                {"url": "https://example.com/2.jpg", "alt": "two"},
                {"url": "https://example.com/3.jpg", "alt": ""},
                {"url": "https://example.com/4.jpg", "alt": "four"},
                {"url": "https://example.com/5.jpg", "alt": "five"},
            ]
        )
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        images = sent[0]["images"]
        assert [img["alt"] for img in images] == ["one", "two", "", "four"]
        assert len(images) == 4

    def test_fetch_failure_skips_image_but_sends_email(self, db_tmp, monkeypatch):
        """A failed fetch must not fail the post — email goes out text-only."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )
        monkeypatch.setattr(scheduler, "fetch_image", lambda url: None)

        echo = _setup_email_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/photo.jpg")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0]["images"] == []

    def test_byte_budget_skips_later_images(self, db_tmp, monkeypatch):
        """Images beyond EMAIL_MAX_IMAGE_BYTES are skipped, not attached."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"0123456789", "image/jpeg")
        )
        monkeypatch.setattr(scheduler, "EMAIL_MAX_IMAGE_BYTES", 10)

        echo = _setup_email_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[{"url": "https://example.com/a.jpg", "alt": ""},
                                 {"url": "https://example.com/b.jpg", "alt": ""}])
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert len(sent[0]["images"]) == 1

class TestEmailClaimGuardOrdering:
    """_send_email_echo must re-validate its claim AFTER the slow image-fetch
    loop, immediately before send_email — not before it.

    All other destination senders (_send_mastodon, _send_bluesky, etc.) call
    _guard_claim right before the irreversible network send, specifically
    because the image-fetch/alt-text pipeline is what can let the claim go
    stale (reclaimed by another worker once PENDING_RECLAIM_SECONDS has
    elapsed). _send_email_echo used to check the claim BEFORE the image-fetch
    loop, leaving no re-check for a reclaim that happens during that fetch —
    which could send a duplicate email.
    """

    def test_stale_claim_during_image_fetch_aborts_the_send(self, db_tmp, monkeypatch):
        """A reclaim that happens mid-fetch must still be caught before send."""
        import scheduler

        echo = _setup_email_echo(db_tmp, {"attach_image": 1})

        with db_tmp.get_db() as db:
            db.execute(
                "INSERT INTO posted_items (id, echo_id, item_id, status, claim_token)"
                " VALUES (1, 1, 'item-1', 'pending', 'our-token')"
            )

        def flaky_fetch_image(url):
            # Simulate another worker reclaiming this row while this worker
            # is still doing the slow image fetch: the lease lapses mid-I/O.
            with db_tmp.get_db() as db:
                db.execute(
                    "UPDATE posted_items SET claim_token = 'someone-elses-token'"
                    " WHERE id = 1"
                )
            return (b"fake-image-bytes", "image/jpeg")

        monkeypatch.setattr(scheduler, "fetch_image", flaky_fetch_image)

        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )

        item = _item(image_url="https://example.com/photo.jpg", image_alt="A photo")
        result = scheduler._send_email_echo(echo, item, "content", 1, 1, "our-token")

        # The point of moving the guard: the lost claim is caught right
        # before send, so no email goes out at all. (With the guard checked
        # before the fetch instead, the fetch happens, the guard has
        # already passed, and send_email fires anyway — a duplicate.)
        assert result is False
        assert sent == [], "email was sent despite having lost the claim mid-fetch"

    def test_fresh_claim_survives_image_fetch_and_sends(self, db_tmp, monkeypatch):
        """Sanity check: an uncontested claim still sends normally."""
        import scheduler

        echo = _setup_email_echo(db_tmp, {"attach_image": 1})

        with db_tmp.get_db() as db:
            db.execute(
                "INSERT INTO posted_items (id, echo_id, item_id, status, claim_token)"
                " VALUES (1, 1, 'item-1', 'pending', 'our-token')"
            )

        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )
        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )

        item = _item(image_url="https://example.com/photo.jpg", image_alt="A photo")
        result = scheduler._send_email_echo(echo, item, "content", 1, 1, "our-token")

        assert result is True
        assert len(sent) == 1


# ── MIME construction in email_sender ────────────────────────────────────────

class _FakeSMTP:
    messages = []

    def __init__(self, *args, **kwargs):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append("login")

    def sendmail(self, from_addr, to_addrs, msg):
        _FakeSMTP.messages.append(msg)

@pytest.fixture()
def fake_smtp(monkeypatch):
    import email_sender

    _FakeSMTP.messages = []
    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(email_sender.settings, "MULTI", False)
    return _FakeSMTP

def _cfg():
    return {
        "host": "smtp.example.com",
        "port": 587,
        "username": "",
        "password": "",
        "from_email": "me@example.com",
        "from_name": "FeedEcho",
        "use_tls": False,
    }

def _parse_messages():
    p = email_parser.BytesParser(policy=policy.default)
    return [p.parsebytes(m.encode("utf-8")) for m in _FakeSMTP.messages]

class TestEmailSenderMime:
    def test_images_produce_related_message(self, fake_smtp):
        """With images, the message is multipart/related with inline parts."""
        import email_sender

        email_sender._send_via(
            _cfg(),
            "to@example.com",
            "Subject",
            "Hello",
            images=[
                {"data": b"\x89PNG\r\n\x1a\n", "content_type": "image/png", "alt": "alt1"},
                {"data": b"gif-bytes", "content_type": "image/gif", "alt": ""},
            ],
        )

        msg = _parse_messages()[0]
        assert msg.get_content_type() == "multipart/related"

        parts = list(msg.walk())
        cids = [p.get("Content-ID") for p in parts if p.get("Content-ID")]
        assert cids == ["<image0@feedecho>", "<image1@feedecho>"]

        html_part = next(p for p in parts if p.get_content_type() == "text/html")
        html_text = html_part.get_content()
        assert 'src="cid:image0@feedecho"' in html_text
        assert 'src="cid:image1@feedecho"' in html_text
        assert 'alt="alt1"' in html_text

        plain_part = next(p for p in parts if p.get_content_type() == "text/plain")
        assert plain_part.get_content() == "Hello"

        img_part = next(p for p in parts if p.get_content_type() == "image/png")
        assert img_part.get("Content-Disposition") == "inline"
        assert img_part.get_content() == b"\x89PNG\r\n\x1a\n"

    def test_no_images_keeps_plain_alternative(self, fake_smtp):
        """The no-image message shape must be exactly what it was before."""
        import email_sender

        email_sender._send_via(_cfg(), "to@example.com", "Subject", "Hello")

        msg = _parse_messages()[0]
        assert msg.get_content_type() == "multipart/alternative"
        parts = list(msg.walk())
        html_parts = [p for p in parts if p.get_content_type() == "text/html"]
        assert html_parts == []
        assert len([p for p in parts if p.get_content_type() == "image/jpeg"]) == 0

    def test_html_body_escapes_template_content(self, fake_smtp):
        """Template output is plain text and must never become live HTML."""
        import email_sender

        body = "<script>alert('x')</script> & <b>bold?</b>\nline2"
        email_sender._send_via(
            _cfg(), "to@example.com", "Subject", body,
            images=[{"data": b"x", "content_type": "image/jpeg", "alt": '"><script>'}],
        )

        msg = _parse_messages()[0]
        html_part = next(
            p for p in msg.walk() if p.get_content_type() == "text/html"
        )
        html_text = html_part.get_content()
        assert "<script>alert" not in html_text
        assert "&lt;script&gt;alert" in html_text
        assert "&lt;b&gt;" in html_text
        # Alt text is escaped too, including quotes.
        assert 'alt="&quot;&gt;&lt;script&gt;"' in html_text

    def test_send_email_passes_images_through(self, monkeypatch):
        """send_email forwards the images kwarg to _send_via."""
        import email_sender

        calls = []
        monkeypatch.setattr(
            email_sender,
            "get_smtp_settings",
            lambda user_id: _cfg(),
        )
        monkeypatch.setattr(
            email_sender,
            "_send_via",
            lambda cfg, to, subject, body, images=None: calls.append(
                (cfg, to, subject, body, images)
            ),
        )

        email_sender.send_email(
            "to@example.com",
            "Subject",
            "Body",
            images=[{"data": b"x", "content_type": "image/jpeg", "alt": ""}],
        )

        assert len(calls) == 1
        assert calls[0][4] == [
            {"data": b"x", "content_type": "image/jpeg", "alt": ""}
        ]