"""Tests for Phase 2.8 — advanced reshaping (unpivot / pivot / transpose).

Covers: unpivot wide→long, pivot totals match manual totals, unpivot is reversible
(unpivot then pivot recovers the original), uneven data → missing cells filled, and
transpose. Standalone (no LLM). Run: python test_reshape.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd

from app.executor import execute_multi, OperationError

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def run(df, ops):
    res, _, notes, _ = execute_multi({"t": df.copy()}, "t", ops)
    return res, notes


print("Running reshape checks...\n")

# --- unpivot (wide → long): monthly columns → tidy rows ---
wide = pd.DataFrame({"Region": ["N", "S"], "Jan": [1, 2], "Feb": [3, 4]})
res, _ = run(wide, [{"action": "unpivot", "id_columns": ["Region"],
                     "value_columns": ["Jan", "Feb"], "var_name": "Month", "value_name": "Sales"}])
check("unpivot row count = rows × value cols", len(res) == 4, f"{len(res)} rows")
check("unpivot columns", set(res.columns) == {"Region", "Month", "Sales"}, str(list(res.columns)))
nfeb = res[(res["Region"] == "N") & (res["Month"] == "Feb")]["Sales"].iloc[0]
check("unpivot value lands correctly (N/Feb = 3)", nfeb == 3, str(nfeb))

# --- pivot totals match MANUAL totals + uneven data filled ---
long = pd.DataFrame({
    "Region": ["N", "N", "S", "N"],
    "Month": ["Jan", "Jan", "Jan", "Feb"],
    "Sales": [10, 5, 20, 7],
})
piv, _ = run(long, [{"action": "pivot", "index_columns": ["Region"],
                     "pivot_column": "Month", "value_column": "Sales", "agg_func": "sum"}])
n_jan = piv[piv["Region"] == "N"]["Jan"].iloc[0]
check("pivot total matches manual (N/Jan = 10+5 = 15)", n_jan == 15, str(n_jan))
s_feb = piv[piv["Region"] == "S"]["Feb"].iloc[0]
check("uneven data → missing cell filled with 0 (S/Feb)", s_feb == 0, str(s_feb))

# --- unpivot is REVERSIBLE: unpivot then pivot recovers the original ---
back, _ = run(wide, [
    {"action": "unpivot", "id_columns": ["Region"], "value_columns": ["Jan", "Feb"],
     "var_name": "Month", "value_name": "Sales"},
    {"action": "pivot", "index_columns": ["Region"], "pivot_column": "Month",
     "value_column": "Sales", "agg_func": "sum"},
])
rn = back[back["Region"] == "N"].iloc[0]
rs = back[back["Region"] == "S"].iloc[0]
check("unpivot→pivot recovers values",
      rn["Jan"] == 1 and rn["Feb"] == 3 and rs["Jan"] == 2 and rs["Feb"] == 4,
      back.to_dict("records").__str__())
check("unpivot→pivot recovers columns", set(back.columns) == {"Region", "Jan", "Feb"},
      str(list(back.columns)))

# --- transpose (with a header column) ---
metrics = pd.DataFrame({"Metric": ["Rev", "Cost"], "Q1": [100, 40], "Q2": [200, 60]})
t, _ = run(metrics, [{"action": "transpose", "header_column": "Metric"}])
check("transpose swaps rows/cols (header_column)",
      set(t.columns) == {"Field", "Rev", "Cost"} and len(t) == 2, str(list(t.columns)))
q1 = t[t["Field"] == "Q1"].iloc[0]
check("transpose keeps values (Q1 Rev = 100)", q1["Rev"] == 100, str(q1.get("Rev")))

# transpose without a header column flips everything
t2, _ = run(metrics, [{"action": "transpose"}])
check("transpose (no header) → 3 rows (Metric, Q1, Q2)", len(t2) == 3, f"{len(t2)} rows")

# --- failures ---
def expect_error(name, df, ops):
    try:
        run(df, ops)
        check(name, False, "no error")
    except OperationError:
        check(name, True)


expect_error("pivot with missing value column errors", long,
             [{"action": "pivot", "index_columns": ["Region"], "pivot_column": "Month",
               "value_column": "Nope"}])
expect_error("pivot with no index errors", long,
             [{"action": "pivot", "pivot_column": "Month", "value_column": "Sales"}])
expect_error("unpivot with nothing to unpivot errors", pd.DataFrame({"A": [1]}),
             [{"action": "unpivot", "id_columns": ["A"]}])
expect_error("transpose with a bad header column errors", metrics,
             [{"action": "transpose", "header_column": "Nope"}])

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
