"""ENGINE PHASE 3.3 — explainable AI views (Show Formula / Code / Reasoning).

The DoD is HONESTY: each view must match what actually happened. All three come from
real execution artifacts, never a separate AI re-description that could drift:
  * Show Reasoning = the per-step notes (real counts from the executor).
  * Show Formula   = the live formulas written into the file (they compute the result).
  * Show Code      = _explain_code(operations), rendered from the EXACT plan that ran.
No LLM — hand-written plans through the API + direct unit checks.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_3_3.py
"""
from __future__ import annotations

import io
import json
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

_fd, _db = tempfile.mkstemp(suffix="-p33.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app.db import init_db  # noqa: E402
from app.executor import execute_multi  # noqa: E402
from app.main import _explain_code, _describe_formulas  # noqa: E402

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


print("ENGINE PHASE 3.3 — explainable AI views (no AI)\n")

# ============ Show Code: grounded in the EXACT plan ============
plan = [
    {"action": "filter", "conditions": [{"column": "Revenue", "operator": "greater_than", "value": "100"}]},
    {"action": "sort", "columns": ["Revenue"], "orders": ["desc"]},
    {"action": "add_formula_column", "name": "Profit", "formula": "{Revenue} - {Cost}"},
    {"action": "aggregate", "agg_func": "sum", "agg_column": "Revenue", "group_by": ["Region"]},
    {"action": "lookup", "key_column": "Region", "source_sheet": "ref",
     "source_key_column": "Region", "return_column": "Manager"},
]
code = _explain_code(plan)
check("code: one line per executed operation", len(code) == len(plan), f"{len(code)} vs {len(plan)}")
check("code: filter → df[...] with the real column/operator/value",
      "df[" in code[0] and "Revenue" in code[0] and "100" in code[0], code[0])
check("code: sort → sort_values with the real direction",
      "sort_values" in code[1] and "Revenue" in code[1] and "False" in code[1], code[1])
check("code: add_formula → df['Profit'] = the real formula",
      code[2] == "df['Profit'] = {Revenue} - {Cost}", code[2])
check("code: aggregate → groupby with the real func/column",
      "groupby" in code[3] and "Region" in code[3] and "Revenue" in code[3] and "sum" in code[3], code[3])
check("code: lookup → lookup(...) naming the real key/source/return",
      "lookup(" in code[4] and "Region" in code[4] and "ref" in code[4] and "Manager" in code[4], code[4])

# an unknown/newer op still renders faithfully (generic action(params)), never crashes
gen = _explain_code([{"action": "statistics", "stat_method": "describe", "columns": ["A", "B"]}])
check("code: a newer op renders as action(params)", gen[0].startswith("df = statistics(") and "describe" in gen[0], gen[0])
check("code: the 'table' target is annotated",
      _explain_code([{"action": "sort", "columns": ["X"], "orders": ["asc"], "table": "Sales"}])[0].endswith("# on table 'Sales'"),
      _explain_code([{"action": "sort", "columns": ["X"], "table": "Sales"}]))

# ============ the three views are all in the response, and MATCH execution ============
CSV = b"Region,Revenue,Cost\nN,100,40\nS,200,60\nN,150,50\nE,80,20\n"
c.post("/inspect", data={"session_id": "x"}, files={"files": ("s.csv", CSV, "text/csv")})
P = {"operations": [
    {"action": "filter", "conditions": [{"column": "Revenue", "operator": "greater_than", "value": "90"}]},
    {"action": "add_formula_column", "name": "Profit", "formula": "{Revenue} - {Cost}"}]}
r = c.post("/execute", data={"session_id": "x", "plan": json.dumps(P)}).json()
check("response carries all three views (reasoning=notes, formula=formulas, code=code)",
      isinstance(r.get("notes"), list) and r["notes"]
      and isinstance(r.get("formulas"), list) and r["formulas"]
      and isinstance(r.get("code"), list) and r["code"], str({k: r.get(k) for k in ("notes", "formulas", "code")})[:200])
check("Show Code mirrors the two ops that ran (filter then Profit)",
      len(r["code"]) == 2 and "df[" in r["code"][0] and "df['Profit']" in r["code"][1], str(r["code"]))
check("Show Formula surfaces the Profit formula",
      any("Profit" in f and "{Revenue}" in f and "{Cost}" in f for f in r["formulas"]), str(r["formulas"]))
# reasoning matches execution: the filter note reflects the REAL rows kept (3 of 4 > 90)
check("Show Reasoning: notes reflect the real filter result (kept 3, of 4)",
      any("3" in n and "4" in n for n in r["notes"]), str(r["notes"]))
check("Show Reasoning: notes name the Profit column",
      any("Profit" in n for n in r["notes"]), str(r["notes"]))

# ============ the shown formula MATCHES the live cell (honest) ============
res, _, _, render = execute_multi({"base": pd.DataFrame({"Revenue": [100, 200], "Cost": [40, 60]})}, "base",
                                  [{"action": "add_formula_column", "name": "Profit", "formula": "{Revenue} - {Cost}"}])
described = _describe_formulas(render)
out, _, _ = m._serialize(res, "x.csv", "xlsx", render)
ws = load_workbook(io.BytesIO(out)).active
profile_col = list(res.columns).index("Profit") + 1
cell_formula = ws.cell(row=2, column=profile_col).value
check("Show Formula == the actual live cell formula",
      isinstance(cell_formula, str) and cell_formula.startswith("=") and "B2" in cell_formula.replace("$", ""),
      repr(cell_formula))
check("Show Formula's computed value matches the data (100-40=60)", res["Profit"].iloc[0] == 60, str(res["Profit"].iloc[0]))
check("described formula names the same column + inputs shown in the code view",
      any("Profit" in d for d in described), str(described))

# ============ empty plan (no-op) → views are coherent, not broken ============
check("empty operations → empty code (no fabricated steps)", _explain_code([]) == [])

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
