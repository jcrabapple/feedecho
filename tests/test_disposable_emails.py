"""Disposable-domain screening used by the register flow."""

import disposable_emails


def test_known_disposable_domains_are_flagged():
    assert disposable_emails.is_disposable_email("bot@mailinator.com")
    assert disposable_emails.is_disposable_email("bot@yopmail.com")
    assert disposable_emails.is_disposable_email("bot@dropmail.me")


def test_observed_feedecho_burner_domains_are_flagged():
    # The dropmail.me family addresses that registered against feedecho.net
    # on 2026-09-13/14 (see resources/disposable_email_extra.txt).
    for addr in (
        "mupyvep0nup3@mailtowin.com",
        "manadydizut3@picomail.biz",
        "konizahuvib3@mail2me.co",
    ):
        assert disposable_emails.is_disposable_email(addr), addr


def test_subdomains_of_blocked_domains_are_flagged():
    assert disposable_emails.is_disposable_email("x@mail.mailtowin.com")


def test_normal_domains_pass():
    for addr in ("jason@fastmail.us", "a@example.com", "a@gmail.com"):
        assert not disposable_emails.is_disposable_email(addr), addr


def test_malformed_input_fails_open():
    for value in ("", "no-at-sign", "a@", "@example.com", None):
        assert disposable_emails.is_disposable_email(value) is False


def test_loader_skips_corrupt_files_and_normalizes_entries(monkeypatch, tmp_path):
    import disposable_emails as de

    # A bad download (non-UTF-8) must not 500 registration; the good file
    # still loads, inline comments are stripped, trailing dots normalized.
    (tmp_path / "disposable_email_domains.txt").write_bytes(b"\xff\xfe\x00not utf8")
    (tmp_path / "disposable_email_extra.txt").write_text(
        "bad.example # inline comment\ntrailing.example.\n"
    )
    monkeypatch.setattr(de, "_RESOURCES", tmp_path)
    monkeypatch.setattr(de, "_domains", None)

    assert de.is_disposable_email("x@bad.example") is True
    assert de.is_disposable_email("x@trailing.example") is True
    assert de.is_disposable_email("x@gmail.com") is False
