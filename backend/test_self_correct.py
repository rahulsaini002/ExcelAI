"""Phase 3.6 — Self-correction loop for generated formulas.

When a formula would yield an Excel error we detect the class and auto-repair:
  SC-a  #REF!   — a referenced column doesn't exist  -> remap to the closest real column
  SC-b  #VALUE! — arithmetic hits text                -> coerce numbers-from-text
  SC-c  #DIV/0! — division by a zero denominator      -> blank those rows + guard the saved formula
  SC-d  Unrepairable cases EXPLAIN (and never loop forever).
  SC-e  End-to-end through the API (mocked LLM).

Run from backend:  .venv\\Scripts\\python.exe test_self_correct.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import math

import pandas as pd
from fastapi.testclient import TestClient

from app import main
from app.executor import OperationError, execute_plan

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def run(op, df):
    return execute_plan(df, [op])


def fcol(name, formula, **extra):
    return {"action": "add_formula_column", "name": name, "formula": formula, **extra}


print("PHASE 3.6 — SELF-CORRECTION LOOP\n")

# =========================================================================
# SC-a  #REF! — referenced column doesn't exist -> remap to closest real one
# =========================================================================
print("SC-a  #REF! detection + repair")

DF = pd.DataFrame({"Revenue": [100, 200, 300], "Cost": [40, 60, 90], "Units": [10, 0, 5]})

# (1) typo in the column name
out, notes, render = run(fcol("Profit", "{Revenu} - {Cost}"), DF)  # "Revenu" -> "Revenue"
check("SC-a typo remapped to real column", list(out["Profit"]) == [60, 140, 210], str(list(out["Profit"])))
check("SC-a note reports the #REF! repair", "#REF!" in notes[0] and "Revenue" in notes[0], notes[0])
check("SC-a saved formula uses the corrected name", "{Revenue}" in render[0]["formula"], render[0]["formula"])

# (2) case / spacing differences are matched without a 'typo'
DF2 = pd.DataFrame({"Customer Name": ["a", "b"], "Order Total": [10, 20]})
out2, notes2, _ = run(fcol("Doubled", "{order total} * 2"), DF2)  # case-insensitive match
check("SC-a case-insensitive ref matched", list(out2["Doubled"]) == [20, 40], str(list(out2["Doubled"])))

# (3) a correct formula is NOT 'repaired' (no false positives)
out3, notes3, _ = run(fcol("Margin", "{Revenue} - {Cost}"), DF)
check("SC-a clean formula has no auto-correction note", "Auto-corrected" not in notes3[0], notes3[0])

# =========================================================================
# SC-b  #VALUE! — arithmetic on text -> coerce numbers-from-text
# =========================================================================
print("\nSC-b  #VALUE! detection + repair")

# (1) plain arithmetic: a column holds numbers-as-text with one junk value
TXT = pd.DataFrame({"Price": ["10", "abc", "30"], "Qty": [2, 2, 2]})
out, notes, _ = run(fcol("Line", "{Price} * {Qty}"), TXT)
vals = list(out["Line"])
check("SC-b coerced numeric rows correct", vals[0] == 20 and vals[2] == 60, str(vals))
check("SC-b non-numeric row blanked", pd.isna(vals[1]), str(vals))
check("SC-b note reports the #VALUE! repair", "#VALUE!" in notes[0], notes[0])

# (2) advanced formula (a function) that hits text -> also coerced
out2, notes2, _ = run(fcol("R", "ROUND({Price} * 2, 0)"), TXT)
r = list(out2["R"])
check("SC-b advanced #VALUE! coerced", r[0] == 20 and r[2] == 60 and pd.isna(r[1]), str(r))
check("SC-b advanced note mentions #VALUE!", "#VALUE!" in notes2[0], notes2[0])

# =========================================================================
# SC-c  #DIV/0! — division by zero -> blank rows + guard the saved formula
# =========================================================================
print("\nSC-c  #DIV/0! detection + repair")

out, notes, render = run(fcol("PerUnit", "{Revenue} / {Units}"), DF)  # Units row 2 == 0
vals = list(out["PerUnit"])
check("SC-c valid rows computed", math.isclose(vals[0], 10.0) and math.isclose(vals[2], 60.0), str(vals))
check("SC-c divide-by-zero row blanked", pd.isna(vals[1]), str(vals))
check("SC-c note reports #DIV/0! repair", "#DIV/0!" in notes[0], notes[0])
# Saved formula is guarded so Excel won't show #DIV/0! either
guarded = render[0]["formula"]
check("SC-c saved formula is guarded with IF", guarded.startswith("IF(") and "=0" in guarded, guarded)
check("SC-c guard references the denominator", "{Units}" in guarded and "{Revenue}" in guarded, guarded)

# nested division (slash inside a function) still blanks values, just no IF-guard
out2, notes2, render2 = run(fcol("Rnd", "ROUND({Revenue} / {Units}, 1)"), DF)
check("SC-c nested div blanks the bad row", pd.isna(list(out2["Rnd"])[1]), str(list(out2["Rnd"])))
check("SC-c nested div still reports #DIV/0!", "#DIV/0!" in notes2[0], notes2[0])

# =========================================================================
# SC-d  Unrepairable -> EXPLAIN clearly (and do NOT loop)
# =========================================================================
print("\nSC-d  Unrepairable cases explained")

# (1) #REF! with no plausible match
try:
    run(fcol("X", "{Nonexistent} * 2"), DF)
    check("SC-d unknown column raises", False, "no error")
except OperationError as e:
    msg = str(e)
    check("SC-d unknown column is a #REF! explanation", "#REF!" in msg and "Nonexistent" in msg, msg)
    check("SC-d explanation lists real columns", "Revenue" in msg and "Units" in msg, msg)

# (2) #VALUE! with a column that has NO numbers at all
PURE_TEXT = pd.DataFrame({"Name": ["alpha", "beta", "gamma"]})
try:
    run(fcol("Y", "{Name} * 2"), PURE_TEXT)
    check("SC-d pure-text raises", False, "no error")
except OperationError as e:
    msg = str(e)
    check("SC-d pure-text is a #VALUE! explanation", "#VALUE!" in msg and "Name" in msg, msg)
    check("SC-d pure-text mentions it can't be converted", "convert" in msg.lower() or "numbers" in msg.lower(), msg)

# (3) an unsupported function is explained (not looped)
try:
    run(fcol("Z", "VLOOKUP({Revenue})"), DF)
    check("SC-d unsupported fn raises", False, "no error")
except OperationError as e:
    check("SC-d unsupported fn explained", "VLOOKUP" in str(e) or "supported" in str(e).lower(), str(e))

# =========================================================================
# SC-e  Combined errors in one formula are both repaired
# =========================================================================
print("\nSC-e  Multiple repairs in one pass")

# typo'd reference AND a divide-by-zero -> fix the ref, then blank the zero row
out, notes, render = run(fcol("Ratio", "{Revenu} / {Units}"), DF)  # Revenu->Revenue, Units has 0
vals = list(out["Ratio"])
check("SC-e ref fixed AND values computed", math.isclose(vals[0], 10.0) and math.isclose(vals[2], 60.0), str(vals))
check("SC-e div-by-zero row blanked", pd.isna(vals[1]), str(vals))
check("SC-e note reports BOTH repairs", "#REF!" in notes[0] and "#DIV/0!" in notes[0], notes[0])

# =========================================================================
# SC-f  End-to-end through the API (mocked LLM)
# =========================================================================
print("\nSC-f  API integration")

client = TestClient(main.app)
_orig = main.llm.parse_instruction
CSV = b"Revenue,Cost,Units\n100,40,10\n200,60,0\n300,90,5\n"

try:
    main.llm.parse_instruction = lambda i, s, h: {
        "operations": [fcol("Profit", "{Revenu} - {Cost}")],  # typo'd ref
        "title": "Add profit",
        "translation": "Add a Profit column",
        "confidence": 90,
    }
    r = client.post(
        "/process",
        data={"instruction": "add profit = revenu - cost", "session_id": "sc", "rewind": "-1", "history": ""},
        files=[("files", ("d.csv", CSV, "text/csv"))],
    )
    body = r.json()
    check("SC-f API status ok (auto-repaired)", r.status_code == 200 and body.get("status") == "ok", str(body)[:200])
    check("SC-f API explanation reports the repair", "#REF!" in (body.get("explanation") or ""), body.get("explanation"))
    # The corrected column actually exists in the preview
    cols = [c["name"] for c in body["preview"][0]["columns"]]
    check("SC-f corrected column present in result", "Profit" in cols, str(cols))
finally:
    main.llm.parse_instruction = _orig
    main._SESSIONS.clear()

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
