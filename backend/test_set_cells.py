"""Checks for the set_cells operation (manual grid edits applied via /execute).

Run from the backend folder:
    .venv\\Scripts\\python.exe test_set_cells.py

set_cells is built by the UI from the user's inline cell edits and sent straight to
/execute (the AI never emits it). Values are coerced to the column's type; rows
outside the table are skipped; unknown columns raise a friendly error.
"""
from __future__ import annotations

import sys

import pandas as pd

from app.executor import OperationError, execute_plan


def sample():
    return pd.DataFrame(
        {
            "Name": ["Asha", "Ravi", "Sam"],
            "Qty": [3, 5, 10],
            "Price": [100.5, 200.0, 50.0],
            "Date": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
        }
    )


passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def edit(*edits):
    return execute_plan(sample(), [{"action": "set_cells", "edits": list(edits)}])


print("Running set_cells checks...\n")

# text edit
df, notes, _ = edit({"row": 0, "column": "Name", "value": "Asha K"})
check("text edit applied", df.loc[0, "Name"] == "Asha K", df["Name"].tolist())
check("note counts one", any("Updated 1 cell" in n for n in notes), notes)

# numeric column stays numeric
df, _, _ = edit({"row": 1, "column": "Qty", "value": "7"})
check("int edit value", df.loc[1, "Qty"] == 7, df["Qty"].tolist())
check("int dtype preserved", pd.api.types.is_numeric_dtype(df["Qty"]), df["Qty"].dtype)

# float value
df, _, _ = edit({"row": 2, "column": "Price", "value": "75.25"})
check("float edit value", df.loc[2, "Price"] == 75.25, df["Price"].tolist())

# date column stays a date
df, _, _ = edit({"row": 0, "column": "Date", "value": "2025-02-15"})
check("date edit parses", df.loc[0, "Date"] == pd.Timestamp("2025-02-15"), df["Date"].tolist())
check("date dtype preserved", pd.api.types.is_datetime64_any_dtype(df["Date"]), df["Date"].dtype)

# multiple edits + count
df, notes, _ = edit(
    {"row": 0, "column": "Name", "value": "X"},
    {"row": 1, "column": "Qty", "value": "9"},
)
check("multi edits applied", df.loc[0, "Name"] == "X" and df.loc[1, "Qty"] == 9)
check("multi note counts two", any("Updated 2 cells" in n for n in notes), notes)

# out-of-range row skipped (preview shows a sample)
df, notes, _ = edit({"row": 99, "column": "Name", "value": "Z"})
check("out-of-range skipped", any("No cells were updated" in n for n in notes), notes)

# unknown column → friendly error, nothing changed
try:
    edit({"row": 0, "column": "Nope", "value": "x"})
    check("unknown column errors", False, "no error raised")
except OperationError as ex:
    check("unknown column errors", "Nope" in str(ex), str(ex))

# empty edits → error
try:
    edit()
    check("empty edits errors", False)
except OperationError:
    check("empty edits errors", True)

# blank value clears the cell (NaN)
df, _, _ = edit({"row": 0, "column": "Name", "value": ""})
check("blank clears cell", pd.isna(df.loc[0, "Name"]), df.loc[0, "Name"])

# the input DataFrame is never mutated in place
base = sample()
execute_plan(base, [{"action": "set_cells", "edits": [{"row": 0, "column": "Name", "value": "Mutated"}]}])
check("input not mutated", base.loc[0, "Name"] == "Asha", base.loc[0, "Name"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
