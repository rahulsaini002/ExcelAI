"""ENGINE PHASE 4.8 — AI guardrails & confidence scores (verify & BUILD).

Guardrails pre-existed (app/guardrails.py + test_guardrails.py 27/27): destructive actions
warn with concrete impact, /execute gates them (guard→confirm_required→confirm), safe plans
run untouched, and forecasts/anomalies carry confidence. This phase adds the missing DoD
piece — real dependency tracing behind "feeds N formulas":

  BUILD — trace precedents across the SESSION. Sumio's tables are values (uploaded-file
    formulas aren't preserved), so the honest precedent graph is the formula columns Sumio
    itself built. A per-session registry {column: formula} now accumulates as
    add_formula_column ops run (pruned when a formula column is dropped), and
    guardrails.assess(..., known_formulas=…) uses it so dropping/renaming/overwriting a
    column warns "feeds 3 existing formulas: Margin, MarginPct, Tax" — dependencies from
    EARLIER steps, not just the current plan. It excludes a formula whose own column is
    being removed in the same op (it can't be "broken" if it's leaving).

  VERIFY — confidence on predictions (forecast/anomaly analysis blocks, from 4.5) still
    surfaces; the existing guard/confirm flow is unchanged; and the new "feeds" clause
    reaches BOTH the /execute confirm gate and the /parse review card (Phase 4.4).

Backward compatible: known_formulas defaults to none, so every existing call is unchanged.
No llm.py change → no schema/serving/quota risk; no battery rows (endpoint/mechanism).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_4_8.py
"""
from __future__ import annotations

import json
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

_fd, _db = tempfile.mkstemp(suffix="-p48.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import guardrails  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("ENGINE PHASE 4.8 — guardrails & confidence (verify & build)\n")

DF = pd.DataFrame({"Rev": [100, 200, 100, 300, 100], "Cost": [40, 60, 40, 90, 40]})
TABLES = {"S": DF}
KNOWN = {"Margin": "{Rev} - {Cost}", "MarginPct": "{Rev} / {Cost}", "Tax": "{Rev} * 0.1"}


def one(ops, known=None):
    a = guardrails.assess(ops, TABLES, "S", known_formulas=known)
    return a


# ===================== BUILD: trace precedents across the session =====================
# drop Cost → Margin & MarginPct depend on it (Tax depends only on Rev).
a = one([{"action": "drop_columns", "columns": ["Cost"]}], KNOWN)
w = a["warnings"][0]
check("drop names the existing formulas it feeds (trace-precedents)",
      "feeds 2 existing formulas: Margin, MarginPct" in w["impact"], w["impact"])
check("drop with dependents is destructive", a["destructive"], str(a))

# drop Rev → all three formulas depend on it.
a = one([{"action": "drop_columns", "columns": ["Rev"]}], KNOWN)
check("drop Rev feeds all 3 formulas",
      "feeds 3 existing formulas: Margin, MarginPct, Tax" in a["warnings"][0]["impact"], a["warnings"][0]["impact"])

# EXCLUSION: dropping Cost AND Margin together — Margin can't be a "broken" dependent
# (it's leaving too); MarginPct (kept) still is.
a = one([{"action": "drop_columns", "columns": ["Cost", "Margin"]}], KNOWN)
imp = a["warnings"][0]["impact"]
check("a formula being dropped in the same op is NOT counted as broken",
      "feeds 1 existing formula: MarginPct" in imp and "Margin," not in imp.split("feeds")[1], imp)

# rename Rev → the dependents are flagged and severity escalates to high.
a = one([{"action": "rename_columns", "rename_from": ["Rev"], "rename_to": ["Revenue"]}], KNOWN)
ren = next(w for w in a["warnings"] if w["action"] == "rename_columns")
check("rename feeds existing formulas + escalates to high severity",
      "feeds 3 existing formulas" in ren["impact"] and ren["severity"] == "high", str(ren))

# overwrite Rev → dependents named (excluding Rev itself).
a = one([{"action": "add_formula_column", "name": "Rev", "formula": "{Rev} * 2", "overwrite": True}], KNOWN)
check("overwrite an existing column feeds its dependents",
      "feeds 3 existing formulas: Margin, MarginPct, Tax" in a["warnings"][0]["impact"], a["warnings"][0]["impact"])

# a column NOTHING depends on: no feeds clause, not escalated.
a = one([{"action": "rename_columns", "rename_from": ["Cost"], "rename_to": ["Spend"]}], {"Tax": "{Rev} * 0.1"})
ren = next(w for w in a["warnings"] if w["action"] == "rename_columns")
check("renaming a column with no dependents adds no 'feeds' clause",
      "feeds" not in ren["impact"] and ren["severity"] == "low", str(ren))

# ===================== BACKWARD COMPAT: no registry → old messages intact =====================
a = one([{"action": "drop_columns", "columns": ["Cost"]}])  # known_formulas=None
check("without a registry, drop impact has NO 'feeds' clause (backward compatible)",
      "feeds" not in a["warnings"][0]["impact"], a["warnings"][0]["impact"])
a = one([
    {"action": "add_formula_column", "name": "M", "formula": "{Rev} - {Cost}"},
    {"action": "drop_columns", "columns": ["Cost"]},
])
dw = next(w for w in a["warnings"] if w["action"] == "drop_columns")
check("in-plan formula counting still works (unchanged)", "used by 1 formula step in this plan" in dw["impact"], dw["impact"])

# ===================== END-TO-END: session registry drives the gate =====================
CSV = b"Rev,Cost\n100,40\n200,60\n100,40\n300,90\n100,40\n"
c.post("/inspect", data={"session_id": "g"}, files=[("files", ("d.csv", CSV, "text/csv"))])

# Step 1: build a formula column (registry should learn Margin = {Rev} - {Cost}).
c.post("/execute", data={"session_id": "g", "plan": json.dumps(
    {"operations": [{"action": "add_formula_column", "name": "Margin", "formula": "{Rev} - {Cost}"}]})}).json()
check("session registry learned the formula column", m._SESSIONS["g"].get("formulas", {}).get("Margin") == "{Rev} - {Cost}", str(m._SESSIONS["g"].get("formulas")))

# Step 2 (a LATER instruction): drop Cost with the guard on → must warn it feeds Margin.
r = c.post("/execute", data={"session_id": "g", "plan": json.dumps(
    {"operations": [{"action": "drop_columns", "columns": ["Cost"]}]}), "guard": "true"}).json()
check("guard blocks the drop and cites the cross-step dependency",
      r.get("status") == "confirm_required" and "feeds 1 existing formula: Margin" in (r.get("summary") or ""), str(r)[:220])

# Confirming runs it; the registry then PRUNES Margin only if Margin itself were dropped —
# here Margin survives (Cost went), so it stays. Confirm and check it ran.
r2 = c.post("/execute", data={"session_id": "g", "plan": json.dumps(
    {"operations": [{"action": "drop_columns", "columns": ["Cost"]}]}), "guard": "true", "confirm": "true"}).json()
check("confirming the guarded drop runs it", r2.get("status") == "ok", str(r2)[:160])

# Step 3: drop Margin itself → registry prunes it (no longer a live dependency).
c.post("/execute", data={"session_id": "g", "plan": json.dumps(
    {"operations": [{"action": "drop_columns", "columns": ["Margin"]}]}), "guard": "true", "confirm": "true"}).json()
check("registry prunes a formula column once it's dropped", "Margin" not in m._SESSIONS["g"].get("formulas", {}), str(m._SESSIONS["g"].get("formulas")))

# ===================== /parse REVIEW surfaces the dependency (ties to 4.4) =====================
c.post("/inspect", data={"session_id": "gp"}, files=[("files", ("d.csv", CSV, "text/csv"))])
c.post("/execute", data={"session_id": "gp", "plan": json.dumps(
    {"operations": [{"action": "add_formula_column", "name": "Margin", "formula": "{Rev} - {Cost}"}]})}).json()
_real = m.llm.parse_instruction
m.llm.parse_instruction = lambda i, s, h="": {"operations": [{"action": "drop_columns", "columns": ["Cost"]}], "title": "drop"}
try:
    r = c.post("/parse", data={"instruction": "drop Cost", "session_id": "gp"}).json()
finally:
    m.llm.parse_instruction = _real
warns = (r.get("review") or {}).get("warnings") or []
check("/parse review warns about the cross-step dependency before running",
      any("feeds 1 existing formula: Margin" in (w.get("impact") or "") for w in warns), str(warns))

# ===================== VERIFY: confidence on predictions still surfaces =====================
CSV_FC = b"Month,Rev\n1,10\n2,20\n3,30\n4,40\n5,50\n6,60\n"
c.post("/inspect", data={"session_id": "cf"}, files=[("files", ("d.csv", CSV_FC, "text/csv"))])
r = c.post("/execute", data={"session_id": "cf", "plan": json.dumps(
    {"operations": [{"action": "forecast", "columns": ["Rev"], "count": 3}]})}).json()
an = r.get("analysis") or []
check("forecast prediction carries a confidence + band in the response",
      an and an[0]["kind"] == "forecast" and isinstance(an[0].get("confidence"), int) and an[0].get("band") in ("high", "moderate", "low"), str(an))

m._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
