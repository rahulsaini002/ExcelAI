"""A report must never present a figure under a label the data can't support.

FROM A REAL USER REPORT: a campus-recruitment shortlist (548 students; columns Roll No,
Name, Gender, Branches, CGPA, Email) was run through the "Executive Summary" template.
The result had a blank Total Revenue, a blank Avg Order Value, empty charts — and
"Orders: 548". That 548 was the ROW COUNT. The number was real; the label was not. The
report then exported to CSV, XLSX and PDF in exactly that state, with nothing anywhere
explaining why it was empty.

Two properties are pinned here:
  1. a computed KPI carries a BASIS saying what it was measured from, so a count can't
     quietly read as a business metric;
  2. blocks the data cannot fill are REPORTED with a reason, not left silently blank.

The fixture is synthetic on purpose — the same shape as the user's file, but owned by
this repo.

Run: .venv\Scripts\python.exe tests\test_report_compute_honesty.py
"""
from __future__ import annotations

import io
import json
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

_fd, _db = tempfile.mkstemp(suffix="-rep.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + Path(_db).as_posix()

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import llm, main  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
client = TestClient(main.app)
passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def shortlist_xlsx(rows: int = 548) -> bytes:
    df = pd.DataFrame({
        "Roll No": range(1, rows + 1),
        "Name": [f"Student {i}" for i in range(1, rows + 1)],
        "Gender": ["F" if i % 2 else "M" for i in range(1, rows + 1)],
        "Branches": ["CSE", "ECE", "MECH", "CIVIL"] * (rows // 4) + ["CSE"] * (rows % 4),
        "Present CGPA": [6 + (i % 40) / 10 for i in range(rows)],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


EXEC_BLOCKS = [
    {"type": "narrative", "title": "Executive insight", "text": ""},
    {"type": "kpi", "title": "Total Revenue"},
    {"type": "kpi", "title": "Orders"},
    {"type": "kpi", "title": "Avg Order Value"},
    {"type": "chart", "title": "Revenue by month"},
    {"type": "table", "title": "Detail table"},
]

# What the model actually did with this file: it had no revenue column to point at, and
# mapped "Orders" to a plain row count.
def _fake_assign(block_list, structure):
    return {"items": [
        {"index": 1, "metric": {"agg": "sum", "column": "Revenue", "format": "currency"}},
        {"index": 2, "metric": {"agg": "count"}},
        {"index": 3, "metric": {"agg": "mean", "column": "Order Value"}},
        {"index": 4, "metric": {}},
        {"index": 5, "metric": {}},
    ]}


print("REPORT COMPUTE — no figure under a label the data can't support\n")

_real = getattr(llm, "assign_report_metrics", None)
llm.assign_report_metrics = _fake_assign
try:
    r = client.post(
        "/report/compute",
        data={"blocks": json.dumps(EXEC_BLOCKS)},
        files=[("files", ("shortlist.xlsx", shortlist_xlsx(), "application/octet-stream"))],
    )
    j = r.json()
    check("compute answers 200", r.status_code == 200, r.text[:160])

    kpis = {b["title"]: b for b in j["blocks"] if b["type"] == "kpi"}

    check("the row count is still computed (it is a real number)",
          kpis["Orders"].get("value") == "548", str(kpis["Orders"])[:160])
    check("...but it now SAYS it is a row count, so the label can't mislead",
          kpis["Orders"].get("basis") == "count of rows", str(kpis["Orders"])[:160])

    check("a KPI with no matching column stays empty",
          kpis["Total Revenue"].get("value") is None, str(kpis["Total Revenue"])[:160])

    unfilled = {u["title"]: u["reason"] for u in j.get("unfilled", [])}
    check("the empty KPI is REPORTED, not silently blank",
          "Total Revenue" in unfilled, str(unfilled)[:200])
    check("and the reason names the missing column",
          "Revenue" in unfilled.get("Total Revenue", ""), unfilled.get("Total Revenue", ""))
    check("the unfillable chart is reported too",
          "Revenue by month" in unfilled, str(list(unfilled))[:200])
    check("the unfillable table is reported too",
          "Detail table" in unfilled, str(list(unfilled))[:200])
    check("narrative blocks are never reported as unfillable (the user writes those)",
          "Executive insight" not in unfilled, str(list(unfilled))[:200])
    check("the filled count matches what actually got a value",
          j.get("filled") == 1, str(j.get("filled")))

    # A file that CAN fill the report must not be warned about.
    sales = pd.DataFrame({"Region": ["N", "S", "N"], "Revenue": [10.0, 20.0, 30.0]})
    buf = io.BytesIO()
    sales.to_excel(buf, index=False)

    def _sales_assign(block_list, structure):
        return {"items": [{"index": 0, "metric": {"agg": "sum", "column": "Revenue"}}]}

    llm.assign_report_metrics = _sales_assign
    r2 = client.post(
        "/report/compute",
        data={"blocks": json.dumps([{"type": "kpi", "title": "Total Revenue"}])},
        files=[("files", ("sales.xlsx", buf.getvalue(), "application/octet-stream"))],
    )
    j2 = r2.json()
    k2 = j2["blocks"][0]
    check("a real metric still computes", k2.get("value") is not None, str(k2)[:160])
    check("and its basis names the column it summed",
          k2.get("basis") == "sum of Revenue", str(k2)[:160])
    check("nothing is reported unfillable when everything filled",
          not j2.get("unfilled"), str(j2.get("unfilled"))[:160])
finally:
    if _real is not None:
        llm.assign_report_metrics = _real

print(f"\n{passed} passed, {failed} failed.")
sys.exit(1 if failed else 0)
