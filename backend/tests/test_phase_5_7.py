"""ENGINE PHASE 5.7 — self-healing workbooks (BUILD).

Detects formula references broken by a dropped/renamed column (via the Phase-4.8 formula
registry) and repairs them — remapping a renamed reference, or RESTORING a dropped column
from Phase-3.2 version history. This suite proves:

  detect        broken_references finds the missing referenced columns.
  remap         a renamed column ("Revenue"→"Revenues") is fuzzy-matched and the formula
                rewritten to point at it.
  restore       a dropped column with no fuzzy match is restored from the newest prior
                version that still had it ("repair via version history" — the DoD headline).
  unrepairable  no match + not in history → reported honestly, never guessed.
  end to end    /heal diagnoses, /heal/apply fixes and recomputes, and the workbook comes
                back healthy.

No llm.py change → no schema/serving/quota risk; no battery rows (endpoint/mechanism).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_5_7.py
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

_fd, _db = tempfile.mkstemp(suffix="-p57.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import selfheal as S  # noqa: E402
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


print("ENGINE PHASE 5.7 — self-healing workbooks\n")

# ===================== module: detect / remap / restore / unrepairable =====================
F = {"Margin": "{Revenue} - {Cost}", "Tax": "{Cost} * 0.1"}
check("broken_references finds the missing ref (Revenue), not the intact one (Cost)",
      S.broken_references(F, ["Revenues", "Cost", "Margin", "Tax"]) == {"Margin": ["Revenue"]}, str(S.broken_references(F, ["Revenues", "Cost", "Margin", "Tax"])))
check("a fully-intact workbook has no broken references",
      S.broken_references(F, ["Revenue", "Cost", "Margin", "Tax"]) == {}, "")

rem = S.plan_repairs(F, ["Revenues", "Cost", "Margin", "Tax"])
check("plan proposes a REMAP to the fuzzy-matched renamed column",
      rem == [{"formula_column": "Margin", "missing": "Revenue", "action": "remap", "to": "Revenues"}], str(rem))
new_f, changed = S.apply_remaps(F, rem)
check("apply_remaps rewrites the formula to the new column", new_f["Margin"] == "{Revenues} - {Cost}" and changed == ["Margin"], str((new_f, changed)))

# restore: Price dropped, no fuzzy match, but present in a prior version
FR = {"Rev": "{Price} * {Qty}"}
hist = [(1, {"Price", "Qty", "Cost", "Rev"}), (0, {"Price", "Qty", "Cost"})]
rst = S.plan_repairs(FR, ["Qty", "Cost", "Rev"], hist)
check("plan proposes a RESTORE from the newest version that had the column",
      rst == [{"formula_column": "Rev", "missing": "Price", "action": "restore", "column": "Price", "from_version": 1}], str(rst))
check("no history → the same case is UNREPAIRABLE (never guessed)",
      S.plan_repairs(FR, ["Qty", "Cost", "Rev"], [])[0]["action"] == "unrepairable", str(S.plan_repairs(FR, ["Qty", "Cost", "Rev"], [])))

# ===================== END TO END — REMAP via /heal =====================
c.post("/inspect", data={"session_id": "hr"}, files=[("files", ("d.csv", b"Revenue,Cost\n100,40\n200,60\n", "text/csv"))])
c.post("/execute", data={"session_id": "hr", "plan": json.dumps({"operations": [{"action": "add_formula_column", "name": "Margin", "formula": "{Revenue} - {Cost}"}]})})
# rename Revenue → Revenues: Margin's formula reference is now broken
c.post("/execute", data={"session_id": "hr", "plan": json.dumps({"operations": [{"action": "rename_columns", "rename_from": ["Revenue"], "rename_to": ["Revenues"]}]})})

diag = c.post("/heal", data={"session_id": "hr"}).json()
check("/heal detects the workbook is unhealthy", diag.get("healthy") is False and "Margin" in diag["broken_references"], str(diag)[:200])
check("/heal proposes remapping Revenue → Revenues",
      any(r["action"] == "remap" and r["to"] == "Revenues" for r in diag["repairs"]), str(diag["repairs"]))

applied = c.post("/heal/apply", data={"session_id": "hr"}).json()
check("/heal/apply reports the remap as healed", any(h["action"] == "remap" and h.get("result") == "remapped" for h in applied["healed"]), str(applied)[:200])
check("/heal/apply leaves NO remaining broken references", applied.get("remaining_broken") == {}, str(applied.get("remaining_broken")))
# re-diagnose confirms healthy
check("/heal now reports healthy after the fix", c.post("/heal", data={"session_id": "hr"}).json().get("healthy") is True, "")

# ===================== END TO END — RESTORE from version history =====================
c.post("/inspect", data={"session_id": "hs"}, files=[("files", ("d.csv", b"Price,Qty,Cost\n10,2,5\n20,3,8\n", "text/csv"))])
c.post("/execute", data={"session_id": "hs", "plan": json.dumps({"operations": [{"action": "add_formula_column", "name": "Rev", "formula": "{Price} * {Qty}"}]})})
c.post("/execute", data={"session_id": "hs", "plan": json.dumps({"operations": [{"action": "drop_columns", "columns": ["Price"]}]})})

diag = c.post("/heal", data={"session_id": "hs"}).json()
check("/heal detects the dropped-column break (Rev → Price)",
      diag.get("healthy") is False and diag["broken_references"].get("Rev") == ["Price"], str(diag)[:200])
check("/heal proposes RESTORE from history for the dropped column",
      any(r["action"] == "restore" and r["column"] == "Price" for r in diag["repairs"]), str(diag["repairs"]))

applied = c.post("/heal/apply", data={"session_id": "hs"}).json()
check("/heal/apply restores the dropped column from history", any(h["action"] == "restore" and h.get("result") == "restored" for h in applied["healed"]), str(applied)[:200])
cols = [col["name"] if isinstance(col, dict) else col for col in applied["preview"][0]["columns"]]
check("the restored column (Price) is back in the workbook", "Price" in cols, str(cols))
check("/heal/apply leaves no remaining broken references", applied.get("remaining_broken") == {}, str(applied.get("remaining_broken")))

# ===================== healthy workbook =====================
c.post("/inspect", data={"session_id": "hh"}, files=[("files", ("d.csv", b"A,B\n1,2\n3,4\n", "text/csv"))])
c.post("/execute", data={"session_id": "hh", "plan": json.dumps({"operations": [{"action": "add_formula_column", "name": "S", "formula": "{A} + {B}"}]})})
hh = c.post("/heal", data={"session_id": "hh"}).json()
check("a healthy workbook reports healthy with no repairs", hh.get("healthy") is True and hh["repairs"] == [], str(hh)[:160])

m._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
