"""Tests the two-phase /execute route (Hands only: run an already-approved plan on the
session's data, no model call). Verifies the OK result shape, multi-step, partial
failure, validation errors, and chaining — the contract the new Workspace relies on.

Run from backend:  .venv\\Scripts\\python.exe test_execute.py
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import main

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


client = TestClient(main.app)
CSV = b"Region,Amount\nNorth,5\nSouth,3\nNorth,1\nNorth,5\n"


def load(sid="e1"):
    return client.post("/inspect", data={"session_id": sid}, files=[("files", ("t.csv", CSV, "text/csv"))])


def execute(ops, sid="e1", title="Test", rewind=-1):
    return client.post(
        "/execute",
        data={"session_id": sid, "plan": json.dumps({"operations": ops, "title": title}), "rewind": str(rewind)},
    )


print("ROUTE /execute — two-phase Hands-only execution\n")

load("e1")

# --- single op: filter keeps the matching rows; real before/after counts ---
b = execute([{"action": "filter", "conditions": [{"column": "Region", "operator": "equals", "value": "North"}]}]).json()
check("filter: status ok", b.get("status") == "ok", str(b)[:160])
check("filter: rows_before is the input count", b.get("rows_before") == 4, str(b.get("rows_before")))
check("filter: row_count after = 3", b.get("row_count") == 3, str(b.get("row_count")))
check("filter: actions listed", b.get("actions") == ["filter"], str(b.get("actions")))
check("filter: plain explanation", isinstance(b.get("explanation"), str) and len(b["explanation"]) > 0, b.get("explanation"))
check("filter: result preview present", isinstance(b.get("preview"), list) and b["preview"], str(b.get("preview"))[:80])
check("filter: a download is offered", bool(b.get("download_id")) and bool(b.get("filename")), str(b.get("download_id")))
check("filter: ai title passed through", b.get("ai_title") == "Test", str(b.get("ai_title")))

# --- chaining: the next /execute runs on the PREVIOUS result (3 rows -> dedup -> 2) ---
b = execute([{"action": "remove_duplicates"}]).json()
check("chain: dedup sees the filtered 3 rows", b.get("rows_before") == 3, str(b.get("rows_before")))
check("chain: dedup -> 2 rows", b.get("row_count") == 2, str(b.get("row_count")))

# --- formula produces a real live-formula description ---
load("e2")
b = execute([{"action": "add_formula_column", "name": "Double", "formula": "{Amount} * 2"}], sid="e2").json()
check("formula: status ok", b.get("status") == "ok", str(b)[:120])
check("formula: 'formulas' written list is real", any("Double" in f for f in (b.get("formulas") or [])), str(b.get("formulas")))

# --- multi-step partial failure: step 1 ok, step 2 (bad column) fails -> ok + partial ---
load("e3")
b = execute(
    [
        {"action": "filter", "conditions": [{"column": "Region", "operator": "equals", "value": "North"}]},
        {"action": "add_formula_column", "name": "X", "formula": "{Cost} * 2"},
    ],
    sid="e3",
).json()
check("partial: still status ok with the partial file", b.get("status") == "ok", str(b)[:160])
check("partial: flagged partial", b.get("partial") is True, str(b.get("partial")))
check("partial: warning names the failing step", "Step 2" in (b.get("warning") or ""), b.get("warning"))

# --- first-step error (bad column) -> friendly 422, not a crash ---
load("e4")
r = execute([{"action": "sort", "columns": ["Nope"], "orders": ["asc"]}], sid="e4")
check("bad column -> 422", r.status_code == 422, str(r.status_code))
check("bad column: friendly, no traceback", ".py" not in r.json().get("error", "").lower(), str(r.json())[:120])

# --- guardrails ---
r = execute([{"action": "filter", "conditions": []}], sid="no-such-session")
check("unknown session -> 400", r.status_code == 400, str(r.json())[:120])

r = execute([], sid="e1")
check("empty plan -> 400", r.status_code == 400, str(r.json())[:120])

r = client.post("/execute", data={"session_id": "e1", "plan": "not json", "rewind": "-1"})
check("bad json plan -> 400", r.status_code == 400, str(r.json())[:120])

print(f"\n{passed} passed, {failed} failed.")
