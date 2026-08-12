"""ENGINE PHASE 2.9 — workbook compare (NO AI).

"What changed between these two files?" — a structured, HONEST diff. Compare rides on
sheet_op (sheet_action "compare" is a free string value → no new schema field; it reuses
sheet_name / source_sheet / key_column). This suite proves the diff is correct and never
invents a change: identical files report identical; changed cells, added/removed rows
(positional AND key-matched) and added/removed columns are each detected; numbers-stored-
as-text don't look like changes; and it declines when there aren't two files.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_2_9.py
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

_fd, _db = tempfile.mkstemp(suffix="-p29.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app.db import init_db  # noqa: E402
from app.executor import execute_multi  # noqa: E402
from app.operations.base import OperationError  # noqa: E402
from app.operations.compare import compare_tables  # noqa: E402

init_db()
client = TestClient(m.app)
passed = failed = 0
OCT = "application/octet-stream"

A = pd.DataFrame({"ID": [1, 2, 3], "Name": ["Asha", "Rahul", "Meera"], "Qty": [10, 20, 30]})
B = pd.DataFrame({"ID": [1, 2, 4], "Name": ["Asha", "Rahul", "Dev"], "Qty": [10, 25, 40],
                  "City": ["Pune", "Delhi", "Kochi"]})


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def rows(df) -> list[tuple]:
    return [tuple(r) for r in df[["Change", "Where", "Was", "Now"]].astype(str).itertuples(index=False)]


def run(ops, tables):
    res, name, notes, _ = execute_multi(tables, next(iter(tables)), ops)
    return res, " ".join(notes)


print("ENGINE PHASE 2.9 — workbook compare (no AI)\n")

# ---- (a) identical files → no differences ----
d, note = compare_tables(A.copy(), A.copy(), "x", "y")
check("identical: reports identical", "identical" in note and len(d) == 1 and d.iloc[0]["Change"] == "No differences", note)

# ---- (b) positional compare ----
d, note = compare_tables(A.copy(), B.copy(), "A", "B")
r = rows(d)
check("positional: detects the added column (City)", ("Column added", "City", "—", "in B") in r, str(r))
check("positional: row 2 Qty 20→25", ("Cell changed", "row 2 · Qty", "20", "25") in r, str(r))
check("positional: row 3 differs (Meera→Dev)", ("Cell changed", "row 3 · Name", "Meera", "Dev") in r, str(r))
check("positional: note counts 4 changed cells", "4 changed cell" in note, note)

# ---- (c) key-matched compare (the smarter mode) ----
d, note = compare_tables(A.copy(), B.copy(), "A", "B", key_column="ID")
r = rows(d)
check("key: ID=3 removed", ("Row removed", "ID=3", "in A", "—") in r, str(r))
check("key: ID=4 added", ("Row added", "ID=4", "—", "in B") in r, str(r))
check("key: only ID=2 Qty changed (not the whole reordered row)",
      ("Cell changed", "ID=2 · Qty", "20", "25") in r
      and sum(1 for x in r if x[0] == "Cell changed") == 1, str(r))
check("key: note says matched by ID + 1 changed / 1 added / 1 removed",
      "matched by 'ID'" in note and "1 changed cell" in note and "1 added row" in note
      and "1 removed row" in note, note)

# ---- (d) added / removed rows at the end (positional) ----
short = pd.DataFrame({"ID": [1, 2], "V": [5, 6]})
longer = pd.DataFrame({"ID": [1, 2, 3], "V": [5, 6, 7]})
d, note = compare_tables(short, longer, "old", "new")
check("positional: extra row in the new file → 'Row added'",
      ("Row added", "row 3", "—", "in new") in rows(d), str(rows(d)))
d2, _ = compare_tables(longer, short, "old", "new")
check("positional: missing row in the new file → 'Row removed'",
      ("Row removed", "row 3", "in old", "—") in rows(d2), str(rows(d2)))

# ---- (e) HONESTY: numbers-stored-as-text are NOT a change; blanks vs blanks equal ----
ta = pd.DataFrame({"ID": [1, 2], "N": [10, 20], "X": [None, "a"]})
tb = pd.DataFrame({"ID": [1, 2], "N": ["10", "20"], "X": [None, "a"]})  # N as text, same values
d, note = compare_tables(ta, tb, "a", "b", key_column="ID")
check("honesty: 10 == '10' is NOT flagged as a change; blank==blank equal",
      "identical" in note, note)

# ---- (f) column removed is detected ----
d, _ = compare_tables(B.copy(), A.copy(), "B", "A")  # A has no City
check("column removed detected", ("Column removed", "City", "in B", "—") in rows(d), str(rows(d)))

# ---- (g) through the executor: Comparison becomes the result ----
res, note = run([{"action": "sheet_op", "sheet_action": "compare", "source_sheet": "fileB", "key_column": "ID"}],
                {"fileA": A.copy(), "fileB": B.copy()})
check("executor: result IS the Comparison table",
      list(res.columns) == ["Change", "Where", "Was", "Now"] and "matched by 'ID'" in note,
      str(list(res.columns)))

# ---- (h) failures ----
def err(ops, tables):
    try:
        execute_multi(tables, next(iter(tables)), ops)
        return ""
    except OperationError as e:
        return str(e)

check("only one file → asks for a second",
      "TWO different" in err([{"action": "sheet_op", "sheet_action": "compare", "source_sheet": "only"}], {"only": A.copy()})
      or "Which two" in err([{"action": "sheet_op", "sheet_action": "compare"}], {"only": A.copy()}))
check("comparing a file with itself declined",
      "same one" in err([{"action": "sheet_op", "sheet_action": "compare", "sheet_name": "a", "source_sheet": "a"}],
                        {"a": A.copy(), "b": B.copy()}))
check("naming a non-existent second file declined",
      "no file/sheet called 'ghost'" in err([{"action": "sheet_op", "sheet_action": "compare", "source_sheet": "ghost"}],
                                             {"a": A.copy(), "b": B.copy()}).lower())

# ---- (i) HTTP: a two-sheet upload compared → a downloadable Comparison sheet ----
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as w:
    A.to_excel(w, index=False, sheet_name="Before")
    B.to_excel(w, index=False, sheet_name="After")
sid = f"p29-{uuid.uuid4().hex[:10]}"
client.post("/inspect", data={"session_id": sid}, files=[("files", ("book.xlsx", buf.getvalue(), OCT))])
r = client.post("/execute", data={"session_id": sid, "plan": json.dumps({"operations": [
    {"action": "sheet_op", "sheet_action": "compare", "sheet_name": "Before", "source_sheet": "After", "key_column": "ID"}]})})
j = r.json()
grid = None
if j.get("status") == "ok" and j.get("download_id"):
    grid = pd.read_excel(io.BytesIO(client.get(f"/download/{j['download_id']}").content))
check("HTTP: compare two sheets → downloadable Comparison table",
      j.get("status") == "ok" and grid is not None and list(grid.columns) == ["Change", "Where", "Was", "Now"]
      and (grid["Change"] == "Cell changed").any(), str(j)[:160])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
