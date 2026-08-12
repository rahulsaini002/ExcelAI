"""ENGINE PHASE 2.6 — plain-language insights (NO AI).

The one job of an insight is HONESTY: every figure must be independently recomputable
from the data, it must never fabricate a trend, and it must stay silent (return None)
when the data is too small or unsuitable to support a claim. This suite proves that:
it recomputes every number the insight prints and checks it matches, and it feeds a
battery of degenerate shapes (tiny, empty, mixed-sign, near-unique category, single
period, zero base, numeric-looks-like-a-date) and asserts the engine declines rather
than invents.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_2_6.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p26.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402

from app.main import _summarize_insight  # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def ins(df) -> str | None:
    return _summarize_insight(df, "t")


print("ENGINE PHASE 2.6 — plain-language insights (no AI)\n")

# ============ VERIFIABILITY: every printed figure is recomputable ============
sales = pd.DataFrame({"Region": ["N", "S", "N", "E"], "Revenue": [100, 200, 150, 50]})
s = ins(sales)
# recompute the claim independently
grouped = sales.groupby("Region")["Revenue"].sum()
top_val, total = float(grouped.max()), float(sales["Revenue"].sum())
share = round(top_val / total * 100)
check("top-contributor: names the real winner (N)", s and "“N”" in s, repr(s))
check("top-contributor: share is the recomputed value (50%)", s and f"{share}%" in s, repr(s))
check("top-contributor: cites the real totals (250 of 500)",
      s and "250" in s and "500" in s, repr(s))

nums = pd.DataFrame({"X": [10, 20, 30, 40]})
s2 = ins(nums)
check("numeric-only: avg + range are the real figures (25, 10, 40)",
      s2 and "25" in s2 and "10" in s2 and "40" in s2 and "averages" in s2, repr(s2))

# period-over-period: recompute the % change and the labels
pop = pd.DataFrame({
    "Date": ["2026-05-03", "2026-05-20", "2026-06-04", "2026-06-18"],
    "Region": ["N", "S", "N", "S"], "Revenue": [60, 40, 50, 32],
})
p = ins(pop)
may, jun = 100.0, 82.0
pct = round((jun - may) / abs(may) * 100)  # -18
check("PoP: real direction + recomputed % (down 18%)", p and "down" in p and f"{abs(pct)}%" in p, repr(p))
check("PoP: real period labels + totals (Jun 2026 82 vs May 2026 100)",
      p and "Jun 2026" in p and "May 2026" in p and "82" in p and "100" in p, repr(p))

# GENERIC no-fabrication: every integer the insight prints must exist as a derivable
# figure from the data (group sums, grand total, share%, min/max/avg, period totals).
def derivable_numbers(df) -> set[int]:
    got: set[int] = set()
    for col in df.columns:
        v = pd.to_numeric(df[col], errors="coerce").dropna()
        if v.empty:
            continue
        vals = [float(v.sum()), float(v.mean()), float(v.min()), float(v.max())]
        for other in df.columns:
            if other != col and not pd.api.types.is_numeric_dtype(df[other]):
                g = pd.DataFrame({"c": df[other].astype(str), "v": pd.to_numeric(df[col], errors="coerce")}).dropna()
                gg = g.groupby("c")["v"].sum()
                vals += [float(x) for x in gg.values]
                if float(v.sum()):
                    vals += [round(float(x) / float(v.sum()) * 100) for x in gg.values]
        for x in vals:
            got.add(round(x))
            got.add(int(x))
    return got

# (PoP is checked separately above — its figures are month-period totals, which this
# category-only helper doesn't enumerate; the dedicated PoP checks recompute them.)
for label, df in [("top-contributor", sales), ("numeric-only", nums)]:
    text = ins(df) or ""
    printed = {int(n) for n in re.findall(r"\d+", text)}
    ok = derivable_numbers(df) | {2026}  # year labels are real calendar facts
    stray = printed - ok
    check(f"no-fabrication: every number in the {label} insight is derivable",
          not stray, f"stray {stray} in {text!r}")

# ============ CAUTION: decline (None) on unsuitable data ============
check("too few rows (<3) → None", ins(pd.DataFrame({"R": ["N", "S"], "V": [1, 2]})) is None)
check("empty → None", ins(pd.DataFrame({"R": [], "V": []})) is None)
check("no numeric column → None", ins(pd.DataFrame({"A": list("xyz"), "B": list("pqr")})) is None)
check("single group (category has one value) → no share claim",
      (ins(pd.DataFrame({"R": ["N", "N", "N"], "V": [1, 2, 3]})) or "").find("largest") == -1)
check("near-unique category (every row distinct) → no share claim",
      "largest" not in (ins(pd.DataFrame({"ID": ["a", "b", "c"], "V": [1, 2, 3]})) or ""))

# MIXED-SIGN: a loss-making group means top_val can exceed the total → a "% of total"
# share would read like a fabricated >100%. The engine must NOT make that claim; it
# falls through to the always-correct average+range.
mixed = pd.DataFrame({"Region": ["N", "S", "E"], "Profit": [100, -80, 30]})
mi = ins(mixed)
check("mixed-sign: NO misleading '% of' share claim", mi is not None and "% of" not in mi, repr(mi))
check("mixed-sign: falls through to a correct avg+range", mi and "averages" in mi, repr(mi))
# and the avg/range printed are real
pv = mixed["Profit"]
check("mixed-sign: the range figures are real (min -80 / max 100)",
      mi and "100" in mi and ("80" in mi), repr(mi))

# single period → no PoP, falls back to top-contributor
one = pd.DataFrame({"Date": ["2026-06-01", "2026-06-10", "2026-06-20"],
                    "Region": ["N", "S", "N"], "V": [10, 20, 30]})
o = ins(one)
check("single period → no ' vs ' PoP claim", o and " vs " not in o)
check("zero base → no PoP claim",
      (lambda z: z is not None and " vs " not in z)(
          ins(pd.DataFrame({"Date": ["2026-05-01", "2026-06-01", "2026-06-15"], "V": [0, 30, 20]}))))

# numeric column must NOT be mistaken for a date (no fabricated time trend)
check("numeric column not read as a date", s and " vs " not in s, repr(s))

# ============ shape coverage ============
check("workbook (dict) result summarized via its sheet", ins({"t": sales}) is not None)
check("all-blank numeric column (after coerce) → None or safe",
      ins(pd.DataFrame({"R": ["N", "S", "E"], "V": [None, None, None]})) is None
      or "averages" in (ins(pd.DataFrame({"R": ["N", "S", "E"], "V": [None, None, None]})) or ""))

# rounding sanity: a 1/3 share rounds to 33, and that exact value appears. (Needs
# nunique < rows, else the category is treated as near-unique and correctly skipped.)
thirds = pd.DataFrame({"G": ["a", "b", "c", "a", "b", "c"], "V": [50, 50, 50, 50, 50, 50]})
t = ins(thirds)
check("share rounding is honest (33% for a third)", t and "33%" in t, repr(t))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
