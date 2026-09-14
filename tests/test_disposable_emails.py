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
