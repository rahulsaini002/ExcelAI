"""Feature 1.12 — Find & replace. Verifies 1.12-a..e + acceptance criteria and edges.

Run from backend:  .venv\\Scripts\\python.exe test_1_12.py
"""
from __future__ import annotations

import pandas as pd

from app.executor import OperationError, execute_plan

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def run(op, df):
    out, notes, _ = execute_plan(df, [op])
    return out, (notes[0] if notes else "")


def fr(**kw):
    base = {"action": "find_replace"}
    base.update(kw)
    return base


print("FEATURE 1.12 — find & replace\n")

# --------------------------------------------------------------------------- #
# 1.12-a  Simple replace in a column -> all matches replaced; count reported
# --------------------------------------------------------------------------- #
df = pd.DataFrame({"Country": ["U.S.A.", "India", "U.S.A.", "UK"]})
out, note = run(fr(find="U.S.A.", replace="USA", column="Country"), df)
check("1.12-a replaced all matches", list(out["Country"]) == ["USA", "India", "USA", "UK"], str(list(out["Country"])))
check("1.12-a count reported", "2 cells" in note, note)
# the dots in the search value are literal, not regex wildcards
out2, _ = run(fr(find="U.S.A.", replace="USA", column="Country"), pd.DataFrame({"Country": ["UXSXAX"]}))
check("1.12-a search is literal (no regex)", out2["Country"].iloc[0] == "UXSXAX", str(out2["Country"].iloc[0]))

# --------------------------------------------------------------------------- #
# 1.12-b  Case-insensitive replace (default) -> mumbai/Mumbai both replaced
# --------------------------------------------------------------------------- #
df = pd.DataFrame({"City": ["Mumbai", "mumbai", "MUMBAI", "Delhi"]})
out, note = run(fr(find="mumbai", replace="Mumbai", column="City"), df)
check("1.12-b case-insensitive default", list(out["City"]) == ["Mumbai", "Mumbai", "Mumbai", "Delhi"], str(list(out["City"])))
check("1.12-b count = 3", "3 cells" in note, note)
# match_case=True -> only the exact-case occurrence is replaced
out, _ = run(fr(find="mumbai", replace="Mumbai", column="City", match_case=True), df)
check("1.12-b match_case respects case", list(out["City"]) == ["Mumbai", "Mumbai", "MUMBAI", "Delhi"], str(list(out["City"])))

# --------------------------------------------------------------------------- #
# 1.12-c  Whole-cell match only -> partial matches left alone
# --------------------------------------------------------------------------- #
df = pd.DataFrame({"City": ["Mumbai", "Navi Mumbai", "Mumbai Central"]})
out, note = run(fr(find="Mumbai", replace="MUM", column="City", whole_cell=True), df)
check("1.12-c whole-cell only exact", list(out["City"]) == ["MUM", "Navi Mumbai", "Mumbai Central"], str(list(out["City"])))
check("1.12-c whole-cell count = 1", "1 cell" in note and "1 cells" not in note, note)
# whole-cell ignores surrounding whitespace ("Mumbai " == "Mumbai")
out, _ = run(fr(find="Mumbai", replace="MUM", column="City", whole_cell=True),
             pd.DataFrame({"City": ["Mumbai ", " mumbai", "Mumbai"]}))
check("1.12-c whole-cell trims + case-insensitive", list(out["City"]) == ["MUM", "MUM", "MUM"], str(list(out["City"])))

# partial (default) replaces the substring within a larger cell
out, _ = run(fr(find="Mumbai", replace="MUM", column="City"),
             pd.DataFrame({"City": ["Navi Mumbai"]}))
check("1.12-c partial replaces substring", out["City"].iloc[0] == "Navi MUM", str(out["City"].iloc[0]))

# --------------------------------------------------------------------------- #
# 1.12-d  Search value not found -> "no matches" message, data unchanged
# --------------------------------------------------------------------------- #
df = pd.DataFrame({"City": ["Delhi", "Pune"]})
out, note = run(fr(find="Mumbai", replace="MUM", column="City"), df)
check("1.12-d no-match message", "No cells matched" in note and "Mumbai" in note, note)
check("1.12-d data unchanged", list(out["City"]) == ["Delhi", "Pune"], str(list(out["City"])))

# --------------------------------------------------------------------------- #
# 1.12-e  Replace creating duplicates -> works; duplicates remain (no auto-dedupe)
# --------------------------------------------------------------------------- #
df = pd.DataFrame({"City": ["Bombay", "Mumbai", "Bombay"]})
out, note = run(fr(find="Bombay", replace="Mumbai", column="City"), df)
check("1.12-e replace works", list(out["City"]) == ["Mumbai", "Mumbai", "Mumbai"], str(list(out["City"])))
check("1.12-e duplicates remain", len(out) == 3, str(len(out)))
check("1.12-e count = 2", "2 cells" in note, note)

# --------------------------------------------------------------------------- #
# Acceptance / edges
# --------------------------------------------------------------------------- #

# whole-sheet scope (no column): replaces across text columns, leaves numbers alone
df = pd.DataFrame({"A": ["x", "y"], "B": ["x", "z"], "Num": [1, 2]})
out, note = run(fr(find="x", replace="X"), df)
check("edge: whole-sheet hits all text columns", list(out["A"]) == ["X", "y"] and list(out["B"]) == ["X", "z"], str(out.to_dict("list")))
check("edge: whole-sheet count = 2", "2 cells" in note, note)
# a numeric column isn't mangled by a whole-sheet replace
out, _ = run(fr(find="1", replace="9"), df)
check("edge: numeric column untouched in whole-sheet", list(out["Num"]) == [1, 2], str(list(out["Num"])))

# replace defaults to "" -> deletes the found text
out, _ = run(fr(find="-", column="Phone"), pd.DataFrame({"Phone": ["98-76", "12-34"]}))
check("edge: empty replace deletes", list(out["Phone"]) == ["9876", "1234"], str(list(out["Phone"])))

# blanks are left as blanks (not turned into "nan")
out, _ = run(fr(find="x", replace="y", column="C"), pd.DataFrame({"C": ["x", None]}))
check("edge: blanks stay blank", out["C"].iloc[0] == "y" and pd.isna(out["C"].iloc[1]), str(list(out["C"])))

# multiple occurrences in one cell -> all replaced, counts as one cell
out, note = run(fr(find="a", replace="A", column="C"), pd.DataFrame({"C": ["banana"]}))
check("edge: all occurrences in a cell replaced", out["C"].iloc[0] == "bAnAnA", str(out["C"].iloc[0]))
check("edge: cell counted once", "1 cell" in note, note)

# empty find -> friendly error
try:
    run(fr(find="", replace="Y", column="C"), pd.DataFrame({"C": ["x"]}))
    check("edge: empty find caught", False, "no error")
except OperationError as e:
    check("edge: empty find friendly", "find" in str(e).lower(), str(e))

# non-existent column -> friendly error
try:
    run(fr(find="x", replace="y", column="Nope"), pd.DataFrame({"C": ["x"]}))
    check("edge: bad column caught", False, "no error")
except OperationError as e:
    check("edge: bad column friendly", "Nope" in str(e), str(e))

# replacement text with regex-special chars is treated literally
out, _ = run(fr(find="cost", replace="$100", column="C"), pd.DataFrame({"C": ["cost"]}))
check("edge: literal replacement text", out["C"].iloc[0] == "$100", str(out["C"].iloc[0]))

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
