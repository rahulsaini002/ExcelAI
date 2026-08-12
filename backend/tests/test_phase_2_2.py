"""ENGINE PHASE 2.2 — verify & harden reshaping: unpivot / pivot / transpose (NO AI).

Program 2.2 acceptance: pivot totals match manual, unpivot is reversible, transpose
flips correctly, ragged data handled. Plus the honesty hardening added this phase:
pivot fills 0 only for sum/count (average/min/max leave absent combos BLANK, never
fabricate a 0), non-numeric value cells are ignored + counted (text value column →
friendly "looks like text"), transpose refuses a duplicate header column, and unpivot
refuses a column used as both id and value.

Direct Hands calls check exact messages; a couple of rows go through the real API
(/inspect → /execute → /download) to confirm the reshaped grid is downloadable.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_2_2.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p22.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import openpyxl  # noqa: E402
import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.executor import execute_multi  # noqa: E402
from app.main import app  # noqa: E402
from app.operations.base import OperationError  # noqa: E402
from app.operations.reshape import pivot, transpose, unpivot  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0
OCT = "application/octet-stream"


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def run(df: pd.DataFrame, ops: list[dict]):
    res, _, notes, _ = execute_multi({"t": df.copy()}, "t", ops)
    return res, " ".join(notes)


def err(fn, df, op) -> str:
    try:
        fn(df, op)
        return ""
    except OperationError as e:
        return str(e)


print("ENGINE PHASE 2.2 — reshaping (unpivot / pivot / transpose), no AI\n")

WIDE = pd.DataFrame({"Region": ["N", "S"], "Jan": [1, 2], "Feb": [3, 4]})
LONG = pd.DataFrame({"Region": ["N", "N", "S", "N"], "Month": ["Jan", "Jan", "Jan", "Feb"],
                     "Sales": [10, 5, 20, 7]})

# ============================ UNPIVOT ============================
res, note = run(WIDE, [{"action": "unpivot", "id_columns": ["Region"],
                        "value_columns": ["Jan", "Feb"], "var_name": "Month", "value_name": "Sales"}])
check("unpivot: wide→long row count = rows × value cols", len(res) == 4, f"{len(res)}")
check("unpivot: columns are id + var + value",
      set(res.columns) == {"Region", "Month", "Sales"}, str(list(res.columns)))
check("unpivot: value lands correctly (N/Feb = 3)",
      res[(res.Region == "N") & (res.Month == "Feb")].Sales.iloc[0] == 3, res.to_string())
check("unpivot: note names the new columns + count",
      "Month" in note and "Sales" in note and "4 rows" in note, note)

# value_columns omitted -> everything except id becomes rows
res2, _ = run(WIDE, [{"action": "unpivot", "id_columns": ["Region"]}])
check("unpivot: omitting value_columns melts all non-id columns", len(res2) == 4, f"{len(res2)}")

# REVERSIBLE: unpivot then pivot recovers the original grid
back, _ = run(WIDE, [
    {"action": "unpivot", "id_columns": ["Region"], "value_columns": ["Jan", "Feb"],
     "var_name": "Month", "value_name": "Sales"},
    {"action": "pivot", "index_columns": ["Region"], "pivot_column": "Month",
     "value_column": "Sales", "agg_func": "sum"}])
rn, rs = back[back.Region == "N"].iloc[0], back[back.Region == "S"].iloc[0]
check("unpivot→pivot is reversible (values recovered)",
      rn.Jan == 1 and rn.Feb == 3 and rs.Jan == 2 and rs.Feb == 4, back.to_string())
check("unpivot→pivot is reversible (columns recovered)",
      set(back.columns) == {"Region", "Jan", "Feb"}, str(list(back.columns)))

check("unpivot: id+value overlap declined",
      "one role" in err(unpivot, WIDE, {"id_columns": ["Region"], "value_columns": ["Region", "Jan"]}))
check("unpivot: nothing to unpivot declined",
      "at least one column" in err(unpivot, pd.DataFrame({"A": [1]}), {"id_columns": ["A"]}))
check("unpivot: missing column named",
      "Nope" in err(unpivot, WIDE, {"id_columns": ["Nope"], "value_columns": ["Jan"]}))

# ============================ PIVOT ============================
piv, note = run(LONG, [{"action": "pivot", "index_columns": ["Region"],
                        "pivot_column": "Month", "value_column": "Sales", "agg_func": "sum"}])
check("pivot: total matches manual (N/Jan = 10+5 = 15)",
      piv[piv.Region == "N"].Jan.iloc[0] == 15, piv.to_string())
check("pivot: SUM fills absent combo with 0 (S/Feb)",
      piv[piv.Region == "S"].Feb.iloc[0] == 0, piv.to_string())

# HONESTY: average must NOT fabricate a 0 for an absent combination
pmean, mnote = run(LONG, [{"action": "pivot", "index_columns": ["Region"],
                           "pivot_column": "Month", "value_column": "Sales", "agg_func": "average"}])
sfeb = pmean[pmean.Region == "S"].Feb.iloc[0]
check("pivot: AVERAGE leaves absent combo BLANK, not 0 (S/Feb is NaN)",
      pd.isna(sfeb), f"S/Feb={sfeb!r}")
check("pivot: N/Jan average = (10+5)/2 = 7.5",
      pmean[pmean.Region == "N"].Jan.iloc[0] == 7.5, pmean.to_string())
check("pivot: average note explains blanks are absent combinations",
      "don't occur" in mnote, mnote)

# ragged / messy: text-stored numbers coerce, blanks + non-numeric ignored + counted
messy = pd.DataFrame({"R": ["N", "N", "S"], "M": ["Jan", "Feb", "Jan"], "V": ["10", None, "abc"]})
pm, mn = run(messy, [{"action": "pivot", "index_columns": ["R"], "pivot_column": "M",
                      "value_column": "V", "agg_func": "sum"}])
check("pivot: text-stored number coerced (N/Jan = 10)",
      pm[pm.R == "N"].Jan.iloc[0] == 10, pm.to_string())
check("pivot: blank + non-numeric cells ignored AND counted in the note",
      "1 blank cell" in mn and "1 non-numeric cell" in mn, mn)

# count aggregation counts occurrences (no numeric coercion)
pc, _ = run(LONG, [{"action": "pivot", "index_columns": ["Region"], "pivot_column": "Month",
                    "value_column": "Sales", "agg_func": "count"}])
check("pivot: count of N/Jan = 2 occurrences",
      pc[pc.Region == "N"].Jan.iloc[0] == 2, pc.to_string())

check("pivot: text value column for SUM declined honestly",
      "looks like text" in err(pivot, pd.DataFrame({"R": ["N"], "M": ["Jan"], "V": ["abc"]}),
                                {"index_columns": ["R"], "pivot_column": "M", "value_column": "V"}))
check("pivot: no index declined",
      "row (index) column" in err(pivot, LONG, {"pivot_column": "Month", "value_column": "Sales"}))
check("pivot: missing value column named",
      "Nope" in err(pivot, LONG, {"index_columns": ["Region"], "pivot_column": "Month", "value_column": "Nope"}))
check("pivot: unknown agg declined",
      "use sum, average" in err(pivot, LONG, {"index_columns": ["Region"], "pivot_column": "Month",
                                              "value_column": "Sales", "agg_func": "median"}))
check("pivot: value column reused as index declined",
      "layout field" in err(pivot, LONG, {"index_columns": ["Sales"], "pivot_column": "Month",
                                          "value_column": "Sales"}))
check("pivot: empty table declined",
      "no data rows" in err(pivot, pd.DataFrame(columns=["R", "M", "V"]),
                            {"index_columns": ["R"], "pivot_column": "M", "value_column": "V"}))

# ============================ TRANSPOSE ============================
metrics = pd.DataFrame({"Metric": ["Rev", "Cost"], "Q1": [100, 40], "Q2": [200, 60]})
t, _ = run(metrics, [{"action": "transpose", "header_column": "Metric"}])
check("transpose: header_column becomes the new columns",
      set(t.columns) == {"Field", "Rev", "Cost"} and len(t) == 2, str(list(t.columns)))
check("transpose: values preserved (Q1 Rev = 100)",
      t[t.Field == "Q1"].Rev.iloc[0] == 100, t.to_string())
t2, _ = run(metrics, [{"action": "transpose"}])
check("transpose: no header column flips everything (3 rows: Metric, Q1, Q2)", len(t2) == 3, f"{len(t2)}")

# transpose is its own inverse for VALUES + shape. (The corner-cell label "Metric"
# can't round-trip — after the first flip it isn't stored anywhere — so the first
# column comes back as the generic "Field". That's inherent, honest transpose.)
tt, _ = run(metrics, [{"action": "transpose", "header_column": "Metric"},
                      {"action": "transpose", "header_column": "Field"}])
check("transpose twice recovers original values + shape (corner label → 'Field')",
      list(tt.columns) == ["Field", "Q1", "Q2"] and len(tt) == 2
      and list(tt["Field"]) == ["Rev", "Cost"] and list(tt["Q1"]) == [100, 40],
      str(list(tt.columns)) + " " + tt.to_string())

check("transpose: duplicate header values declined (would lose data)",
      "repeated values" in err(transpose, pd.DataFrame({"Metric": ["Rev", "Rev"], "Q1": [1, 2]}),
                               {"header_column": "Metric"}))
check("transpose: bad header column named",
      "Nope" in err(transpose, metrics, {"header_column": "Nope"}))
check("transpose: empty table declined",
      "no data rows" in err(transpose, pd.DataFrame(columns=["A", "B"]), {}))

# ============================ HTTP round-trip ============================
def api_run(df: pd.DataFrame, ops: list[dict]):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Sheet1")
    sid = f"p22-{uuid.uuid4().hex[:10]}"
    r = client.post("/inspect", data={"session_id": sid},
                    files=[("files", ("reshape.xlsx", buf.getvalue(), OCT))])
    assert r.status_code == 200, r.text[:200]
    r = client.post("/execute", data={"session_id": sid, "plan": json.dumps({"operations": ops})})
    j = r.json()
    grid = None
    if j.get("status") == "ok" and j.get("download_id"):
        grid = pd.read_excel(io.BytesIO(client.get(f"/download/{j['download_id']}").content))
    return j, grid

j, grid = api_run(WIDE, [{"action": "unpivot", "id_columns": ["Region"],
                          "value_columns": ["Jan", "Feb"], "var_name": "Month", "value_name": "Sales"}])
check("HTTP: unpivot runs and the long table downloads",
      j.get("status") == "ok" and grid is not None and len(grid) == 4
      and set(grid.columns) == {"Region", "Month", "Sales"},
      (grid.to_string() if grid is not None else str(j)[:200]))

j, grid = api_run(LONG, [{"action": "pivot", "index_columns": ["Region"],
                          "pivot_column": "Month", "value_column": "Sales", "agg_func": "sum"}])
check("HTTP: pivot runs and the grid downloads with the right total",
      grid is not None and grid[grid.Region == "N"].Jan.iloc[0] == 15,
      (grid.to_string() if grid is not None else str(j)[:200]))

j, _ = api_run(pd.DataFrame({"R": ["N"], "M": ["Jan"], "V": ["abc"]}),
               [{"action": "pivot", "index_columns": ["R"], "pivot_column": "M", "value_column": "V"}])
check("HTTP: text-sum pivot fails friendly (not a 500)",
      j.get("status") != "ok" and "text" in json.dumps(j).lower(), str(j)[:200])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
