"""Scheduled export delivery (Phase 3.7).

Sends a rendered report (PDF/PPTX/XLSX) to email or Slack recipients — but only under
strict, auditable rules, because nothing is scarier than software that emails people on
its own. The guarantees, and where they're enforced:

  • Sends only on explicit user setup — `create_schedule` NEVER sends; it produces a
    *draft*. Sending requires a separate `arm_schedule(..., confirm=True)`. No confirm,
    no arming, no delivery.
  • No accidental sends — `run_due` delivers a schedule only when it is active, armed,
    and actually due; after sending it advances `next_run`, so the same window can't fire
    twice. `manual` schedules never auto-fire. `test_render` produces the file WITHOUT
    sending.
  • Correct recipients — recipients are validated + normalised up front; the run report
    lists exactly who was targeted, with no silent extras.
  • Failed sends reported — delivery is per-recipient; failures are captured (with the
    error) into the run report and the schedule's history, never swallowed. An unconfigured
    transport fails loudly rather than pretending to send.

Transport (the actual email/Slack call) is injected, so this module is fully testable
without touching the network. The default transports refuse to send unless their
environment is configured.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from typing import Callable

from . import store

CHANNELS = {"email", "slack"}
FORMATS = {"pdf", "pptx", "xlsx"}
CADENCES = {"daily", "weekly", "monthly", "once", "manual"}
_CADENCE_SECONDS = {"daily": 86_400, "weekly": 7 * 86_400, "monthly": 30 * 86_400}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SLACK_RE = re.compile(r"(#[\w\-]+|@[\w.\-]+|[A-Z0-9]{8,})")  # #channel, @user, or channel id

# schedule_id -> schedule dict (loaded from / snapshotted to the DB when persistence is on)
_SCHEDULES: dict[str, dict] = store.register("schedules", store.load_dict("schedules"))
_MAX_SCHEDULES = 500

# A transport sends ONE message; it raises on failure. render(schedule) -> (bytes, filename, mime).
Transport = Callable[[str, str, str, str, bytes, str], None]
Render = Callable[[dict], "tuple[bytes, str, str]"]


class ScheduleError(Exception):
    """User-facing error. `status` maps to the HTTP code (400 bad request, 403 forbidden,
    404 not found)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class TransportError(Exception):
    """A delivery attempt failed (or the transport isn't configured). Surfaced per
    recipient in the run report — never silently dropped."""


def _now() -> float:
    return time.time()


def _get(schedule_id: str) -> dict:
    s = _SCHEDULES.get(schedule_id)
    if s is None:
        raise ScheduleError("That delivery schedule doesn't exist.", status=404)
    return s


def _validate_recipients(channel: str, recipients: list[str]) -> list[str]:
    cleaned = [r.strip() for r in (recipients or []) if r and r.strip()]
    if not cleaned:
        raise ScheduleError("Add at least one recipient before setting up delivery.", 400)
    pattern = _EMAIL_RE if channel == "email" else _SLACK_RE
    bad = [r for r in cleaned if not pattern.fullmatch(r)]
    if bad:
        raise ScheduleError(
            f"These {channel} recipient(s) don't look valid: {', '.join(bad)}.", 400
        )
    # de-dupe, preserve order
    return list(dict.fromkeys(cleaned))


# --------------------------------------------------------------------------- #
# Setup (never sends)
# --------------------------------------------------------------------------- #
def create_schedule(
    name: str, created_by: str, channel: str, recipients: list[str],
    fmt: str, cadence: str, source: dict | None = None,
) -> dict:
    """Create a DRAFT delivery schedule. This does not send anything and is not armed —
    the user must explicitly arm it afterwards."""
    channel = (channel or "").lower()
    fmt = (fmt or "").lower()
    cadence = (cadence or "").lower()
    if channel not in CHANNELS:
        raise ScheduleError(f"Channel must be one of: {', '.join(sorted(CHANNELS))}.", 400)
    if fmt not in FORMATS:
        raise ScheduleError(f"Format must be one of: {', '.join(sorted(FORMATS))}.", 400)
    if cadence not in CADENCES:
        raise ScheduleError(f"Cadence must be one of: {', '.join(sorted(CADENCES))}.", 400)
    recips = _validate_recipients(channel, recipients)

    sid = f"sch_{uuid.uuid4().hex[:12]}"
    schedule = {
        "id": sid,
        "name": (name or "Report delivery").strip(),
        "created_by": created_by or "unknown",
        "created_at": _now(),
        "channel": channel,
        "recipients": recips,
        "format": fmt,
        "cadence": cadence,
        "source": source or {},
        "status": "draft",   # draft -> active (armed) -> paused
        "armed": False,      # the explicit safety latch; nothing sends until True
        "next_run": None,
        "last_run": None,
        "history": [],
    }
    _SCHEDULES[sid] = schedule
    while len(_SCHEDULES) > _MAX_SCHEDULES:
        _SCHEDULES.pop(next(iter(_SCHEDULES)))
    return schedule


def arm_schedule(schedule_id: str, confirm: bool, now: float | None = None) -> dict:
    """Arm (activate) a schedule so it can send on cadence. REQUIRES an explicit confirm —
    this is the gate behind 'sends only on explicit user setup'."""
    s = _get(schedule_id)
    if not confirm:
        raise ScheduleError(
            "Explicit confirmation is required to start sending. Re-submit with confirm=true.",
            400,
        )
    now = _now() if now is None else now
    s["status"] = "active"
    s["armed"] = True
    # manual schedules never auto-fire; recurring/once become due at the next tick.
    s["next_run"] = None if s["cadence"] == "manual" else now
    return s


def pause_schedule(schedule_id: str) -> dict:
    """Disarm + pause a schedule. After this it cannot auto-send."""
    s = _get(schedule_id)
    s["status"] = "paused"
    s["armed"] = False
    s["next_run"] = None
    return s


def delete_schedule(schedule_id: str) -> None:
    _get(schedule_id)
    _SCHEDULES.pop(schedule_id, None)


def list_schedules(created_by: str | None = None) -> list[dict]:
    items = list(_SCHEDULES.values())
    if created_by:
        items = [s for s in items if s["created_by"] == created_by]
    return items


def get_schedule(schedule_id: str) -> dict:
    return _get(schedule_id)


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def _deliver(schedule: dict, render: Render, transport: Transport, now: float) -> dict:
    """Render once, then send to each recipient independently, capturing per-recipient
    failures. Returns a run report; also appends it to the schedule's history."""
    try:
        artifact, filename, _mime = render(schedule)
    except Exception as exc:  # rendering failed — report it, send nothing
        report = {
            "at": now, "sent": [], "failed": [],
            "status": "render_failed", "error": str(exc),
            "recipients": list(schedule["recipients"]),
        }
        schedule["history"].append(report)
        return report

    subject = f"{schedule['name']} — Sumio report"
    body = "Your scheduled report from Sumio is attached."
    sent: list[str] = []
    failed: list[dict] = []
    for recipient in schedule["recipients"]:
        try:
            transport(schedule["channel"], recipient, subject, body, artifact, filename)
            sent.append(recipient)
        except Exception as exc:
            failed.append({"recipient": recipient, "error": str(exc)})

    status = "sent" if not failed else ("partial" if sent else "failed")
    report = {
        "at": now, "sent": sent, "failed": failed, "status": status,
        "recipients": list(schedule["recipients"]), "format": schedule["format"],
    }
    schedule["history"].append(report)
    return report


def run_due(render: Render, transport: Transport, now: float | None = None) -> list[dict]:
    """Deliver every schedule that is active, armed, AND due. Idempotent per window: a
    delivered schedule has its next_run advanced (or is paused, for 'once'), so calling
    this again for the same instant sends nothing. Drafts, paused, unarmed, manual, and
    not-yet-due schedules are skipped entirely."""
    now = _now() if now is None else now
    reports: list[dict] = []
    for s in list(_SCHEDULES.values()):
        if not (s["status"] == "active" and s["armed"]):
            continue
        if s["cadence"] == "manual" or s["next_run"] is None or s["next_run"] > now:
            continue
        report = _deliver(s, render, transport, now)
        s["last_run"] = now
        if s["cadence"] == "once":
            s["status"] = "paused"
            s["armed"] = False
            s["next_run"] = None
        else:
            s["next_run"] = now + _CADENCE_SECONDS[s["cadence"]]
        reports.append({"schedule_id": s["id"], **report})
    return reports


def send_now(schedule_id: str, confirm: bool, render: Render, transport: Transport,
             now: float | None = None) -> dict:
    """Send a schedule immediately, on demand. REQUIRES explicit confirm — a manual send
    is still an explicit user action, never automatic."""
    s = _get(schedule_id)
    if not confirm:
        raise ScheduleError("Explicit confirmation is required to send now.", 400)
    now = _now() if now is None else now
    report = _deliver(s, render, transport, now)
    s["last_run"] = now
    return report


def test_render(schedule_id: str, render: Render) -> tuple[bytes, str, str]:
    """Produce the report file for a schedule WITHOUT sending it — a safe preview."""
    s = _get(schedule_id)
    return render(s)


# --------------------------------------------------------------------------- #
# Default transports — refuse to send unless explicitly configured.
# --------------------------------------------------------------------------- #
def email_configured() -> bool:
    """True if the server has an SMTP host set — i.e. email_transport can actually send.
    Callers gate on this to report 'skipped' instead of failing when mail is unconfigured."""
    return bool(os.environ.get("SUMIO_SMTP_HOST"))


def email_transport(recipient, subject, body, attachment, filename) -> None:
    host = os.environ.get("SUMIO_SMTP_HOST")
    if not host:
        raise TransportError(
            "Email delivery isn't configured on the server "
            "(set SUMIO_SMTP_HOST / PORT / USER / PASSWORD / FROM)."
        )
    import smtplib
    from email.message import EmailMessage

    port = int(os.environ.get("SUMIO_SMTP_PORT", "587"))
    user = os.environ.get("SUMIO_SMTP_USER", "")
    password = os.environ.get("SUMIO_SMTP_PASSWORD", "")
    sender = os.environ.get("SUMIO_SMTP_FROM", user or "sumio@localhost")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)
    if attachment:
        msg.add_attachment(
            attachment, maintype="application", subtype="octet-stream", filename=filename
        )
    with smtplib.SMTP(host, port, timeout=20) as server:
        server.starttls()
        if user:
            server.login(user, password)
        server.send_message(msg)


def slack_transport(recipient, subject, body, attachment, filename) -> None:
    webhook = os.environ.get("SUMIO_SLACK_WEBHOOK")
    if not webhook:
        raise TransportError(
            "Slack delivery isn't configured on the server (set SUMIO_SLACK_WEBHOOK)."
        )
    import json
    import urllib.request

    text = f"*{subject}*\n{body}\n_(attachment: {filename} — download from Sumio)_"
    payload = json.dumps({"channel": recipient, "text": text}).encode()
    req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (configured webhook)
        if resp.status >= 300:
            raise TransportError(f"Slack webhook returned HTTP {resp.status}.")


def default_transport(channel, recipient, subject, body, attachment, filename) -> None:
    """Dispatch to the configured transport for `channel`; raises if unconfigured so the
    failure is reported rather than a send being silently skipped."""
    if channel == "email":
        return email_transport(recipient, subject, body, attachment, filename)
    if channel == "slack":
        return slack_transport(recipient, subject, body, attachment, filename)
    raise TransportError(f"No transport for channel {channel!r}.")
