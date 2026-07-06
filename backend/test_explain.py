"""Tests for Explainable AI (Phase 2.4 — "show your work").

The frontend toggle reveals each result's steps (notes), live formulas, and the exact
plan. The backend guarantees behind that:
  1. EVERY operation type returns a plain-language explain note.
  2. The shown formula matches the actual computed result (honest by design).
Run: python test_explain.py
"""
import pandas as pd

from app.executor import execute_multi
from app.main import _describe_formulas

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("Running explainability checks...\n")

base = pd.DataFrame({
    "Region": ["N", "S", "N", "E"],
    "Revenue": [100, 200, 150, 50],
    "Cost": [40, 60, 50, 20],
    "Qty": [1, 2, 3, 4],
    "Name": [" a ", "b ", "c", "d"],
})
ref = pd.DataFrame({"Region": ["N", "S", "E"], "Manager": ["Asha", "Ravi", "Sam"]})
blanks = pd.DataFrame({"Region": ["N", None, "E"], "Revenue": [100, None, 50]})


def t(*tables):
    """Build a fresh {name: df} namespace so each case starts from clean data."""
    return {name: df.copy() for name, df in tables}


# (label, tables, primary, op) — one per AI-emittable operation type.
CASES = [
    ("sort", t(("base", base)), "base", {"action": "sort", "columns": ["Revenue"], "orders": ["desc"]}),
    ("filter", t(("base", base)), "base",
     {"action": "filter", "conditions": [{"column": "Revenue", "operator": "greater_than", "value": "100"}]}),
    ("limit", t(("base", base)), "base", {"action": "limit", "count": 2}),
    ("remove_duplicates", t(("base", base)), "base", {"action": "remove_duplicates", "columns": ["Region"]}),
    ("fill_missing", t(("blanks", blanks)), "blanks",
     {"action": "fill_missing", "columns": ["Region"], "fill_value": "Unknown"}),
    ("drop_missing", t(("blanks", blanks)), "blanks", {"action": "drop_missing", "columns": ["Region"]}),
    ("drop_invalid", t(("base", base)), "base", {"action": "drop_invalid", "columns": ["Revenue"], "data_type": "number"}),
    ("trim", t(("base", base)), "base", {"action": "trim", "columns": ["Name"]}),
    ("add_formula_column", t(("base", base)), "base",
     {"action": "add_formula_column", "name": "Profit", "formula": "{Revenue} - {Cost}"}),
    ("lookup", t(("base", base), ("ref", ref)), "base",
     {"action": "lookup", "key_column": "Region", "source_sheet": "ref",
      "source_key_column": "Region", "return_column": "Manager"}),
    ("aggregate", t(("base", base)), "base",
     {"action": "aggregate", "agg_func": "sum", "agg_column": "Revenue", "group_by": ["Region"]}),
    ("find_replace", t(("base", base)), "base",
     {"action": "find_replace", "find": "N", "replace": "North", "column": "Region"}),
    ("rename_columns", t(("base", base)), "base",
     {"action": "rename_columns", "rename_from": ["Qty"], "rename_to": ["Quantity"]}),
    ("drop_columns", t(("base", base)), "base", {"action": "drop_columns", "columns": ["Cost"]}),
    ("select_columns", t(("base", base)), "base", {"action": "select_columns", "columns": ["Region", "Revenue"]}),
    ("flag_missing", t(("blanks", blanks)), "blanks", {"action": "flag_missing", "columns": ["Region"]}),
    ("format_cells", t(("base", base)), "base",
     {"action": "format_cells", "format_columns": ["Revenue"], "number_format": "currency"}),
    ("merge", t(("base", base), ("ref", ref)), "base", {"action": "merge", "merge_tables": ["base", "ref"]}),
    ("combine_sheets", t(("base", base), ("ref", ref)), "base",
     {"action": "combine_sheets", "sheet_tables": ["base", "ref"]}),
    ("chart", t(("base", base)), "base",
     {"action": "chart", "chart_type": "bar", "x_column": "Region", "y_columns": ["Revenue"]}),
    ("dashboard", t(("base", base)), "base",
     {"action": "dashboard", "kpis": [{"label": "Rev", "agg": "sum", "column": "Revenue"}],
      "charts": [{"chart_type": "bar", "x_column": "Region", "y_columns": ["Revenue"]}]}),
    ("unpivot", t(("base", base)), "base",
     {"action": "unpivot", "id_columns": ["Region"], "value_columns": ["Revenue", "Cost"],
      "var_name": "Metric", "value_name": "Amount"}),
    ("pivot", t(("base", base)), "base",
     {"action": "pivot", "index_columns": ["Region"], "pivot_column": "Name",
      "value_column": "Revenue", "agg_func": "sum"}),
    ("transpose", t(("base", base)), "base", {"action": "transpose"}),
]

# 1. Every operation type explains itself with a non-empty note.
for label, tables, primary, op in CASES:
    _, _, notes, _ = execute_multi(tables, primary, [op])
    ok = bool(notes) and all(isinstance(n, str) and n.strip() for n in notes)
    check(f"{label} explains itself", ok, str(notes))

# 2. The shown formula matches the actual computed result.
res, _, _, render = execute_multi(t(("base", base)), "base",
    [{"action": "add_formula_column", "name": "Profit", "formula": "{Revenue} - {Cost}"}])
described = _describe_formulas(render)
check("live formula is surfaced for 'show your work'",
      any("Profit" in d and "{Revenue}" in d and "{Cost}" in d for d in described), str(described))
expected = (base["Revenue"] - base["Cost"]).tolist()
check("shown formula matches the actual result",
      res["Profit"].tolist() == expected, f"{res['Profit'].tolist()} vs {expected}")

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
