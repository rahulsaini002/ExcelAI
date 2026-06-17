"""Feature 1.5 — Filter. Verifies every operator, acceptance criterion and test
case (1.5-a..i) plus edges. No API key needed.

Run from backend:  .venv\\Scripts\\python.exe test_1_5.py
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


def filt(df, conditions, combine=None):
    op = {"action": "filter", "conditions": conditions}
    if combine:
        op["combine"] = combine
    d, notes, _ = execute_plan(df, [op])
    return d, notes[0]


def C(column, operator, value=None, value2=None):
    c = {"column": column, "operator": operator}
    if value is not None:
        c["value"] = value
    if value2 is not None:
        c["value2"] = value2
    return c


print("FEATURE 1.5 — filter\n")

df = pd.DataFrame({
    "Region": ["North", "South", "North", "", None],
    "Price": [100, 200, 50, 300, 150],
    "When": pd.to_datetime(["2021-01-01", "2022-06-01", "2020-03-03", "2023-01-01", "2019-05-05"]),
})

# 1.5-a text equals + count reported
out, note = filt(df, [C("Region", "equals", "North")])
check("1.5-a equals keeps matches", len(out) == 2 and set(out["Region"]) == {"North"}, str(list(out["Region"])))
check("1.5-a count reported", "Kept 2 of 5" in note, note)
# equals is case-insensitive
out, _ = filt(df, [C("Region", "equals", "north")])
check("equals case-insensitive", len(out) == 2)

# not_equals
out, _ = filt(df, [C("Region", "not_equals", "North")])
check("not_equals", len(out) == 3)

# 1.5-b number greater than / less than / >= / <=
check("1.5-b greater_than", len(filt(df, [C("Price", "greater_than", "100")])[0]) == 3)
check("less_than", len(filt(df, [C("Price", "less_than", "100")])[0]) == 1)
check("greater_or_equal", len(filt(df, [C("Price", "greater_or_equal", "150")])[0]) == 3)
check("less_or_equal", len(filt(df, [C("Price", "less_or_equal", "100")])[0]) == 2)

# between (numbers) and between (dates)
check("between numbers", len(filt(df, [C("Price", "between", "60", "160")])[0]) == 2)
check("between dates", len(filt(df, [C("When", "between", "2020-01-01", "2021-12-31")])[0]) == 2)

# 1.5-c date after a date
check("1.5-c date after", len(filt(df, [C("When", "greater_than", "2021-06-01")])[0]) == 2)

# 1.5-d contains / starts_with / ends_with
check("1.5-d contains", len(filt(df, [C("Region", "contains", "out")])[0]) == 1)
check("starts_with (case-insensitive)", len(filt(df, [C("Region", "starts_with", "no")])[0]) == 2)
check("ends_with", len(filt(df, [C("Region", "ends_with", "th")])[0]) == 3)

# 1.5-h is_blank / not_blank
check("1.5-h is_blank", len(filt(df, [C("Region", "is_blank")])[0]) == 2)
check("not_blank", len(filt(df, [C("Region", "not_blank")])[0]) == 3)

# 1.5-e two conditions AND
out, _ = filt(df, [C("Region", "equals", "North"), C("Price", "greater_or_equal", "100")], "and")
check("1.5-e AND", len(out) == 1 and out["Price"].iloc[0] == 100, str(list(out["Price"])))

# combine OR
out, _ = filt(df, [C("Region", "equals", "South"), C("Price", "greater_than", "250")], "or")
check("OR combine", len(out) == 2, str(list(out["Price"])))

# 1.5-f matches nothing -> 0 rows + clear count
out, note = filt(df, [C("Region", "equals", "Mars")])
check("1.5-f matches nothing", len(out) == 0 and "Kept 0 of 5" in note, note)

# 1.5-g matches everything
out, note = filt(df, [C("Price", "greater_or_equal", "0")])
check("1.5-g matches everything", len(out) == 5 and "Kept 5 of 5" in note, note)

# 1.5-i wrong-type comparison -> friendly error, no crash
try:
    filt(df, [C("Price", "greater_than", "North")])
    check("1.5-i wrong-type errors", False, "no error")
except OperationError as e:
    check("1.5-i wrong-type friendly error", "Price" in str(e), str(e))

# edge: filter needs at least one condition
try:
    execute_plan(df, [{"action": "filter", "conditions": []}])
    check("edge: empty conditions errors", False, "no error")
except OperationError:
    check("edge: empty conditions errors", True)

# edge: non-existent column in a condition -> caught
try:
    filt(df, [C("Ghost", "equals", "x")])
    check("edge: non-existent column caught", False, "no error")
except OperationError as e:
    check("edge: non-existent column caught", "Ghost" in str(e))

# in / not_in (membership in a set of values), case-insensitive
df_in = pd.DataFrame({"Status": ["Completed", "Pending", "Cancelled", "completed", "Refunded"]})
out, note = filt(df_in, [{"column": "Status", "operator": "in", "values": ["Completed", "Pending"]}])
check("in: keeps listed values (case-insensitive)", set(out["Status"]) == {"Completed", "Pending", "completed"}, str(list(out["Status"])))
check("in: note lists the set", "one of" in note and "Completed" in note, note)
out, _ = filt(df_in, [{"column": "Status", "operator": "not_in", "values": ["Completed"]}])
check("not_in: drops listed values", "Completed" not in list(out["Status"]) and "completed" not in list(out["Status"]), str(list(out["Status"])))
try:
    filt(df_in, [{"column": "Status", "operator": "in", "values": []}])
    check("in: empty list errors", False, "no error")
except OperationError as e:
    check("in: empty list errors", "list of values" in str(e), str(e))

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
