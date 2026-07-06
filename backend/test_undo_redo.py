"""Tests for Phase 2.7 — simple version history (undo + REDO).

Verifies: each step is recoverable; undo then redo restores the forward step (redo
isn't lost); a NEW operation after undo discards the redo branch; redo with nothing
errors. Uses TestClient + hand-written plans (no LLM). Run: python test_undo_redo.py
"""
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from fastapi.testclient import TestClient

import app.main as m

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


c = TestClient(m.app)
CSV = b"Name,Revenue\na,50\nb,150\nc,200\nd,80\ne,300\n"  # 5 rows
FILTER = json.dumps({"operations": [{"action": "filter", "conditions": [
    {"column": "Revenue", "operator": "greater_than", "value": "100"}]}]})  # -> 3 rows
LIMIT1 = json.dumps({"operations": [{"action": "limit", "count": 1}]})  # -> 1 row
LIMIT2 = json.dumps({"operations": [{"action": "limit", "count": 2}]})  # -> 2 rows


def rc(resp):
    return resp.json().get("row_count")


print("Running undo/redo (version history) checks...\n")

c.post("/inspect", data={"session_id": "ur"}, files={"files": ("s.csv", CSV, "text/csv")})

# two forward steps: 5 -> filter 3 -> limit 1
check("filter step → 3 rows", rc(c.post("/execute", data={"session_id": "ur", "plan": FILTER})) == 3)
check("limit step → 1 row", rc(c.post("/execute", data={"session_id": "ur", "plan": LIMIT1})) == 1)

# undo walks back and each step is recoverable
u1 = c.post("/undo", data={"session_id": "ur"}).json()
check("undo → back to 3 rows", u1["row_count"] == 3)
check("undo reports redo available", u1["can_redo"] is True)
u2 = c.post("/undo", data={"session_id": "ur"}).json()
check("undo → back to original 5 rows", u2["row_count"] == 5)
check("no undo left at the upload", u2["can_undo"] is False)
check("undo past the upload errors", c.post("/undo", data={"session_id": "ur"}).status_code == 400)

# redo restores the forward steps (redo isn't lost by undo)
check("redo → forward to 3 rows", rc(c.post("/redo", data={"session_id": "ur"})) == 3)
r2 = c.post("/redo", data={"session_id": "ur"}).json()
check("redo → forward to 1 row", r2["row_count"] == 1)
check("no redo left at the tip", r2["can_redo"] is False)
check("redo past the tip errors", c.post("/redo", data={"session_id": "ur"}).status_code == 400)

# a NEW operation after undo discards the redo branch (standard model)
check("undo again → 3 rows", rc(c.post("/undo", data={"session_id": "ur"})) == 3)
check("new op after undo → 2 rows", rc(c.post("/execute", data={"session_id": "ur", "plan": LIMIT2})) == 2)
check("redo discarded after a new op", c.post("/redo", data={"session_id": "ur"}).status_code == 400)

# failure: redo on a fresh session with no undo history
c.post("/inspect", data={"session_id": "fresh"}, files={"files": ("s.csv", CSV, "text/csv")})
check("redo with no history errors", c.post("/redo", data={"session_id": "fresh"}).status_code == 400)

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
