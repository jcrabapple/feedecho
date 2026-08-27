"""Invite codes: the hosted beta's registration gate.

An invite code is a random opaque token the operator generates in /admin and
hands to a specific person. Registration (multi mode, when
settings.INVITES_REQUIRED) consumes one with a SINGLE conditional UPDATE
stamped with the new user's id, inside the same transaction as the user
INSERT. There is no hold phase: either the user row and the consumed code
commit together, or (duplicate-email race, code failure) neither does.
Two simultaneous signups with the same code: the first UPDATE wins, the
second matches zero rows and aborts its signup. Race structurally closed.

Codes are not hashed: they are operator-minted, single-use, revocable, and
short-lived in practice; a DB leak is already game over for session secrets.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import settings


class InviteError(ValueError):
    """Raised when a supplied invite code cannot be used."""


def _now_str() -> str:
    """Explicit UTC timestamp string. Never CURRENT_TIMESTAMP on PG: it
    resolves in the session time zone while every reader assumes UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def generate_code() -> str:
    """A new invite code: 12 URL-safe characters, no padding."""
    return secrets.token_urlsafe(9)[:12]


def _normalize(code: str) -> str:
    """Codes are compared case-insensitively and ignore surrounding space."""
    return (code or "").strip().upper()


def create_codes(db, count: int, created_by: int) -> list[str]:
    """Insert ``count`` fresh codes. Returns the plaintext codes."""
    codes: list[str] = []
    for _ in range(max(1, min(count, 50))):
        code = generate_code()
        db.execute(
            "INSERT INTO invite_codes (code, created_by) VALUES (?, ?)",
            (_normalize(code), created_by),
        )
        codes.append(_normalize(code))
    return codes


def validate_and_consume(db, code: str, user_id: int) -> str:
    """Atomically consume an invite code, stamping it with its consumer.

    Called AFTER the user row exists, inside the same transaction. The
    single conditional UPDATE (unused + unrevoked + matching code, rowcount
    1) is the entire validation and the entire claim: there is no hold
    phase, no SELECT-then-UPDATE window, and a rolled-back user INSERT
    rolls the consume back with it. Raises InviteError when the code is
    unknown/used/revoked — the caller must abort the signup.
    """
    normalized = _normalize(code)
    if not normalized:
        raise InviteError("An invite code is required to sign up.")
    result = db.execute(
        """
        UPDATE invite_codes
           SET used_by = ?, used_at = ?
         WHERE code = ? AND used_by IS NULL AND revoked = 0
        """,
        (user_id, _now_str(), normalized),
    )
    if result.rowcount != 1:
        # One message for every failure mode: no code-state enumeration for
        # visitors probing the endpoint.
        raise InviteError("That invite code is not valid.")
    return normalized


def looks_usable(db, code: str) -> bool:
    """Non-consuming pre-check for early form validation UX only.

    NEVER authoritative — the atomic consume in validate_and_consume decides.
    Exists so the register form can render its error banner (HTML) instead of
    a JSON 400 from mid-transaction, without creating any claim on the code.
    """
    normalized = _normalize(code)
    if not normalized:
        return False
    row = db.execute(
        "SELECT 1 FROM invite_codes WHERE code = ? AND used_by IS NULL AND revoked = 0",
        (normalized,),
    ).fetchone()
    return row is not None


def list_codes(db) -> list:
    """All codes, newest first, for the admin table."""
    return db.execute(
        """
        SELECT code, created_by, used_by, used_at, revoked, created_at
          FROM invite_codes
         ORDER BY created_at DESC
        """
    ).fetchall()


def revoke(db, code: str, admin_uid: int) -> bool:
    """Revoke an unused code. Returns False for unknown/already-used codes."""
    result = db.execute(
        """
        UPDATE invite_codes SET revoked = 1
         WHERE code = ? AND used_by IS NULL AND revoked = 0
        """,
        (_normalize(code),),
    )
    if result.rowcount == 1:
        return True
    return False


def invites_required() -> bool:
    """Whether /register demands an invite code (multi mode only)."""
    return settings.MULTI and settings.INVITES_REQUIRED
