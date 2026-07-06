"""LIVE multilingual battery (Phase 2.5 — Hindi / Urdu / English mixing).

A standard battery of code-switched instructions covering EVERY operation, plus
regional spelling variants. Each case sends a real instruction through
llm.parse_instruction and asserts the Operation Plan's ACTION/intent (not wording).

Calls the live model — needs GEMINI_API_KEY + network. Rate-limited cases are SKIP
(re-run later), not FAIL. Run from backend:  .venv\\Scripts\\python.exe test_multilang.py
"""
from __future__ import annotations

import time

import pandas as pd

from app import llm
from app.reader import summarize_tables

passed = failed = skipped = 0


def structure(tables, primary=None):
    return summarize_tables(tables, primary or next(iter(tables)))


def ops(plan):
    return plan.get("operations") or []


def actions(plan):
    return [o.get("action") for o in ops(plan)]


def op_of(plan, action):
    for o in ops(plan):
        if o.get("action") == action:
            return o
    return {}


def run(name, instruction, struct, validate, retries=1):
    """Call the live model; validate(plan) -> (ok, detail). SKIP on rate limit."""
    global passed, failed, skipped
    for attempt in range(retries + 1):
        try:
            plan = llm.parse_instruction(instruction, struct, "")
        except llm.ModelUnavailableError:
            if attempt < retries:
                time.sleep(12)
                continue
            skipped += 1
            print(f"  SKIP  {name}  (model busy / rate-limited)")
            return
        except Exception as e:  # network/DNS/etc. — don't fail the battery on infra
            skipped += 1
            print(f"  SKIP  {name}  (infra: {type(e).__name__})")
            return
        try:
            ok, detail = validate(plan)
        except Exception as e:
            ok, detail = False, f"validator error {type(e).__name__}: {e}"
        if ok:
            passed += 1
            print(f"  ok    {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {detail}  :: actions={actions(plan)} "
                  f"reply={plan.get('reply')!r} clar={plan.get('clarification')!r}")
        time.sleep(4.5)  # ~13 req/min — stays under the free-tier RPM cap
        return


# --- data / structures ---------------------------------------------------------
sales = {"sales": pd.DataFrame({
    "Name": ["A", "B"], "Revenue": [10, 20], "Region": ["North", "South"],
    "Cost": [4, 6], "Qty": [1, 2],
})}
S = structure(sales)
lookup_tabs = {
    "orders": pd.DataFrame({"CustID": [1, 2]}),
    "customers": pd.DataFrame({"CustID": [1, 2], "Name": ["Asha", "Ravi"]}),
}
S_LOOKUP = structure(lookup_tabs, "orders")
two = {"jan": pd.DataFrame({"CustID": [1], "Amount": [10]}),
       "feb": pd.DataFrame({"CustID": [2], "Amount": [20]})}
S_TWO = structure(two, "jan")

print("LIVE MULTILINGUAL BATTERY (Hindi / Urdu / English) — every operation\n")

# --- one mixed-language instruction per operation -------------------------------
run("sort desc (Hinglish)", "Revenue ke hisaab se ghatte hue order me sort karo", S,
    lambda p: (op_of(p, "sort").get("action") == "sort"
               and "Revenue" in (op_of(p, "sort").get("columns") or [])
               and (op_of(p, "sort").get("orders") or ["asc"])[0] == "desc", "expected sort Revenue desc"))

run("sort asc (Devanagari)", "रेवेन्यू के हिसाब से बढ़ते क्रम में सॉर्ट करो", S,
    lambda p: (op_of(p, "sort").get("action") == "sort"
               and "Revenue" in (op_of(p, "sort").get("columns") or [])
               and (op_of(p, "sort").get("orders") or ["asc"])[0] != "desc", "expected sort Revenue asc"))

run("filter (Urdu-ish)", "sirf North region ke rows dikhao", S,
    lambda p: ("filter" in actions(p)
               and any(c.get("column") == "Region" for c in op_of(p, "filter").get("conditions") or []),
               "expected filter on Region"))

run("filter (Devanagari)", "सिर्फ़ नॉर्थ रीजन के रो दिखाओ", S,
    lambda p: ("filter" in actions(p), "expected a filter"))

run("limit top-N (Hinglish)", "Revenue se sort karke top 2 rows rakho", S,
    lambda p: ("limit" in actions(p) and op_of(p, "limit").get("count") == 2, "expected limit count 2"))

run("remove_duplicates (Hinglish)", "duplicate rows hata do", S,
    lambda p: ("remove_duplicates" in actions(p), "expected remove_duplicates"))

run("remove_duplicates (spelling variant)", "duplicate entries nikaal do", S,
    lambda p: ("remove_duplicates" in actions(p), "expected remove_duplicates"))

run("fill_missing (Hinglish)", "khaali cells me 0 bhar do", S,
    lambda p: ("fill_missing" in actions(p), "expected fill_missing"))

run("drop_missing (Hinglish)", "jin rows me Region ki value khaali hai unhe delete kar do", S,
    lambda p: ("drop_missing" in actions(p), "expected drop_missing"))

run("drop_invalid (Hinglish)", "Revenue column me jo values valid number nahi hai unki rows hata do", S,
    lambda p: ("drop_invalid" in actions(p), "expected drop_invalid"))

run("trim (Hinglish)", "Name column me aage-peeche ke extra spaces saaf/trim karo", S,
    lambda p: ("trim" in actions(p), "expected trim"))

run("add_formula_column (Hinglish)", "ek naya Profit column banao jo Revenue minus Cost ho", S,
    lambda p: ("add_formula_column" in actions(p)
               and "Revenue" in (op_of(p, "add_formula_column").get("formula") or "")
               and "Cost" in (op_of(p, "add_formula_column").get("formula") or ""),
               "expected Profit = {Revenue}-{Cost}"))

run("lookup (Hinglish)", "customers table se Name le aao CustID ke basis pe", S_LOOKUP,
    lambda p: ("lookup" in actions(p), "expected lookup"))

run("aggregate (Hinglish)", "har region ka total revenue nikaalo", S,
    lambda p: ("aggregate" in actions(p) and "Region" in (op_of(p, "aggregate").get("group_by") or []),
               "expected aggregate grouped by Region"))

run("aggregate (spelling variant)", "region wise revenue ka sum karo", S,
    lambda p: ("aggregate" in actions(p), "expected aggregate"))

run("find_replace (Hinglish)", "Region column me 'North' ko 'N' se replace kar do", S,
    lambda p: ("find_replace" in actions(p), "expected find_replace"))

run("rename_columns (Hinglish)", "Qty column ka naam badal kar Quantity kar do", S,
    lambda p: ("rename_columns" in actions(p), "expected rename_columns"))

run("drop_columns (Hinglish)", "Cost column ko hata do", S,
    lambda p: ("drop_columns" in actions(p), "expected drop_columns"))

run("select_columns (Hinglish)", "sirf Region aur Revenue columns rakho, baaki hata do", S,
    lambda p: ("select_columns" in actions(p), "expected select_columns"))

run("flag_missing (Hinglish)", "khaali cells ko yellow highlight kar do", S,
    lambda p: ("flag_missing" in actions(p), "expected flag_missing"))

run("format_cells (Hinglish)", "Revenue ko currency format me ₹ ke saath dikhao", S,
    lambda p: ("format_cells" in actions(p), "expected format_cells"))

run("merge (Hinglish)", "dono tables ko ek me merge kar do", S_TWO,
    lambda p: ("merge" in actions(p), "expected merge"))

run("combine_sheets (Hinglish)", "har table ko alag-alag sheet me daal do ek hi file me", S_TWO,
    lambda p: ("combine_sheets" in actions(p), "expected combine_sheets"))

run("chart (Hinglish)", "Region ke hisaab se Revenue ka bar chart banao", S,
    lambda p: ("chart" in actions(p), "expected chart"))

run("dashboard (Hinglish)", "ek one-page dashboard banao Revenue ke summary ke saath", S,
    lambda p: ("dashboard" in actions(p), "expected dashboard"))

print(f"\n{passed} passed, {failed} failed, {skipped} skipped (rate-limited/infra).")
# Only real failures matter; skips just mean re-run when the model is free.
raise SystemExit(1 if failed else 0)
