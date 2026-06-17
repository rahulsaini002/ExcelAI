"""Deterministic fallback parser (used only when the Brain/LLM is unavailable).
Covers ALL operations it supports + that it DEFERS (None) when unsure, plus the
end-to-end /process path when the model fails.

Run from backend:  .venv\\Scripts\\python.exe test_fallback.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from fastapi.testclient import TestClient

from app import fallback, main, llm

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def tbl(name, *cols):
    return {name: {"row_count": 5, "columns": [{"name": c} for c in cols]}}


STRUCT = {"primary_table": "t", "tables": tbl("t", "Time", "Amount", "Revenue", "Cost",
                                              "Region", "Customer Name", "Status", "Date")}
# multi-table for merge / lookup
MULTI = {"primary_table": "Orders", "tables": {
    **tbl("Orders", "Order_ID", "Customer_ID", "Revenue"),
    **tbl("Customers", "Customer_ID", "Customer_Name", "Phone"),
}}


def p(s, st=STRUCT):
    return fallback.parse(s, st)


def act(s, st=STRUCT):
    plan = p(s, st)
    ops = (plan or {}).get("operations") or []
    return [o["action"] for o in ops] if ops else None


def op0(s, st=STRUCT):
    return p(s, st)["operations"][0]


print("FALLBACK PARSER — all operations (offline, no AI)\n")

# sort / limit
check("sort desc", op0("Sort Amount descending") == {"action": "sort", "columns": ["Amount"], "orders": ["desc"]})
check("X high to low", op0("Amount high to low")["orders"] == ["desc"])
check("sort + top N", act("sort Amount descending and keep top 100") == ["sort", "limit"])
check("top N alone", op0("top 50") == {"action": "limit", "count": 50})

# remove duplicates / trim
check("dedupe", act("remove duplicates") == ["remove_duplicates"])
check("trim", act("trim spaces") == ["trim"])

# fill_missing
check("fill with value", op0("fill blanks with Unknown") == {"action": "fill_missing", "fill_value": "Unknown"})
check("fill preserves case", op0("fill missing with N/A")["fill_value"] == "N/A")
check("fill with column scope", op0("fill empty Region with Unknown") == {"action": "fill_missing", "fill_value": "Unknown", "columns": ["Region"]})
check("fill previous", op0("fill blanks with previous value") == {"action": "fill_missing", "fill_method": "previous"})
check("fill average DEFERS (AI declines)", p("fill blanks with the average") is None)

# drop_missing / drop_invalid / flag_missing
check("drop blank rows", act("remove rows with blank values") == ["drop_missing"])
check("drop invalid", op0("remove rows with invalid Revenue") == {"action": "drop_invalid", "columns": ["Revenue"]})
check("flag missing", act("highlight missing values") == ["flag_missing"])

# rename / drop columns / select columns
check("rename", op0("rename Amount to Total") == {"action": "rename_columns", "rename_from": ["Amount"], "rename_to": ["Total"]})
check("drop column", op0("delete the Region column") == {"action": "drop_columns", "columns": ["Region"]})
check("select columns (gated on 'column')", op0("keep only the Amount and Region columns") == {"action": "select_columns", "columns": ["Amount", "Region"]})
check("'keep only North region' is NOT a select", act("keep only North region") != ["select_columns"])

# find & replace
check("replace with", op0("replace USA with United States") == {"action": "find_replace", "find": "USA", "replace": "United States"})
check("change to + in column", op0("change North to N in Region") == {"action": "find_replace", "find": "North", "replace": "N", "column": "Region"})

# format
check("currency", op0("format Amount as currency") == {"action": "format_cells", "format_columns": ["Amount"], "number_format": "currency"})
check("percent", op0("show Revenue as percentage")["number_format"] == "percent")
check("bold header", op0("bold the header") == {"action": "format_cells", "bold_header": True})

# aggregate
check("total of column", op0("total Revenue") == {"action": "aggregate", "agg_func": "sum", "agg_column": "Revenue"})
check("average", op0("average Amount")["agg_func"] == "average")
check("grouped sum", op0("total Revenue by Region") == {"action": "aggregate", "agg_func": "sum", "agg_column": "Revenue", "group_by": ["Region"]})
check("count rows", op0("count")["agg_func"] == "count")

# formula
check("profit formula", op0("Create Profit = Revenue - Cost") == {"action": "add_formula_column", "name": "Profit", "formula": "{Revenue} - {Cost}"})
check("formula 'as'", op0("add a column Total as Amount * 2")["formula"] == "{Amount} * 2")
check("Margin % (no expr) DEFERS", p("Create Margin %") is None)

# MULTI-INSTRUCTION: several commands in one message run in order
check("multi: newlines", act("Filter Amount > 500\nSort Amount descending\nCreate Tax = Amount * 0.18") == ["filter", "sort", "add_formula_column"])
check("multi: blank lines (user's case)", act("Filter Amount > 500\n\nSort Amount descending\n\nCreate Tax column") == ["filter", "sort"])  # 3rd has no formula -> skipped
check("multi: 'then'", act("filter Amount > 500 then sort Amount descending") == ["filter", "sort"])
check("multi: semicolons", act("remove duplicates; sort Amount desc") == ["remove_duplicates", "sort"])
check("multi: inline numbered list", act("1. remove duplicates 2. sort Amount descending") == ["remove_duplicates", "sort"])
check("multi: order preserved", act("Sort Amount asc\nFilter Amount > 500")[0] == "sort")
check("single instruction still 1 op", act("Sort Amount descending") == ["sort"])
check("multi: decimals not split", op0("Create Tax = Amount * 0.18")["formula"] == "{Amount} * 0.18")

# merge / combine_sheets / lookup (multi-table)
check("merge", op0("merge both files", MULTI) == {"action": "merge", "merge_tables": ["Orders", "Customers"]})
check("combine into separate sheets", op0("combine the files into separate sheets", MULTI)["action"] == "combine_sheets")
check("lookup", op0("bring Customer_Name from Customers", MULTI) == {"action": "lookup", "key_column": "Customer_ID", "source_sheet": "Customers", "source_key_column": "Customer_ID", "return_column": "Customer_Name"})

# simple filters
check("col > N", op0("Amount > 5000")["conditions"][0] == {"column": "Amount", "operator": "greater_than", "value": "5000"})
check("col = text", op0("Region = North")["conditions"][0]["operator"] == "equals")
check("contains", op0("Customer Name contains Rahul")["conditions"][0]["operator"] == "contains")

# DEFERS (returns None) — not confident
check("defers on Hindi", p("रेवेन्यू के हिसाब से सॉर्ट करो") is None)
check("defers on vague", p("do something clever with this data") is None)
check("defers on unsupported", p("make a pie chart") is None)
check("defers on unknown column", p("sort by Profit") is None)
check("defers on empty", p("") is None)

# --- END TO END via /process when the model is down ---
client = TestClient(main.app)
_orig = main.llm.parse_instruction
try:
    main.llm.parse_instruction = lambda i, s, h: (_ for _ in ()).throw(llm.ModelUnavailableError("rate limited"))
    r = client.post("/process", data={"instruction": "Sort Amount descending", "session_id": "fb", "rewind": "-1", "history": ""},
                    files=[("files", ("t.csv", b"Amount\n10\n50\n30\n", "text/csv"))])
    body = r.json()
    check("E2E sort works model-down", r.status_code == 200 and body["status"] == "ok" and body["preview"][0]["sample_rows"][0]["Amount"] == 50, str(body)[:140])
    # the fallback result looks like a normal result — NO "AI unavailable" disclaimer
    check("E2E no fallback disclaimer shown", not any("simple built-in" in n or "AI service was unavailable" in n for n in body["notes"]), str(body.get("notes")))
    r2 = client.post("/process", data={"instruction": "do something clever", "session_id": "fb2", "rewind": "-1", "history": ""},
                     files=[("files", ("t.csv", b"Amount\n1\n", "text/csv"))])
    check("E2E unparseable -> honest 503", r2.status_code == 503 and "rate limited" in r2.json()["error"])
finally:
    main.llm.parse_instruction = _orig
    main._SESSIONS.clear()

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
