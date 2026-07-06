"""Tests for Phase 2.9 — plain-language insights.

Insights are COMPUTED from the data (never an LLM free-generation), so numbers can't be
fabricated. Covers: factual correctness, the numeric-only fallback, and caution when the
data is too small/unsuitable to conclude. Run: python test_insight.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd

from app.main import _summarize_insight

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("Running insight checks...\n")

# category + value: N totals 100+150=250 of grand 500 → 50% (largest)
sales = pd.DataFrame({"Region": ["N", "S", "N", "E"], "Revenue": [100, 200, 150, 50]})
ins = _summarize_insight(sales, "t")
check("insight is produced for adequate data", bool(ins), repr(ins))
check("insight names the grouping column", ins and "Region" in ins, repr(ins))
check("insight calls out the largest", ins and "largest" in ins, repr(ins))
check("insight share matches MANUAL (50%)", ins and "50%" in ins, repr(ins))
check("insight cites REAL totals (250 of 500)", ins and "250" in ins and "500" in ins, repr(ins))

# no fabrication: recompute the share independently and confirm it appears verbatim
expected_share = round((100 + 150) / 500 * 100)  # 50
check("share equals an independently recomputed value", ins and f"{expected_share}%" in ins, repr(ins))

# numeric-only fallback: avg + range (all real)
nums = pd.DataFrame({"X": [10, 20, 30]})
ins2 = _summarize_insight(nums, "t")
check("numeric-only insight gives avg + range",
      ins2 and "averages" in ins2 and "20" in ins2 and "10" in ins2 and "30" in ins2, repr(ins2))

# cautious: too small / empty / no numeric → NO claim
check("too-small data → no insight (cautious)",
      _summarize_insight(pd.DataFrame({"Region": ["N", "S"], "Revenue": [1, 2]}), "t") is None)
check("empty data → no insight",
      _summarize_insight(pd.DataFrame({"Region": [], "Revenue": []}), "t") is None)
check("no numeric column → no insight",
      _summarize_insight(pd.DataFrame({"A": ["x", "y", "z"], "B": ["p", "q", "r"]}), "t") is None)

# workbook (dict) result is summarized via its named sheet
check("workbook result summarized via its sheet",
      _summarize_insight({"main": sales}, "main") is not None)

# a numeric column must NOT be mistaken for a date (no fabricated period trend on sales)
check("numeric column not mistaken for a date", ins and " vs " not in ins, repr(ins))

# --- period-over-period (the "down X% vs last month" path) ---
# May = 60+40 = 100, June = 50+32 = 82 → down 18%, driven by Region N (−10 of −18)
pop = pd.DataFrame({
    "Date": ["2026-05-03", "2026-05-20", "2026-06-04", "2026-06-18"],
    "Region": ["N", "S", "N", "S"],
    "Revenue": [60, 40, 50, 32],
})
p = _summarize_insight(pop, "t")
check("PoP reports the real direction + % (down 18%)", p and "down" in p and "18%" in p, repr(p))
check("PoP uses real period labels (Jun 2026 vs May 2026)",
      p and "Jun 2026" in p and "May 2026" in p, repr(p))
check("PoP cites real totals (82 vs 100)", p and "82" in p and "100" in p, repr(p))
check("PoP names the driver (Region N)", p and "driven mostly" in p and "Region" in p, repr(p))

# cautious: a single period can't be compared → falls back to the top-contributor insight
one = pd.DataFrame({
    "Date": ["2026-06-01", "2026-06-10", "2026-06-20"],
    "Region": ["N", "S", "N"], "Revenue": [10, 20, 30],
})
o = _summarize_insight(one, "t")
check("single period → no PoP, falls back", o and " vs " not in o and "largest" in o, repr(o))

# cautious: a zero previous-period base can't yield a % change → no PoP claim
zero = pd.DataFrame({"Date": ["2026-05-01", "2026-06-01", "2026-06-15"], "Revenue": [0, 30, 20]})
z = _summarize_insight(zero, "t")
check("zero base → no PoP claim", z is not None and " vs " not in z, repr(z))

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
