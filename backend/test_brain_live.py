"""LIVE Brain tests — verifies the AI-dependent half that the per-feature suites
could not (they test the executor; this tests Gemini's instruction -> Operation Plan).
Covers the "couldn't live-verify (Gemini rate-limited)" items across Features 1.2-1.14
plus the merge "prefer to act" fix.

Needs a working GEMINI_API_KEY (already in backend/.env). Rate-limited calls are
reported as SKIP, not FAIL, so a busy free tier doesn't look like a regression.
Run from backend:  .venv\\Scripts\\python.exe test_brain_live.py
"""
from __future__ import annotations

import time

from app import llm

passed = failed = skipped = 0
GAP = 7.0          # seconds between calls (~8/min, under the ~10 RPM free-tier limit)
RATE_WAIT = 30.0   # on a 429, the free tier wants ~30-60s — wait it out and retry
RATE_RETRIES = 3

# --- sample structures the Brain plans against ----------------------------- #
SALES = {
    "tables": [{
        "name": "sales",
        "columns": [
            {"name": "Name", "type": "text"},
            {"name": "Region", "type": "text"},
            {"name": "Revenue", "type": "number"},
            {"name": "Date", "type": "date"},
        ],
        "row_count": 50,
    }],
    "primary": "sales",
}
SURVEY = {
    "tables": [{
        "name": "survey",
        "columns": [{"name": "Person", "type": "text"}, {"name": "Response", "type": "text"}],
        "row_count": 20,
    }],
    "primary": "survey",
}
PRICING = {
    "tables": [{
        "name": "items",
        "columns": [{"name": "Qty", "type": "number"}, {"name": "Price", "type": "number"}],
        "row_count": 8,
    }],
    "primary": "items",
}
LOOKUP = {
    "tables": [
        {"name": "Orders", "columns": [{"name": "CustID", "type": "number"}, {"name": "Amount", "type": "number"}], "row_count": 10},
        {"name": "People", "columns": [{"name": "ID", "type": "number"}, {"name": "Name", "type": "text"}], "row_count": 10},
    ],
    "primary": "Orders",
}
DISJOINT = {
    "tables": [
        {"name": "Testing 1", "columns": [{"name": "Name", "type": "text"}, {"name": "Roll No.", "type": "number"}], "row_count": 10},
        {"name": "testing", "columns": [{"name": "product", "type": "number"}, {"name": "Quantity", "type": "number"}], "row_count": 12},
    ],
    "primary": "Testing 1",
}


def actions(plan):
    return [o.get("action") for o in (plan.get("operations") or [])]


def first(plan):
    ops = plan.get("operations") or []
    return ops[0] if ops else {}


def case(name, structure, instruction, predicate, history=""):
    """Run one live instruction; PASS/FAIL by predicate(plan). On a rate limit, wait
    out the free-tier window and retry a few times; only SKIP if it stays busy."""
    global passed, failed, skipped
    plan = None
    for attempt in range(RATE_RETRIES + 1):
        try:
            plan = llm.parse_instruction(instruction, structure, history)
            break
        except llm.ModelUnavailableError:
            if attempt < RATE_RETRIES:
                time.sleep(RATE_WAIT)
                continue
            skipped += 1
            print(f"  SKIP  {name}  (model busy / rate-limited after {RATE_RETRIES} retries)")
            return
        except Exception as e:  # pragma: no cover - unexpected
            failed += 1
            print(f"  FAIL  {name}  unexpected error: {type(e).__name__}: {str(e)[:160]}")
            return
    try:
        ok, detail = predicate(plan)
    except Exception as e:
        ok, detail = False, f"predicate error: {e}"
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}  | actions={actions(plan)} clar={bool(plan.get('clarification'))} reply={bool(plan.get('reply'))}")
    time.sleep(GAP)


print("LIVE BRAIN TESTS — instruction -> Operation Plan (Gemini)\n")

# --- 1.2 / 1.4 language understanding: sort in English, Hindi, Hinglish ----- #
case("1.2 English sort desc", SALES, "sort by Revenue from highest to lowest",
     lambda p: ("sort" in actions(p) and "Revenue" in (first(p).get("columns") or [])
                and (first(p).get("orders") or ["desc"])[0] == "desc", "expected sort Revenue desc"))
case("1.2 Hindi sort desc", SALES, "रेवेन्यू के हिसाब से घटते क्रम में सॉर्ट करो",
     lambda p: ("sort" in actions(p) and "Revenue" in (first(p).get("columns") or []), "expected sort Revenue"))
case("1.2 Hinglish dedupe", SALES, "duplicate rows hata do",
     lambda p: ("remove_duplicates" in actions(p), "expected remove_duplicates"))
case("1.2 Urdu-ish filter", SALES, "sirf North region ke rows dikhao",
     lambda p: ("filter" in actions(p), "expected filter"))

# --- 1.4 / 1.5 sort + filter chained --------------------------------------- #
case("1.5 filter then sort", SALES, "keep only North region, then sort by Revenue highest first",
     lambda p: ("filter" in actions(p) and "sort" in actions(p), "expected filter + sort"))

# --- 1.3 / 1.14-b unsupported -> reply (no operations) ---------------------- #
case("1.14-b unsupported (chart)", SALES, "make a pie chart of revenue by region",
     lambda p: (not (p.get("operations") or []) and bool(p.get("reply")), "expected reply, no ops"))
case("1.14-b unsupported (forecast)", SALES, "predict next month's revenue",
     lambda p: (not (p.get("operations") or []) and bool(p.get("reply")), "expected reply, no ops"))

# --- 1.3 / 1.14-c ambiguous -> clarification ------------------------------- #
case("1.14-c non-existent column", SALES, "sort by Profit",
     lambda p: (bool(p.get("clarification")) and not (p.get("operations") or []), "expected clarification"))

# --- data question -> reply ------------------------------------------------ #
case("data question -> reply", SALES, "what columns are there?",
     lambda p: (bool(p.get("reply")) and not (p.get("operations") or []), "expected reply"))

# --- 1.7-f statistical fill declined -> reply ------------------------------ #
case("1.7-f decline average-fill", SALES, "fill the blank revenue cells with the average revenue",
     lambda p: (bool(p.get("reply")) and not (p.get("operations") or []), "expected friendly decline reply"))

# --- 1.8 formula column ---------------------------------------------------- #
case("1.8 formula column", PRICING, "add a Total column that is Qty times Price",
     lambda p: ("add_formula_column" in actions(p) and "*" in (first(p).get("formula") or ""), "expected add_formula_column with *"))

# --- 1.9 lookup (Hinglish, cross-table) ------------------------------------ #
case("1.9 lookup Hinglish", LOOKUP, "Orders me har CustID ke saamne People se uska Name lao",
     lambda p: ("lookup" in actions(p) and first(p).get("return_column") == "Name", "expected lookup returning Name"))

# --- 1.10 aggregate: grouped sum + count-of-value -------------------------- #
case("1.10 grouped sum (Hinglish)", SALES, "har region ka total revenue nikaalo",
     lambda p: ("aggregate" in actions(p) and "Region" in (first(p).get("group_by") or [])
                and first(p).get("agg_func") == "sum", "expected aggregate sum group_by Region"))
case("1.10 count of 'Yes'", SURVEY, "count how many responses were Yes",
     lambda p: ("aggregate" in actions(p) and first(p).get("agg_func") == "count"
                and ((first(p).get("count_value") or "").strip().lower() == "yes"), "expected count with count_value=Yes"))

# --- 1.11 formatting: currency + date -------------------------------------- #
case("1.11 currency format", SALES, "show Revenue as currency with 2 decimals",
     lambda p: ("format_cells" in actions(p) and first(p).get("number_format") == "currency", "expected currency format"))
case("1.11 date format DD-MM-YYYY", SALES, "format the Date column as DD-MM-YYYY",
     lambda p: ("format_cells" in actions(p) and first(p).get("number_format") == "date", "expected date format"))

# --- 1.12 find & replace --------------------------------------------------- #
case("1.12 find & replace (Hinglish)", SALES, "sab 'mumbai' ko 'Mumbai' kar do Region me",
     lambda p: ("find_replace" in actions(p) and (first(p).get("replace") or "") == "Mumbai", "expected find_replace -> Mumbai"))

# --- NEW: merge two disjoint files + compute across them (prefer to act) ---- #
case("merge + multiply (prefer to act)", DISJOINT,
     "merge both files and multiply Roll No. by Quantity",
     lambda p: ("merge" in actions(p) and "add_formula_column" in actions(p)
                and not p.get("clarification"), "expected [merge, add_formula_column], no clarification"))

print(f"\n{passed} passed, {failed} failed, {skipped} skipped (rate-limited).")
# Only real FAILs break the build; SKIPs (busy free tier) do not.
raise SystemExit(1 if failed else 0)
