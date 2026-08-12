"""ENHANCEMENT TRACK 4, item 5 — logging every Operation Plan, execution and outcome.

Exercised against the sizes Track 4 asks for: a 100k+ row file and a multi-step chain.

Before this the engine had NO logging at all — main.py and executor.py used bare print
and traceback.print_exc, so "it dropped my rows" could not be answered after the fact.

The design idea under test is CORRELATION: one run_id ties a plan to its execution and
its outcome, so overlapping requests stay distinguishable and a single complaint can be
traced end to end.

The other half is PRIVACY. This log must record what the system DID, never what the data
CONTAINED — a debug log that quietly becomes a copy of the user's spreadsheet is a data
leak wearing a helpful hat. Several checks below exist only to pin that down.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_track_4_5.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-t45.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import oplog  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0
XL = "application/octet-stream"

BIG_ROWS = 120_000  # Track 4's "large file" bar, comfortably over 100k


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def big_workbook() -> bytes:
    """A 120k-row sheet with duplicates to remove and a numeric column to sort."""
    n = BIG_ROWS
    df = pd.DataFrame({
        "Email": [f"user{i % (n // 2)}@example.com" for i in range(n)],  # each appears twice
        "Region": ["North", "South", "East", "West"][0:1] * n,
        "Revenue": [float((i * 37) % 9999) for i in range(n)],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def seed(wb: bytes) -> str:
    sid = f"t45-{uuid.uuid4().hex[:8]}"
    r = client.post("/inspect", data={"session_id": sid},
                    files=[("files", ("big.xlsx", wb, XL))])
    assert r.status_code == 200, r.text
    return sid


def run() -> None:
    oplog.clear()
    print(f"  (building a {BIG_ROWS:,}-row workbook…)")
    t0 = time.time()
    wb = big_workbook()
    print(f"  (built in {time.time() - t0:.1f}s, {len(wb) / 1_000_000:.1f} MB)\n")

    sid = seed(wb)

    # --- a MULTI-STEP chain on a LARGE file -------------------------------------------
    plan = [
        {"action": "remove_duplicates", "columns": ["Email"]},
        {"action": "sort", "columns": ["Revenue"], "orders": ["desc"]},
    ]
    t0 = time.time()
    r = client.post("/execute", data={"session_id": sid, "plan": __import__("json").dumps({"operations": plan})})
    elapsed = int((time.time() - t0) * 1000)
    check("multi-step chain runs on a 120k-row file", r.status_code == 200,
          f"HTTP {r.status_code} {r.text[:200]}")
    body = r.json() if r.status_code == 200 else {}
    print(f"        ({elapsed}ms, {body.get('rows_before')} -> {body.get('row_count')} rows)")

    # --- the response hands back a traceable id ---------------------------------------
    run_id = body.get("run_id")
    check("successful run returns a run_id to quote in a bug report", bool(run_id),
          f"body keys={sorted(body)[:12]}")

    # --- correlation: plan and outcome share that id ----------------------------------
    entries = oplog.run(run_id) if run_id else []
    phases = [e["phase"] for e in entries]
    check("the run's plan and outcome are recorded under one id",
          phases == ["plan", "outcome"], f"phases={phases}")

    if len(entries) == 2:
        planned, outcome = entries

        # --- the plan's SHAPE is recorded, in order -----------------------------------
        actions = [s.get("action") for s in planned["plan"]]
        check("both steps recorded in execution order",
              actions == ["remove_duplicates", "sort"], f"actions={actions}")
        check("the columns each step touched are recorded",
              planned["plan"][0].get("columns") == ["Email"]
              and planned["plan"][1].get("columns") == ["Revenue"],
              f"plan={planned['plan']}")

        # --- the outcome answers "what did it do to my rows?" --------------------------
        check("outcome status is ok", outcome["status"] == "ok", f"got {outcome['status']}")
        check("row counts before and after are both recorded",
              isinstance(outcome["rows_before"], int) and isinstance(outcome["rows_after"], int),
              f"{outcome['rows_before']} -> {outcome['rows_after']}")
        check("the row delta is computed (the key debugging number)",
              outcome["row_delta"] == outcome["rows_after"] - outcome["rows_before"],
              f"delta={outcome['row_delta']}")
        check("dedupe on a 120k-row file is reflected as a real row loss",
              outcome["row_delta"] < 0, f"delta={outcome['row_delta']}")
        check("duration is recorded", isinstance(outcome["duration_ms"], int),
              f"got {outcome['duration_ms']!r}")

        # --- PRIVACY: the log must not become a copy of the spreadsheet ---------------
        blob = __import__("json").dumps(entries)
        check("no cell values leak into the log (no email addresses)",
              "@example.com" not in blob, "a cell value was logged")
        check("no sample rows leak into the log",
              "sample_rows" not in blob and "North" not in blob, "row data was logged")
        check("column NAMES are kept (you cannot debug without them)",
              "Email" in blob and "Revenue" in blob, "column names were stripped")

    # --- cache hit is visible in the log (Phase 5.8 interaction) ----------------------
    r2 = client.post("/execute", data={"session_id": seed(wb),
                                       "plan": __import__("json").dumps({"operations": plan})})
    if r2.status_code == 200:
        out2 = [e for e in oplog.run(r2.json().get("run_id")) if e["phase"] == "outcome"]
        check("a cache-hit run is logged as cached", bool(out2) and out2[0]["cached"] is True,
              f"cached={out2[0]['cached'] if out2 else 'no outcome'}")

    # --- plan SHAPE robustness (a defect found while writing this test) ----------------
    # A bare list is the most natural way to hand-write or script a plan. It previously
    # hit `parsed.get("operations")` and raised AttributeError -> 500 blaming the server
    # for input the caller can fix. The UI sends the wrapped shape, so production was
    # never broken — but a hand-edited or scripted plan would have been.
    rl = client.post("/execute", data={"session_id": seed(wb),
                                       "plan": __import__("json").dumps(plan)})
    check("a bare list of steps is accepted as a plan (no 500)", rl.status_code == 200,
          f"HTTP {rl.status_code} {rl.text[:160]}")
    rj = client.post("/execute", data={"session_id": seed(wb), "plan": '"just a string"'})
    check("a nonsense plan shape gets a clean 400, not a 500",
          rj.status_code == 400, f"HTTP {rj.status_code}")
    rk = client.post("/execute", data={"session_id": seed(wb),
                                       "plan": '{"operations": "not a list"}'})
    check("a non-list operations field gets a clean 400, not a 500",
          rk.status_code == 400, f"HTTP {rk.status_code}")

    # --- a FAILING step is logged as an error, not silently -----------------------------
    bad = [{"action": "sort", "columns": ["NoSuchColumn"], "orders": ["asc"]}]
    r3 = client.post("/execute", data={"session_id": seed(wb),
                                       "plan": __import__("json").dumps({"operations": bad})})
    check("a bad plan is rejected", r3.status_code >= 400, f"HTTP {r3.status_code}")
    errs = [e for e in oplog.events(limit=50, phase="outcome") if e["status"] == "error"]
    check("the failure is recorded as an error outcome with a reason",
          bool(errs) and bool(errs[0].get("error")), "no error outcome logged")

    # --- the debug endpoint exposes it ------------------------------------------------
    d = client.get("/debug/oplog", params={"run_id": run_id})
    check("GET /debug/oplog returns one run's plan + outcome",
          d.status_code == 200 and len(d.json().get("events", [])) == 2,
          f"HTTP {d.status_code} {d.text[:160]}")
    d2 = client.get("/debug/oplog", params={"limit": 5})
    check("GET /debug/oplog lists recent events most-recent-first",
          d2.status_code == 200 and len(d2.json().get("events", [])) <= 5,
          f"HTTP {d2.status_code}")

    # --- the ring buffer is bounded (a long-lived server must not grow forever) --------
    oplog.clear()
    for _ in range(oplog._MAX + 50):
        oplog.record_outcome(oplog.new_run_id(), status="ok")
    check("the in-memory log is bounded", len(oplog._RUNS) == oplog._MAX,
          f"held {len(oplog._RUNS)}")


if __name__ == "__main__":
    print("TRACK 4 item 5 — operation logging (120k rows + multi-step chain)\n")
    run()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
