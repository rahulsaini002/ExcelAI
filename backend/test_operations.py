"""Quick checks for the executor ("the Hands"), no API key needed.

Run from the backend folder:
    .venv\\Scripts\\python.exe test_operations.py

It builds small in-memory tables, runs each operation through execute_plan with a
hand-written plan, and prints PASS/FAIL for each. This verifies the trusted code
independently of the LLM.
"""
from __future__ import annotations

import pandas as pd

from app.executor import OperationError, execute_plan


def sample():
    return pd.DataFrame(
        {
            "Name": ["Asha", "Ravi", "Asha", "Sam", "Mira"],
            "Region": ["North", "South", "North", "", "East"],
            "Qty": [3, 5, 3, 10, 2],
            "Price": [100, 200, 100, 50, 300],
            "Email": ["a@x.com", "r@x.com", "A@X.com ", "s@x.com", "m@x.com"],
        }
    )


passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def run(ops, df=None, sheets=None):
    df = sample() if df is None else df
    return execute_plan(df, ops, sheets)


print("Running operation checks...\n")

# drop_columns — the one the user reported
df, notes, _ = run([{"action": "drop_columns", "columns": ["Email"]}])
check("drop_columns removes the column", "Email" not in df.columns, str(list(df.columns)))

# select_columns
df, _, _ = run([{"action": "select_columns", "columns": ["Name", "Qty"]}])
check("select_columns keeps only chosen", list(df.columns) == ["Name", "Qty"], str(list(df.columns)))

# rename_columns
df, _, _ = run([{"action": "rename_columns", "rename_from": ["Qty"], "rename_to": ["Quantity"]}])
check("rename_columns renames", "Quantity" in df.columns and "Qty" not in df.columns)

# sort
df, _, _ = run([{"action": "sort", "columns": ["Price"], "orders": ["desc"]}])
check("sort descending by Price", list(df["Price"]) == [300, 200, 100, 100, 50], str(list(df["Price"])))

# filter — numeric greater_than
df, _, _ = run([{"action": "filter", "conditions": [{"column": "Price", "operator": "greater_than", "value": "100"}]}])
check("filter Price > 100", len(df) == 2, f"got {len(df)} rows")

# filter — text contains, AND combine
df, _, _ = run([{"action": "filter", "combine": "and", "conditions": [
    {"column": "Region", "operator": "equals", "value": "North"},
    {"column": "Qty", "operator": "less_than", "value": "5"},
]}])
check("filter Region=North AND Qty<5", len(df) == 2, f"got {len(df)} rows")

# filter — is_blank
df, _, _ = run([{"action": "filter", "conditions": [{"column": "Region", "operator": "is_blank"}]}])
check("filter Region is_blank", len(df) == 1, f"got {len(df)} rows")

# remove_duplicates by Name
df, _, _ = run([{"action": "remove_duplicates", "columns": ["Name"]}])
check("remove_duplicates by Name", len(df) == 4, f"got {len(df)} rows")

# fill_missing
df, notes, _ = run([{"action": "fill_missing", "columns": ["Region"], "fill_value": "Unknown"}])
check("fill_missing fills blank Region", (df["Region"] == "Unknown").sum() == 1, notes[-1])

# drop_missing
df, _, _ = run([{"action": "drop_missing", "columns": ["Region"]}])
check("drop_missing drops blank Region row", len(df) == 4, f"got {len(df)} rows")

# add_formula_column
df, _, _ = run([{"action": "add_formula_column", "name": "Total", "formula": "{Qty} * {Price}"}])
check("add_formula_column Total = Qty*Price", list(df["Total"]) == [300, 1000, 300, 500, 600], str(list(df["Total"])))

# aggregate — sum grouped by Region
df, notes, _ = run([{"action": "aggregate", "agg_func": "sum", "agg_column": "Price", "group_by": ["Region"]}])
check("aggregate sum Price by Region", "sum_of_Price" in df.columns, str(list(df.columns)))

# aggregate — plain count: scalar value reported in the note, data kept
df, notes, _ = run([{"action": "aggregate", "agg_func": "count", "agg_column": "Name"}])
check("aggregate count", ": 5" in notes[0] and len(df) == 5, notes[0])

# find_replace — substring, case-insensitive
df, notes, _ = run([{"action": "find_replace", "column": "Region", "find": "north", "replace": "N"}])
check("find_replace north->N (case-insensitive)", (df["Region"] == "N").sum() == 2, notes[-1])

# lookup — cross-sheet
main_df = pd.DataFrame({"CustID": [1, 2, 3], "Order": ["A", "B", "C"]})
ref_df = pd.DataFrame({"CustID": [1, 2], "CustName": ["Asha", "Ravi"]})
df, notes, _ = run([{"action": "lookup", "key_column": "CustID", "source_sheet": "Customers",
                     "source_key_column": "CustID", "return_column": "CustName"}],
                   df=main_df, sheets={"Orders": main_df, "Customers": ref_df})
check("lookup brings CustName across sheets",
      list(df["CustName"]) == ["Asha", "Ravi", "Not found"], str(list(df.get("CustName", []))))

# format_cells — returns a directive, data unchanged
df, notes, fmt = run([{"action": "format_cells", "format_columns": ["Price"], "number_format": "currency", "bold_header": True}])
check("format_cells produces a directive", len(fmt) == 1 and fmt[0]["format"] == "currency", str(fmt))

# --- error cases (should raise OperationError, not crash) ---
def expect_error(name, ops, df=None, sheets=None):
    try:
        run(ops, df, sheets)
        check(name, False, "no error raised")
    except OperationError:
        check(name, True)
    except Exception as exc:  # any other exception is a real bug
        check(name, False, f"wrong error type: {type(exc).__name__}: {exc}")


expect_error("drop unknown column errors cleanly", [{"action": "drop_columns", "columns": ["Nope"]}])
expect_error("sum of text column errors cleanly", [{"action": "aggregate", "agg_func": "sum", "agg_column": "Name"}])
expect_error("filter compare text numerically errors cleanly",
             [{"action": "filter", "conditions": [{"column": "Name", "operator": "greater_than", "value": "5"}]}])

# limit — keep the first/last N rows (e.g. "top N" after a sort)
df, notes, _ = run([{"action": "sort", "columns": ["Price"], "orders": ["desc"]}, {"action": "limit", "count": 2}])
check("limit keeps top N after sort", list(df["Price"]) == [300, 200] and len(df) == 2, str(list(df["Price"])))
check("limit note mentions kept count", "top 2" in notes[-1], notes[-1])
df, _, _ = run([{"action": "sort", "columns": ["Price"], "orders": ["asc"]}, {"action": "limit", "count": 2, "from_end": True}])
check("limit from_end keeps last N", list(df["Price"]) == [200, 300], str(list(df["Price"])))
df, _, _ = run([{"action": "limit", "count": 99}])
check("limit larger than table keeps all", len(df) == 5, str(len(df)))
expect_error("limit zero errors cleanly", [{"action": "limit", "count": 0}])
expect_error("limit non-number errors cleanly", [{"action": "limit", "count": "abc"}])

# drop_invalid — remove rows whose value isn't a valid number/date (not blanks)
di = pd.DataFrame({"Customer": ["A", "B", "C", "D", "E"], "Revenue": ["5000", "ABC", "7000", "12A", ""]})
df, notes, _ = run([{"action": "drop_invalid", "columns": ["Revenue"]}], di)
check("drop_invalid removes non-numeric rows", list(df["Customer"]) == ["A", "C", "E"], str(list(df["Customer"])))
check("drop_invalid keeps blanks (drop_missing's job)", "E" in list(df["Customer"]))
check("drop_invalid note names offenders", "ABC" in notes[0] and "Removed 2" in notes[0], notes[0])
dd = pd.DataFrame({"D": ["2025-01-01", "notadate", "Jan 03 2025"]})
df, _, _ = run([{"action": "drop_invalid", "columns": ["D"], "data_type": "date"}], dd)
check("drop_invalid date mode", list(df["D"]) == ["2025-01-01", "Jan 03 2025"], str(list(df["D"])))
expect_error("drop_invalid needs a column", [{"action": "drop_invalid"}])

# trim — strip ends + collapse internal runs (Excel TRIM); numbers untouched
tr = pd.DataFrame({"Name": ["  Rahul ", "Amit  Kumar", "Priya"], "Qty": [1, 2, 3]})
df, notes, _ = run([{"action": "trim"}], tr)
check("trim strips + collapses", list(df["Name"]) == ["Rahul", "Amit Kumar", "Priya"], str(list(df["Name"])))
check("trim leaves numbers alone", list(df["Qty"]) == [1, 2, 3])
check("trim reports affected count", "2 cells" in notes[0], notes[0])
df, notes, _ = run([{"action": "trim"}], pd.DataFrame({"X": ["clean", "data"]}))
check("trim no-op message", "No extra spaces" in notes[0], notes[0])

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
