"""Live Google Sheets add-on — backend boundary tests.

The add-on is the "different Hands": Apps Script reads the live grid, /sheets/plan is the
Brain, /sheets/apply runs the trusted executor and returns the new grid. Key behaviours
(the Apps Script layer adds the document lock + the view-only write guard on top):

  SH-apply    Operations apply live — apply returns the correctly transformed grid.
  SH-suggest  View-only users get a SUGGESTION, not an edit (status reflects can_edit).
  SH-seq      Simultaneous human+AI edits are sequenced — a stale base_hash → 409 conflict.
  SH-broken   Empty / headerless / ragged sheets are handled with friendly errors, no crash.
  SH-guard    Destructive edits need confirmation (reuses the 3.10 guardrail).

Run from backend:  .venv\\Scripts\\python.exe test_sheets.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from fastapi.testclient import TestClient

from app import main

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


client = TestClient(main.app)
_orig = main.llm.parse_instruction

VALUES = [["Region", "Revenue"], ["North", 100], ["South", 200], ["North", 50]]
SORT_PLAN = {"operations": [{"action": "sort", "columns": ["Revenue"], "orders": ["desc"]}]}

print("LIVE GOOGLE SHEETS ADD-ON\n")

# =========================================================================
# SH-apply  Operations apply live
# =========================================================================
print("SH-apply  Operations apply to the grid")

r = client.post("/sheets/apply", json={"plan": SORT_PLAN, "values": VALUES}).json()
check("apply returns status applied", r.get("status") == "applied", str(r)[:120])
check("apply returns a header row + data", r["values"][0] == ["Region", "Revenue"], str(r["values"][0]))
check("apply transformed the grid (sorted desc)",
      [row[1] for row in r["values"][1:]] == [200, 100, 50], str(r["values"]))
check("apply reports row_count", r["row_count"] == 3, str(r["row_count"]))
check("apply returns a fresh hash", isinstance(r.get("new_hash"), str) and len(r["new_hash"]) == 64, "")

# add a computed column live
calc = client.post("/sheets/apply", json={
    "plan": {"operations": [{"action": "add_formula_column", "name": "Double", "formula": "{Revenue} * 2"}]},
    "values": VALUES,
}).json()
check("apply add-column live", calc["values"][0] == ["Region", "Revenue", "Double"], str(calc["values"][0]))
check("computed values correct", [row[2] for row in calc["values"][1:]] == [200, 400, 100], str(calc["values"]))

# =========================================================================
# SH-plan / SH-suggest  Brain + permission awareness
# =========================================================================
print("\nSH-suggest  Permission-aware (view-only → suggestion)")

try:
    main.llm.parse_instruction = lambda i, s, h: {
        "operations": [{"action": "sort", "columns": ["Revenue"], "orders": ["desc"]}],
        "title": "Sort by revenue", "translation": "Sort by Revenue (high to low)", "confidence": 92,
    }
    # editor → an actionable plan
    p = client.post("/sheets/plan", json={"instruction": "sort by revenue", "values": VALUES, "can_edit": True}).json()
    check("editor gets an actionable plan", p["status"] == "plan", str(p)[:120])
    check("plan carries operations", p["plan"]["operations"][0]["action"] == "sort", str(p["plan"]))
    check("plan returns a base_hash for concurrency", isinstance(p.get("base_hash"), str), "")
    check("plan returns a translation + confidence", p["translation"] and p["confidence"] == 92, str(p)[:120])

    # view-only → a suggestion, NOT an edit
    s = client.post("/sheets/plan", json={"instruction": "sort by revenue", "values": VALUES, "can_edit": False}).json()
    check("view-only user gets a suggestion", s["status"] == "suggestion", str(s)[:120])
    check("suggestion still describes what it would do", bool(s["translation"]), str(s)[:120])
    check("suggestion reports can_edit False", s["can_edit"] is False, str(s))
finally:
    main.llm.parse_instruction = _orig

# =========================================================================
# SH-seq  Sequencing — stale base_hash conflicts (no silent overwrite)
# =========================================================================
print("\nSH-seq  Human + AI edits sequenced safely")

base_hash = client.post("/sheets/apply", json={"plan": SORT_PLAN, "values": VALUES}).json()["new_hash"]
# Get the hash the plan was based on:
H0 = main._sheet_hash(VALUES)
# Someone edits the sheet (adds a row) before apply runs:
V1 = VALUES + [["West", 999]]
conflict = client.post("/sheets/apply", json={"plan": SORT_PLAN, "values": V1, "base_hash": H0})
body = conflict.json()
check("stale base_hash → 409 conflict", conflict.status_code == 409 and body["status"] == "conflict", str(body)[:120])
check("conflict explains re-run", "re-run" in body["message"].lower() or "changed" in body["message"].lower(), body["message"])
# Matching hash → applies fine
ok = client.post("/sheets/apply", json={"plan": SORT_PLAN, "values": VALUES, "base_hash": H0}).json()
check("matching base_hash applies", ok["status"] == "applied", str(ok)[:100])

# =========================================================================
# SH-broken  Empty / headerless / ragged handled
# =========================================================================
print("\nSH-broken  Broken/empty sheets handled")

empty = client.post("/sheets/apply", json={"plan": SORT_PLAN, "values": []})
check("empty sheet → friendly 400", empty.status_code == 400 and "empty" in empty.json()["error"].lower(), str(empty.json()))

headerless = client.post("/sheets/apply", json={"plan": SORT_PLAN, "values": [[" ", ""], ["x", "y"]]})
check("headerless sheet → friendly 400", headerless.status_code == 400, str(headerless.json()))

# header row only (no data) → valid, applies to 0 rows
hdr_only = client.post("/sheets/apply", json={"plan": SORT_PLAN, "values": [["Region", "Revenue"]]}).json()
check("header-only sheet applies with 0 rows", hdr_only["status"] == "applied" and hdr_only["row_count"] == 0, str(hdr_only)[:120])

# ragged rows are padded/truncated, no crash
ragged = client.post("/sheets/apply", json={
    "plan": {"operations": [{"action": "trim"}]},
    "values": [["A", "B"], ["x"], ["y", "z", "extra"]],
}).json()
check("ragged rows handled (no crash)", ragged["status"] == "applied" and ragged["values"][0] == ["A", "B"], str(ragged)[:120])
check("ragged short row padded", len(ragged["values"][1]) == 2, str(ragged["values"]))

# plan with no instruction → friendly
noinstr = client.post("/sheets/plan", json={"instruction": "", "values": VALUES})
check("empty instruction → 400", noinstr.status_code == 400, str(noinstr.json()))

# =========================================================================
# SH-guard  Destructive edits need confirmation (reuses 3.10)
# =========================================================================
print("\nSH-guard  Destructive-edit confirmation")

drop = {"operations": [{"action": "drop_columns", "columns": ["Revenue"]}]}
need = client.post("/sheets/apply", json={"plan": drop, "values": VALUES}).json()
check("destructive edit asks to confirm first", need["status"] == "confirm_required", str(need)[:120])
check("confirmation names the impact", "Revenue" in need["summary"], need["summary"])
done = client.post("/sheets/apply", json={"plan": drop, "values": VALUES, "confirm": True}).json()
check("confirmed destructive edit applies", done["status"] == "applied" and done["values"][0] == ["Region"], str(done["values"][0]))

# multi-step partial failure → still returns the good steps + a warning
partial = client.post("/sheets/apply", json={
    "plan": {"operations": [
        {"action": "sort", "columns": ["Revenue"], "orders": ["desc"]},
        {"action": "sort", "columns": ["Ghost"], "orders": ["asc"]},
    ]},
    "values": VALUES, "confirm": True,
}).json()
check("partial multi-step returns applied-so-far + warning",
      partial["status"] == "applied" and partial["partial"] is True and partial["failed_step"] == 2, str(partial)[:160])

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
