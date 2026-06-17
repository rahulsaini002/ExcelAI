"""Feature 1.6 — Remove duplicates. Verifies acceptance criteria + 1.6-a..f + edges.
Run from backend:  .venv\\Scripts\\python.exe test_1_6.py
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


def dedup(df, columns=None):
    op = {"action": "remove_duplicates"}
    if columns is not None:
        op["columns"] = columns
    d, notes, _ = execute_plan(df, [op])
    return d, notes[0]


print("FEATURE 1.6 — remove duplicates\n")

# 1.6-a exact duplicate rows -> removed, count reported
out, note = dedup(pd.DataFrame({"a": [1, 1, 2], "b": ["x", "x", "y"]}))
check("1.6-a exact dups removed", len(out) == 2, str(len(out)))
check("1.6-a count reported", "Removed 1 duplicate row" in note, note)

# 1.6-b duplicates by one key column (Email) -> collapsed to one, keep first
df = pd.DataFrame({"Email": ["a@x.com", "a@x.com", "b@x.com"], "Name": ["First", "Second", "Bob"]})
out, note = dedup(df, ["Email"])
check("1.6-b by key column", len(out) == 2 and list(out["Email"]) == ["a@x.com", "b@x.com"])
check("1.6-b kept the FIRST occurrence", out[out["Email"] == "a@x.com"]["Name"].iloc[0] == "First", str(list(out["Name"])))

# 1.6-c no duplicates present -> nothing removed, message says so
out, note = dedup(pd.DataFrame({"Email": ["a", "b", "c"]}), ["Email"])
check("1.6-c nothing removed", len(out) == 3)
check("1.6-c message says none", "No duplicate rows found" in note, note)

# 1.6-d case/space differences treated as same (default)
out, _ = dedup(pd.DataFrame({"Email": ["a@x.com ", "A@X.com", " A@x.COM "]}), ["Email"])
check("1.6-d case/space collapsed", len(out) == 1, str(list(out["Email"])))

# 1.6-e all rows identical -> single row
out, _ = dedup(pd.DataFrame({"a": [1, 1, 1], "b": ["z", "z", "z"]}))
check("1.6-e all identical -> 1", len(out) == 1)

# 1.6-f duplicate key, different other data -> keep first, explained
df = pd.DataFrame({"Email": ["x@x.com", "x@x.com"], "Score": [10, 99]})
out, note = dedup(df, ["Email"])
check("1.6-f keep first of differing rows", len(out) == 1 and out["Score"].iloc[0] == 10, str(list(out["Score"])))
check("1.6-f kept-copy rule explained", "kept the first" in note.lower(), note)

# edge: dedup by MULTIPLE columns
df = pd.DataFrame({"A": ["p", "p", "p"], "B": ["q", "q", "r"]})
out, _ = dedup(df, ["A", "B"])
check("edge: multi-column dedup", len(out) == 2)

# edge: whole-row dedup with case/space in text treated same
out, _ = dedup(pd.DataFrame({"c": ["Hello ", "hello", "world"]}))
check("edge: whole-row case/space collapse", len(out) == 2, str(list(out["c"])))

# edge: numeric keys dedup exactly
out, _ = dedup(pd.DataFrame({"n": [1, 1, 2, 2, 2]}), ["n"])
check("edge: numeric dedup", len(out) == 2)

# edge: non-existent column -> caught
try:
    dedup(pd.DataFrame({"a": [1]}), ["Ghost"])
    check("edge: non-existent column caught", False, "no error")
except OperationError as e:
    check("edge: non-existent column caught", "Ghost" in str(e))

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
