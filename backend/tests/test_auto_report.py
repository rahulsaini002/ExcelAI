"""Uploading ANY file must produce a real report — no template, no model call.

THE USER'S POSITION, and it is the right one: "revenue and all other things should not
matter, it should generate live ui and downloadable reports for whatever has been
uploaded". The template-first flow could only fill blocks chosen before the file existed,
so a campus-recruitment shortlist came out blank.

What is pinned here:
  * a report is produced for data with NO business columns at all;
  * it is produced WITHOUT the model, so a spent free-tier quota cannot stop it;
  * nothing is labelled as something it isn't — serial columns are not summed, scores are
    not totalled, and one person is never "the largest contributor".

Run: .venv\\Scripts\\python.exe tests\\test_auto_report.py
"""
from __future__ import annotations

import io
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

_fd, _db = tempfile.mkstemp(suffix="-auto.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + Path(_db).as_posix()

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import autoreport, execsummary, llm  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def shortlist(rows: int = 548) -> pd.DataFrame:
    """The shape of the file that exposed this: no revenue, a serial column, scores."""
    return pd.DataFrame({
        "S.NO. ": range(1, rows + 1),
        "Roll No": [102100000 + i for i in range(rows)],
        "Name": [f"Student {i}" for i in range(rows)],
        "Gender": ["Female" if i % 2 else "Male" for i in range(rows)],
        "Branches": [["CSE", "ECE", "MECH", "CIVIL", "CHEM"][i % 5] for i in range(rows)],
        "Present CGPA": [7.5 + (i % 20) / 10 for i in range(rows)],
    })


def as_xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


print("AUTO REPORT — any file, no template, no model\n")

# The model must never be reached. If it is, this raises and the test fails loudly.
_real = llm.assign_report_metrics


def _boom(*a, **k):
    raise AssertionError("the auto report must not call the model")


llm.assign_report_metrics = _boom
try:
    df = shortlist()
    r = client.post("/report/auto",
                    files=[("files", ("shortlist.xlsx", as_xlsx(df), "application/octet-stream"))])
    j = r.json()
    check("answers 200 for a file with no business columns", r.status_code == 200, r.text[:200])

    blocks = j.get("blocks", [])
    kinds = [b["type"] for b in blocks]
    check("produces a real report, not an empty shell", len(blocks) >= 6, f"{len(blocks)} blocks")
    check("with KPIs, a chart and tables", {"kpi", "chart", "table"} <= set(kinds), str(set(kinds)))

    kpis = {b["title"]: b for b in blocks if b["type"] == "kpi"}
    check("every KPI has a value", all(b.get("value") for b in kpis.values()), str(kpis)[:200])
    check("every KPI says what it measured",
          all(b.get("basis") for b in kpis.values()),
          str({k: v.get("basis") for k, v in kpis.items()})[:240])
    check("row count is present and correct", kpis.get("Records", {}).get("value") == "548",
          str(kpis.get("Records")))

    tables = [b for b in blocks if b["type"] == "table"]
    check("tables carry real rows", all(b.get("rows") for b in tables),
          str([(b["title"], len(b.get("rows") or [])) for b in tables]))
    check("one table shows the actual data rows",
          any(b["title"].startswith("First ") and len(b["rows"]) > 0 for b in tables),
          str([b["title"] for b in tables]))

    chart = next((b for b in blocks if b["type"] == "chart"), None)
    check("the chart has a real series", bool(chart and chart.get("data")), str(chart)[:160])

    # --- the honesty rules -------------------------------------------------------------
    titles = " | ".join(b["title"] for b in blocks)
    check("no KPI invents a business metric the file lacks",
          not any(w in titles.lower() for w in ("revenue", "orders", "order value")), titles)

    bases = " ".join(b.get("basis", "") for b in blocks if b["type"] == "kpi")
    check("a serial column is never averaged or totalled",
          "S.NO." not in bases and "Roll No" not in bases, bases)

    narrative = next((b for b in blocks if b["type"] == "narrative"), None)
    text = (narrative or {}).get("text", "")
    check("the narrative never totals a serial column", "Total S.NO." not in text, text[:200])
    check("the narrative never totals a score (a CGPA is a level, not a quantity)",
          "Total Present CGPA" not in text, text[:200])
    check("no single person is called the largest contributor",
          "Student " not in text, text[:200])

    # --- it groups by something informative ---------------------------------------------
    check("breaks the data down by a column that actually groups it",
          any("Branches" in b["title"] for b in blocks),
          str([b["title"] for b in blocks]))

    # --- empty and odd inputs -----------------------------------------------------------
    r2 = client.post("/report/auto",
                     files=[("files", ("empty.xlsx", as_xlsx(pd.DataFrame({"A": []})),
                                       "application/octet-stream"))])
    check("an empty sheet is refused clearly, not reported on", r2.status_code == 400,
          f"HTTP {r2.status_code} {r2.text[:120]}")

    text_only = pd.DataFrame({"City": ["Pune", "Delhi", "Pune"], "Note": ["a", "b", "c"]})
    r3 = client.post("/report/auto",
                     files=[("files", ("text.xlsx", as_xlsx(text_only), "application/octet-stream"))])
    j3 = r3.json()
    check("a file with NO numeric columns still gets a report",
          r3.status_code == 200 and len(j3.get("blocks", [])) >= 4,
          f"HTTP {r3.status_code} blocks={len(j3.get('blocks', []))}")
finally:
    llm.assign_report_metrics = _real

# --- unit level -------------------------------------------------------------------------
df = shortlist()
check("is_measure rejects a named id column", not autoreport.is_measure(df, "Roll No"))
check("is_measure rejects an unnamed serial run", not autoreport.is_measure(df, "S.NO. "))
check("is_measure accepts a genuine score", autoreport.is_measure(df, "Present CGPA"))
check("choose_dimension prefers a grouping column over a name",
      autoreport.choose_dimension(df) in ("Branches", "Gender"),
      str(autoreport.choose_dimension(df)))

summary = execsummary.generate(df, title="x")
kinds = [i["kind"] for i in summary["insights"]]
check("execsummary omits a total for non-additive measures", "total" not in kinds, str(kinds))

sales = pd.DataFrame({"Region": ["N", "S", "N", "E"], "Revenue": [10.0, 20.0, 30.0, 5.0]})
kinds2 = [i["kind"] for i in execsummary.generate(sales, title="s")["insights"]]
check("but still totals a genuinely additive one", "total" in kinds2, str(kinds2))

print(f"\n{passed} passed, {failed} failed.")
sys.exit(1 if failed else 0)
