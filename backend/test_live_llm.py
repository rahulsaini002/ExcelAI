"""LIVE Brain (Gemini) verification — the checks that were deferred across Features
1.2–1.14 because the free tier was rate-limited. Each case sends a real instruction
through llm.parse_instruction and asserts the Operation Plan (not wording).

NOTE: this calls the live model, so it needs GEMINI_API_KEY and network. Rate-limit
hits are reported as SKIP, not FAIL (re-run later to finish them). Results can vary
slightly run-to-run; assertions check the ACTION/intent, not exact phrasing.

Run from backend:  .venv\\Scripts\\python.exe test_live_llm.py
"""
from __future__ import annotations

import time

import pandas as pd

from app import llm
from app.reader import summarize_tables

passed = failed = skipped = 0


def structure(tables: dict[str, pd.DataFrame], primary: str | None = None) -> str:
    return summarize_tables(tables, primary or next(iter(tables)))


def ops(plan):
    return plan.get("operations") or []


def first(plan):
    o = ops(plan)
    return o[0] if o else {}


def actions(plan):
    return [o.get("action") for o in ops(plan)]


def run(name, instruction, struct, validate, history="", retries=1):
    """Call the live model; validate(plan)->(*ok,detail). SKIP on rate limit."""
    global passed, failed, skipped
    for attempt in range(retries + 1):
        try:
            plan = llm.parse_instruction(instruction, struct, history)
        except llm.ModelUnavailableError:
            if attempt < retries:
                time.sleep(12)
                continue
            skipped += 1
            print(f"  SKIP  {name}  (model busy / rate-limited)")
            return
        except Exception as e:  # noqa
            failed += 1
            print(f"  FAIL  {name}  unexpected error: {type(e).__name__}: {str(e)[:120]}")
            return
        try:
            ok, detail = validate(plan)
        except Exception as e:  # validator blew up
            ok, detail = False, f"validator error {type(e).__name__}: {e}"
        if ok:
            passed += 1
            print(f"  ok    {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {detail}  :: plan_actions={actions(plan)} reply={plan.get('reply')!r} clar={plan.get('clarification')!r}")
        time.sleep(2.5)  # pace to be gentle on the free tier
        return


# Common test data / structures
sales = {"sales": pd.DataFrame({"Name": ["A", "B"], "Revenue": [10, 20], "Region": ["North", "South"]})}
S_SALES = structure(sales)
two_num = {"orders": pd.DataFrame({"Qty": [2, 3], "Price": [10, 20]})}
S_TWO = structure(two_num)
resp = {"survey": pd.DataFrame({"Response": ["Yes", "No", "Yes"]})}
S_RESP = structure(resp)
amt = {"report": pd.DataFrame({"Amount": [1000.0, 2500.0], "Date": pd.to_datetime(["2026-01-02", "2026-03-04"])})}
S_AMT = structure(amt)
city = {"data": pd.DataFrame({"City": ["mumbai", "Mumbai", "Delhi"]})}
S_CITY = structure(city)
lookup_tabs = {
    "orders": pd.DataFrame({"CustID": [1, 2]}),
    "people": pd.DataFrame({"ID": [1, 2], "Name": ["Asha", "Rohan"]}),
}
S_LOOKUP = structure(lookup_tabs, "orders")
# two DISJOINT files (the live-bug scenario)
disjoint = {
    "Testing 1": pd.DataFrame({"Name": ["A", "B", "C"], "Roll No.": [1, 11, 12]}),
    "testing": pd.DataFrame({"product": [1001, 1002, 1003], "Quantity": [2, 3, 4]}),
}
S_DISJOINT = structure(disjoint, "Testing 1")


print("LIVE BRAIN (Gemini) VERIFICATION — deferred Phase-1 checks\n")

# --- 1.2 / 1.4  languages -> sort descending ------------------------------- #
run("1.2 EN sort desc", "sort by revenue, highest first", S_SALES,
    lambda p: ("sort" in actions(p) and first(p).get("orders") == ["desc"], "expected sort desc"))
run("1.2 Hinglish sort desc", "revenue ke hisaab se ghatte hue order me sort karo", S_SALES,
    lambda p: ("sort" in actions(p) and (first(p).get("orders") or ["?"])[0] == "desc", "expected sort desc"))
run("1.2 Hindi (Devanagari) sort desc", "रेवेन्यू के हिसाब से घटते क्रम में सॉर्ट करो", S_SALES,
    lambda p: ("sort" in actions(p) and (first(p).get("orders") or ["?"])[0] == "desc", "expected sort desc"))

# --- 1.5 filter ------------------------------------------------------------ #
run("1.5 filter equals", "keep only the rows where Region is North", S_SALES,
    lambda p: ("filter" in actions(p)
               and any((c.get("column") == "Region" and str(c.get("value")).lower() == "north")
                       for c in (first(p).get("conditions") or [])), "expected filter Region=North"))

# --- 1.6 remove duplicates ------------------------------------------------- #
run("1.6 remove duplicates", "remove duplicate rows", S_SALES,
    lambda p: ("remove_duplicates" in actions(p), "expected remove_duplicates"))

# --- 1.7 fill missing + statistical decline -------------------------------- #
run("1.7 fill blanks fixed", "fill blank Region cells with Unknown", S_SALES,
    lambda p: ("fill_missing" in actions(p) and str(first(p).get("fill_value")).lower() == "unknown",
               "expected fill_missing Unknown"))
run("1.7 decline statistical fill", "fill the blanks in Revenue with the average value", S_SALES,
    lambda p: (not ops(p) and bool(p.get("reply")), "expected a friendly reply, no ops"))

# --- 1.8 formula column ---------------------------------------------------- #
run("1.8 add formula column", "add a Total column that multiplies Qty by Price", S_TWO,
    lambda p: ("add_formula_column" in actions(p) and "Qty" in (first(p).get("formula") or "")
               and "Price" in (first(p).get("formula") or ""), "expected add_formula_column Qty*Price"))

# --- 1.9 lookup ------------------------------------------------------------ #
run("1.9 cross-sheet lookup", "bring each customer's Name from the people table using CustID", S_LOOKUP,
    lambda p: ("lookup" in actions(p) and first(p).get("return_column") == "Name", "expected lookup return Name"))

# --- 1.10 aggregate (grouped + count of a value) --------------------------- #
run("1.10 sum grouped by", "total revenue by region", S_SALES,
    lambda p: ("aggregate" in actions(p) and (first(p).get("agg_func") in ("sum",))
               and "Region" in (first(p).get("group_by") or []), "expected aggregate sum group_by Region"))
run("1.10 count of a value", "how many responses were Yes?", S_RESP,
    lambda p: ("aggregate" in actions(p) and (
        str(first(p).get("count_value")).lower() == "yes"
        or "Response" in (first(p).get("group_by") or [])), "expected count of Yes (count_value or grouped)"))

# --- 1.11 formatting ------------------------------------------------------- #
run("1.11 currency format", "format the Amount column as currency with 2 decimals", S_AMT,
    lambda p: ("format_cells" in actions(p) and first(p).get("number_format") == "currency",
               "expected format_cells currency"))
run("1.11 date format", "show the Date column as DD-MM-YYYY", S_AMT,
    lambda p: ("format_cells" in actions(p) and first(p).get("number_format") == "date",
               "expected format_cells date"))

# --- 1.12 find & replace --------------------------------------------------- #
run("1.12 find & replace", "replace every mumbai with Mumbai in the City column", S_CITY,
    lambda p: ("find_replace" in actions(p)
               and "mumbai" in str(first(p).get("find")).lower()
               and "Mumbai" in str(first(p).get("replace")),  # tolerate stray quotes
               "expected find_replace mumbai -> Mumbai"))

# --- 1.14-b unsupported -> reply ------------------------------------------- #
run("1.14-b unsupported -> reply", "make a pie chart of revenue by region", S_SALES,
    lambda p: (not ops(p) and bool(p.get("reply")) and not p.get("clarification"),
               "expected a friendly reply (no ops, no clarify)"))

# --- 1.14-c ambiguous -> ONE clarifying question --------------------------- #
run("1.14-c ambiguous -> clarify", "sort it", S_SALES,
    lambda p: (not ops(p) and bool(p.get("clarification")), "expected a clarification"))
run("1.3 non-existent column -> clarify", "sort by Profit", S_SALES,
    lambda p: (bool(p.get("clarification")) and not ops(p), "expected clarify about missing column"))

# --- Bug-2 fix: merge two disjoint files + compute across them (PREFER TO ACT) #
run("BUG2 merge+multiply (prefer to act)",
    "merge both files and multiply Roll No. by Quantity", S_DISJOINT,
    lambda p: ("merge" in actions(p) and "add_formula_column" in actions(p)
               and actions(p).index("merge") < actions(p).index("add_formula_column"),
               "expected [merge, then add_formula_column] without clarifying"))

print(f"\n{passed} passed, {failed} failed, {skipped} skipped (rate-limited).")
# Don't hard-fail on skips; only real failures are a problem.
raise SystemExit(1 if failed else 0)
