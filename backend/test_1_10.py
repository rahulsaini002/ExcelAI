"""Feature 1.10 — Aggregate (sum / average / count, with optional grouping).
Verifies 1.10-a..f + acceptance criteria and edges.

Run from backend:  .venv\\Scripts\\python.exe test_1_10.py
"""
from __future__ import annotations

import math

import numpy as np
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
    return out, notes[0]


def agg(**kw):
    base = {"action": "aggregate"}
    base.update(kw)
    return base


print("FEATURE 1.10 — aggregate (sum / average / count, optional grouping)\n")

# --------------------------------------------------------------------------- #
# 1.10-a  Sum a column -> correct total
# --------------------------------------------------------------------------- #
# A scalar aggregate now ANSWERS via the note and KEEPS the data (so filter+average
# still hands back the rows). Grouped aggregates still produce a summary table.
df = pd.DataFrame({"Revenue": [100, 200, 50]})
out, note = run(agg(agg_func="sum", agg_column="Revenue"), df)
check("1.10-a sum value in note", ": 350" in note and "sum" in note, note)
check("1.10-a data preserved (not collapsed to 1 cell)", list(out.columns) == ["Revenue"] and len(out) == 3, str(out.shape))

# numbers stored as TEXT still sum numerically
out, note = run(agg(agg_func="sum", agg_column="Revenue"), pd.DataFrame({"Revenue": ["100", "200", "50"]}))
check("1.10-a sum of numbers-as-text", ": 350" in note, note)

# the user's case: filter THEN average -> rows kept, average reported in the note
out, notes, _ = execute_plan(
    pd.DataFrame({"Class": [1, 0, 1, 1, 0], "Amount": [100, 200, 300, 500, 50]}),
    [{"action": "filter", "conditions": [{"column": "Class", "operator": "equals", "value": "1"}]},
     {"action": "aggregate", "agg_func": "average", "agg_column": "Amount"}],
)
check("filter+average keeps the filtered rows", len(out) == 3 and list(out["Amount"]) == [100, 300, 500], str(out.shape))
check("filter+average reports the average in a note", ": 300" in notes[-1], str(notes))

# --------------------------------------------------------------------------- #
# 1.10-b  Average ignoring blanks -> correct mean of non-blank values, says so
# --------------------------------------------------------------------------- #
df = pd.DataFrame({"Score": [10, None, 20, np.nan, 30]})
out, note = run(agg(agg_func="average", agg_column="Score"), df)
check("1.10-b mean ignores blanks", ": 20" in note, note)
check("1.10-b data preserved", len(out) == 5, str(len(out)))
check("1.10-b uses word 'average'", "average" in note, note)
check("1.10-b says it ignored blanks", "ignored 2" in note and "blank" in note, note)
# "avg" alias also works
out, note = run(agg(agg_func="avg", agg_column="Score"), df)
check("1.10-b avg alias", ": 20" in note, note)

# --------------------------------------------------------------------------- #
# 1.10-c  Count occurrences of a value -> correct count
# --------------------------------------------------------------------------- #
df = pd.DataFrame({"Response": ["Yes", "No", "Yes", "yes", "No", " YES "]})
out, note = run(agg(agg_func="count", agg_column="Response", count_value="Yes"), df)
check("1.10-c count of value (case/space-insensitive)", ": 4" in note, note)
check("1.10-c data preserved", len(out) == 6, str(len(out)))
check("1.10-c note mentions value", "Yes" in note and "4" in note, note)
# a value that never occurs -> 0, no crash
out, note = run(agg(agg_func="count", agg_column="Response", count_value="Maybe"), df)
check("1.10-c missing value -> 0", ": 0" in note, note)

# plain count (non-blank cells) + says how many blanks ignored
df_b = pd.DataFrame({"Email": ["a@x", None, "b@x", ""]})
out, note = run(agg(agg_func="count", agg_column="Email"), df_b)
check("1.10-c plain count of non-blank", ": 2" in note, note)
check("1.10-c plain count notes blanks", "ignored 2" in note, note)

# count with no column = total row count
out, note = run(agg(agg_func="count"), df_b)
check("1.10-c count all rows", ": 4" in note, note)

# --------------------------------------------------------------------------- #
# 1.10-d  Sum grouped by category -> per-group totals in a summary table
# --------------------------------------------------------------------------- #
df = pd.DataFrame({
    "Region": ["North", "South", "North", "South", "North"],
    "Revenue": [100, 50, 200, 25, 50],
})
out, note = run(agg(agg_func="sum", agg_column="Revenue", group_by=["Region"]), df)
totals = dict(zip(out["Region"], out["sum_of_Revenue"]))
check("1.10-d per-group totals", totals == {"North": 350, "South": 75}, str(totals))
check("1.10-d summary table shape", out.shape == (2, 2), str(out.shape))
check("1.10-d note says grouped", "grouped by Region" in note and "2 groups" in note, note)

# average grouped
out, _ = run(agg(agg_func="average", agg_column="Revenue", group_by=["Region"]), df)
avgs = dict(zip(out["Region"], out["average_of_Revenue"]))
check("1.10-d grouped average", avgs["North"] == 350 / 3 and avgs["South"] == 37.5, str(avgs))

# count grouped (size per group)
out, _ = run(agg(agg_func="count", group_by=["Region"]), df)
counts = dict(zip(out["Region"], out["count"]))
check("1.10-d grouped count", counts == {"North": 3, "South": 2}, str(counts))

# count of a specific value, grouped
df2 = pd.DataFrame({
    "Region": ["N", "S", "N", "S"],
    "Response": ["Yes", "Yes", "No", "Yes"],
})
out, _ = run(agg(agg_func="count", agg_column="Response", count_value="Yes", group_by=["Region"]), df2)
yc = dict(zip(out["Region"], out["count_of_Yes"]))
check("1.10-d grouped count of value", yc == {"N": 1, "S": 2}, str(yc))

# --------------------------------------------------------------------------- #
# 1.10-e  Group column has blanks -> blank group handled and labeled
# --------------------------------------------------------------------------- #
df = pd.DataFrame({
    "Region": ["North", None, "North", np.nan],
    "Revenue": [10, 20, 30, 40],
})
out, _ = run(agg(agg_func="sum", agg_column="Revenue", group_by=["Region"]), df)
totals = dict(zip(out["Region"], out["sum_of_Revenue"]))
check("1.10-e blank group labeled '(blank)'", "(blank)" in totals, str(totals))
check("1.10-e blank group total correct", totals.get("(blank)") == 60, str(totals))
check("1.10-e named group total correct", totals.get("North") == 40, str(totals))

# --------------------------------------------------------------------------- #
# 1.10-f  Aggregate a text column with sum -> friendly error
# --------------------------------------------------------------------------- #
try:
    run(agg(agg_func="sum", agg_column="Name"), pd.DataFrame({"Name": ["Asha", "Rohan"]}))
    check("1.10-f text sum caught", False, "no error")
except OperationError as e:
    check("1.10-f friendly 'can't sum text'", "sum" in str(e).lower() and "text" in str(e).lower() and "Name" in str(e), str(e))

# average of text also refused
try:
    run(agg(agg_func="average", agg_column="Name"), pd.DataFrame({"Name": ["Asha", "Rohan"]}))
    check("1.10-f text average caught", False, "no error")
except OperationError as e:
    check("1.10-f friendly average error", "average" in str(e).lower() and "Name" in str(e), str(e))

# --------------------------------------------------------------------------- #
# Acceptance / edges
# --------------------------------------------------------------------------- #

# min / max (bonus funcs)
df = pd.DataFrame({"V": [3, 9, 1, 7]})
out, note = run(agg(agg_func="min", agg_column="V"), df)
check("edge: min", ": 1" in note, note)
out, note = run(agg(agg_func="max", agg_column="V"), df)
check("edge: max", ": 9" in note, note)

# unknown function -> friendly error
try:
    run(agg(agg_func="median", agg_column="V"), df)
    check("edge: unknown func caught", False, "no error")
except OperationError as e:
    check("edge: unknown func friendly", "median" in str(e), str(e))

# sum without a column -> asks which column
try:
    run(agg(agg_func="sum"), df)
    check("edge: sum needs a column", False, "no error")
except OperationError as e:
    check("edge: sum-needs-column friendly", "average" not in str(e).lower() and "column" in str(e).lower(), str(e))

# count_value without a column -> friendly error
try:
    run(agg(agg_func="count", count_value="Yes"), df)
    check("edge: count_value needs column", False, "no error")
except OperationError as e:
    check("edge: count_value-needs-column friendly", "column" in str(e).lower(), str(e))

# group_by a non-existent column -> friendly error
try:
    run(agg(agg_func="sum", agg_column="V", group_by=["Nope"]), df)
    check("edge: bad group column caught", False, "no error")
except OperationError as e:
    check("edge: bad group column friendly", "Nope" in str(e), str(e))

# count_value matching numbers stored as text vs numeric
df = pd.DataFrame({"Code": [123, 456, 123]})
out, note = run(agg(agg_func="count", agg_column="Code", count_value="123"), df)
check("edge: count_value number==text", ": 2" in note, note)

# no blanks -> note does NOT claim it ignored any
out, note = run(agg(agg_func="sum", agg_column="V"), pd.DataFrame({"V": [1, 2, 3]}))
check("edge: no-blank note clean", "ignored" not in note, note)

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
