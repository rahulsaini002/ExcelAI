"""ENGINE PHASE 3.4 — self-correction + full error taxonomy (NO AI).

The existing self-correction (test_self_correct) repairs #REF!/#VALUE!/#DIV/0! and
explains unrepairable cases. Phase 3.4 completes the Excel error taxonomy:
  * #NUM!  — invalid math (root of a negative, log of <=0, overflow) → blank ONLY the
            rows whose inputs were present (a blank propagating a blank is NOT an error),
            never a text result; honestly reported.
  * #NAME? — an unknown function is explained (labeled), never looped.
  * #N/A   — prevented: lookup writes "Not found", never leaves #N/A.
Repairs never loop, and combined errors are fixed in one pass.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_3_4.py
"""
from __future__ import annotations

import math
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

_fd, _db = tempfile.mkstemp(suffix="-p34.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402

from app.executor import execute_plan, OperationError  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def run(name, formula, df):
    out, notes, render = execute_plan(df.copy(), [{"action": "add_formula_column", "name": name, "formula": formula}])
    return out[name].tolist(), notes[0], render


print("ENGINE PHASE 3.4 — self-correction + full error taxonomy (no AI)\n")

# ============ #NUM! : invalid math ============
vals, note, _ = run("R", "SQRT({X})", pd.DataFrame({"X": [4, -9, 16, -1, 25]}))
check("#NUM!: square root of a negative is blanked (rows 2 & 4)",
      vals[0] == 2 and math.isnan(vals[1]) and vals[2] == 4 and math.isnan(vals[3]) and vals[4] == 5, str(vals))
check("#NUM!: valid rows still computed correctly", vals[0] == 2 and vals[4] == 5, str(vals))
check("#NUM!: note reports the repair with a count (2 rows)",
      "#NUM!" in note and "2 row" in note, note)

# honesty guards ----------------------------------------------------------------
# a BLANK input propagates a blank — that's NOT a #NUM! error
vb, nb, _ = run("S", "{A} + {B}", pd.DataFrame({"A": [1, None, 3], "B": [10, 20, 30]}))
check("#NUM! honesty: a blank input is NOT flagged as an error",
      vb[0] == 11 and math.isnan(vb[1]) and vb[2] == 33 and "#NUM!" not in nb, str(vb) + " | " + nb)
# a TEXT-valued formula must never be flagged as #NUM!
vt, nt, _ = run("T", 'IF({A} > 0, "pos", "neg")', pd.DataFrame({"A": [1, -2, 3]}))
check("#NUM! honesty: a text result is NOT flagged", vt == ["pos", "neg", "pos"] and "#NUM!" not in nt, str(vt))
# a clean numeric formula gets no #NUM! note
vc, nc, _ = run("M", "{A} - {B}", pd.DataFrame({"A": [100, 200], "B": [40, 60]}))
check("#NUM! honesty: a clean formula has no #NUM! note", vc == [60, 140] and "#NUM!" not in nc, nc)

# ============ combined errors repaired in one pass (no loop) ============
# a typo'd column (#REF!) AND a root-of-negative (#NUM!) in one formula
vcomb, ncomb, _ = run("C", "SQRT({Valu})", pd.DataFrame({"Value": [9, -4, 16]}))  # Valu → Value
check("combined #REF! + #NUM!: ref fixed AND bad row blanked",
      vcomb[0] == 3 and math.isnan(vcomb[1]) and vcomb[2] == 4, str(vcomb))
check("combined: note reports BOTH #REF! and #NUM!",
      "#REF!" in ncomb and "#NUM!" in ncomb, ncomb)

# ============ #NAME? : unknown function explained (labeled, not looped) ============
try:
    run("Z", "NOTAFUNC({X})", pd.DataFrame({"X": [1, 2]}))
    check("#NAME?: unknown function raises", False, "no error")
except OperationError as e:
    check("#NAME?: unknown function is labeled and explained",
          "#NAME?" in str(e) and "NOTAFUNC" in str(e), str(e))

# a known-but-unsupported function redirects honestly
try:
    run("V", "VLOOKUP({X})", pd.DataFrame({"X": [1, 2]}))
    check("VLOOKUP redirect raises", False, "no error")
except OperationError as e:
    check("VLOOKUP is redirected (not a raw crash)", "VLOOKUP" in str(e) and "isn't generated here" in str(e), str(e))

# ============ #REF! unrepairable → explained, lists real columns, no loop ============
try:
    run("X", "{Nonexistent} * 2", pd.DataFrame({"Revenue": [1], "Units": [2]}))
    check("#REF! unrepairable raises", False, "no error")
except OperationError as e:
    check("#REF! unrepairable is explained + lists real columns",
          "#REF!" in str(e) and "Nonexistent" in str(e) and "Revenue" in str(e), str(e))

# ============ #N/A prevented: a lookup miss writes 'Not found', never #N/A ============
from app.executor import execute_multi  # noqa: E402
main_df = pd.DataFrame({"Key": ["a", "zzz"]})
src = pd.DataFrame({"K": ["a"], "V": ["hit"]})
res, _, _, _ = execute_multi({"m": main_df.copy(), "s": src.copy()}, "m",
                             [{"action": "lookup", "key_column": "Key", "source_sheet": "s",
                               "source_key_column": "K", "return_column": "V", "new_column": "V"}])
check("#N/A prevented: an unmatched lookup key reads 'Not found', not #N/A",
      res["V"].tolist() == ["hit", "Not found"], str(res["V"].tolist()))

# ============ existing repairs still work (regression touch) ============
vd, nd, rd = run("D", "{A} / {B}", pd.DataFrame({"A": [10, 20], "B": [2, 0]}))
check("#DIV/0! still repaired (row 2 blanked + guarded formula)",
      vd[0] == 5 and math.isnan(vd[1]) and "#DIV/0!" in nd and rd[0]["formula"].startswith("IF("), str(vd) + nd)
vv, nv, _ = run("E", "{P} * {Q}", pd.DataFrame({"P": ["10", "x", "30"], "Q": [2, 2, 2]}))
check("#VALUE! still repaired (text coerced, junk blanked)",
      vv[0] == 20 and math.isnan(vv[1]) and vv[2] == 60 and "#VALUE!" in nv, str(vv))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
