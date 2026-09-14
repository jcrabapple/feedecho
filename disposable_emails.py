"""Disposable / burner email-domain screening for hosted signups.

Registration is public when the invite gate is off, and a burner address
costs the deployment real work: a scrypt hash, a tenant row, and a
verification email sent to an address the registrant may not even control.
(2026-09-13: three dropmail.me-family signups hit feedecho.net in two days.)

The domain set is a bundled snapshot plus a small curated extra file:

  resources/disposable_email_domains.txt
      Upstream snapshot of the community blocklist
      (github.com/disposable-email-domains/disposable-email-domains).
      Refresh with a single curl and commit the result:
        curl -sL https://raw.githubusercontent.com/disposable-email-domains/disposable-email-domains/master/disposable_email_blocklist.conf > resources/disposable_email_domains.txt

  resources/disposable_email_extra.txt
      Hand-curated additions (# comments allowed), e.g. burner domains
      observed registering against feedecho.net that upstream does not
      (yet) list.

Matching covers the exact domain and every parent, so a@x.mailtowin.com
matches mailtowin.com. Malformed input fails open: a signup must never
break because of this filter.
"""

from pathlib import Path
import logging
import threading

_RESOURCES = Path(__file__).resolve().parent / "resources"
_FILES = ("disposable_email_domains.txt", "disposable_email_extra.txt")

_lock = threading.Lock()
_domains: set[str] | None = None


def _load() -> set[str]:
    """Read both list files. A missing or unreadable file is skipped.

    A corrupt list (non-UTF-8 bytes from a bad download) must never break
    registration: the filter fails OPEN with a warning rather than 500 the
    register route. Inline comments are stripped and trailing dots
    normalized so every entry compares the way the email side does.
    """
    domains: set[str] = set()
    for name in _FILES:
        try:
            text = (_RESOURCES / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as exc:
            logging.getLogger("feedecho").warning(
                "Disposable-domain list %s unreadable: %s", name, exc
            )
            continue
        for line in text.splitlines():
            entry = line.split("#", 1)[0].strip().rstrip(".").lower()
            if entry:
                domains.add(entry)
    return domains


def _domain_set() -> set[str]:
    """The loaded domain set (read once per process)."""
    global _domains
    if _domains is None:
        with _lock:
            if _domains is None:
                _domains = _load()
    return _domains


def is_disposable_email(email: str | None) -> bool:
    """Whether the address sits on a known disposable domain or a child.

    Tolerates None/empty input (validation callers may pass raw form data).
    """
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].strip().strip(".").lower()
    if not domain:
        return False
    domains = _domain_set()
    candidate = domain
    while candidate:
        if candidate in domains:
            return True
        candidate = candidate.partition(".")[2]
    return False
