"""ENHANCEMENT TRACK 4, item 1 — async execution with job status streamed to the UI.

Exercised against the sizes Track 4 asks for: a 120,000-row file and multi-step chains.

Before this, /execute ran the whole plan inside the request — and because the endpoint is
`async def`, a 100k-row run occupied the EVENT LOOP, so one big file stalled every other
request in the process. This suite pins down the four things that have to be true for the
async path to be worth having:

  IT RETURNS      submitting hands back a job id in milliseconds while a run that takes
                  seconds is still going, and the server stays responsive meanwhile.
  IT IS HONEST    every number a poller sees is OBSERVED — the executor really reached that
                  step, the run really reached the saving phase. Progress never reaches
                  100% before the job is done, and never goes backwards. This is the same
                  property the frontend tracker was fixed to hold; an async endpoint that
                  reported invented progress would hand the lie back to it.
  IT IS THE SAME  the async result is compared field by field against what synchronous
                  /execute produces for the same plan on the same data. The async path is
                  a change of transport, not of semantics — including the guard/confirm
                  flow, the plan shape-checks, and MultiStepError's partial result.
  IT IS BOUNDED   a long-lived server must not grow forever, so both the job RECORDS and
                  the retained result BODIES are capped, and a running job is never evicted.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_track_4_1.py
"""
from __future__ import annotations

import base64
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

_fd, _db = tempfile.mkstemp(suffix="-t41.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import jobs, oplog, scale  # noqa: E402
from app.db import init_db  # noqa: E402
from app.executor import execute_multi  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0
XL = "application/octet-stream"

BIG_ROWS = 120_000  # Track 4's "large file" bar, comfortably over 100k

# A multi-step chain that does real work on every step, so progress has something to
# report and the run lasts long enough for "returned immediately" to mean something.
CHAIN = [
    {"action": "remove_duplicates", "columns": ["Email"]},
    {"action": "add_formula_column", "name": "Doubled", "formula": "{Revenue}*2"},
    {"action": "sort", "columns": ["Revenue"], "orders": ["desc"]},
]

# Fields where the async and sync responses MUST agree. Excluded on purpose: run_id and
# download_id (unique per run by design), elapsed_ms (wall clock), session_id (different
# sessions), and `cached` (the second of two identical runs is a legitimate cache hit).
SAME_FIELDS = (
    "status", "explanation", "notes", "formulas", "code", "row_count", "rows_before",
    "preview", "insight", "actions", "ai_title", "partial", "warning",
    "completed_steps", "failed_step", "filename", "media_type",
)
# file_size / file_base64 are compared by DECODING the workbooks instead. An .xlsx is a
# zip, and a zip embeds the moment each entry was written, so two identical spreadsheets
# written a second apart are never byte-identical. Comparing the parsed sheets is both the
# stronger claim and the one that isn't hostage to a timestamp.


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
    sid = f"t41-{uuid.uuid4().hex[:8]}"
    r = client.post("/inspect", data={"session_id": sid},
                    files=[("files", ("big.xlsx", wb, XL))])
    assert r.status_code == 200, r.text
    return sid


def submit(sid: str, plan, **extra) -> "tuple[int, dict]":
    data = {"session_id": sid, "plan": json.dumps(plan)}
    data.update(extra)
    r = client.post("/execute/async", data=data)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def watch(job_id: str, timeout: float = 120.0, interval: float = 0.01) -> list[dict]:
    """Poll a job to completion, keeping every snapshot we saw. The tight interval is a
    TEST choice — it maximises the chance of catching mid-run states so the progress
    claims below are actually exercised. Real clients back off (see lib/job-progress.ts)."""
    seen: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/jobs/{job_id}")
        if r.status_code != 200:
            break
        snap = r.json()["job"]
        if not seen or snap != seen[-1]:
            seen.append(snap)
        if snap["done"]:
            return seen
        time.sleep(interval)
    return seen


def run() -> None:
    oplog.clear()
    jobs.clear()
    print(f"  (building a {BIG_ROWS:,}-row workbook…)")
    t0 = time.time()
    wb = big_workbook()
    print(f"  (built in {time.time() - t0:.1f}s, {len(wb) / 1_000_000:.1f} MB)\n")

    # =====================================================================
    # 1. SUBMITTING RETURNS IMMEDIATELY
    # =====================================================================
    scale.RESULT_CACHE.clear()  # force a genuine recompute, not a memoized answer
    sid = seed(wb)
    t0 = time.time()
    code, body = submit(sid, {"operations": CHAIN})
    submit_ms = int((time.time() - t0) * 1000)
    check("POST /execute/async is accepted with 202", code == 202, f"HTTP {code} {body}")
    job_id = body.get("job_id")
    check("the response carries a job id straight away", bool(job_id), f"body={body}")
    check("the job id IS the oplog run id (one id, not two)",
          body.get("run_id") == job_id, f"run_id={body.get('run_id')} job_id={job_id}")
    check("the plan's step count is reported up front",
          body.get("total_steps") == len(CHAIN), f"got {body.get('total_steps')}")

    # ...and the server is still answering while that job runs. This is the whole point:
    # the old path did this work ON the event loop, so nothing else got served.
    t0 = time.time()
    h = client.get("/health")
    health_ms = int((time.time() - t0) * 1000)
    check("the server still answers /health while a 120k-row job runs",
          h.status_code == 200 and health_ms < 2000, f"HTTP {h.status_code} in {health_ms}ms")

    # =====================================================================
    # 2. PROGRESS GENUINELY ADVANCES
    # =====================================================================
    snaps = watch(job_id)
    check("the job reaches a terminal state (never hangs)",
          bool(snaps) and snaps[-1]["done"], f"last={snaps[-1] if snaps else None}")
    final = snaps[-1] if snaps else {}
    run_ms = final.get("elapsed_ms", 0)
    print(f"        (submit {submit_ms}ms, run {run_ms}ms, {len(snaps)} distinct snapshots)")
    check("submitting returned in a fraction of the run it started",
          submit_ms * 4 < run_ms, f"submit {submit_ms}ms vs run {run_ms}ms")

    live = [s for s in snaps if not s["done"]]
    steps_seen = sorted({s["current_step"] for s in live if s["current_step"]})
    actions_seen = {s["current_action"] for s in live} - {None}
    phases_seen = [s["phase"] for s in snaps]
    check("a real mid-plan step is observed from outside the process",
          bool(steps_seen), f"steps observed while running: {steps_seen}")
    check("the step being run is named, not just numbered",
          actions_seen <= {"remove_duplicates", "add_formula_column", "sort"} and actions_seen,
          f"actions seen: {sorted(actions_seen)}")
    check("saving the file is reported as its own phase, not a frozen last step",
          "saving" in phases_seen, f"phases={phases_seen}")
    counts = [s["completed_steps"] for s in snaps]
    check("completed steps never go backwards",
          all(b >= a for a, b in zip(counts, counts[1:])), f"counts={counts}")
    check("an intermediate 'k of N done' is actually observed",
          any(0 < c < len(CHAIN) for c in counts), f"counts={counts}")
    # WORTH KNOWING, and the reason 'saving' is a phase rather than a rounding error: on a
    # 120k-row file the three pandas steps are a small slice of the wall clock and writing
    # the .xlsx is most of it. A tracker that only counted steps would sit at "3 of 3" for
    # the majority of the run.
    exec_snaps = sum(1 for s in snaps if s["phase"] == "executing")
    print(f"        (phases: {exec_snaps} snapshots executing, "
          f"{sum(1 for s in snaps if s['phase'] == 'saving')} saving)")

    # HONESTY: this is the property the frontend tracker was fixed to hold. A backend that
    # reported 100% while still writing the file would hand the lie straight back.
    check("progress never claims 100% before the job is done",
          all(s["progress"] < 1.0 for s in live),
          f"premature: {[s['progress'] for s in live if s['progress'] >= 1.0]}")
    check("progress is 100% once done", final.get("progress") == 1.0,
          f"final progress={final.get('progress')}")
    check("the finished job reports every step complete",
          final.get("state") == "ok" and final.get("completed_steps") == len(CHAIN),
          f"state={final.get('state')} completed={final.get('completed_steps')}")
    check("per-step statuses are all ok",
          [s["status"] for s in final.get("steps", [])] == ["ok"] * len(CHAIN),
          f"steps={final.get('steps')}")

    # The correlation from item 5 still holds through the async path.
    entries = oplog.run(job_id)
    check("the job id finds the run's plan and outcome in the oplog",
          [e["phase"] for e in entries] == ["plan", "outcome"],
          f"phases={[e['phase'] for e in entries]}")

    # --- where the progress numbers COME from -----------------------------------------
    # The HTTP observation above can only see what a poller happens to catch, and pandas
    # steps on 120k rows are faster than a poll under GIL contention. So the two halves of
    # the claim are pinned down separately and deterministically:
    #   (a) the executor fires a step event for every step, in order, as it reaches it;
    #   (b) the job store turns those events into an advancing, monotonic status.
    fired: list[tuple] = []
    small = pd.DataFrame({"Email": ["a@x", "a@x", "b@x"], "Revenue": [1.0, 1.0, 2.0]})
    execute_multi({"t": small}, "t", CHAIN, on_step=lambda i, a: fired.append((i, a)))
    check("(a) the executor reports every step, in order, as it reaches it",
          fired == [(0, "remove_duplicates"), (1, "add_formula_column"), (2, "sort")],
          f"fired={fired}")
    out, _, _, _ = execute_multi({"t": small}, "t", CHAIN,
                                 on_step=lambda i, a: (_ for _ in ()).throw(RuntimeError("x")))
    check("a broken progress reporter cannot break a real execution", len(out) == 2,
          f"rows={len(out)}")

    STEPS = ["filter", "sort", "merge", "format_cells"]

    def paced(progress):
        for i, action in enumerate(STEPS):
            progress.step(i, action)
            time.sleep(0.12)
        progress.steps_done()
        progress.phase(jobs.PHASE_SAVING)
        time.sleep(0.12)
        return 200, {"status": "ok", "row_count": 3}

    jobs.submit("paced-job", "s-paced", [{"action": a} for a in STEPS], paced)
    seen: list[dict] = []
    deadline = time.time() + 20
    while time.time() < deadline:
        s = jobs.snapshot("paced-job")
        if not seen or (s["current_step"], s["completed_steps"], s["phase"], s["done"]) != (
            seen[-1]["current_step"], seen[-1]["completed_steps"], seen[-1]["phase"], seen[-1]["done"]
        ):
            seen.append(s)
        if s["done"]:
            break
        time.sleep(0.02)
    order = [s["current_action"] for s in seen if s["current_action"]]
    check("(b) the store streams each step of a 4-step plan, in order",
          order == STEPS, f"observed={order}")
    fracs = [s["progress"] for s in seen]
    check("progress rises monotonically and only reaches 1.0 at the end",
          all(b >= a for a, b in zip(fracs, fracs[1:])) and fracs[-1] == 1.0
          and max(fracs[:-1]) < 1.0, f"progress={fracs}")
    check("the saving phase is visible between the last step and done",
          "saving" in [s["phase"] for s in seen], f"phases={[s['phase'] for s in seen]}")

    # =====================================================================
    # 3. THE RESULT IS THE SAME AS SYNCHRONOUS /execute
    # =====================================================================
    r = client.get(f"/jobs/{job_id}/result")
    check("GET /jobs/{id}/result returns the finished result", r.status_code == 200,
          f"HTTP {r.status_code} {r.text[:160]}")
    async_body = r.json() if r.status_code == 200 else {}
    check("the async result is a normal ok result",
          async_body.get("status") == "ok" and async_body.get("row_count") == BIG_ROWS // 2,
          f"status={async_body.get('status')} rows={async_body.get('row_count')}")
    check("the async result carries the same run_id as the job",
          async_body.get("run_id") == job_id, f"got {async_body.get('run_id')}")

    scale.RESULT_CACHE.clear()  # make the sync run compute for real too
    sync = client.post("/execute", data={"session_id": seed(wb),
                                         "plan": json.dumps({"operations": CHAIN})})
    check("the same plan runs synchronously for comparison", sync.status_code == 200,
          f"HTTP {sync.status_code} {sync.text[:160]}")
    sync_body = sync.json() if sync.status_code == 200 else {}
    mismatched = [f for f in SAME_FIELDS if async_body.get(f) != sync_body.get(f)]
    check("async and sync results agree on every meaningful field",
          not mismatched, f"differ on {mismatched}")
    def sheet_of(body: dict):
        raw = body.get("file_base64")
        return pd.read_excel(io.BytesIO(base64.b64decode(raw))) if raw else None

    a_df, s_df = sheet_of(async_body), sheet_of(sync_body)
    check("both runs actually produced a downloadable workbook",
          a_df is not None and s_df is not None,
          f"async={a_df is not None} sync={s_df is not None}")
    check("the two workbooks contain exactly the same 60,000 rows of data",
          a_df is not None and s_df is not None and a_df.equals(s_df),
          f"shapes {None if a_df is None else a_df.shape} vs "
          f"{None if s_df is None else s_df.shape}")
    check("their file sizes match to within zip metadata",
          abs((async_body.get("file_size") or 0) - (sync_body.get("file_size") or 0)) < 1024,
          f"{async_body.get('file_size')} vs {sync_body.get('file_size')}")

    # =====================================================================
    # 4. FAILURE — surfaced, not hung
    # =====================================================================
    bad = [{"action": "sort", "columns": ["NoSuchColumn"], "orders": ["asc"]}]
    code, body = submit(seed(wb), {"operations": bad})
    check("a doomed plan is still accepted as a job (the error is data-dependent)",
          code == 202, f"HTTP {code} {body}")
    fsnaps = watch(body.get("job_id", ""))
    ffinal = fsnaps[-1] if fsnaps else {}
    check("a failing run reaches the terminal 'error' state",
          ffinal.get("state") == "error" and ffinal.get("done") is True,
          f"final={ffinal.get('state')} done={ffinal.get('done')}")
    check("the failure carries a reason, not just a status",
          bool(ffinal.get("error")), f"error={ffinal.get('error')!r}")
    fr = client.get(f"/jobs/{body.get('job_id')}/result")
    check("collecting a failed job replays /execute's own 422 + message",
          fr.status_code == 422 and "NoSuchColumn" in fr.text,
          f"HTTP {fr.status_code} {fr.text[:160]}")

    # =====================================================================
    # 5. PARTIAL FAILURE — the completed steps are kept (MS-b), via a job
    # =====================================================================
    partial_plan = [
        {"action": "remove_duplicates", "columns": ["Email"]},
        {"action": "sort", "columns": ["Ghost"], "orders": ["asc"]},
    ]
    code, body = submit(seed(wb), {"operations": partial_plan})
    psnaps = watch(body.get("job_id", ""))
    pfinal = psnaps[-1] if psnaps else {}
    check("a later-step failure ends in the 'partial' state, distinct from 'error'",
          pfinal.get("state") == "partial", f"state={pfinal.get('state')}")
    check("the partial job reports 1 of 2 steps completed",
          pfinal.get("completed_steps") == 1 and pfinal.get("total_steps") == 2,
          f"{pfinal.get('completed_steps')}/{pfinal.get('total_steps')}")
    check("per-step statuses mark step 2 as the failure",
          [s["status"] for s in pfinal.get("steps", [])] == ["ok", "error"],
          f"steps={pfinal.get('steps')}")
    pr = client.get(f"/jobs/{body.get('job_id')}/result")
    pbody = pr.json() if pr.status_code == 200 else {}
    check("the partial result is still a downloadable 200 with the completed work",
          pr.status_code == 200 and pbody.get("partial") is True
          and pbody.get("failed_step") == 2 and pbody.get("row_count") == BIG_ROWS // 2,
          f"HTTP {pr.status_code} partial={pbody.get('partial')} rows={pbody.get('row_count')}")

    # =====================================================================
    # 6. THE SYNCHRONOUS RULES ARE NOT FORKED
    # =====================================================================
    # Guard/confirm: a destructive plan must be stopped BEFORE a job exists, so the user
    # never gets a job id for work they haven't agreed to.
    before = len(jobs._JOBS)
    destructive = [{"action": "drop_columns", "columns": ["Region"]}]
    code, body = submit(seed(wb), {"operations": destructive}, guard="true")
    check("guard=true still returns confirm_required instead of starting a job",
          body.get("status") == "confirm_required", f"HTTP {code} {body}")
    check("no job is created for a plan awaiting confirmation",
          len(jobs._JOBS) == before, f"{len(jobs._JOBS)} vs {before}")
    code, body = submit(seed(wb), {"operations": destructive}, guard="true", confirm="true")
    check("confirming lets the same plan through as a job", code == 202, f"HTTP {code} {body}")
    watch(body.get("job_id", ""))

    # Plan shape-checks: the same 400/422s as the sync door, and no job for any of them.
    before = len(jobs._JOBS)
    shapes = [
        ('"just a string"', 400, "a nonsense plan shape"),
        ('{"operations": "not a list"}', 400, "a non-list operations field"),
        ('{"operations": []}', 400, "an empty plan"),
        ('[{"columns": ["Email"]}]', 422, "a step with no action"),
    ]
    for raw, want, label in shapes:
        r = client.post("/execute/async", data={"session_id": seed(wb), "plan": raw})
        check(f"{label} gets a clean {want} from the async door too", r.status_code == want,
              f"HTTP {r.status_code} {r.text[:120]}")
    check("no jobs were created for any malformed plan", len(jobs._JOBS) == before,
          f"{len(jobs._JOBS)} vs {before}")
    r = client.post("/execute/async", data={"session_id": "nope-not-a-session",
                                            "plan": json.dumps(CHAIN)})
    check("an unknown session is rejected before a job is made", r.status_code == 400,
          f"HTTP {r.status_code}")

    # A bare list of steps still works (the item-5 defect fix), on this door too.
    code, body = submit(seed(wb), CHAIN)
    check("a bare list of steps is accepted as a plan here as well", code == 202,
          f"HTTP {code} {body}")
    cached_job = body.get("job_id")
    csnaps = watch(cached_job)
    cfinal = csnaps[-1] if csnaps else {}
    cbody = client.get(f"/jobs/{cached_job}/result").json()
    check("a repeat of an identical plan is served from the result cache (5.8 intact)",
          cbody.get("cached") is True, f"cached={cbody.get('cached')}")
    # A cache hit has NO steps to walk through, and the cached phase lasts microseconds —
    # so the job latches the fact instead of leaving a poller to catch a flicker, and the
    # step counter jumps to complete rather than pantomiming work that never happened.
    check("a cache hit still ends complete and is flagged as cached, not faked",
          cfinal.get("state") == "ok"
          and cfinal.get("completed_steps") == len(CHAIN)
          and cfinal.get("cached") is True,
          f"state={cfinal.get('state')} cached={cfinal.get('cached')}")

    # =====================================================================
    # 7. ONE CHANGE AT A TIME PER SESSION
    # =====================================================================
    scale.RESULT_CACHE.clear()
    busy_sid = seed(wb)
    code, body = submit(busy_sid, {"operations": CHAIN})
    check("first job on a session is accepted", code == 202, f"HTTP {code}")
    code2, body2 = submit(busy_sid, {"operations": CHAIN})
    check("a second job on the SAME session is refused with 409, not raced",
          code2 == 409, f"HTTP {code2} {body2}")
    sync_clash = client.post("/execute", data={"session_id": busy_sid,
                                               "plan": json.dumps({"operations": CHAIN})})
    check("synchronous /execute is refused too while that session has a live job",
          sync_clash.status_code == 409, f"HTTP {sync_clash.status_code}")
    watch(body.get("job_id", ""))
    code3, body3 = submit(busy_sid, {"operations": CHAIN})
    check("once the job finishes the session accepts work again", code3 == 202, f"HTTP {code3}")
    watch(body3.get("job_id", ""))

    # =====================================================================
    # 8. THE POLLING CONTRACT
    # =====================================================================
    r = client.get("/jobs/does-not-exist")
    check("an unknown job id is a clean 404", r.status_code == 404, f"HTTP {r.status_code}")
    r = client.get("/jobs/does-not-exist/result")
    check("collecting an unknown job is a clean 404", r.status_code == 404,
          f"HTTP {r.status_code}")
    jobs.clear()
    jobs.submit("pending-job", "sess-pending", CHAIN, lambda p: (200, {"status": "ok"}),
                pool=_NeverRuns())
    r = client.get("/jobs/pending-job/result")
    check("collecting a job that hasn't finished is a 409, not a lie",
          r.status_code == 409 and r.json().get("job", {}).get("done") is False,
          f"HTTP {r.status_code} {r.text[:120]}")
    r = client.get("/jobs")
    check("GET /jobs lists recent jobs for a live server",
          r.status_code == 200 and isinstance(r.json().get("jobs"), list),
          f"HTTP {r.status_code}")

    # =====================================================================
    # 9. THE JOB STORE IS BOUNDED
    # =====================================================================
    jobs.clear()
    from app import config as cfg
    for i in range(cfg.MAX_JOBS + 50):
        jid = f"bound-{i}"
        jobs.submit(jid, f"s{i}", [{"action": "sort"}], lambda p: (200, {}), pool=_NeverRuns())
        jobs.finish(jid, 200, {"status": "ok", "row_count": 1, "download_id": f"d{i}"})
    check("the number of job records is capped", len(jobs._JOBS) == cfg.MAX_JOBS,
          f"held {len(jobs._JOBS)}")
    check("the jobs kept are the most recent ones",
          f"bound-{cfg.MAX_JOBS + 49}" in jobs._JOBS and "bound-0" not in jobs._JOBS,
          "eviction dropped the wrong end")

    # A running job must never be evicted — losing track of work in progress is worse than
    # keeping one extra record.
    jobs.clear()
    jobs.submit("long-runner", "s-long", CHAIN, lambda p: (200, {}), pool=_NeverRuns())
    jobs._JOBS["long-runner"]["state"] = jobs.RUNNING
    for i in range(cfg.MAX_JOBS + 20):
        jid = f"filler-{i}"
        jobs.submit(jid, f"f{i}", [{"action": "sort"}], lambda p: (200, {}), pool=_NeverRuns())
        jobs.finish(jid, 200, {"status": "ok"})
    check("a RUNNING job survives eviction pressure", "long-runner" in jobs._JOBS,
          "a job still in flight was forgotten")

    # Result BODIES are the big things (inline base64), so they get their own byte budget.
    jobs.clear()
    chunk = "x" * (4 * 1024 * 1024)  # ~4 MB of "file" per result
    n = (cfg.MAX_JOB_RESULT_MB // 4) + 4
    for i in range(n):
        jid = f"heavy-{i}"
        jobs.submit(jid, f"h{i}", [{"action": "sort"}], lambda p: (200, {}), pool=_NeverRuns())
        jobs.finish(jid, 200, {"status": "ok", "file_base64": chunk,
                               "download_id": f"dl{i}", "filename": "out.xlsx"})
    held = sum(j["body_bytes"] for j in jobs._JOBS.values() if j["body"] is not None)
    check("retained result bodies stay inside the memory budget",
          held <= cfg.MAX_JOB_RESULT_MB * 1024 * 1024,
          f"held {held / 1024 / 1024:.0f} MB of {cfg.MAX_JOB_RESULT_MB} MB")
    check("the newest result is the one still in memory",
          jobs._JOBS[f"heavy-{n - 1}"]["body"] is not None, "the newest body was dropped")
    dropped = jobs.snapshot("heavy-0")
    check("a job whose body was dropped still says where its file is",
          dropped["result_available"] is False and dropped["receipt"].get("download_id") == "dl0",
          f"receipt={dropped['receipt']}")
    r = client.get("/jobs/heavy-0/result")
    check("collecting a released result is a 410 that points at the download, not a 500",
          r.status_code == 410 and r.json().get("job", {}).get("receipt", {}).get("download_id") == "dl0",
          f"HTTP {r.status_code} {r.text[:160]}")

    # =====================================================================
    # 10. A CRASHING JOB FAILS, IT DOES NOT HANG
    # =====================================================================
    jobs.clear()

    def explode(progress):
        raise RuntimeError("boom")

    jobs.submit("crash-job", "s-crash", [{"action": "sort"}], explode)
    deadline = time.time() + 10
    while time.time() < deadline and not jobs.snapshot("crash-job")["done"]:
        time.sleep(0.01)
    snap = jobs.snapshot("crash-job")
    check("an unexpected crash on the worker thread ends the job as 'error'",
          snap["done"] and snap["state"] == "error", f"snap={snap['state']} done={snap['done']}")
    got = jobs.result("crash-job")
    check("the crash is reported to the user in the house voice, not as a traceback",
          got is not None and got[0] == 500 and "went wrong on our side" in got[1]["error"],
          f"got={got}")


class _NeverRuns:
    """A stand-in pool for tests that want a job RECORD without executing it — the store's
    bookkeeping is what's under test there, not the engine."""

    def submit(self, fn, *args, **kwargs):
        return None


if __name__ == "__main__":
    print("TRACK 4 item 1 — async execution + job status (120k rows + multi-step chains)\n")
    run()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
