"""Feature 1.4 — Sort. Verifies every acceptance criterion + test case (1.4-a..g)
plus edge cases. No API key needed.

Run from backend:  .venv\\Scripts\\python.exe test_1_4.py
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


def sort(df, columns, orders=None):
    op = {"action": "sort", "columns": columns}
    if orders:
        op["orders"] = orders
    return execute_plan(df, [op])[0]


print("FEATURE 1.4 — sort\n")

# 1.4-a numbers ascending (real numeric dtype) — numeric order, not string order
out = sort(pd.DataFrame({"n": [10, 2, 1]}), ["n"])
check("1.4-a numbers ascending numeric", list(out["n"]) == [1, 2, 10], str(list(out["n"])))

# 1.4-a numbers stored as TEXT still sort numerically (not "1,10,2")
out = sort(pd.DataFrame({"n": ["10", "2", "1"]}), ["n"])
check("1.4-a numbers-as-text sort numerically", list(out["n"]) == ["1", "2", "10"], str(list(out["n"])))

# 1.4-b descending — largest first
out = sort(pd.DataFrame({"n": [1, 3, 2]}), ["n"], ["desc"])
check("1.4-b descending", list(out["n"]) == [3, 2, 1], str(list(out["n"])))

# 1.4-c dates (datetime dtype) chronological
out = sort(pd.DataFrame({"d": pd.to_datetime(["2022-05-01", "2020-01-01", "2021-03-03"])}), ["d"])
check("1.4-c datetime chronological", list(out["d"].dt.year) == [2020, 2021, 2022], str(list(out["d"].dt.year)))

# 1.4-c dates stored as TEXT, mixed display formats -> still chronological
out = sort(pd.DataFrame({"d": ["2022-01-31", "2020-12-01", "2021-06-15"]}), ["d"])
check("1.4-c text dates chronological", list(out["d"]) == ["2020-12-01", "2021-06-15", "2022-01-31"], str(list(out["d"])))

# 1.4-d text alphabetical, case-insensitive
out = sort(pd.DataFrame({"t": ["banana", "Apple", "cherry", "Date"]}), ["t"])
check("1.4-d text case-insensitive asc", list(out["t"]) == ["Apple", "banana", "cherry", "Date"], str(list(out["t"])))
out = sort(pd.DataFrame({"t": ["banana", "apple", "Cherry"]}), ["t"])
check("1.4-d case-insensitive (mixed case)", list(out["t"]) == ["apple", "banana", "Cherry"], str(list(out["t"])))

# 1.4-e secondary sort: Region asc, then Price desc within region
df = pd.DataFrame({"Region": ["S", "N", "N", "S"], "Price": [10, 5, 20, 30]})
out = sort(df, ["Region", "Price"], ["asc", "desc"])
check("1.4-e secondary sort", list(zip(out["Region"], out["Price"])) == [("N", 20), ("N", 5), ("S", 30), ("S", 10)], str(list(zip(out["Region"], out["Price"]))))

# 1.4-f blanks grouped at the end (ascending AND descending)
out = sort(pd.DataFrame({"n": [3, None, 1, None, 2]}), ["n"])
check("1.4-f blanks to end (asc)", pd.isna(out["n"].iloc[-1]) and pd.isna(out["n"].iloc[-2]) and list(out["n"].iloc[:3]) == [1, 2, 3], str(list(out["n"])))
out = sort(pd.DataFrame({"t": ["b", None, "a"]}), ["t"], ["desc"])
check("1.4-f blanks to end (desc)", pd.isna(out["t"].iloc[-1]) and list(out["t"].iloc[:2]) == ["b", "a"], str(list(out["t"])))

# 1.4-g non-existent column -> caught (validation)
try:
    sort(pd.DataFrame({"n": [1]}), ["Ghost"])
    check("1.4-g non-existent column caught", False, "no error")
except OperationError as e:
    check("1.4-g non-existent column caught", "Ghost" in str(e))

# edge: mixed numbers+text column -> sorts as text, no crash
out = sort(pd.DataFrame({"v": [3, "apple", 1]}), ["v"])
check("edge: mixed column sorts without crash", len(out) == 3)

# edge: stable sort keeps original order for equal keys
df = pd.DataFrame({"k": [1, 1, 1], "id": ["a", "b", "c"]})
out = sort(df, ["k"])
check("edge: stable sort", list(out["id"]) == ["a", "b", "c"], str(list(out["id"])))

# edge: sort needs at least one column
try:
    execute_plan(pd.DataFrame({"n": [1]}), [{"action": "sort", "columns": []}])
    check("edge: empty columns errors", False, "no error")
except OperationError:
    check("edge: empty columns errors", True)

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
