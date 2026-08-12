"""ENGINE PHASE 5.9 — executive insight generator (BUILD).

Composes several INDEPENDENTLY-COMPUTED findings into a board-ready narrative (headline +
grounded insights, each with raw figures). Builds on the Phase-2.6 principle: every number is
calculated from the data, never model-generated, so nothing can be fabricated. This suite
proves both the composition AND the honesty guards:

  grounded      total / top-driver share / trend % match manual computation exactly.
  composition   overview + total + range + top_driver + trend, with the trend as headline.
  honest        tiny data → caveat, no invented trend; no numeric cols → structural only;
                no date column → the trend is OMITTED (never guessed); mixed-sign data → no
                fabricated >100% "share"; empty → "No data".
  end to end    /executive/summary returns the narrative + plain text.

No llm.py change → no schema/serving/quota risk; no battery rows (computed/endpoint).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_5_9.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p59.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import execsummary as E  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def kinds(s):
    return [i["kind"] for i in s["insights"]]


def fig(s, kind):
    return next((i["figures"] for i in s["insights"] if i["kind"] == kind), None)


print("ENGINE PHASE 5.9 — executive insight generator\n")

DATED = pd.DataFrame({
    "Month": pd.date_range("2026-01", periods=6, freq="MS").astype(str),
    "Region": ["N", "S", "N", "S", "N", "S"],
    "Revenue": [100, 200, 150, 250, 300, 260],
})

# ===================== composition + grounding =====================
s = E.generate(DATED, title="Q1 Sales")
check("composes overview + total + range + top_driver + trend",
      {"overview", "total", "range", "top_driver", "trend"} <= set(kinds(s)), str(kinds(s)))
check("every finding is marked grounded (computed)", s["grounded"] is True, "")
check("the headline is the period-over-period trend", s["headline"] == next(i["text"] for i in s["insights"] if i["kind"] == "trend"), s["headline"])
# grounded figures match manual math
check("total is exactly the column sum (1260)", fig(s, "total")["total"] == 1260, str(fig(s, "total")))
check("range avg/min/max are exact", fig(s, "range")["avg"] == 210 and fig(s, "range")["min"] == 100 and fig(s, "range")["max"] == 300, str(fig(s, "range")))
# top driver: Region S = 200+250+260 = 710 of 1260 = 56%
check("top-driver names the right leader + exact share", fig(s, "top_driver")["top"] == "S" and fig(s, "top_driver")["value"] == 710 and fig(s, "top_driver")["share_pct"] == 56, str(fig(s, "top_driver")))
# trend: Jun 260 vs May 300 = down 13%
check("trend % is exact and correctly signed (down 13%)", fig(s, "trend")["pct"] == -13 and fig(s, "trend")["last"] == 260 and fig(s, "trend")["prev"] == 300, str(fig(s, "trend")))
check("as_text renders the headline + bulleted findings", s["headline"] in E.as_text(s) and "•" in E.as_text(s), E.as_text(s)[:120])

# ===================== honesty: no date → NO trend (omitted, not guessed) =====================
NODATE = pd.DataFrame({"Region": ["N", "S", "N", "S"], "Revenue": [100, 300, 200, 400]})
s2 = E.generate(NODATE)
check("no date column → the trend insight is omitted (never invented)", "trend" not in kinds(s2), str(kinds(s2)))
check("without a trend, the headline falls back to the top driver", s2["headline"] == next(i["text"] for i in s2["insights"] if i["kind"] == "top_driver"), s2["headline"])

# ===================== honesty: mixed-sign → no fabricated >100% share =====================
MIXED = pd.DataFrame({"Unit": ["A", "B", "C"], "Profit": [500, -400, 100]})  # total 200, A alone = 250% of total
s3 = E.generate(MIXED)
check("mixed-sign data does NOT claim a bogus part-of-whole share", "top_driver" not in kinds(s3), str(kinds(s3)))
check("mixed-sign still reports the honest total/range", {"total", "range"} <= set(kinds(s3)), str(kinds(s3)))

# ===================== honesty: tiny / no-numeric / empty =====================
tiny = E.generate(pd.DataFrame({"A": [1]}))
check("tiny data → a caveat, and no fabricated trend", any("indicative" in x.lower() or "little" in x.lower() for x in tiny["caveats"]) and "trend" not in kinds(tiny), str(tiny["caveats"]))
nonum = E.generate(pd.DataFrame({"Name": ["a", "b", "c"], "City": ["x", "y", "z"]}))
check("no numeric columns → structural only + a caveat", kinds(nonum) == ["overview"] and nonum["caveats"], str(nonum))
empty = E.generate(pd.DataFrame())
check("empty data → 'No data to summarize' + empty caveat", empty["headline"] == "No data to summarize." and empty["insights"] == [], str(empty))

# ===================== data-quality note when material =====================
blanky = pd.DataFrame({"Region": ["N", "S", "N", "S"], "Rev": [10, 20, 30, 40], "Email": ["a@x", "", "", ""]})
sq = E.generate(blanky)
check("a materially-blank column surfaces a data-quality note", any(i["kind"] == "data_quality" and i["figures"]["column"] == "Email" for i in sq["insights"]), str(kinds(sq)))

# ===================== END TO END =====================
CSV = ("Month,Region,Revenue\n" + "\n".join(
    f"{mon},{reg},{rev}" for mon, reg, rev in [
        ("2026-01-01", "N", 100), ("2026-02-01", "S", 200), ("2026-03-01", "N", 150),
        ("2026-04-01", "S", 250), ("2026-05-01", "N", 300), ("2026-06-01", "S", 260)])).encode()
c.post("/inspect", data={"session_id": "ex"}, files=[("files", ("d.csv", CSV, "text/csv"))])
r = c.post("/executive/summary", data={"session_id": "ex", "title": "Board Review"}).json()
check("/executive/summary returns a grounded narrative", r.get("status") == "ok" and r.get("grounded") is True and r.get("title") == "Board Review", str(r)[:160])
check("/executive/summary includes multiple findings + a headline + text",
      len(r.get("insights", [])) >= 4 and r.get("headline") and r.get("text"), str(r.get("headline")))
check("/executive/summary trend figure is verifiable (down 13%)",
      any(i["kind"] == "trend" and i["figures"]["pct"] == -13 for i in r["insights"]), str([i.get("figures") for i in r["insights"]]))

m._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
