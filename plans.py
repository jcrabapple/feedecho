"""Plan limits and trial state for hosted (multi) mode.

One module answers every quota question the app and scheduler ask:

- ``limit_for(plan, key)`` — the numeric cap for a plan (0 = unlimited)
- ``trial_state(row)``     — 'active' | 'expired' | 'na' from a users row
- ``posting_paused(plan, trial_ends_at)`` — whether the scheduler must skip
  this user's feeds entirely (expired trial). Nothing is deleted: feeds,
  echoes and history are intact, and an operator moving the user to a paid
  plan (or extending trial_ends_at) resumes posting on the next tick.

Single mode never consults any of this: callers gate on settings.MULTI, and
these helpers are written so that single-mode callers fall through without
touching the users table.
"""

from __future__ import annotations

from datetime import datetime, timezone

import settings

# trial_ends_at sentinel written at registration when billing is enabled: the
# card-gated trial clock does NOT start until Stripe Checkout completes and the
# webhook stamps the real trial_end. Posting is paused while this value is
# active, but the trial has not ENDED — it has not begun. The UI must render
# this as "finish checkout", not "trial ended".
TRIAL_PENDING = "2000-01-01 00:00:00"


class PlanError(ValueError):
    """Raised when an action would exceed the plan's limits."""


def limit_for(plan: str, key: str) -> int:
    """The numeric limit for a plan (0 = unlimited). Unknown plan → trial."""
    limits = settings.PLAN_LIMITS.get(plan) or settings.PLAN_LIMITS["trial"]
    return int(limits.get(key, 0))


def normalize_trial_ends(value) -> datetime | None:
    """Parse trial_ends_at into an aware UTC datetime, or None.

    Handles both dialects' readouts (sqlite str, PG datetime) and legacy
    ISO strings with a trailing Z.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # sqlite's "YYYY-MM-DD HH:MM:SS" form (auth.py writes this)
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def trial_state(plan: str, trial_ends_at) -> str:
    """'active' | 'expired' for trial plans; 'na' for non-trial plans."""
    if plan != "trial":
        return "na"
    end = normalize_trial_ends(trial_ends_at)
    if end is None:
        # No expiry recorded: treat as active rather than locking the user
        # out on a data quirk (the register flow always writes one, so this
        # is a direct-DB-edit situation, not a real state).
        return "active"
    return "expired" if end <= datetime.now(timezone.utc) else "active"


def trial_pending(trial_ends_at) -> bool:
    """True when the card-gated trial has NOT started (checkout unfinished).

    Distinct from 'expired': the clock simply hasn't begun. Posting stays
    paused (the sentinel is a past date), but the message must be 'finish
    checkout', not 'trial ended'.
    """
    return normalize_trial_ends(trial_ends_at) == normalize_trial_ends(TRIAL_PENDING)


def posting_paused(plan: str, trial_ends_at) -> bool:
    """Whether the scheduler must skip this user's feeds entirely.

    Only an EXPIRED trial pauses. Paid/beta plans never pause on this path
    (suspension is the operator's kill switch and lives on users.suspended).
    """
    return trial_state(plan, trial_ends_at) == "expired"


def check_feed_allowance(current_count: int, plan: str) -> None:
    """Raise PlanError when adding one more feed would exceed the plan.

    Known residual: the count is checked then the row inserted in separate
    statements, so two simultaneous requests can both pass and overshoot by
    one. Accepted for beta — the overshoot is bounded by concurrent clicks on
    the same account, and a strict lock would serialize every signup.
    """
    cap = limit_for(plan, "max_feeds")
    if cap and current_count >= cap:
        raise PlanError(
            f"Your plan allows {cap} feed{'s' if cap != 1 else ''}. "
            "Upgrade or remove a feed to add another."
        )


def check_destination_allowance(current_count: int, plan: str) -> None:
    """Raise PlanError when connecting one more account would exceed the plan."""
    cap = limit_for(plan, "max_destinations")
    if cap and current_count >= cap:
        raise PlanError(
            f"Your plan allows {cap} connected account{'s' if cap != 1 else ''}. "
            "Upgrade or disconnect one to add another."
        )


def clamp_poll_interval(minutes: int, plan: str) -> int:
    """Clamp a requested poll interval down to the plan's floor.

    Clamping (not rejecting) means a plan downgrade never breaks existing
    feeds — they just poll less often. The app-level 1..1440 clamp still
    applies first; this can only push the value UP.
    """
    floor = limit_for(plan, "min_poll_interval")
    if floor and minutes < floor:
        return floor
    return minutes


def clamp_drip_limit(per_hour: int, plan: str) -> int:
    """Clamp a requested drip ceiling to the plan's ceiling."""
    cap = limit_for(plan, "max_posts_per_hour")
    if cap and per_hour > cap:
        return cap
    return per_hour


def reader_enabled(plan: str) -> bool:
    """Whether the plan includes the RSS reader (issue #11).

    Single mode never consults this — callers gate on ``settings.MULTI``.
    Unknown plans read as disabled (fail closed): a plan must explicitly
    carry a truthy ``reader`` key to enable the reader. (Not via
    ``limit_for``, whose unknown-plan fallback to trial would now leak the
    reader to typos/new plans.)
    """
    limits = settings.PLAN_LIMITS.get(plan)
    if limits is None:
        return False
    return bool(limits.get("reader", 0))
