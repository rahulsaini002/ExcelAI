"""Slack slash-command tests — signature verification AND the endpoint's three states.

Proven here:
  SLK-sig      verify_signature accepts Slack's real signature and rejects a tampered body,
               a stale timestamp (replay), the wrong secret, and malformed input.
  SLK-cmd      handle_command: empty/help → help text; an instruction → a Workspace deep link.
  SLK-endpoint /slack/command: 'not configured' when no signing secret; 401 on a bad
               signature; 200 with a reply on a valid, signed request.

Run from backend:  .venv\\Scripts\\python.exe test_slack.py
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import tempfile
import time
from urllib.parse import urlencode

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="-slack-test.db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")
os.environ.pop("SUMIO_SLACK_SIGNING_SECRET", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import slack  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


SECRET = "8f742231b10e8888abcd99yyyzzz85a5"


def sign(secret: str, ts: str, body: bytes) -> str:
    base = b"v0:%s:%s" % (ts.encode(), body)
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------- SLK-sig -------------
now = time.time()
ts = str(int(now))
body = urlencode({"command": "/sumio", "text": "remove duplicates", "user_name": "ada"}).encode()
good = sign(SECRET, ts, body)

check("SLK-sig accepts a valid signature", slack.verify_signature(SECRET, ts, body, good, now=now))
check("SLK-sig rejects a tampered body",
      not slack.verify_signature(SECRET, ts, body + b"&x=1", good, now=now))
check("SLK-sig rejects the wrong secret",
      not slack.verify_signature("wrong-secret", ts, body, good, now=now))
check("SLK-sig rejects a stale timestamp (replay)",
      not slack.verify_signature(SECRET, str(int(now) - 600), body,
                                 sign(SECRET, str(int(now) - 600), body), now=now))
check("SLK-sig rejects a future timestamp",
      not slack.verify_signature(SECRET, str(int(now) + 600), body,
                                 sign(SECRET, str(int(now) + 600), body), now=now))
check("SLK-sig rejects empty/malformed", not slack.verify_signature(SECRET, "notanumber", body, good, now=now))
check("SLK-sig rejects missing signature", not slack.verify_signature(SECRET, ts, body, "", now=now))

# ------------------------------------------------------------------- SLK-cmd -------------
help_r = slack.handle_command("help")
check("SLK-cmd help returns help text", "/sumio <instruction>" in help_r["text"] and help_r["response_type"] == "ephemeral")
check("SLK-cmd empty returns help", "/sumio <instruction>" in slack.handle_command("")["text"])
inst_r = slack.handle_command("remove duplicate rows on Email", "ada")
check("SLK-cmd instruction returns a workspace deep link",
      "/workspace?instruction=" in inst_r["text"] and "remove%20duplicate%20rows" in inst_r["text"], inst_r["text"])
check("SLK-cmd greets the user when known", " ada" in inst_r["text"])

# --------------------------------------------------------------- SLK-endpoint ------------
# (1) not configured
r = client.post("/slack/command", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
check("SLK-endpoint reports not-configured (200, ephemeral)",
      r.status_code == 200 and "isn't set up" in r.json().get("text", ""), r.text)

# configure the signing secret for the remaining cases
os.environ["SUMIO_SLACK_SIGNING_SECRET"] = SECRET

# (2) valid signature → 200 reply
ts2 = str(int(time.time()))
sig2 = sign(SECRET, ts2, body)
r = client.post(
    "/slack/command",
    content=body,
    headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts2,
        "X-Slack-Signature": sig2,
    },
)
check("SLK-endpoint valid signature → 200 reply",
      r.status_code == 200 and r.json().get("response_type") == "ephemeral"
      and "/workspace?instruction=" in r.json().get("text", ""), r.text)

# (3) bad signature → 401
r = client.post(
    "/slack/command",
    content=body,
    headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts2,
        "X-Slack-Signature": "v0=deadbeef",
    },
)
check("SLK-endpoint bad signature → 401", r.status_code == 401, r.text)

os.environ.pop("SUMIO_SLACK_SIGNING_SECRET", None)

# ---------------------------------------------------------------------------- done -------
print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_DB_PATH)
except Exception:
    pass
sys.exit(1 if failed else 0)
