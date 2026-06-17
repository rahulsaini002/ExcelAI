"""Checks for the multi-file engine (load_files + execute_multi), no API key needed.

Run from the backend folder:
    .venv\\Scripts\\python.exe test_multifile.py
"""
from __future__ import annotations

import io

import pandas as pd

from app.executor import OperationError, execute_multi
from app.reader import load_files, summarize_tables

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


def csv_bytes(df):
    buf = io.BytesIO()
    df.to_csv(buf, index=False)
    return buf.getvalue()


# Two "files": January and February sales, plus a customers reference file.
jan = pd.DataFrame({"CustID": [1, 2], "Amount": [100, 200]})
feb = pd.DataFrame({"CustID": [2, 3], "Amount": [50, 70]})
customers = pd.DataFrame({"CustID": [1, 2, 3], "Name": ["Asha", "Ravi", "Sam"]})

uploads = [
    ("sales_jan.csv", csv_bytes(jan)),
    ("sales_feb.csv", csv_bytes(feb)),
    ("customers.csv", csv_bytes(customers)),
]

print("Running multi-file checks...\n")

data = load_files(uploads)
check("load_files names tables after files",
      set(data.tables) == {"sales_jan", "sales_feb", "customers"}, str(list(data.tables)))
check("primary is the first file", data.primary == "sales_jan", data.primary)

summary = summarize_tables(data.tables, data.primary)
check("summary lists all tables", set(summary["tables"]) == set(data.tables))

# merge the two sales files
df, name, notes, _ = execute_multi(data.tables, data.primary,
    [{"action": "merge", "merge_tables": ["sales_jan", "sales_feb"], "new_table": "all_sales"}])
check("merge stacks rows", len(df) == 4 and name == "all_sales", f"{len(df)} rows, name={name}")

# merge then aggregate total by customer
df, name, notes, _ = execute_multi(data.tables, data.primary, [
    {"action": "merge", "merge_tables": ["sales_jan", "sales_feb"], "new_table": "all_sales"},
    {"action": "aggregate", "agg_func": "sum", "agg_column": "Amount", "group_by": ["CustID"]},
])
check("merge + group-sum gives 3 customers", len(df) == 3, f"{len(df)} rows; notes={notes}")

# cross-FILE lookup: bring Name from customers into sales_jan
df, name, notes, _ = execute_multi(data.tables, data.primary, [
    {"action": "lookup", "table": "sales_jan", "key_column": "CustID",
     "source_sheet": "customers", "source_key_column": "CustID", "return_column": "Name"},
])
check("cross-file lookup adds Name", list(df["Name"]) == ["Asha", "Ravi"], str(list(df.get("Name", []))))

# operation targeting a specific (non-primary) table
df, name, notes, _ = execute_multi(data.tables, data.primary, [
    {"action": "drop_columns", "table": "customers", "columns": ["Name"]},
])
check("table-targeted op acts on the right table",
      name == "customers" and list(df.columns) == ["CustID"], f"name={name}, cols={list(df.columns)}")

# error: unknown table name
try:
    execute_multi(data.tables, data.primary, [{"action": "sort", "table": "nope", "columns": ["CustID"]}])
    check("unknown table errors cleanly", False, "no error")
except OperationError:
    check("unknown table errors cleanly", True)

# --- smart column mapping on merge ---
# Synonym groups: client_id and cust_no both mean Customer_ID.
a = pd.DataFrame({"Customer_ID": [1], "Amount": [10]})
b = pd.DataFrame({"client_id": [2], "Amount": [20]})
c = pd.DataFrame({"cust_no": [3], "Amount": [30]})
syn = {"a": a, "b": b, "c": c}
df, name, notes, _ = execute_multi(syn, "a", [{
    "action": "merge", "merge_tables": ["a", "b", "c"], "new_table": "all",
    "column_groups": [{"name": "Customer_ID", "aliases": ["client_id", "cust_no"]}],
}])
check("synonym groups unify into one column",
      "Customer_ID" in df.columns and "client_id" not in df.columns
      and "cust_no" not in df.columns and len(df.columns) == 2,
      f"cols={list(df.columns)}")
check("synonym merge stacks all rows", len(df) == 3 and list(df["Customer_ID"]) == [1, 2, 3],
      str(list(df.get("Customer_ID", []))))

# Auto-normalization: differ only by case/spacing/punctuation -> unified, no groups.
p = pd.DataFrame({"Customer ID": [1], "Amount": [10]})
q = pd.DataFrame({"customer_id": [2], "Amount": [20]})
df, name, notes, _ = execute_multi({"p": p, "q": q}, "p",
    [{"action": "merge", "merge_tables": ["p", "q"]}])
check("auto-unify case/space variants without groups",
      len(df.columns) == 2 and len(df) == 2, f"cols={list(df.columns)}, rows={len(df)}")

# Genuinely different columns are kept separate.
x = pd.DataFrame({"Name": ["Asha"], "Amount": [10]})
y = pd.DataFrame({"City": ["Pune"], "Amount": [20]})
df, name, notes, _ = execute_multi({"x": x, "y": y}, "x",
    [{"action": "merge", "merge_tables": ["x", "y"]}])
check("different columns stay separate",
      set(df.columns) == {"Name", "City", "Amount"} and len(df) == 2, f"cols={list(df.columns)}")

# Merge two DISJOINT files side-by-side, THEN compute across them (the real
# "merge both files and multiply Roll No. x Quantity" scenario from the UI).
names_t = pd.DataFrame({"Name": ["A", "B", "C"], "Roll No.": [1, 11, 12]})
qty_t = pd.DataFrame({"product": [1001, 1002, 1003], "Quantity": [2, 3, 4]})
df, name, notes, render = execute_multi(
    {"Testing 1": names_t, "testing": qty_t}, "Testing 1",
    [{"action": "merge", "merge_tables": ["Testing 1", "testing"], "new_table": "merged"},
     {"action": "add_formula_column", "name": "Roll x Qty", "formula": "{Roll No.} * {Quantity}"}],
)
check("disjoint merge places columns side by side",
      set(df.columns) == {"Name", "Roll No.", "product", "Quantity", "Roll x Qty"}, f"cols={list(df.columns)}")
check("compute across two merged files aligns by row",
      list(df["Roll x Qty"]) == [2, 33, 48], str(list(df["Roll x Qty"])))
check("merge note explains side-by-side", any("side by side" in n for n in notes), str(notes))

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
