"""ENGINE PHASE 5.1 — collaboration workspace (verify & HARDEN).

The workspace layer pre-existed (app/collab.py + /workspace/* endpoints, backend/
test_collab.py 53/53): optimistic concurrency (stale edits 409, never a silent overwrite),
approval gates (propose → pending → a DIFFERENT approver applies; AI can propose but never
approves), and attribution (applied changes + comments record who). This suite re-verifies
that core and adds two gaps the DoD's "attribution" and "approvals" left open:

  HARDEN 1 — decision audit: rejected/withdrawn changes were invisible (state_summary only
    exposed the pending queue; the log holds applied changes only). A `decisions` trail now
    records every change that LEFT the queue — approved/rejected/withdrawn — with who decided
    and why, so "Bob's change declined by Alice: out of scope" is attributable.

  HARDEN 2 — withdraw: a proposer (or owner) can now cancel their OWN pending change without
    needing an approver to reject it — a 'never mind' path the approval queue lacked.
    (POST /workspace/{id}/withdraw.)

Offline: pure collab functions + a TestClient for the new endpoint. No llm.py change → no
schema/serving/quota risk; no battery rows (endpoint/mechanism, like 3.2/4.7/4.8/4.9).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_5_1.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p51.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import collab  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0

SORT = {"action": "sort", "columns": ["Rev"], "orders": ["desc"]}
DEDUPE = {"action": "remove_duplicates"}


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def seed():
    df = pd.DataFrame({"Region": ["N", "S", "N"], "Rev": [100, 200, 100]})
    return {"tables": {"data": df.copy()}, "primary": "data", "exts": {"data": "csv"}}


def decisions(ws_id):
    return collab.state_summary(ws_id)["decisions"]


print("ENGINE PHASE 5.1 — collaboration workspace (verify & harden)\n")

# ===================== VERIFY: concurrency + approval + attribution =====================
collab._WORKSPACES.clear()
ws = collab.create_workspace("Team", "alice", "Alice", seed(), require_approval=True)
wid = ws["id"]
collab.join_workspace(wid, "bob", "Bob", "editor")

# Two proposals on rev 0; approve one → the other must conflict at approval (no overwrite).
pa = collab.propose_change(wid, "alice", [SORT], base_revision=0)
pb = collab.propose_change(wid, "bob", [DEDUPE], base_revision=0)
collab.approve_change(wid, "bob", pa["change_id"])  # bob approves alice's → rev 1
check("approval gate: a change applies only after a DIFFERENT member approves", collab.state_summary(wid)["revision"] == 1, "")
try:
    collab.approve_change(wid, "alice", pb["change_id"])
    check("stale pending change conflicts at approval (no silent overwrite)", False, "applied a stale change")
except collab.CollabError as e:
    check("stale pending change conflicts at approval (409)", e.status == 409, str(e))
# self-approval: alice proposes a FRESH change (on the current rev 1) and can't approve it herself
pself = collab.propose_change(wid, "alice", [SORT], base_revision=1)
try:
    collab.approve_change(wid, "alice", pself["change_id"])
    check("self-approval blocked", False)
except collab.CollabError as e:
    check("self-approval blocked (403)", e.status == 403, str(e))
log = collab._WORKSPACES[wid]["log"]
check("attribution: applied change records author + approver", log[0]["author"] == "alice" and log[0]["approved_by"] == "bob", str(log[0]))

# ===================== HARDEN 1: decision audit (reject + approve are attributable) =====================
collab._WORKSPACES.clear()
ws = collab.create_workspace("Audit", "alice", "Alice", seed(), require_approval=True)
wid = ws["id"]
collab.join_workspace(wid, "bob", "Bob", "editor")

# a rejected change must be auditable (was invisible before)
p = collab.propose_change(wid, "bob", [DEDUPE], base_revision=0)
check("before decision: nothing in the decision trail", decisions(wid) == [], str(decisions(wid)))
collab.reject_change(wid, "alice", p["change_id"], reason="out of scope")
d = decisions(wid)
check("HARDEN: a REJECTED change is now in the decision audit", len(d) == 1 and d[0]["status"] == "rejected", str(d))
check("rejection records who decided + the reason", d[0]["decided_by"] == "alice" and d[0]["reason"] == "out of scope", str(d[0]))
check("a rejected change never touched the data (still revision 0)", collab.state_summary(wid)["revision"] == 0, "")
check("a rejected change is NOT in the pending queue", collab.state_summary(wid)["pending"] == [], str(collab.state_summary(wid)["pending"]))

# an approved change also shows in the trail (with its approver)
p2 = collab.propose_change(wid, "bob", [SORT], base_revision=0)
collab.approve_change(wid, "alice", p2["change_id"])
d = decisions(wid)
appr = next(x for x in d if x["id"] == p2["change_id"])
check("an APPROVED change is in the decision audit too, with its approver", appr["status"] == "approved" and appr["decided_by"] == "alice", str(appr))

# ===================== HARDEN 2: withdraw your own pending change =====================
collab._WORKSPACES.clear()
ws = collab.create_workspace("Withdraw", "alice", "Alice", seed(), require_approval=True)
wid = ws["id"]
collab.join_workspace(wid, "bob", "Bob", "editor")
collab.join_workspace(wid, "carl", "Carl", "editor")

p = collab.propose_change(wid, "bob", [DEDUPE], base_revision=0)
# a DIFFERENT member (not owner) can't withdraw someone else's change
try:
    collab.withdraw_change(wid, "carl", p["change_id"])
    check("another member can't withdraw your change", False)
except collab.CollabError as e:
    check("another member can't withdraw your change (403)", e.status == 403, str(e))
# the author withdraws their own
res = collab.withdraw_change(wid, "bob", p["change_id"])
check("author withdraws their own pending change", res["status"] == "withdrawn", str(res))
check("withdrawn change leaves the pending queue", collab.state_summary(wid)["pending"] == [], "")
check("withdrawn change is in the decision audit (attributed to the author)",
      any(x["id"] == p["change_id"] and x["status"] == "withdrawn" and x["decided_by"] == "bob" for x in decisions(wid)), str(decisions(wid)))
check("withdrawn change never touched the data (revision 0)", collab.state_summary(wid)["revision"] == 0, "")
# can't withdraw a change that was already decided
try:
    collab.withdraw_change(wid, "bob", p["change_id"])
    check("can't withdraw an already-decided change", False)
except collab.CollabError as e:
    check("can't withdraw an already-decided change (400)", e.status == 400, str(e))
# the OWNER can withdraw another member's pending change
p3 = collab.propose_change(wid, "carl", [SORT], base_revision=0)
check("owner can withdraw another member's pending change", collab.withdraw_change(wid, "alice", p3["change_id"])["status"] == "withdrawn", "")

# ===================== ENDPOINT: /workspace/{id}/withdraw + decisions surfaced =====================
collab._WORKSPACES.clear()
ws = collab.create_workspace("HTTP", "alice", "Alice", seed(), require_approval=True)
wid = ws["id"]
collab.join_workspace(wid, "bob", "Bob", "editor")
p = collab.propose_change(wid, "bob", [DEDUPE], base_revision=0)
r = c.post(f"/workspace/{wid}/withdraw", data={"user_id": "bob", "change_id": p["change_id"]}).json()
check("API withdraw returns outcome=withdrawn", r.get("status") == "ok" and r.get("outcome") == "withdrawn", str(r)[:160])
check("API workspace state exposes the decision audit", any(x["status"] == "withdrawn" for x in (r.get("workspace", {}).get("decisions") or [])), str(r.get("workspace", {}).get("decisions")))
# a non-author over HTTP is refused
p2 = collab.propose_change(wid, "bob", [SORT], base_revision=0)
r2 = c.post(f"/workspace/{wid}/withdraw", data={"user_id": "carl", "change_id": p2["change_id"]})
check("API withdraw by a non-member/non-author is refused", r2.status_code in (403, 404), f"HTTP {r2.status_code}")

collab._WORKSPACES.clear()
m._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
