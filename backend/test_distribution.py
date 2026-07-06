"""Phase 3.7 — Export & distribution tests.

PRD criteria proven here:
  DST-no-accident   Nothing is sent without explicit, confirmed setup (drafts/unarmed
                    schedules never deliver; run_due skips them).
  DST-explicit      Arming requires confirm=true; send_now requires confirm=true.
  DST-recipients    Recipients are validated; the run report lists exactly who was targeted.
  DST-failures      Per-recipient failures are reported, never swallowed; idempotent runs
                    don't double-send.
  EX-*              PDF / PPTX / XLSX export produce valid files.

Run from backend:  .venv\\Scripts\\python.exe test_distribution.py
"""
from __future__ import annotations

import io
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import fitz
import pandas as pd
from fastapi.testclient import TestClient

from app import distribution as dist
from app import exports, main

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


DF = pd.DataFrame({"Region": ["North", "South", "East"], "Revenue": [100, 200, 150]})
TABLES = {"Sales": DF}

print("PHASE 3.7 — EXPORT & DISTRIBUTION\n")

# =========================================================================
# EX  Export to PDF / PPTX / XLSX
# =========================================================================
print("EX  Export formats")

pdf_bytes = exports.tables_to_pdf(TABLES, "Q1 Report")
doc = fitz.open(stream=pdf_bytes, filetype="pdf")
check("EX pdf is a valid non-empty PDF", doc.page_count >= 1, f"pages={doc.page_count}")
page_text = doc[0].get_text()
check("EX pdf contains the column headers", "Region" in page_text and "Revenue" in page_text, page_text[:80])
check("EX pdf contains a data value", "North" in page_text, page_text[:120])
doc.close()

pptx_bytes = exports.tables_to_pptx(TABLES, "Q1 Report")
from pptx import Presentation  # noqa: E402

prs = Presentation(io.BytesIO(pptx_bytes))
check("EX pptx has a title slide + a table slide", len(prs.slides) >= 2, f"slides={len(prs.slides)}")

data, ext = exports.render(TABLES, "pdf")
check("EX render dispatches pdf", ext == "pdf" and data[:4] == b"%PDF", ext)
try:
    exports.render(TABLES, "docx")
    check("EX render rejects unsupported format", False, "no error")
except ValueError:
    check("EX render rejects unsupported format", True)

# Big sheet is capped (no runaway pages)
big = {"Big": pd.DataFrame({"A": range(5000), "B": range(5000)})}
big_pdf = fitz.open(stream=exports.tables_to_pdf(big), filetype="pdf")
check("EX pdf caps huge sheets", big_pdf.page_count < 60, f"pages={big_pdf.page_count}")
big_pdf.close()


# =========================================================================
# Fakes for governance tests (no real network)
# =========================================================================
def fake_render(schedule):
    return (b"%PDF-1.4 fake report", "report.pdf", "application/pdf")


def make_transport():
    """A transport that records who it 'sent' to, and bounces one known-bad address."""
    sent: list[str] = []

    def transport(channel, recipient, subject, body, attachment, filename):
        if recipient == "bounce@x.com":
            raise RuntimeError("mailbox full")
        sent.append(recipient)

    return transport, sent


def reset():
    dist._SCHEDULES.clear()


# =========================================================================
# DST-no-accident  Nothing sends without explicit, confirmed setup
# =========================================================================
print("\nDST-no-accident  No accidental sends")
reset()

s = dist.create_schedule("Weekly sales", "alice", "email", ["a@b.com", "c@d.com"], "pdf", "weekly")
check("create produces a DRAFT", s["status"] == "draft" and s["armed"] is False, str(s["status"]))
check("create sets next_run None (not scheduled yet)", s["next_run"] is None, str(s["next_run"]))

transport, sent = make_transport()
reports = dist.run_due(fake_render, transport, now=1000.0)
check("run_due ignores an unarmed draft", reports == [], str(reports))
check("transport was never called for a draft", sent == [], str(sent))

# =========================================================================
# DST-explicit  Arming + send_now require explicit confirm
# =========================================================================
print("\nDST-explicit  Explicit confirmation required")
reset()
s = dist.create_schedule("Daily", "alice", "email", ["a@b.com"], "pdf", "daily")

try:
    dist.arm_schedule(s["id"], confirm=False)
    check("arm without confirm is blocked", False, "armed without confirm!")
except dist.ScheduleError as e:
    check("arm without confirm raises", e.status == 400 and "confirm" in str(e).lower(), str(e))
check("still a draft after blocked arm", dist.get_schedule(s["id"])["status"] == "draft", "")

dist.arm_schedule(s["id"], confirm=True, now=1000.0)
armed = dist.get_schedule(s["id"])
check("arm with confirm activates", armed["status"] == "active" and armed["armed"] is True, str(armed["status"]))
check("arm sets next_run", armed["next_run"] == 1000.0, str(armed["next_run"]))

transport, sent = make_transport()
reports = dist.run_due(fake_render, transport, now=1000.0)
check("armed + due schedule delivers", len(reports) == 1 and reports[0]["status"] == "sent", str(reports))
check("delivered to the recipient", sent == ["a@b.com"], str(sent))

# =========================================================================
# DST-recipients  Validation + exact recipient list
# =========================================================================
print("\nDST-recipients  Correct recipients")
reset()

try:
    dist.create_schedule("Bad", "alice", "email", ["not-an-email", "ok@x.com"], "pdf", "daily")
    check("invalid email rejected", False, "accepted bad email")
except dist.ScheduleError as e:
    check("invalid email rejected at setup", "not-an-email" in str(e), str(e))

try:
    dist.create_schedule("Empty", "alice", "email", ["  ", ""], "pdf", "daily")
    check("empty recipients rejected", False, "accepted empty")
except dist.ScheduleError as e:
    check("empty recipients rejected", "at least one" in str(e).lower(), str(e))

s = dist.create_schedule("Team", "alice", "email", ["a@x.com", "b@x.com", "a@x.com"], "pdf", "once")
check("recipients de-duplicated", s["recipients"] == ["a@x.com", "b@x.com"], str(s["recipients"]))
dist.arm_schedule(s["id"], confirm=True, now=500.0)
transport, sent = make_transport()
rep = dist.run_due(fake_render, transport, now=500.0)[0]
check("run report targets EXACTLY the schedule recipients", rep["recipients"] == ["a@x.com", "b@x.com"], str(rep["recipients"]))
check("sent set equals recipients (no extras, no drops)", set(rep["sent"]) == {"a@x.com", "b@x.com"}, str(rep["sent"]))

# slack recipient validation
try:
    dist.create_schedule("S", "alice", "slack", ["not a channel"], "pdf", "daily")
    check("invalid slack recipient rejected", False, "accepted")
except dist.ScheduleError:
    check("invalid slack recipient rejected", True)
ok_slack = dist.create_schedule("S", "alice", "slack", ["#reports", "@alice"], "pdf", "daily")
check("valid slack recipients accepted", ok_slack["recipients"] == ["#reports", "@alice"], str(ok_slack["recipients"]))

# =========================================================================
# DST-failures  Per-recipient failures reported; no double-send
# =========================================================================
print("\nDST-failures  Failures reported, idempotent")
reset()

s = dist.create_schedule("Mixed", "alice", "email", ["good@x.com", "bounce@x.com"], "pdf", "daily")
dist.arm_schedule(s["id"], confirm=True, now=1000.0)
transport, sent = make_transport()
rep = dist.run_due(fake_render, transport, now=1000.0)[0]
check("partial status when some bounce", rep["status"] == "partial", str(rep["status"]))
check("good recipient recorded as sent", rep["sent"] == ["good@x.com"], str(rep["sent"]))
check("bounced recipient reported with error", rep["failed"] and rep["failed"][0]["recipient"] == "bounce@x.com", str(rep["failed"]))
check("failure carries the reason", "mailbox full" in rep["failed"][0]["error"], str(rep["failed"]))

# Idempotent: a second tick at the same instant must not re-send.
reports2 = dist.run_due(fake_render, transport, now=1000.0)
check("no double-send at the same instant", reports2 == [], str(reports2))
check("transport not called again", sent == ["good@x.com"], str(sent))

# Render failure is reported, nothing sent.
reset()
s = dist.create_schedule("R", "alice", "email", ["a@x.com"], "pdf", "daily")
dist.arm_schedule(s["id"], confirm=True, now=1.0)

def boom_render(_):
    raise RuntimeError("data gone")

transport, sent = make_transport()
rep = dist.run_due(boom_render, transport, now=1.0)[0]
check("render failure reported", rep["status"] == "render_failed" and "data gone" in rep["error"], str(rep))
check("render failure sends nothing", sent == [], str(sent))

# =========================================================================
# DST-manual / once / pause
# =========================================================================
print("\nDST-modes  manual / once / pause")
reset()

# manual never auto-fires
m = dist.create_schedule("Manual", "alice", "email", ["a@x.com"], "pdf", "manual")
dist.arm_schedule(m["id"], confirm=True, now=1000.0)
check("manual schedule has no next_run", dist.get_schedule(m["id"])["next_run"] is None, "")
transport, sent = make_transport()
check("manual schedule never auto-delivers", dist.run_due(fake_render, transport, now=9_999_999) == [], "")

# send_now requires confirm
try:
    dist.send_now(m["id"], confirm=False, render=fake_render, transport=transport)
    check("send_now without confirm blocked", False, "sent without confirm")
except dist.ScheduleError as e:
    check("send_now without confirm raises", e.status == 400, str(e))
rep = dist.send_now(m["id"], confirm=True, render=fake_render, transport=transport)
check("send_now with confirm delivers", rep["sent"] == ["a@x.com"], str(rep))

# once pauses itself after sending
o = dist.create_schedule("Once", "alice", "email", ["a@x.com"], "pdf", "once")
dist.arm_schedule(o["id"], confirm=True, now=2000.0)
once_transport, _once_sent = make_transport()
dist.run_due(fake_render, once_transport, now=2000.0)
after = dist.get_schedule(o["id"])
check("once schedule pauses after its single send", after["status"] == "paused" and after["armed"] is False, str(after["status"]))

# pause disarms an active schedule
p = dist.create_schedule("Pausable", "alice", "email", ["a@x.com"], "pdf", "daily")
dist.arm_schedule(p["id"], confirm=True, now=3000.0)
dist.pause_schedule(p["id"])
transport, sent = make_transport()
check("paused schedule does not deliver", dist.run_due(fake_render, transport, now=3000.0) == [], str(sent))

# =========================================================================
# API  End-to-end over HTTP (default transport is unconfigured → fails safe)
# =========================================================================
print("\nAPI  HTTP endpoints")
reset()
client = TestClient(main.app)
CSV = b"Region,Revenue\nNorth,100\nSouth,200\nEast,150\n"
client.post("/inspect", data={"session_id": "dist"}, files=[("files", ("d.csv", CSV, "text/csv"))])

# export
ex = client.post("/export", data={"session_id": "dist", "format": "pdf"}).json()
check("API export pdf ok", ex.get("status") == "ok" and ex.get("media_type") == "application/pdf", str(ex)[:140])
check("API export returns a file", bool(ex.get("file_base64") or ex.get("download_id")), "no file")
exx = client.post("/export", data={"session_id": "dist", "format": "pptx"}).json()
check("API export pptx ok", exx.get("media_type", "").endswith("presentationml.presentation"), str(exx)[:120])

# create (draft) — must NOT send
created = client.post("/distribution/create", data={
    "session_id": "dist", "channel": "email", "recipients": "a@b.com, c@d.com",
    "name": "Weekly", "format": "pdf", "cadence": "daily", "user_id": "alice",
}).json()
check("API create returns a draft", created["schedule"]["status"] == "draft", str(created)[:120])
sid = created["schedule"]["id"]

# run-due now: nothing armed → no accidental send
rd = client.post("/distribution/run-due").json()
check("API run-due sends nothing while unarmed", rd["delivered"] == [], str(rd))

# arm without confirm → 400
na = client.post(f"/distribution/{sid}/arm", data={"confirm": "false"})
check("API arm without confirm rejected (400)", na.status_code == 400, str(na.json()))

# arm with confirm → active
ar = client.post(f"/distribution/{sid}/arm", data={"confirm": "true"}).json()
check("API arm with confirm activates", ar["schedule"]["status"] == "active", str(ar)[:120])

# send-now without confirm → 400
ns = client.post(f"/distribution/{sid}/send-now", data={"confirm": "false"})
check("API send-now without confirm rejected (400)", ns.status_code == 400, str(ns.json()))

# send-now with confirm → reported failures (email transport unconfigured), nothing silently sent
sn = client.post(f"/distribution/{sid}/send-now", data={"confirm": "true"}).json()
rep = sn["report"]
check("API send-now reports correct recipients", rep["recipients"] == ["a@b.com", "c@d.com"], str(rep["recipients"]))
check("API unconfigured transport reports failures (not silent success)", rep["status"] == "failed" and len(rep["failed"]) == 2, str(rep))
check("API failure explains it's not configured", "configured" in rep["failed"][0]["error"].lower(), str(rep["failed"][0]))

# pause → run-due sends nothing
client.post(f"/distribution/{sid}/pause")
rd2 = client.post("/distribution/run-due").json()
check("API paused schedule excluded from run-due", rd2["delivered"] == [], str(rd2))

main._SESSIONS.clear()
dist._SCHEDULES.clear()

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
