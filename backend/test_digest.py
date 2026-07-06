"""Weekly digest tests — the server-side activity record + the digest job.

Proven here (all offline; no SMTP, no network — a fake transport captures every send):
  DIG-record     A signed-in successful run writes exactly one RunEvent; anonymous doesn't.
  DIG-compose    compose_digest is pure and correct: count/plural, listed titles, row total,
                 the honest opt-out line.
  DIG-send       run_due_digests emails users with runs this week, once, to the right address.
  DIG-cadence    Running the job twice does NOT double-send (7-day guard); an OLD-only user
                 is not emailed.
  DIG-active     Inactive / email-less users are skipped, not sent.
  DIG-failure    A transport error is reported (never swallowed) and NOT recorded as sent, so
                 the next run retries.
  DIG-gate       POST /digest/run-due reports 'skipped' when SMTP is unconfigured.

Run from backend:  .venv\\Scripts\\python.exe test_digest.py
"""
from __future__ import annotations

import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Isolate the DB *before* importing anything that reads config/db at import time, so this
# suite never touches the real sumio.db. A temp file (not :memory:) because session_scope
# opens fresh pooled connections and in-memory SQLite would give each its own empty DB.
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="-digest-test.db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")
os.environ.pop("SUMIO_SMTP_HOST", None)  # ensure the endpoint sees email as unconfigured

from datetime import datetime, timedelta, timezone  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import digest, main  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import DigestLog, RunEvent, User  # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def _reset() -> None:
    """Empty the three tables so each scenario starts clean."""
    with session_scope() as db:
        db.query(RunEvent).delete()
        db.query(DigestLog).delete()
        db.query(User).delete()


def _add_user(uid: str, email: str, name: str | None = None, active: bool = True) -> None:
    # email passed through as-is (the column is NOT NULL); "" is used to exercise the
    # empty-email guard in run_due_digests.
    with session_scope() as db:
        db.add(User(id=uid, email=email, name=name, is_active=active))


def _add_run(uid: str, summary: str | None, rows: int | None, created_at: datetime) -> None:
    with session_scope() as db:
        db.add(RunEvent(user_id=uid, summary=summary, row_count=rows, created_at=created_at))


NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
RECENT = NOW - timedelta(days=2)
OLD = NOW - timedelta(days=30)


class FakeTransport:
    """Captures (to, subject, body) instead of sending. Can be told to fail for one address."""
    def __init__(self, fail_for: set[str] | None = None):
        self.sent: list[tuple[str, str, str]] = []
        self.fail_for = fail_for or set()

    def __call__(self, to: str, subject: str, body: str) -> None:
        if to in self.fail_for:
            raise RuntimeError("smtp exploded")
        self.sent.append((to, subject, body))


init_db()

# --------------------------------------------------------------- DIG-compose (pure) ------
_u = User(id="u", email="ada@example.com", name="Ada Lovelace")
_events = [
    digest.make_event("Cleaned up dates", 1200, RECENT),
    digest.make_event("Merged two sheets", 340, RECENT - timedelta(hours=3)),
]
subj, body = digest.compose_digest(_u, _events, NOW.replace(tzinfo=None))
check("DIG-compose subject counts + pluralizes", subj == "Your Sumio week: 2 spreadsheet tasks", subj)
check("DIG-compose greets by first name", body.startswith("Hi Ada,"), body[:20])
check("DIG-compose lists both task titles", "Cleaned up dates" in body and "Merged two sheets" in body)
check("DIG-compose totals rows", "1,540 rows" in body, body)
check("DIG-compose has honest opt-out", "reply to this email" in body.lower())

subj1, _ = digest.compose_digest(_u, [digest.make_event("One task", None, RECENT)], NOW.replace(tzinfo=None))
check("DIG-compose singular for one task", subj1 == "Your Sumio week: 1 spreadsheet task", subj1)

_many = [digest.make_event(f"Task {i}", 10, RECENT) for i in range(12)]
_, body_many = digest.compose_digest(_u, _many, NOW.replace(tzinfo=None))
check("DIG-compose caps the list and notes the rest", "…and 4 more." in body_many, body_many)

# ------------------------------------------------------------------- DIG-record ----------
_reset()
_add_user("u1", "a@example.com")
digest.record_run("u1", "Did a thing", 50)
with session_scope() as db:
    rows = db.query(RunEvent).filter_by(user_id="u1").all()
check("DIG-record writes one event", len(rows) == 1 and rows[0].summary == "Did a thing")
digest.record_run("", "anon", 5)  # no user id → no record
with session_scope() as db:
    total = db.query(RunEvent).count()
check("DIG-record skips anonymous (no user id)", total == 1, f"total={total}")

# ------------------------------------------------------------------- DIG-send ------------
_reset()
_add_user("ua", "ada@example.com", name="Ada")
_add_run("ua", "Cleaned up dates", 1200, RECENT)
_add_run("ua", "Merged sheets", 340, RECENT)
t = FakeTransport()
reports = digest.run_due_digests(t, now=NOW)
check("DIG-send emails the eligible user once", len(t.sent) == 1, f"sent={len(t.sent)}")
check("DIG-send targets the right address", t.sent and t.sent[0][0] == "ada@example.com")
check("DIG-send reports 'sent' with run count", reports == [{"user_id": "ua", "email": "ada@example.com", "runs": 2, "status": "sent", "reason": None}], str(reports))
with session_scope() as db:
    log = db.get(DigestLog, "ua")
check("DIG-send records the send in DigestLog", log is not None and log.sent_count == 1)

# ------------------------------------------------------------------- DIG-cadence ---------
t2 = FakeTransport()
reports2 = digest.run_due_digests(t2, now=NOW + timedelta(days=1))
check("DIG-cadence does NOT re-send within 7 days", len(t2.sent) == 0 and reports2[0]["status"] == "skipped", str(reports2))

# A fresh run lands the following week, and 7+ days have passed since the last digest:
# now the same user is eligible again.
_add_run("ua", "New week task", 90, NOW + timedelta(days=7))
t3 = FakeTransport()
reports3 = digest.run_due_digests(t3, now=NOW + timedelta(days=8))
check("DIG-cadence re-sends after the window passes", len(t3.sent) == 1, str(reports3))

_reset()
_add_user("uo", "old@example.com")
_add_run("uo", "Ancient task", 10, OLD)
t4 = FakeTransport()
reports4 = digest.run_due_digests(t4, now=NOW)
check("DIG-cadence ignores users whose only runs are old", len(t4.sent) == 0 and reports4 == [], str(reports4))

# ------------------------------------------------------------------- DIG-active ----------
_reset()
_add_user("uinact", "inactive@example.com", active=False)
_add_run("uinact", "task", 10, RECENT)
_add_user("unoemail", "")  # email column is NOT NULL; "" exercises the empty-email guard
_add_run("unoemail", "task", 10, RECENT)
t5 = FakeTransport()
reports5 = digest.run_due_digests(t5, now=NOW)
check("DIG-active skips inactive + email-less users", len(t5.sent) == 0 and all(r["status"] == "skipped" for r in reports5), str(reports5))

# ------------------------------------------------------------------- DIG-failure ---------
_reset()
_add_user("uf", "boom@example.com")
_add_run("uf", "task", 10, RECENT)
t6 = FakeTransport(fail_for={"boom@example.com"})
reports6 = digest.run_due_digests(t6, now=NOW)
check("DIG-failure reports the failure (not swallowed)", reports6 and reports6[0]["status"] == "failed", str(reports6))
with session_scope() as db:
    check("DIG-failure does NOT record a failed send", db.get(DigestLog, "uf") is None)
# next run with a working transport retries the same user
t7 = FakeTransport()
reports7 = digest.run_due_digests(t7, now=NOW + timedelta(hours=1))
check("DIG-failure is retried on the next run", len(t7.sent) == 1, str(reports7))

# ------------------------------------------------------------------- DIG-gate ------------
with TestClient(main.app) as client:
    r = client.post("/digest/run-due")
    body = r.json()
check("DIG-gate 200 + 'skipped' when SMTP unconfigured", r.status_code == 200 and body.get("status") == "skipped", str(body))

# ------------------------------------------------------------------------------ done -----
print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_DB_PATH)
except Exception:
    pass
sys.exit(1 if failed else 0)
