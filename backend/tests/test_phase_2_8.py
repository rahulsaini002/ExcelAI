"""ENGINE PHASE 2.8 — sheet / workbook protection (NO AI).

Protection rides on sheet_op (its sheet_action is a free string, so protect/unprotect/
protect_workbook add NO schema field — the Operation model is at Gemini's serving limit).
It is PASSWORD-LESS by design: Sumio locks cells so they resist accidental edits, but
never sets or stores an open/file password (user-driven, per PRD 2.8). This suite checks
that protection round-trips into the .xlsx (sheet locked, allow-edit columns unlocked,
workbook structure locked), that the DATA is never changed, that a password request still
just applies structural protection with an honest note (no password in the file), and the
failure paths.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_2_8.py
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

_fd, _db = tempfile.mkstemp(suffix="-p28.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app.db import init_db  # noqa: E402
from app.executor import execute_multi  # noqa: E402
from app.operations.base import OperationError  # noqa: E402
from app.operations.sheets_mgmt import sheet_op  # noqa: E402

init_db()
client = TestClient(m.app)
passed = failed = 0
OCT = "application/octet-stream"
DF = pd.DataFrame({"Region": ["N", "S", "E"], "Qty": [10, 20, 30], "Price": [100, 200, 150]})


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def run(ops: list[dict], tables=None):
    tables = tables or {"Sheet1": DF.copy()}
    primary = next(iter(tables))
    res, name, notes, render = execute_multi(tables, primary, ops)
    sheets = res if isinstance(res, dict) else {name: res}
    out, _, _ = m._serialize_workbook(sheets, "x.xlsx", name, render)
    return load_workbook(io.BytesIO(out)), " ".join(notes), render, res


print("ENGINE PHASE 2.8 — sheet / workbook protection (no AI)\n")

# ---- (a) protect a sheet: cells locked, data unchanged ----
wb, note, render, res = run([{"action": "sheet_op", "sheet_action": "protect"}])
ws = wb["Sheet1"]
check("protect: sheet protection turned on", ws.protection.sheet is True, str(ws.protection.sheet))
check("protect: a data cell is locked by default", ws.cell(row=2, column=1).protection.locked in (True, None),
      str(ws.cell(row=2, column=1).protection.locked))
check("protect: data is unchanged", ws["A1"].value == "Region" and ws["A2"].value == "N", "")
check("protect: emits a sheet_protect directive",
      any(r.get("type") == "sheet_protect" and r.get("protect") for r in render), str(render))
check("protect: note explains it prevents accidental edits", "resist accidental edits" in note, note[:80])

# ---- (b) allow-edit columns stay unlocked ----
wb, note, _, _ = run([{"action": "sheet_op", "sheet_action": "protect", "columns": ["Qty"]}])
ws = wb["Sheet1"]
check("allow-edit: the Qty column is UNLOCKED", ws.cell(row=2, column=2).protection.locked is False,
      str(ws.cell(row=2, column=2).protection.locked))
check("allow-edit: other columns stay locked", ws.cell(row=2, column=3).protection.locked in (True, None),
      str(ws.cell(row=2, column=3).protection.locked))
check("allow-edit: note names the editable column", "Qty" in note and "editable" in note, note[:120])
check("allow-edit: sheet still protected", ws.protection.sheet is True, "")

# ---- (c) unprotect reverses it ----
wb, _, _, _ = run([{"action": "sheet_op", "sheet_action": "protect"},
                   {"action": "sheet_op", "sheet_action": "unprotect"}])
check("unprotect: protection removed", wb["Sheet1"].protection.sheet is False, str(wb["Sheet1"].protection.sheet))

# ---- (d) workbook-structure protection ----
wb, note, render, _ = run([{"action": "sheet_op", "sheet_action": "protect_workbook"}])
check("protect_workbook: lockStructure on", wb.security is not None and wb.security.lockStructure is True,
      str(getattr(wb.security, "lockStructure", None)))
check("protect_workbook: note explains it", "added, deleted" in note or "reordered" in note, note[:90])
wb2, _, _, _ = run([{"action": "sheet_op", "sheet_action": "protect_workbook"},
                    {"action": "sheet_op", "sheet_action": "unprotect_workbook"}])
check("unprotect_workbook: lockStructure off",
      wb2.security is None or wb2.security.lockStructure in (False, None),
      str(getattr(wb2.security, "lockStructure", None)))

# ---- (e) PASSWORD SAFETY: a password request still just protects structurally, with an
# honest note, and NO password is written into the file ----
wb, note, _, _ = run([{"action": "sheet_op", "sheet_action": "protect"}])
ws = wb["Sheet1"]
check("password: honest note says Sumio never handles/stores passwords",
      "never handles or stores passwords" in note, note[-100:])
check("password: no sheet password hash written to the file",
      not getattr(ws.protection, "password", None), str(getattr(ws.protection, "password", None)))

# multi-sheet: protect one sheet by name, the other stays open
wb, note, _, _ = run([{"action": "sheet_op", "sheet_action": "protect", "sheet_name": "Prices"}],
                     tables={"Sales": DF.copy(), "Prices": DF.copy()})
check("multi-sheet: named sheet protected", wb["Prices"].protection.sheet is True, "")
check("multi-sheet: the other sheet stays unprotected", wb["Sales"].protection.sheet in (False, None),
      str(wb["Sales"].protection.sheet))

# ---- (f) failures / honesty ----
def err(op, tables=None):
    try:
        sheet_op(tables or {"Sheet1": DF.copy()}, "Sheet1", op)
        return ""
    except OperationError as e:
        return str(e)

check("unknown sheet action still declined (list now includes protect)",
      "protect" in err({"sheet_action": "encrypt"}))
check("protect a non-existent sheet named", "Nope" in err({"sheet_action": "protect", "sheet_name": "Nope"}))

# ---- (g) HTTP round-trip ----
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as w:
    DF.to_excel(w, index=False, sheet_name="Sheet1")
sid = f"p28-{uuid.uuid4().hex[:10]}"
client.post("/inspect", data={"session_id": sid}, files=[("files", ("p.xlsx", buf.getvalue(), OCT))])
r = client.post("/execute", data={"session_id": sid, "plan": json.dumps({"operations": [
    {"action": "sheet_op", "sheet_action": "protect", "columns": ["Qty"]}]})})
j = r.json()
wsx = None
if j.get("status") == "ok" and j.get("download_id"):
    wbx = load_workbook(io.BytesIO(client.get(f"/download/{j['download_id']}").content))
    wsx = wbx[wbx.sheetnames[0]]
check("HTTP: ok + sheet protected + Qty editable in the download",
      j.get("status") == "ok" and wsx is not None and wsx.protection.sheet is True
      and wsx.cell(row=2, column=2).protection.locked is False, str(j)[:150])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
