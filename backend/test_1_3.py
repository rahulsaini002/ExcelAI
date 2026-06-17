"""Feature 1.3 — Instruction understanding: the VALIDATION half (the parts that run
without the LLM). The language-understanding half (1.3-a/b/c/d/f/g) is Brain-driven and
tested live. Here we verify: a plan is validated before execution, bad columns/tables/
actions are caught with friendly errors, and multi-step plans run in order.

Run from backend:  .venv\\Scripts\\python.exe test_1_3.py
"""
from __future__ import annotations

import pandas as pd

from app.executor import OperationError, execute_multi, execute_plan

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def err(ops, df, sheets=None):
    """Return the OperationError message, or None if it didn't raise (or wrong type)."""
    try:
        execute_plan(df, ops, sheets)
        return None
    except OperationError as e:
        return str(e)
    except Exception:
        return None


print("FEATURE 1.3 — plan validation\n")

df = pd.DataFrame({"Revenue": [3, 1, 2], "Cost": [1, 1, 1]})

# 1.3-e Non-existent column -> caught, friendly, lists available columns
msg = err([{"action": "sort", "columns": ["Profit"]}], df)
check("1.3-e non-existent column caught", msg is not None)
check("1.3-e message names the column", msg and "Profit" in msg, str(msg))
check("1.3-e message lists available", msg and "Revenue" in msg and "Cost" in msg, str(msg))

# validation catches missing column across operations
check("non-existent in filter caught", err([{"action": "filter", "conditions": [{"column": "Nope", "operator": "equals", "value": "x"}]}], df) is not None)
check("non-existent in drop caught", err([{"action": "drop_columns", "columns": ["Ghost"]}], df) is not None)
check("non-existent in formula caught", err([{"action": "add_formula_column", "name": "T", "formula": "{Nope} * 2"}], df) is not None)

# Unknown/invalid action -> caught, not a crash (defensive; schema also blocks it)
check("unknown action caught", err([{"action": "frobnicate"}], df) is not None)

# Missing required fields -> caught
check("sort without columns caught", err([{"action": "sort"}], df) is not None)
check("lookup missing fields caught", err([{"action": "lookup", "key_column": "Revenue"}], df, {"t": df}) is not None)
check("rename mismatch caught", err([{"action": "rename_columns", "rename_from": ["Revenue"], "rename_to": []}], df) is not None)

# Non-existent TABLE (multi) -> caught
try:
    execute_multi({"t": df}, "t", [{"action": "sort", "table": "ghost", "columns": ["Revenue"]}])
    check("non-existent table caught", False, "no error")
except OperationError:
    check("non-existent table caught", True)

# 1.3-h Multi-step plan runs IN ORDER (filter then sort)
ms = pd.DataFrame({"R": ["N", "S", "N"], "P": [3, 1, 2]})
out = execute_multi({"t": ms}, "t", [
    {"action": "filter", "conditions": [{"column": "R", "operator": "equals", "value": "N"}]},
    {"action": "sort", "columns": ["P"], "orders": ["desc"]},
])[0]
check("1.3-h multi-step runs in order", list(out["P"]) == [3, 2], str(list(out.get("P", []))))

# A valid single-action plan executes (sanity: sort desc)
out = execute_plan(df, [{"action": "sort", "columns": ["Revenue"], "orders": ["desc"]}])[0]
check("valid plan executes", list(out["Revenue"]) == [3, 2, 1])

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
