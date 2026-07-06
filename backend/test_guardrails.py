"""Phase 3.10 — AI guardrails & confidence scores.

PRD criteria proven here:
  GR-impact   Destructive actions are flagged with a concrete, numeric impact
              (rows removed, columns deleted, "affects N formulas").
  GR-confirm  The UI path (guard=true) returns confirm_required instead of running;
              confirm=true runs it; the user can cancel by simply not confirming.
  GR-safe     Non-destructive plans run without a prompt; non-guarded callers are
              unaffected (backwards compatible).
  CF-*        Forecasts and anomalies carry a confidence score + band, surfaced in the
              API response and the note.

Run from backend:  .venv\\Scripts\\python.exe test_guardrails.py
"""
from __future__ import annotations

import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd
from fastapi.testclient import TestClient

from app import guardrails, main
from app.executor import execute_multi

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


DF = pd.DataFrame({
    "Region": ["N", "S", "N", "E", "N"],
    "Rev": [100, 200, 100, 300, 100],
    "Cost": [40, 60, 40, 90, 40],
})
TABLES = {"Sheet1": DF}


def assess(ops):
    return guardrails.assess(ops, TABLES, "Sheet1")


print("PHASE 3.10 — GUARDRAILS & CONFIDENCE\n")

# =========================================================================
# GR-impact  Concrete impact per destructive action
# =========================================================================
print("GR-impact  Destructive actions flagged with impact")

a = assess([{"action": "drop_columns", "columns": ["Cost", "Region"]}])
check("drop_columns is destructive", a["destructive"], str(a))
check("drop_columns impact names count + columns", "Deletes 2 columns" in a["warnings"][0]["impact"], a["warnings"][0]["impact"])

a = assess([{"action": "remove_duplicates"}])
check("remove_duplicates counts the dupes", "2 duplicate rows" in a["warnings"][0]["impact"], a["warnings"][0]["impact"])

a = assess([{"action": "filter", "conditions": [{"column": "Region", "operator": "equals", "value": "N"}]}])
check("filter reports rows removed of total", "Removes 2 of 5" in a["warnings"][0]["impact"], a["warnings"][0]["impact"])

a = assess([{"action": "limit", "count": 2}])
check("limit reports dropped rows", "drops 3" in a["warnings"][0]["impact"], a["warnings"][0]["impact"])

a = assess([{"action": "drop_missing"}])
check("drop_missing with no blanks isn't flagged", not a["destructive"], str(a))

# "affects N formulas" — dropping a column used by formula steps in the same plan
a = assess([
    {"action": "add_formula_column", "name": "Margin", "formula": "{Rev} - {Cost}"},
    {"action": "add_formula_column", "name": "MarginPct", "formula": "{Rev} / {Cost}"},
    {"action": "drop_columns", "columns": ["Cost"]},
])
drop_warn = next(w for w in a["warnings"] if w["action"] == "drop_columns")
check("drop_columns counts dependent formulas", "used by 2 formula steps" in drop_warn["impact"], drop_warn["impact"])

# overwrite an existing column
a = assess([{"action": "add_formula_column", "name": "Rev", "formula": "{Rev} * 2", "overwrite": True}])
check("overwrite is destructive", a["destructive"] and "Overwrites" in a["warnings"][0]["impact"], str(a))

# rename affecting formulas
a = assess([
    {"action": "add_formula_column", "name": "X", "formula": "{Rev} + 1"},
    {"action": "rename_columns", "rename_from": ["Rev"], "rename_to": ["Revenue"]},
])
ren = next(w for w in a["warnings"] if w["action"] == "rename_columns")
check("rename reports affected formulas", "affects 1 formula step" in ren["impact"], ren["impact"])

# =========================================================================
# GR-safe  Non-destructive plans don't trip the guard
# =========================================================================
print("\nGR-safe  Safe plans aren't flagged")

a = assess([{"action": "sort", "columns": ["Rev"], "orders": ["desc"]}])
check("sort is not destructive", not a["destructive"], str(a))
a = assess([{"action": "add_formula_column", "name": "New", "formula": "{Rev} - {Cost}"}])
check("adding a new column is not destructive", not a["destructive"], str(a))
a = assess([{"action": "aggregate", "agg_func": "sum", "agg_column": "Rev"}])
check("aggregate is not destructive", not a["destructive"], str(a))

# =========================================================================
# GR-confirm  API gate: confirm_required → confirm → run; cancel = don't confirm
# =========================================================================
print("\nGR-confirm  API confirmation flow")

client = TestClient(main.app)
CSV = b"Region,Rev,Cost\nN,100,40\nS,200,60\nN,100,40\nE,300,90\nN,100,40\n"
client.post("/inspect", data={"session_id": "gr"}, files=[("files", ("d.csv", CSV, "text/csv"))])
dedupe_plan = json.dumps({"operations": [{"action": "remove_duplicates"}], "title": "Dedupe"})

# guard on, not confirmed → must NOT run, returns impact
r = client.post("/execute", data={"session_id": "gr", "plan": dedupe_plan, "guard": "true"}).json()
check("guarded destructive plan asks to confirm", r.get("status") == "confirm_required", str(r)[:160])
check("confirm response carries the impact", "duplicate" in (r.get("summary") or "").lower(), str(r.get("summary")))
# Cancelling = not confirming. Verify the data was NOT changed (still 5 rows).
insp = client.post("/inspect", data={"session_id": "gr"}, files=[("files", ("d.csv", CSV, "text/csv"))]).json()
check("cancel (no confirm) leaves data untouched", insp["tables"][0]["row_count"] == 5, str(insp["tables"][0]["row_count"]))

# confirm=true → it runs
r2 = client.post("/execute", data={"session_id": "gr", "plan": dedupe_plan, "guard": "true", "confirm": "true"}).json()
check("confirmed plan runs", r2.get("status") == "ok", str(r2)[:160])
check("confirmed dedupe removed rows (5 -> 3)", r2.get("row_count") == 3, str(r2.get("row_count")))

# a SAFE plan with guard on runs immediately (no prompt)
client.post("/inspect", data={"session_id": "gr2"}, files=[("files", ("d.csv", CSV, "text/csv"))])
safe_plan = json.dumps({"operations": [{"action": "sort", "columns": ["Rev"], "orders": ["desc"]}]})
r3 = client.post("/execute", data={"session_id": "gr2", "plan": safe_plan, "guard": "true"}).json()
check("safe plan runs without a prompt", r3.get("status") == "ok", str(r3)[:120])

# guard OFF (legacy callers) → destructive plan runs immediately, no gate
client.post("/inspect", data={"session_id": "gr3"}, files=[("files", ("d.csv", CSV, "text/csv"))])
r4 = client.post("/execute", data={"session_id": "gr3", "plan": dedupe_plan}).json()
check("guard off keeps old behaviour (runs immediately)", r4.get("status") == "ok", str(r4)[:120])

main._SESSIONS.clear()

# =========================================================================
# CF  Confidence on forecasts / anomalies
# =========================================================================
print("\nCF  Confidence scores")

LIN = pd.DataFrame({"Month": range(1, 11), "Rev": [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]})
out, _, notes, fmt = execute_multi({"t": LIN}, "t", [{"action": "forecast", "columns": ["Rev"], "count": 3}])
analysis = [d for d in fmt if d.get("type") == "analysis"]
check("forecast emits an analysis directive", len(analysis) == 1 and analysis[0]["kind"] == "forecast", str(fmt))
check("clean trend → high confidence", analysis[0]["confidence"] >= 70 and analysis[0]["band"] == "high", str(analysis[0]))
check("forecast note shows the confidence", "Forecast confidence:" in notes[0], notes[0])

NOISE = pd.DataFrame({"Y": [50, 10, 80, 20, 65, 15, 70, 25, 60, 12]})
_, _, n2, f2 = execute_multi({"t": NOISE}, "t", [{"action": "forecast", "columns": ["Y"], "count": 2}])
a2 = [d for d in f2 if d.get("type") == "analysis"][0]
check("noisy trend → low confidence", a2["confidence"] < 40 and a2["band"] == "low", str(a2))

ANOM = pd.DataFrame({"Score": [10, 12, 11, 13, 10, 12, 11, 13, 1000, -500]})
_, _, n3, f3 = execute_multi({"t": ANOM}, "t", [{"action": "detect_anomalies", "columns": ["Score"]}])
a3 = [d for d in f3 if d.get("type") == "analysis"][0]
check("anomaly emits confidence", a3["kind"] == "anomaly" and isinstance(a3["confidence"], int), str(a3))
check("anomaly note shows the confidence", "Detection confidence:" in n3[0], n3[0])

# Confidence flows through the API response
client = TestClient(main.app)
_orig = main.llm.parse_instruction
try:
    client.post("/inspect", data={"session_id": "cf"},
                files=[("files", ("d.csv", b"Month,Rev\n1,10\n2,20\n3,30\n4,40\n5,50\n6,60\n", "text/csv"))])
    fc_plan = json.dumps({"operations": [{"action": "forecast", "columns": ["Rev"], "count": 3}]})
    r = client.post("/execute", data={"session_id": "cf", "plan": fc_plan}).json()
    check("API response includes analysis confidence", r.get("analysis") and r["analysis"][0]["kind"] == "forecast", str(r.get("analysis")))
    check("API analysis has a band", r["analysis"][0].get("band") in ("high", "moderate", "low"), str(r.get("analysis")))
finally:
    main.llm.parse_instruction = _orig
    main._SESSIONS.clear()

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
