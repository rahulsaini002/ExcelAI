"""Slack slash command (`/sumio …`) — the signature-verified entry point.

What this IS: a real, secure endpoint that Slack can call. It verifies every request came
from Slack (HMAC-SHA256 over the raw body with your app's signing secret, per Slack's spec)
and rejects replays, then replies. It's honest about scope — a slash command is a fast way
to jump into Sumio with an instruction in hand; the actual transform still runs in the app
(Slack has your files, we don't). So `/sumio clean up emails` replies with a ready-to-run
deep link, not a fabricated "done".

What still needs YOU: creating the Slack app and pasting its Signing Secret into
SUMIO_SLACK_SIGNING_SECRET (and pointing the command's Request URL at /slack/command). Until
that secret is set, the endpoint reports it's not configured rather than trusting anything.

The signature + command logic here are pure and unit-tested; the endpoint in main.py just
wires the raw request to them.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import quote

from . import config

# Slack signs with a versioned scheme; v0 is current. Requests older than this are treated
# as replays and rejected.
_SIG_VERSION = "v0"
MAX_SKEW_SECONDS = 60 * 5


def signing_secret() -> str:
    return os.environ.get("SUMIO_SLACK_SIGNING_SECRET", "")


def slack_configured() -> bool:
    return bool(signing_secret())


def verify_signature(
    signing_secret: str,
    timestamp: str,
    raw_body: bytes,
    signature: str,
    now: float | None = None,
) -> bool:
    """True iff `signature` is Slack's valid signature for this exact request. Follows
    https://api.slack.com/authentication/verifying-requests-from-slack:
      basestring = "v0:{timestamp}:{raw_body}"
      expected   = "v0=" + HMAC_SHA256(signing_secret, basestring)
    Also rejects a timestamp more than 5 minutes off (replay protection). Constant-time
    compare; never raises on malformed input — returns False."""
    if not signing_secret or not signature or not timestamp:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    if abs(now - ts) > MAX_SKEW_SECONDS:
        return False
    basestring = b"%s:%s:%s" % (_SIG_VERSION.encode(), str(ts).encode(), raw_body)
    digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    expected = f"{_SIG_VERSION}={digest}"
    return hmac.compare_digest(expected, signature)


def _ephemeral(text: str) -> dict:
    """A reply only the invoking user sees (the default for slash commands)."""
    return {"response_type": "ephemeral", "text": text}


HELP = (
    "*Sumio* — talk to your spreadsheets.\n"
    "• `/sumio <instruction>` — get a ready-to-run link to the Workspace with your "
    "instruction filled in (e.g. `/sumio remove duplicate rows on Email`).\n"
    "• `/sumio help` — show this message.\n"
    "_Your file stays in Sumio; run the instruction there in one click._"
)


def handle_command(text: str, user_name: str | None = None) -> dict:
    """Turn the command text into a Slack reply. Pure — no I/O — so it's fully testable.
    Empty or `help` → help; anything else → a deep link that pre-fills the Workspace."""
    instruction = (text or "").strip()
    if not instruction or instruction.lower() in {"help", "?"}:
        return _ephemeral(HELP)

    link = f"{config.FRONTEND_URL}/workspace?instruction={quote(instruction)}"
    who = f" {user_name}" if user_name else ""
    return _ephemeral(
        f"Got it{who} — open this to run it in Sumio:\n"
        f"“{instruction}”\n{link}"
    )
