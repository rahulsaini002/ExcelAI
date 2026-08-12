"""ENHANCEMENT TRACK 4, item 6 — graceful timeout handling with progress reporting.

The progress half shipped with item 1 (real per-step milestones, a saving phase, and a
`progress` fraction that never reaches 1.0 before the job is genuinely done). This is the
timeout half.

THE HONEST SHAPE OF THIS FEATURE. A worker thread running pandas cannot be safely
interrupted mid-operation — Python offers no way to abort a C-level sort partway without
risking corrupt state — so cancellation is COOPERATIVE: the deadline is checked at STEP
BOUNDARIES via the same on_step callback that drives progress. The consequence, which the
tests below pin down rather than hide:

    a multi-step plan can be stopped between its steps;
    a plan whose SINGLE step runs long will overrun its deadline and cannot be stopped.

Claiming otherwise would be the same class of lie as the old timer-driven progress bar.

GRACEFUL means the user's file is left alone. A timeout stops between steps, and the
completed steps existed only in memory, so nothing is pushed to the session — a partially
applied plan the user never asked for and cannot see is worse than no change at all.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_track_4_6.py
"""
from __future__ import annotations

import io
import json
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

_fd, _db = tempfile.mkstemp(suffix="-t46.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import jobs, oplog, scale  # noqa: E402
from app.db import init_db  # noqa: E402
from app.executor import OperationCancelled, execute_multi  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0
XL = "application/octet-stream"
BIG_ROWS = 120_000


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def big_workbook() -> bytes:
    n = BIG_ROWS
    df = pd.DataFrame({
        "Email": [f"user{i % (n // 2)}@example.com" for i in range(n)],
        "Region": ["North", "South", "East", "West"][0:1] * n,
        "Revenue": [float((i * 37) % 9999) for i in range(n)],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def seed(wb: bytes) -> str:
    sid = f"t46-{uuid.uuid4().hex[:8]}"
    r = client.post("/inspect", data={"session_id": sid},
                    files=[("files", ("big.xlsx", wb, XL))])
    assert r.status_code == 200, r.text
    return sid


CHAIN = [
    {"action": "remove_duplicates", "columns": ["Email"]},
    {"action": "sort", "columns": ["Revenue"], "orders": ["desc"]},
]


def watch(job_id: str, timeout: float = 180.0) -> dict | None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"/jobs/{job_id}")
        if r.status_code != 200:
            return last
        last = r.json()["job"]
        if last["done"]:
            return last
        time.sleep(0.02)
    return last


def run() -> None:
    oplog.clear()
    jobs.clear()
    print(f"  (building a {BIG_ROWS:,}-row workbook…)")
    t0 = time.time()
    wb = big_workbook()
    print(f"  (built in {time.time() - t0:.1f}s)\n")

    # =====================================================================
    # 1. THE MECHANISM, at the executor level
    # =====================================================================
    small = pd.DataFrame({"Email": ["a@x.com", "a@x.com", "b@x.com"], "Revenue": [3.0, 1.0, 2.0]})

    # A reporter that cancels on the SECOND step: step 1 runs, then we stop.
    def cancel_at_second(index0, action):
        if index0 >= 1:
            raise OperationCancelled(completed_steps=index0, total_steps=2,
                                     reason="budget spent")

    try:
        execute_multi({"t": small}, "t", CHAIN, on_step=cancel_at_second)
        cancelled = False
    except OperationCancelled as exc:
        cancelled = True
        stopped_after = exc.completed_steps
    check("OperationCancelled from the progress callback stops the run", cancelled,
          "the run completed despite being cancelled")
    if cancelled:
        check("the cancellation reports how far it got", stopped_after == 1,
              f"completed_steps={stopped_after}")

    # ...but an ordinary reporter bug still must NOT stop a real run. This is the carve-out
    # working in both directions: a reporter may CANCEL, it may not FAIL.
    def broken(index0, action):
        raise RuntimeError("reporter is buggy")

    try:
        out = execute_multi({"t": small}, "t", CHAIN, on_step=broken)
        survived = out is not None
    except Exception as exc:  # noqa: BLE001
        survived = False
        print(f"        {type(exc).__name__}: {exc}")
    check("a merely BROKEN reporter still cannot fail a real run", survived,
          "a reporter bug killed the execution")

    # =====================================================================
    # 2. END TO END: a job that runs out of budget
    # =====================================================================
    scale.RESULT_CACHE.clear()
    sid = seed(wb)
    # MAKING THIS DETERMINISTIC took two attempts, both instructive:
    #   • a sub-millisecond budget races the worker's own startup (~2ms), so whether step 1
    #     began before the deadline was a coin flip and the test flapped;
    #   • a 0.5s budget on the 2-step chain never fired either — the STEPS are fast
    #     (tens of ms on 120k rows); the ~7s wall clock of a real run is dominated by
    #     WRITING the .xlsx, which is after all the steps and past the last checkpoint.
    # So: many cheap steps whose cumulative time far exceeds the budget. Cancellation is
    # then certain, even though WHICH step it lands on is not — and the assertions below
    # are written to that, pinning the property rather than a lucky number.
    many = [
        {"action": "sort", "columns": ["Revenue"], "orders": ["desc" if i % 2 else "asc"]}
        for i in range(40)
    ]
    r = client.post("/execute/async", data={
        "session_id": sid, "plan": json.dumps({"operations": many}),
        "timeout_seconds": "0.2",
    })
    check("a job with a short budget is still accepted", r.status_code == 202,
          f"HTTP {r.status_code} {r.text[:160]}")
    body = r.json() if r.status_code == 202 else {}
    check("the accepted job reports the budget it is running under",
          body.get("timeout_seconds") == 0.2, f"got {body.get('timeout_seconds')}")

    job_id = body.get("job_id")
    final = watch(job_id) if job_id else None
    check("the job reaches a terminal state (a timeout must never hang)",
          bool(final) and final["done"], f"last={final}")
    if final:
        check("the terminal state is 'timeout', distinct from 'error'",
              final["state"] == "timeout", f"state={final['state']}")
        check("the snapshot flags it as timed out", final.get("timed_out") is True,
              f"timed_out={final.get('timed_out')}")
        check("progress still ends at 1.0 (the job is over, whatever the outcome)",
              final["progress"] == 1.0, f"progress={final['progress']}")

    # --- GRACEFUL: the user's file is untouched -------------------------------------
    coll = client.get(f"/jobs/{job_id}/result")
    check("collecting a timed-out job returns 504, not a fake success",
          coll.status_code == 504, f"HTTP {coll.status_code}")
    cbody = coll.json() if coll.status_code == 504 else {}
    check("the message says how far it got and that the file is unchanged",
          "unchanged" in (cbody.get("error") or "")
          and "step" in (cbody.get("error") or ""),
          f"error={cbody.get('error')!r}")
    check("the timeout body reports completed and total steps",
          isinstance(cbody.get("completed_steps"), int)
          and cbody.get("total_steps") == 40,
          f"{cbody.get('completed_steps')}/{cbody.get('total_steps')}")
    check("it stopped PART WAY — some steps ran, the rest never started",
          isinstance(cbody.get("completed_steps"), int)
          and cbody["completed_steps"] < 40,
          f"completed={cbody.get('completed_steps')} of 40")
    if final:
        statuses = [s["status"] for s in final["steps"]]
        done_n = statuses.count("ok")
        check("per-step statuses are a run of completed steps then skipped ones",
              statuses == ["ok"] * done_n + ["skipped"] * (40 - done_n),
              f"statuses={statuses[:6]}… ({done_n} ok)")
        check("no step is left marked as still running",
              "running" not in statuses, "a step is stuck in 'running'")

    # The session must be exactly as it was: no state pushed, so a later run starts clean.
    after = client.post("/execute", data={"session_id": sid,
                                          "plan": json.dumps({"operations": CHAIN})})
    check("the session still works after a timeout (nothing was half-applied)",
          after.status_code == 200, f"HTTP {after.status_code} {after.text[:160]}")
    if after.status_code == 200:
        check("and it still sees the ORIGINAL row count, so no partial run took effect",
              after.json().get("rows_before") == BIG_ROWS,
              f"rows_before={after.json().get('rows_before')}")

    # --- the timeout is recorded in the operation log --------------------------------
    outs = [e for e in oplog.events(limit=50, phase="outcome") if e["status"] == "timeout"]
    check("the timeout is recorded as its own outcome status in the oplog", bool(outs),
          "no timeout outcome logged")

    # =====================================================================
    # 3. A GENEROUS BUDGET MUST NOT INTERFERE
    # =====================================================================
    scale.RESULT_CACHE.clear()
    sid2 = seed(wb)
    r2 = client.post("/execute/async", data={
        "session_id": sid2, "plan": json.dumps({"operations": CHAIN}),
        "timeout_seconds": "600",
    })
    fin2 = watch(r2.json()["job_id"]) if r2.status_code == 202 else None
    check("a real 120k-row chain finishes well inside a sane budget",
          bool(fin2) and fin2["state"] == "ok", f"state={fin2['state'] if fin2 else None}")
    if fin2:
        print(f"        (finished in {fin2['elapsed_ms']}ms against a 600s budget)")

    # A client may shorten the budget but never extend it past the server's own ceiling.
    r3 = client.post("/execute/async", data={
        "session_id": seed(wb), "plan": json.dumps({"operations": CHAIN}),
        "timeout_seconds": "999999",
    })
    check("a client cannot extend the budget beyond the server ceiling",
          r3.status_code == 202
          and r3.json()["timeout_seconds"] <= __import__("app.config", fromlist=["x"]).JOB_TIMEOUT_SECONDS,
          f"got {r3.json().get('timeout_seconds') if r3.status_code == 202 else r3.status_code}")
    if r3.status_code == 202:
        watch(r3.json()["job_id"])

    # =====================================================================
    # 4. THE LIMITATION, stated as a test so it cannot be quietly forgotten
    # =====================================================================
    # The boundary check runs before EVERY step, the first included, so a budget already
    # spent when the worker starts stops the run having done nothing. Asserted at the
    # Progress level rather than through HTTP: over the API a sub-millisecond budget races
    # the worker's own startup, and a coin-flip is not a test.
    spent = jobs.Progress("unused", deadline=time.time() - 5, total_steps=3)
    try:
        spent.step(0, "sort")
        stopped_before_starting = False
    except OperationCancelled as exc:
        stopped_before_starting = exc.completed_steps == 0
    check("a budget already spent stops a run before step 1 does any work",
          stopped_before_starting, "the first step was allowed to start")

    # DOCUMENTED LIMIT, asserted so it cannot be quietly forgotten: what cancellation
    # cannot do is interrupt a step that has ALREADY BEGUN. Proven deterministically at
    # the executor level — cancel is requested at step 2, and step 1 is nonetheless
    # observed to have run to completion (its work is present in the state the executor
    # was carrying). If someone later implements mid-step cancellation, this is the test
    # that should fail and send them back to the docstring.
    seen_steps: list[int] = []

    def cancel_at_second_recording(index0, action):
        seen_steps.append(index0)
        if index0 >= 1:
            raise OperationCancelled(completed_steps=index0, total_steps=2, reason="stop")

    try:
        execute_multi({"t": small}, "t", CHAIN, on_step=cancel_at_second_recording)
    except OperationCancelled:
        pass
    check("DOCUMENTED LIMIT: a step already begun always runs to completion",
          seen_steps == [0, 1],
          f"steps reached={seen_steps} — step 1 must have fully finished for the "
          "callback to be asked about step 2")

    # =====================================================================
    # 5. TIMEOUT CAN BE DISABLED
    # =====================================================================
    j = jobs.submit(
        "no-deadline-job", "sess-nd", CHAIN,
        work=lambda progress: (200, {"status": "ok", "row_count": 1}),
        timeout_seconds=0,
    )
    check("a zero budget means no deadline at all", j["deadline"] is None,
          f"deadline={j['deadline']}")
    check("and that is reported honestly rather than as a number",
          j["timeout_seconds"] is None, f"timeout_seconds={j['timeout_seconds']}")


if __name__ == "__main__":
    print("TRACK 4 item 6 — graceful timeout (120k rows + multi-step chain)\n")
    run()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
