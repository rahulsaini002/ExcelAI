"""Weekly digest: a plain-text summary email of what a signed-in user did in Sumio.

The honest prerequisite for this feature was *server-side* activity — the in-browser
History lives only in the user's browser, so there was nothing on the server to summarize.
`RunEvent` (written on every successful signed-in /execute) is that record, and this
module turns it into a once-a-week email.

Three pieces, in order of testability:
  - `record_run(...)`     best-effort insert of one RunEvent. Never raises into the request.
  - `compose_digest(...)` PURE: (user, events, period) -> (subject, body). No I/O — the
                          core the tests pin down exactly.
  - `run_due_digests(...)` the job: find users with runs this week who are due, compose,
                          hand off to an injected `transport`, and record the send. Takes
                          `now` and `transport` as arguments so it runs fully offline in
                          tests (same pattern as distribution.run_due).

Nothing here sends on its own: `run_due_digests` only emails users who actually ran a task
in the window AND haven't been emailed in the last 7 days. Anonymous usage is never
recorded and never emailed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from . import config
from .db import session_scope
from .models import DigestLog, RunEvent, User

# A transport takes (recipient_email, subject, body) and sends it, or raises on failure.
# The endpoint injects the real SMTP transport; tests inject a list-appending fake.
Transport = Callable[[str, str, str], None]

PERIOD_DAYS = 7
_MAX_LISTED = 8  # show at most this many task titles in the body; summarize the rest


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_naive_utc(dt: datetime) -> datetime:
    """Normalize any datetime to naive UTC so comparisons are dialect-independent.

    SQLite drops tzinfo on read (stored values come back naive) while Postgres keeps it.
    Coercing both sides through here means the 7-day window math is identical on both, and
    we never hit the "can't compare offset-naive and offset-aware datetimes" TypeError."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# --------------------------------------------------------------------------- recording --
def record_run(user_id: str, summary: str | None, row_count: int | None) -> None:
    """Insert one RunEvent for a signed-in, successful run. Best-effort by design: a
    digest-bookkeeping failure must NEVER turn a user's successful task into an error, so
    every exception is swallowed. Call only when there IS a user (anonymous runs skip this)."""
    if not user_id:
        return
    try:
        with session_scope() as db:
            db.add(RunEvent(
                user_id=user_id,
                summary=(summary or None),
                row_count=(int(row_count) if row_count is not None else None),
            ))
    except Exception:
        # Deliberately silent — see docstring. The user's result is already on its way.
        pass


# ---------------------------------------------------------------------------- composing --
@dataclass(frozen=True)
class _Event:
    """The subset of a RunEvent the composer needs, detached from the DB session."""
    summary: str | None
    row_count: int | None
    created_at: datetime


def _greeting_name(user: User) -> str:
    name = (user.name or "").strip()
    if name:
        return name.split()[0]  # first name only, feels personal without being presumptuous
    return "there"


def compose_digest(
    user: User, events: list[_Event], period_end: datetime
) -> tuple[str, str]:
    """PURE. Build (subject, body) for one user's week. `events` are that user's runs in the
    window, any order; we sort newest-first here. No I/O, no send — just text."""
    n = len(events)
    ordered = sorted(events, key=lambda e: _as_naive_utc(e.created_at), reverse=True)
    period_start = period_end - timedelta(days=PERIOD_DAYS)

    task_word = "task" if n == 1 else "tasks"
    subject = f"Your Sumio week: {n} spreadsheet {task_word}"

    total_rows = sum(e.row_count for e in ordered if e.row_count)
    span = f"{period_start:%b %-d}–{period_end:%b %-d}" if _supports_dash_d() else \
        f"{period_start.strftime('%b %d').lstrip('0')}–{period_end.strftime('%b %d').lstrip('0')}"

    lines = [
        f"Hi {_greeting_name(user)},",
        "",
        f"Here's what you got done in Sumio this week ({span}): "
        f"{n} {task_word}"
        + (f", touching about {total_rows:,} rows in total." if total_rows else "."),
        "",
    ]

    listed = ordered[:_MAX_LISTED]
    for e in listed:
        title = (e.summary or "Ran a task").strip()
        when = _as_naive_utc(e.created_at).strftime("%a %b %d").replace(" 0", " ")
        rows = f" ({e.row_count:,} rows)" if e.row_count else ""
        lines.append(f"  • {title}{rows} — {when}")
    if n > len(listed):
        lines.append(f"  • …and {n - len(listed)} more.")

    lines += [
        "",
        # Built from config.FRONTEND_URL like every other outbound link (password reset
        # in auth.py, invites and the OIDC callback in main.py, /slack). This line alone
        # hardcoded "https://sumio.app/workspace" — a domain we don't serve — so the one
        # call-to-action in the weekly email led users away from the actual app.
        f"Pick up where you left off: {config.FRONTEND_URL}/workspace",
        "",
        "— Sumio",
        "",
        "You're getting this because you ran tasks in Sumio this week. "
        "Prefer not to? Just reply to this email and we'll turn it off.",
    ]
    return subject, "\n".join(lines)


def _supports_dash_d() -> bool:
    """`%-d` (no leading zero) is glibc-only; Windows strftime rejects it. Detect once so
    the composer produces clean dates on both without a try/except on every call."""
    try:
        datetime(2026, 7, 4).strftime("%-d")
        return True
    except ValueError:
        return False


# -------------------------------------------------------------------------------- job ---
def run_due_digests(
    transport: Transport,
    now: datetime | None = None,
    period_days: int = PERIOD_DAYS,
) -> list[dict]:
    """Send the weekly digest to every user who (a) ran >=1 task in the last `period_days`
    and (b) hasn't been sent a digest in the last `period_days`. Returns one report dict per
    considered user: {user_id, email, runs, status, reason}. status is sent/failed/skipped.

    Deterministic and offline-testable: `now` fixes the window, `transport` does the send.
    Windowing is done in Python (after loading) so the 7-day math is identical on SQLite and
    Postgres regardless of how each stores timezones — fine at this app's scale."""
    now = now or _now_utc()
    cutoff = _as_naive_utc(now) - timedelta(days=period_days)
    reports: list[dict] = []

    with session_scope() as db:
        # Group this-week's runs by user (Python-side window; see docstring).
        recent: dict[str, list[_Event]] = {}
        for ev in db.query(RunEvent).all():
            if _as_naive_utc(ev.created_at) >= cutoff:
                recent.setdefault(ev.user_id, []).append(
                    _Event(ev.summary, ev.row_count, ev.created_at)
                )
        if not recent:
            return reports

        sent_at: dict[str, datetime] = {
            log.user_id: _as_naive_utc(log.last_sent_at)
            for log in db.query(DigestLog).filter(DigestLog.user_id.in_(recent.keys())).all()
        }

        for user_id, events in sorted(recent.items()):
            user = db.get(User, user_id)
            if user is None or not user.is_active or not (user.email or "").strip():
                reports.append({"user_id": user_id, "email": None, "runs": len(events),
                                "status": "skipped", "reason": "no active user/email"})
                continue

            last = sent_at.get(user_id)
            if last is not None and last > cutoff:
                reports.append({"user_id": user_id, "email": user.email, "runs": len(events),
                                "status": "skipped", "reason": "already sent this period"})
                continue

            subject, body = compose_digest(user, events, _as_naive_utc(now))
            try:
                transport(user.email, subject, body)
            except Exception as exc:
                # Never swallowed: a failed send is reported, and we do NOT record it as
                # sent, so the next cron run retries this user.
                reports.append({"user_id": user_id, "email": user.email, "runs": len(events),
                                "status": "failed", "reason": str(exc)[:200]})
                continue

            log = db.get(DigestLog, user_id)
            if log is None:
                db.add(DigestLog(user_id=user_id, last_sent_at=now, sent_count=1))
            else:
                log.last_sent_at = now
                log.sent_count = (log.sent_count or 0) + 1
            reports.append({"user_id": user_id, "email": user.email, "runs": len(events),
                            "status": "sent", "reason": None})

    return reports


def make_event(summary: str | None, row_count: int | None, created_at: datetime) -> _Event:
    """Public constructor for the composer's event shape — lets tests build events without
    reaching into the private dataclass."""
    return _Event(summary, row_count, created_at)
