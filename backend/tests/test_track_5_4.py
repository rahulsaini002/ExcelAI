"""ENHANCEMENT TRACK 5, item 4 — observability: where users struggle.

An aggregation over what oplog already records, so the tests care less about "does it add
up" and more about the ways a metrics endpoint can quietly lie:

  - averaging two different failure modes into one "success rate", hiding whether the
    problem is the prompt or the engine;
  - reporting 0.0 for an empty sample, which reads as "everything failed" when the truth
    is "nothing ran";
  - a mean time-to-result that no user ever experienced;
  - counting failed runs' durations as if they measured work;
  - a hundred unique error strings instead of a handful of causes;
  - claiming a retry rate with nothing behind it.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_track_5_4.py
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

_fd, _db = tempfile.mkstemp(suffix="-t54.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import metrics, oplog, scale  # noqa: E402
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


def workbook() -> bytes:
    df = pd.DataFrame({
        "Email": [f"u{i % 40}@x.com" for i in range(80)],
        "Amount": [float(i) for i in range(80)],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def seed(wb: bytes) -> str:
    sid = f"t54-{uuid.uuid4().hex[:8]}"
    r = client.post("/inspect", data={"session_id": sid},
                    files=[("files", ("b.xlsx", wb, XL))])
    assert r.status_code == 200, r.text
    return sid


def run_plan(sid: str, ops: list, **extra):
    data = {"session_id": sid, "plan": json.dumps({"operations": ops})}
    data.update(extra)
    return client.post("/execute", data=data)


def run() -> None:
    # --- empty sample: rates must be None, not 0.0 -----------------------------------
    oplog.clear()
    empty = metrics.summary()
    check("an empty log reports no execution success rate (not 0.0, which reads as 'all failed')",
          empty["execution"]["success_rate"] is None,
          f"got {empty['execution']['success_rate']!r}")
    check("an empty log reports no retry rate", empty["retries"]["retry_rate"] is None)
    check("an empty log reports no median duration", empty["time_to_result_ms"]["median"] is None)
    check("an empty log still returns a well-formed shape",
          {"understanding", "execution", "retries", "operations", "failures"} <= set(empty))

    # --- real traffic ----------------------------------------------------------------
    wb = workbook()
    scale.RESULT_CACHE.clear()
    sid = seed(wb)
    ok1 = run_plan(sid, [{"action": "remove_duplicates", "columns": ["Email"]}])
    check("a successful run is recorded", ok1.status_code == 200, f"HTTP {ok1.status_code}")

    scale.RESULT_CACHE.clear()
    sid2 = seed(wb)
    ok2 = run_plan(sid2, [{"action": "sort", "columns": ["Amount"], "orders": ["desc"]}])

    # A failure with a recognisable cause.
    bad = run_plan(seed(wb), [{"action": "sort", "columns": ["NoSuchCol"], "orders": ["asc"]}])
    check("a failing run is rejected", bad.status_code >= 400, f"HTTP {bad.status_code}")

    # A retry: rewind >= 0 is the UI's Retry/Edit path.
    scale.RESULT_CACHE.clear()
    sid3 = seed(wb)
    run_plan(sid3, [{"action": "sort", "columns": ["Amount"], "orders": ["asc"]}])
    run_plan(sid3, [{"action": "sort", "columns": ["Amount"], "orders": ["desc"]}], rewind=0)

    m = metrics.summary()

    # --- execution success is measured, and separately from understanding ------------
    ex = m["execution"]
    check("execution success rate is a real fraction", isinstance(ex["success_rate"], float),
          f"got {ex['success_rate']!r}")
    check("execution counts both successes and failures",
          ex["runs"] >= 4 and ex["ok"] >= 3, f"runs={ex['runs']} ok={ex['ok']}")
    check("statuses are broken out, not collapsed",
          "ok" in ex["by_status"] and "error" in ex["by_status"], f"{ex['by_status']}")
    check("understanding is reported as its OWN population, not merged in",
          "plan_rate" in m["understanding"] and "success_rate" not in m["understanding"],
          f"{sorted(m['understanding'])}")
    check("the response says plainly that the two are not a funnel",
          "not a funnel" in m["note"], m["note"])

    # --- retries ---------------------------------------------------------------------
    check("the retry was counted", m["retries"]["retried"] >= 1, f"{m['retries']}")
    check("retry rate is between 0 and 1",
          0.0 <= (m["retries"]["retry_rate"] or 0) <= 1.0, f"{m['retries']['retry_rate']}")
    check("retry rate is not 100% — ordinary runs are counted too",
          (m["retries"]["retry_rate"] or 1) < 1.0, f"{m['retries']}")

    # --- operation usage --------------------------------------------------------------
    ops = m["operations"]["by_action"]
    check("operation usage counts the actions actually run",
          ops.get("sort", 0) >= 3 and ops.get("remove_duplicates", 0) >= 1, f"{ops}")
    check("most_used is ordered by frequency",
          m["operations"]["most_used"][0][1] >= m["operations"]["most_used"][-1][1],
          f"{m['operations']['most_used']}")

    # --- failure reasons are GROUPED --------------------------------------------------
    fails = m["failures"]
    check("failures are grouped into causes, not raw strings",
          all(" " not in k for k in fails["by_reason"]), f"{list(fails['by_reason'])}")
    check("the missing-column failure is classified as such",
          "missing_column" in fails["by_reason"], f"{fails['by_reason']}")
    check("one example is kept per reason so a cause stays traceable",
          bool(fails["examples"].get("missing_column")), f"{fails['examples']}")

    # classification itself
    check("a timeout is classified as timed_out",
          metrics.classify_failure("This took longer than the time budget") == "timed_out")
    check("an unsupported action is classified",
          metrics.classify_failure("I don't have an operation called 'x'")
          == "unsupported_operation")
    check("unrecognised text falls into 'other', never silently dropped",
          metrics.classify_failure("something entirely new") == "other")
    check("empty text is 'unknown', distinct from 'other'",
          metrics.classify_failure("") == "unknown")

    # --- time to result ---------------------------------------------------------------
    t = m["time_to_result_ms"]
    check("median and p95 are reported (never a mean)",
          t["median"] is not None and t["p95"] is not None, f"{t}")
    check("p95 is at least the median", t["p95"] >= t["median"], f"{t}")
    check("only successful runs are timed",
          t["runs_measured"] == m["execution"]["ok"],
          f"measured={t['runs_measured']} ok={m['execution']['ok']}")

    # --- the endpoint -----------------------------------------------------------------
    r = client.get("/metrics/usage")
    check("GET /metrics/usage returns 200", r.status_code == 200, f"HTTP {r.status_code}")
    body = r.json() if r.status_code == 200 else {}
    check("the endpoint returns the same shape as the module",
          set(body.get("metrics", {})) == set(m), f"{sorted(body.get('metrics', {}))}")

    # --- privacy: metrics must not leak data ------------------------------------------
    blob = json.dumps(body)
    check("no cell values leak through the metrics endpoint",
          "@x.com" not in blob, "a cell value reached the metrics output")


if __name__ == "__main__":
    print("TRACK 5 item 4 — usage metrics\n")
    run()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
