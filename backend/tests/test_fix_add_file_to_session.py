"""BUG FIX — uploading a second file destroyed the work already done.

Reported from real use: upload a file, do some work, then upload another file and
"the previous work and things in the same session get removed".

WHAT HAPPENED. Every upload replaced the session. The frontend minted a NEW backend
session id, cleared the transcript and reset the undo stack; /inspect called
_remember_session, which overwrites states[] with just the newly uploaded tables. So
adding a price list to look values up from threw away everything done so far — and the
app's own features (merge, lookup across sheets) exist precisely to work across several
files, so wanting a second file in a session is the normal case, not an edge one.

THE FIX. /inspect takes mode="add": the new tables join the CURRENT state instead of
replacing it, pushed as a NEW STATE so Undo removes the added file and puts the session
back exactly as it was — the same model every other operation follows. The working table
deliberately does NOT move to the new file: adding a reference sheet must not silently
redirect the next instruction.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_fix_add_file_to_session.py
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

_fd, _db = tempfile.mkstemp(suffix="-addfile.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0
XL = "application/octet-stream"


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


SALES = pd.DataFrame({
    "Item": ["apple", "banana", "apple", "cherry"],
    "Qty": [5, 3, 5, 9],
})
PRICES = pd.DataFrame({
    "Item": ["apple", "banana", "cherry"],
    "Rate": [10.0, 5.0, 20.0],
})


def upload(sid: str, name: str, df: pd.DataFrame, mode: str = "replace"):
    return client.post(
        "/inspect",
        data={"session_id": sid, "mode": mode},
        files=[("files", (name, xlsx(df), XL))],
    )


def run_plan(sid: str, ops: list):
    return client.post("/execute", data={"session_id": sid,
                                         "plan": json.dumps({"operations": ops})})


def run() -> None:
    # =================================================================
    # THE REPORTED BUG
    # =================================================================
    sid = f"add-{uuid.uuid4().hex[:8]}"
    r = upload(sid, "sales.xlsx", SALES)
    check("first upload works", r.status_code == 200, f"HTTP {r.status_code}")

    # do some real work: 4 rows -> 3 after dedupe
    ex = run_plan(sid, [{"action": "remove_duplicates"}])
    check("an operation runs on the first file", ex.status_code == 200,
          f"HTTP {ex.status_code} {ex.text[:150]}")
    rows_after_work = ex.json().get("row_count") if ex.status_code == 200 else None
    check("the work actually changed the data", rows_after_work == 3,
          f"row_count={rows_after_work}")

    # ...now add a second file, which used to wipe all of the above
    r2 = upload(sid, "prices.xlsx", PRICES, mode="add")
    check("adding a second file succeeds", r2.status_code == 200,
          f"HTTP {r2.status_code} {r2.text[:150]}")
    body = r2.json() if r2.status_code == 200 else {}
    check("the response says it ADDED rather than replaced", body.get("added") is True,
          f"added={body.get('added')}")
    names = [t["name"] for t in body.get("tables", [])]
    check("the response lists BOTH files' tables", len(names) == 2, f"tables={names}")
    check("it names just the newly added table separately",
          len(body.get("added_tables") or []) == 1, f"added_tables={body.get('added_tables')}")

    # THE HEART OF IT: the earlier work must still be there.
    ex2 = run_plan(sid, [{"action": "sort", "columns": ["Qty"], "orders": ["desc"]}])
    check("the session still works after the add", ex2.status_code == 200,
          f"HTTP {ex2.status_code} {ex2.text[:150]}")
    if ex2.status_code == 200:
        check("THE BUG: the dedupe from before the upload SURVIVED",
              ex2.json().get("rows_before") == 3,
              f"rows_before={ex2.json().get('rows_before')} (4 = the work was lost)")

    # =================================================================
    # THE ADDED FILE IS USABLE — the reason you'd add one
    # =================================================================
    sid2 = f"add-{uuid.uuid4().hex[:8]}"
    upload(sid2, "sales.xlsx", SALES)
    upload(sid2, "prices.xlsx", PRICES, mode="add")
    look = run_plan(sid2, [{
        "action": "lookup", "source_sheet": "prices", "key_column": "Item",
        "source_key_column": "Item", "return_column": "Rate", "new_column": "Rate",
    }])
    check("a lookup against the ADDED file works", look.status_code == 200,
          f"HTTP {look.status_code} {look.text[:160]}")
    if look.status_code == 200:
        prev = look.json().get("preview") or []
        cols = [c["name"] for c in prev[0]["columns"]] if prev else []
        check("the looked-up column arrived", "Rate" in cols, f"columns={cols}")

    # =================================================================
    # UNDO REMOVES THE ADDED FILE
    # =================================================================
    sid3 = f"add-{uuid.uuid4().hex[:8]}"
    upload(sid3, "sales.xlsx", SALES)
    upload(sid3, "prices.xlsx", PRICES, mode="add")
    undo = client.post("/undo", data={"session_id": sid3})
    check("undo after an add succeeds", undo.status_code == 200,
          f"HTTP {undo.status_code} {undo.text[:140]}")

    # =================================================================
    # REPLACE STILL REPLACES (the default is unchanged)
    # =================================================================
    sid4 = f"add-{uuid.uuid4().hex[:8]}"
    upload(sid4, "sales.xlsx", SALES)
    run_plan(sid4, [{"action": "remove_duplicates"}])
    r4 = upload(sid4, "prices.xlsx", PRICES)  # default mode
    check("a default upload still replaces", r4.status_code == 200,
          f"HTTP {r4.status_code}")
    check("...and reports that it did NOT add", r4.json().get("added") is False,
          f"added={r4.json().get('added')}")
    ex4 = run_plan(sid4, [{"action": "sort", "columns": ["Rate"], "orders": ["desc"]}])
    check("after a replace the session holds ONLY the new file",
          ex4.status_code == 200 and ex4.json().get("rows_before") == 3,
          f"HTTP {ex4.status_code} rows_before="
          f"{ex4.json().get('rows_before') if ex4.status_code == 200 else '?'}")

    # =================================================================
    # NAME COLLISION — adding a file with the same sheet name
    # =================================================================
    sid5 = f"add-{uuid.uuid4().hex[:8]}"
    upload(sid5, "data.xlsx", SALES)
    r5 = upload(sid5, "data.xlsx", PRICES, mode="add")
    names5 = [t["name"] for t in (r5.json().get("tables") or [])]
    check("a same-named sheet is kept under a suffixed name, not overwritten",
          len(names5) == 2 and len(set(names5)) == 2, f"tables={names5}")

    # =================================================================
    # "add" ON A SESSION THAT DOESN'T EXIST YET behaves like a first upload
    # =================================================================
    sid6 = f"add-{uuid.uuid4().hex[:8]}"
    r6 = upload(sid6, "sales.xlsx", SALES, mode="add")
    check("'add' with no existing session just loads the file", r6.status_code == 200,
          f"HTTP {r6.status_code}")
    check("...and reports it as a plain load, not an add",
          r6.json().get("added") is False, f"added={r6.json().get('added')}")


if __name__ == "__main__":
    print("BUG FIX — adding a second file no longer wipes the session\n")
    run()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
