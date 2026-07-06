"""Phase 3.8 — Collaborative workspace tests.

PRD criteria proven here:
  CO-a  Simultaneous edits don't silently overwrite (optimistic concurrency).
  CO-b  Approval gates respected (pending until a DIFFERENT approver accepts).
  CO-c  Change attribution correct (author + approver recorded; AI attributed).
  Plus: conflict re-checked at approval, permissions, comments, invalid-op atomicity,
        and an end-to-end pass over the /workspace/* HTTP API.

Run from backend:  .venv\\Scripts\\python.exe test_collab.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd
from fastapi.testclient import TestClient

from app import collab, main
from app.reader import summarize_structure

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def seed_state():
    return {
        "tables": {"Sheet1": pd.DataFrame({"Region": ["N", "S", "N"], "Rev": [100, 200, 100]})},
        "primary": "Sheet1",
        "exts": {"Sheet1": "csv"},
    }


def rows(ws_id):
    ws = collab._WORKSPACES[ws_id]
    return len(ws["state"]["tables"][ws["state"]["primary"]])


def rev(ws_id):
    return collab._WORKSPACES[ws_id]["revision"]


SORT = {"action": "sort", "columns": ["Rev"], "orders": ["desc"]}
DEDUPE = {"action": "remove_duplicates"}            # 3 rows -> 2 (the two N/100 dupes)
BAD = {"action": "sort", "columns": ["Nope"], "orders": ["asc"]}

print("PHASE 3.8 — COLLABORATIVE WORKSPACE\n")

# =========================================================================
# CO-a  Simultaneous edits don't silently overwrite
# =========================================================================
print("CO-a  Optimistic concurrency (no silent overwrite)")

ws = collab.create_workspace("Sales", "alice", "Alice", seed_state(), require_approval=False)
wid = ws["id"]
collab.join_workspace(wid, "bob", "Bob", "editor")

# Both alice and bob are looking at revision 0. Alice applies first.
r1 = collab.propose_change(wid, "alice", [SORT], base_revision=0)
check("CO-a first edit applied", r1["status"] == "applied" and rev(wid) == 1, str(r1))

# Bob's edit is ALSO based on revision 0 — it must be rejected as a conflict, not applied.
try:
    collab.propose_change(wid, "bob", [DEDUPE], base_revision=0)
    check("CO-a stale edit rejected", False, "no conflict raised — silent overwrite!")
except collab.CollabError as e:
    check("CO-a stale edit rejected as conflict", e.status == 409, str(e))
    check("CO-a conflict message names both versions", "version 0" in str(e) and "version 1" in str(e), str(e))

check("CO-a data not overwritten by stale edit (still 3 rows)", rows(wid) == 3, f"rows={rows(wid)}")

# Bob refreshes to revision 1 and re-applies — now it works.
r2 = collab.propose_change(wid, "bob", [DEDUPE], base_revision=1)
check("CO-a re-based edit applies", r2["status"] == "applied" and rev(wid) == 2, str(r2))
check("CO-a re-based edit changed data (3 -> 2 rows)", rows(wid) == 2, f"rows={rows(wid)}")

# =========================================================================
# CO-b  Approval gates respected
# =========================================================================
print("\nCO-b  Approval gates")

ws = collab.create_workspace("Audit", "alice", "Alice", seed_state(), require_approval=True)
wid = ws["id"]
collab.join_workspace(wid, "bob", "Bob", "editor")
collab.join_workspace(wid, "carol", "Carol", "viewer")

prop = collab.propose_change(wid, "bob", [DEDUPE], base_revision=0, summary="Remove duplicate rows")
check("CO-b proposal goes to pending", prop["status"] == "pending", str(prop))
check("CO-b data NOT changed while pending", rows(wid) == 3 and rev(wid) == 0, f"rows={rows(wid)} rev={rev(wid)}")
state = collab.state_summary(wid, table_summarizer=lambda df: summarize_structure(df, sample_rows=2))
check("CO-b pending queue has the change", len(state["pending"]) == 1, str(state["pending"]))

cid = prop["change_id"]

# Proposer can't approve their own change.
try:
    collab.approve_change(wid, "bob", cid)
    check("CO-b self-approval blocked", False, "bob approved his own change")
except collab.CollabError as e:
    check("CO-b self-approval blocked (403)", e.status == 403, str(e))

# A viewer can't approve.
try:
    collab.approve_change(wid, "carol", cid)
    check("CO-b viewer can't approve", False, "carol approved")
except collab.CollabError as e:
    check("CO-b viewer can't approve (403)", e.status == 403, str(e))

# Still pending, still unchanged after the blocked attempts.
check("CO-b gate held (data still unchanged)", rows(wid) == 3 and rev(wid) == 0, f"rows={rows(wid)}")

# The owner approves — NOW it applies.
appr = collab.approve_change(wid, "alice", cid)
check("CO-b owner approval applies the change", appr["status"] == "applied" and rev(wid) == 1, str(appr))
check("CO-b approved change changed data (3 -> 2)", rows(wid) == 2, f"rows={rows(wid)}")
state = collab.state_summary(wid, table_summarizer=lambda df: summarize_structure(df, sample_rows=2))
check("CO-b approved change left the pending queue", len(state["pending"]) == 0, str(state["pending"]))

# Reject path: a proposed change that's rejected never touches the data.
prop2 = collab.propose_change(wid, "bob", [SORT], base_revision=1)
rej = collab.reject_change(wid, "alice", prop2["change_id"], reason="not needed")
check("CO-b reject returns rejected", rej["status"] == "rejected", str(rej))
check("CO-b rejected change didn't apply", rev(wid) == 1, f"rev={rev(wid)}")

# =========================================================================
# CO-c  Change attribution correct
# =========================================================================
print("\nCO-c  Attribution")

ws = collab.create_workspace("Attrib", "alice", "Alice", seed_state(), require_approval=True)
wid = ws["id"]
collab.join_workspace(wid, "bob", "Bob", "editor")

# Bob proposes, Alice approves.
p = collab.propose_change(wid, "bob", [DEDUPE], base_revision=0, summary="Dedupe rows")
collab.approve_change(wid, "alice", p["change_id"])
log = collab._WORKSPACES[wid]["log"]
check("CO-c log records one applied change", len(log) == 1, str(log))
check("CO-c author attributed to proposer (bob)", log[0]["author"] == "bob", str(log[0]))
check("CO-c approver attributed to approver (alice)", log[0]["approved_by"] == "alice", str(log[0]))
check("CO-c summary carried through", log[0]["summary"] == "Dedupe rows", str(log[0]))

# The AI is a member and can propose, but a human must approve it.
check("CO-c AI is a workspace member", collab.AI_USER_ID in collab._WORKSPACES[wid]["members"], "")
ai_prop = collab.propose_change(wid, collab.AI_USER_ID, [SORT], base_revision=1, summary="Sort by Rev desc")
check("CO-c AI proposal goes to pending", ai_prop["status"] == "pending", str(ai_prop))
try:
    collab.approve_change(wid, collab.AI_USER_ID, ai_prop["change_id"])
    check("CO-c AI can't approve", False, "AI approved a change")
except collab.CollabError as e:
    check("CO-c AI can't approve (403)", e.status == 403, str(e))
collab.approve_change(wid, "alice", ai_prop["change_id"])
check("CO-c AI change attributed to 'ai', approved by human", log[1]["author"] == "ai" and log[1]["approved_by"] == "alice", str(log[1]))

# Comments are attributed too; viewers may comment.
collab.join_workspace(wid, "carol", "Carol", "viewer")
c = collab.add_comment(wid, "carol", "Should we keep refunds?", target={"type": "cell", "ref": "B2"})
check("CO-c comment attributed to author", c["author"] == "carol", str(c))
check("CO-c comment keeps its target", c["target"] == {"type": "cell", "ref": "B2"}, str(c))
resolved = collab.resolve_comment(wid, "alice", c["id"])
check("CO-c comment resolution attributed", resolved["resolved"] and resolved["resolved_by"] == "alice", str(resolved))

# =========================================================================
# CO-d  Conflict re-checked at APPROVAL time
# =========================================================================
print("\nCO-d  Conflict re-checked at approval")

ws = collab.create_workspace("Race", "alice", "Alice", seed_state(), require_approval=True)
wid = ws["id"]
collab.join_workspace(wid, "bob", "Bob", "editor")

# Two proposals based on the same revision 0.
pa = collab.propose_change(wid, "alice", [SORT], base_revision=0)
pb = collab.propose_change(wid, "bob", [DEDUPE], base_revision=0)
# Approve the first -> revision advances to 1.
collab.approve_change(wid, "bob", pa["change_id"])  # bob approves alice's
check("CO-d first approval applied (rev 1)", rev(wid) == 1, f"rev={rev(wid)}")
# The second is now based on a stale revision 0 -> approving it must conflict, not overwrite.
try:
    collab.approve_change(wid, "alice", pb["change_id"])
    check("CO-d stale pending change conflicts at approval", False, "applied a stale change")
except collab.CollabError as e:
    check("CO-d stale pending conflicts (409)", e.status == 409, str(e))
check("CO-d data reflects only the first change (still 3 rows, just sorted)", rows(wid) == 3, f"rows={rows(wid)}")

# =========================================================================
# CO-e  Permissions + invalid-op atomicity
# =========================================================================
print("\nCO-e  Permissions and atomicity")

ws = collab.create_workspace("Perms", "alice", "Alice", seed_state(), require_approval=False)
wid = ws["id"]
collab.join_workspace(wid, "vic", "Vic", "viewer")

try:
    collab.propose_change(wid, "vic", [SORT], base_revision=0)
    check("CO-e viewer can't propose", False, "viewer changed data")
except collab.CollabError as e:
    check("CO-e viewer can't propose (403)", e.status == 403, str(e))

try:
    collab.propose_change(wid, "stranger", [SORT], base_revision=0)
    check("CO-e non-member can't propose", False, "stranger changed data")
except collab.CollabError as e:
    check("CO-e non-member rejected (403)", e.status == 403, str(e))

# join is idempotent (re-joining doesn't duplicate or reset)
before = len(collab._WORKSPACES[wid]["members"])
collab.join_workspace(wid, "vic", "Vic", "editor")
check("CO-e re-join is idempotent (no duplicate member)", len(collab._WORKSPACES[wid]["members"]) == before, "")

# only the owner can change roles
try:
    collab.set_role(wid, "vic", "alice", "viewer")
    check("CO-e non-owner can't set roles", False, "vic changed a role")
except collab.CollabError as e:
    check("CO-e non-owner can't set roles (403)", e.status == 403, str(e))
collab.set_role(wid, "alice", "vic", "editor")
check("CO-e owner promoted viewer to editor", collab._WORKSPACES[wid]["members"]["vic"]["role"] == "editor", "")

# an invalid operation fails atomically — data + revision unchanged, nothing half-applied
before_rev, before_rows = rev(wid), rows(wid)
try:
    collab.propose_change(wid, "alice", [BAD], base_revision=before_rev)
    check("CO-e invalid op rejected", False, "bad op applied")
except collab.CollabError as e:
    check("CO-e invalid op rejected (422)", e.status == 422, str(e))
check("CO-e invalid op left data untouched", rev(wid) == before_rev and rows(wid) == before_rows, "")

# =========================================================================
# CO-f  End-to-end over the HTTP API
# =========================================================================
print("\nCO-f  HTTP API")

client = TestClient(main.app)
CSV = b"Region,Rev\nN,100\nS,200\nN,100\n"

# Load data into a session, then share it as a workspace.
client.post("/inspect", data={"session_id": "wsapi"}, files=[("files", ("d.csv", CSV, "text/csv"))])
r = client.post("/workspace/create", data={
    "session_id": "wsapi", "name": "Q1 Review", "user_id": "alice", "user_name": "Alice",
    "require_approval": "true",
})
body = r.json()
check("CO-f create ok", r.status_code == 200 and body.get("status") == "ok", str(body)[:200])
wid = body["id"]
member_ids = {m["id"] for m in body["members"]}
check("CO-f owner + AI are members", "alice" in member_ids and "ai" in member_ids, str(member_ids))
check("CO-f starts at revision 0", body["revision"] == 0, str(body.get("revision")))

# Bob joins and proposes a dedupe.
client.post(f"/workspace/{wid}/join", data={"user_id": "bob", "user_name": "Bob", "role": "editor"})
pr = client.post(f"/workspace/{wid}/propose", data={
    "user_id": "bob", "plan": '{"operations":[{"action":"remove_duplicates"}]}',
    "base_revision": "0", "summary": "Remove duplicate rows",
}).json()
check("CO-f proposal pending via API", pr.get("status") == "ok" and pr.get("outcome") == "pending", str(pr)[:200])
check("CO-f API change is pending (data unchanged)", pr["workspace"]["tables"][0]["row_count"] == 3, str(pr["workspace"]["tables"][0]))
change_id = pr["change_id"]

# Owner approves -> data updates.
ap = client.post(f"/workspace/{wid}/approve", data={"user_id": "alice", "change_id": change_id}).json()
check("CO-f approve applies via API (rev 1)", ap.get("outcome") == "applied" and ap.get("workspace", {}).get("revision") == 1, str(ap)[:200])
check("CO-f approved data deduped (3 -> 2)", ap["workspace"]["tables"][0]["row_count"] == 2, str(ap["workspace"]["tables"][0]))
check("CO-f log attributes author+approver", ap["workspace"]["log"][0]["author"] == "bob" and ap["workspace"]["log"][0]["approved_by"] == "alice", str(ap["workspace"]["log"]))

# Self-approval is blocked over the API too (bob proposes, bob approves).
pr2 = client.post(f"/workspace/{wid}/propose", data={
    "user_id": "bob", "plan": '{"operations":[{"action":"sort","columns":["Rev"],"orders":["desc"]}]}',
    "base_revision": "1",
}).json()
self_appr = client.post(f"/workspace/{wid}/approve", data={"user_id": "bob", "change_id": pr2["change_id"]})
check("CO-f API self-approval blocked (403)", self_appr.status_code == 403, str(self_appr.json()))

# Stale proposal conflicts via API (409).
conflict = client.post(f"/workspace/{wid}/propose", data={
    "user_id": "alice", "plan": '{"operations":[{"action":"remove_duplicates"}]}', "base_revision": "0",
})
check("CO-f stale propose conflicts (409)", conflict.status_code == 409, str(conflict.json()))

# Comment + GET snapshot.
client.post(f"/workspace/{wid}/comment", data={"user_id": "bob", "text": "LGTM"})
snap = client.get(f"/workspace/{wid}").json()
check("CO-f GET returns comment attributed to bob", any(c["author"] == "bob" and c["text"] == "LGTM" for c in snap["comments"]), str(snap["comments"]))

# =========================================================================
# CO-g  Session ↔ workspace sync (the 3.8 run-flow integration)
# =========================================================================
print("\nCO-g  Linked session stays in sync")


def session_rows(sid):
    st = main._SESSIONS[sid]["states"][-1]
    return len(st["tables"][st["primary"]])


# Approval ON: the session must NOT change until a change is approved, then it must.
client.post("/inspect", data={"session_id": "wssync"}, files=[("files", ("d.csv", CSV, "text/csv"))])
created = client.post("/workspace/create", data={
    "session_id": "wssync", "name": "Sync", "user_id": "alice", "user_name": "Alice",
    "require_approval": "true",
}).json()
wid = created["id"]
check("CO-g session starts at 3 rows", session_rows("wssync") == 3, str(session_rows("wssync")))

client.post(f"/workspace/{wid}/join", data={"user_id": "bob", "user_name": "Bob", "role": "editor"})
pr = client.post(f"/workspace/{wid}/propose", data={
    "user_id": "bob", "plan": '{"operations":[{"action":"remove_duplicates"}]}', "base_revision": "0",
}).json()
check("CO-g pending change does NOT sync the session", session_rows("wssync") == 3, str(session_rows("wssync")))

client.post(f"/workspace/{wid}/approve", data={"user_id": "alice", "change_id": pr["change_id"]})
check("CO-g approved change syncs into the session (3 -> 2)", session_rows("wssync") == 2, str(session_rows("wssync")))

# Approval OFF: an applied proposal syncs immediately.
client.post("/inspect", data={"session_id": "wssync2"}, files=[("files", ("d.csv", CSV, "text/csv"))])
created2 = client.post("/workspace/create", data={
    "session_id": "wssync2", "name": "Sync2", "user_id": "alice", "user_name": "Alice",
    "require_approval": "false",
}).json()
wid2 = created2["id"]
client.post(f"/workspace/{wid2}/propose", data={
    "user_id": "alice", "plan": '{"operations":[{"action":"remove_duplicates"}]}', "base_revision": "0",
})
check("CO-g approval-off proposal syncs the session immediately (3 -> 2)", session_rows("wssync2") == 2, str(session_rows("wssync2")))

main._SESSIONS.clear()
collab._WORKSPACES.clear()

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
