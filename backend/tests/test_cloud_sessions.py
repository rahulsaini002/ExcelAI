"""Cross-device sessions: upload on one device, open it on another.

USER-REPORTED BUG THIS CLOSES: "I upload the sheet and if I open next day it again asked
to upload sheet" — then: "it should work on different devices also".

The same-device half is solved in the browser (lib/file-cache.ts), but a second device has
never seen the file, so the only fix is storing it against the ACCOUNT. "Another device" is
simulated the honest way: a SEPARATE TestClient with no shared browser state, carrying only
the user's token — which is exactly what a phone has that a laptop doesn't share.

Run: .venv\\Scripts\\python.exe tests\\test_cloud_sessions.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-cloudsess.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app, _SESSIONS  # noqa: E402

init_db()
XL = "application/octet-stream"
WB = TESTS / "standard_test_workbook.xlsx"

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def signup(client: TestClient, email: str) -> str:
    r = client.post("/auth/signup", json={
        "email": email, "password": "correct horse battery staple", "name": "Test User",
    })
    assert r.status_code == 200, r.text[:200]
    return r.json()["token"]


print("CROSS-DEVICE SESSIONS — the file follows the ACCOUNT, not the browser\n")

# --- device A: sign up, upload, sync ------------------------------------------------
laptop = TestClient(app)
email = f"cross-{uuid.uuid4().hex[:8]}@example.com"
token = signup(laptop, email)
auth = {"Authorization": f"Bearer {token}"}
sid = f"s-{uuid.uuid4().hex[:10]}"
wb_bytes = WB.read_bytes()

r = laptop.post(
    "/inspect", data={"session_id": sid},
    files=[("files", ("standard_test_workbook.xlsx", wb_bytes, XL))],
)
check("device A uploads a sheet", r.status_code == 200, r.text[:120])

r = laptop.post(
    "/sessions/sync", headers=auth,
    data={"session_id": sid, "name": "Q3 numbers"},
    files=[("files", ("standard_test_workbook.xlsx", wb_bytes, XL))],
)
check("device A syncs it to the account", r.json().get("synced") is True, r.text[:160])

# --- the crucial simulation: the server forgets everything in memory ----------------
# This is the "next day" condition — the host slept and restarted.
_SESSIONS.clear()
r = laptop.post("/parse", data={"instruction": "sort by Price", "session_id": sid})
check(
    "after a restart the live session really is gone",
    r.json().get("status") == "error" and "upload" in r.json().get("error", "").lower(),
    r.text[:160],
)

# --- device B: a different client, same account -------------------------------------
phone = TestClient(app)  # no shared state: a genuinely different device
r = phone.get("/sessions", headers=auth)
body = r.json()
names = [s["name"] for s in body.get("sessions", [])]
check("device B sees the saved session", "Q3 numbers" in names, str(body)[:200])
check("listing never ships the file bytes",
      all("blob" not in s for s in body.get("sessions", [])), str(body)[:200])

r = phone.post(f"/sessions/{sid}/restore", headers=auth)
j = r.json()
check("device B restores it", j.get("restored") is True, r.text[:200])
tnames = [t["name"] for t in j.get("tables", [])]
check("restored preview matches a fresh upload's shape",
      any("Sales" in n for n in tnames) and all(
          {"name", "row_count", "columns", "sample_rows"} <= set(t) for t in j.get("tables", [])),
      str(tnames)[:200])

# The real proof: work can continue on device B without re-uploading anything.
r = phone.post("/parse", data={"instruction": "sort by Price", "session_id": sid})
check("device B can work WITHOUT re-uploading",
      r.json().get("status") in ("plan", "clarify", "message"), r.text[:200])

# --- isolation ----------------------------------------------------------------------
other = TestClient(app)
other_token = signup(other, f"other-{uuid.uuid4().hex[:8]}@example.com")
other_auth = {"Authorization": f"Bearer {other_token}"}
r = other.get("/sessions", headers=other_auth)
check("another user sees none of it", r.json().get("sessions") == [], r.text[:160])
r = other.post(f"/sessions/{sid}/restore", headers=other_auth)
check("another user cannot restore it (404, not 403 — existence isn't leaked)",
      r.status_code == 404, f"HTTP {r.status_code}")

# --- anonymous ----------------------------------------------------------------------
anon = TestClient(app)
r = anon.post(
    "/sessions/sync", data={"session_id": "anon-1", "name": "x"},
    files=[("files", ("standard_test_workbook.xlsx", wb_bytes, XL))],
)
j = r.json()
check("anonymous sync is an honest no-op, not a crash or a false success",
      j.get("status") == "ok" and j.get("synced") is False and j.get("reason") == "not_signed_in",
      r.text[:160])
r = anon.get("/sessions")
check("anonymous listing says signed_in=False", r.json().get("signed_in") is False, r.text[:120])

# --- bounds -------------------------------------------------------------------------
big = b"x" * (config.CLOUD_FILE_MAX_MB * 1024 * 1024 + 1024)
r = laptop.post(
    "/sessions/sync", headers=auth, data={"session_id": "big-1", "name": "huge"},
    files=[("files", ("huge.xlsx", big, XL))],
)
j = r.json()
check("an over-cap file is reported as NOT synced, not as a failed upload",
      j.get("status") == "ok" and j.get("synced") is False and j.get("reason") == "too_large",
      r.text[:200])

# --- delete -------------------------------------------------------------------------
r = laptop.post(f"/sessions/{sid}/delete", headers=auth)
check("owner can delete", r.json().get("deleted") is True, r.text[:120])
r = phone.get("/sessions", headers=auth)
check("deleting locally removes the cloud copy everywhere",
      all(s["session_id"] != sid for s in r.json().get("sessions", [])), r.text[:200])

print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_db)
except Exception:
    pass
sys.exit(1 if failed else 0)
