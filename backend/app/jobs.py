"""Async execution jobs (Enhancement Track 4, item 1).

WHY. `/execute` runs the whole plan inside the request. On a 100k+ row workbook that is
tens of seconds of pandas + openpyxl work, and because the endpoint is `async def` it runs
ON THE EVENT LOOP — so a single big file doesn't just make one caller wait, it stalls
every other request in the process. This module moves that work onto a worker thread and
hands the caller a job to watch instead.

WHAT THIS IS NOT. It is not a second execution engine. The actual work is passed in as a
`work` callable (same injectable-runner shape as sync.py / workflow.py), so `/execute` and
`/execute/async` run the *identical* code path — state history, result cache, guard/confirm
and MultiStepError partial handling all keep working because none of them live here.

IDENTITY. A job's id IS the oplog `run_id` (Track 4 item 5). One id ties plan → execution →
outcome → job status, so a user quoting an id from the UI lands on the same log entry. A
separate job id would have been a second name for the same thing.

PROGRESS IS OBSERVED, NEVER GUESSED. Every number reported here comes from the executor
actually reaching a step, or from the run actually reaching the serialize phase. Nothing is
driven by a timer, and `progress` never reaches 1.0 before the job is genuinely finished.

BOUNDS. A long-lived server must not grow forever, so two separate limits:
  • a cap on job RECORDS (tiny dicts) — oldest finished evicted first;
  • a byte budget for retained RESULT BODIES, which are the big things (a result body can
    carry several MB of inline base64). Past the budget the oldest finished bodies are
    dropped while their receipt (download id, filename, row count) is kept, so the result
    file is still reachable — only the in-memory copy goes.
A RUNNING job is never evicted; you cannot forget work that is still happening.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from . import config

# Job states. The first two are live, the last three terminal — and they deliberately
# mirror the outcome vocabulary oplog.record_outcome already uses.
QUEUED = "queued"
RUNNING = "running"
OK = "ok"
PARTIAL = "partial"   # a later step failed; earlier steps are kept (MultiStepError, MS-b)
ERROR = "error"
TERMINAL = (OK, PARTIAL, ERROR)

# Coarse milestones inside a run, in the order they happen. "saving" is a real phase, not
# padding: serializing a 120k-row workbook to .xlsx is a large share of the wall clock.
PHASE_QUEUED = "queued"
PHASE_EXECUTING = "executing"
PHASE_CACHED = "cached"     # an identical (data, plan) was memoized — no steps to run
PHASE_SAVING = "saving"
PHASE_DONE = "done"

_JOBS: "OrderedDict[str, dict]" = OrderedDict()
_LOCK = threading.RLock()
_POOL: ThreadPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


class JobError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _pool() -> ThreadPoolExecutor:
    """Created lazily so merely importing the app starts no threads (tests import it a
    lot). Bounded on purpose — see config.JOB_WORKERS."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(
                max_workers=max(1, config.JOB_WORKERS), thread_name_prefix="sumio-job"
            )
        return _POOL


class Progress:
    """The handle a running job uses to report where it actually is.

    Every method is best-effort and swallows its own errors: progress reporting must never
    be able to fail a real execution. A vanished job (evicted mid-run — shouldn't happen,
    since running jobs aren't evicted) simply reports nowhere.
    """

    def __init__(self, job_id: str):
        self.job_id = job_id

    def step(self, index0: int, action: str | None = None) -> None:
        """The executor is ABOUT TO run step `index0` (0-based). Called from
        executor.execute_multi's loop, so it fires only when a step is really reached."""
        try:
            with _LOCK:
                job = _JOBS.get(self.job_id)
                if not job:
                    return
                job["phase"] = PHASE_EXECUTING
                job["current_step"] = index0 + 1
                job["current_action"] = action
                job["completed_steps"] = max(job["completed_steps"], index0)
                for s in job["steps"]:
                    if s["index"] < index0 + 1:
                        s["status"] = OK
                    elif s["index"] == index0 + 1:
                        s["status"] = RUNNING
        except Exception:
            pass

    def steps_done(self) -> None:
        """Every step finished (or the whole plan came back from the result cache)."""
        try:
            with _LOCK:
                job = _JOBS.get(self.job_id)
                if not job:
                    return
                job["completed_steps"] = job["total_steps"]
                job["current_step"] = None
                job["current_action"] = None
                for s in job["steps"]:
                    if s["status"] in (QUEUED, RUNNING):
                        s["status"] = OK
        except Exception:
            pass

    def phase(self, name: str) -> None:
        try:
            with _LOCK:
                job = _JOBS.get(self.job_id)
                if job:
                    job["phase"] = name
                    # "served from cache" is a FACT about the run, not a moment in it: the
                    # cached phase lasts microseconds before saving begins, so a poller
                    # would never catch it. Latch it so the client can still say "reused
                    # an identical earlier result" rather than implying work was done.
                    if name == PHASE_CACHED:
                        job["cached"] = True
        except Exception:
            pass


def submit(
    job_id: str,
    session_id: str,
    operations: list,
    work,
    pool: ThreadPoolExecutor | None = None,
) -> dict:
    """Register a job and start `work(progress)` on a worker thread.

    `work` is the injectable runner: it takes a `Progress` and returns
    (http_status: int, body: dict) — exactly what the synchronous endpoint would have
    returned. Keeping the contract at that level is what stops this from becoming a
    divergent second implementation of /execute.

    Returns the job record. Raises JobError(409) if that session already has a live job:
    two plans executing against one session would race on its undo/redo history, and the
    second one's "previous state" would be undefined.
    """
    with _LOCK:
        live = active_for_session(session_id)
        if live:
            raise JobError(
                "That spreadsheet already has a change running. Wait for it to finish "
                "(or undo it) before starting another.",
                status=409,
            )
        now = time.time()
        job = {
            "id": job_id,
            "session_id": session_id,
            "state": QUEUED,
            "phase": PHASE_QUEUED,
            "total_steps": len(operations or []),
            "completed_steps": 0,
            "current_step": None,
            "current_action": None,
            "steps": [
                {"index": i, "action": (op or {}).get("action"), "status": QUEUED}
                for i, op in enumerate(operations or [], 1)
            ],
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "cached": False,
            "error": None,
            "http_status": None,
            "body": None,          # the full result body, until the byte budget drops it
            "body_bytes": 0,
            "receipt": {},         # small fields kept even after the body is dropped
        }
        _JOBS[job_id] = job
        _evict()

    (pool or _pool()).submit(_run, job_id, work)
    return job


def _run(job_id: str, work) -> None:
    """The worker-thread body. Nothing that happens in here may escape: an unhandled
    exception on a pool thread would otherwise leave the job stuck in 'running' forever —
    a hung job is worse than a failed one, because nobody knows to stop waiting."""
    progress = Progress(job_id)
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job["state"] = RUNNING
            job["started_at"] = time.time()
            job["phase"] = PHASE_EXECUTING
    try:
        http_status, body = work(progress)
    except BaseException as exc:  # noqa: BLE001 - deliberately total
        finish(job_id, 500, {
            "status": "error",
            "error": "Something went wrong on our side while processing your file — "
                     "please try again, or rephrase your instruction.",
            "detail_type": type(exc).__name__,
        })
        return
    finish(job_id, http_status, body)


def finish(job_id: str, http_status: int, body: dict) -> dict | None:
    """Record a terminal outcome. The job's state is DERIVED from the response the shared
    execution path produced, so success / partial / failure can't drift from what a
    synchronous caller would have seen."""
    body = body if isinstance(body, dict) else {}
    if http_status >= 400 or body.get("status") == "error":
        state = ERROR
    elif body.get("partial"):
        state = PARTIAL
    else:
        state = OK
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        job["state"] = state
        job["phase"] = PHASE_DONE
        job["finished_at"] = time.time()
        job["http_status"] = http_status
        job["body"] = body
        job["body_bytes"] = _rough_size(body)
        job["cached"] = bool(job["cached"] or body.get("cached"))
        job["error"] = body.get("error") or body.get("warning")
        job["receipt"] = {
            k: body.get(k)
            for k in ("download_id", "filename", "media_type", "row_count", "file_size")
            if body.get(k) is not None
        }
        if state == PARTIAL:
            job["completed_steps"] = int(body.get("completed_steps") or job["completed_steps"])
            failed = body.get("failed_step")
            for s in job["steps"]:
                if failed and s["index"] == failed:
                    s["status"] = ERROR
                elif failed and s["index"] > failed:
                    s["status"] = "skipped"
                else:
                    s["status"] = OK
            job["current_step"] = None
            job["current_action"] = None
        elif state == OK:
            job["completed_steps"] = job["total_steps"]
            job["current_step"] = None
            job["current_action"] = None
            for s in job["steps"]:
                s["status"] = OK
        else:
            for s in job["steps"]:
                if s["status"] == RUNNING:
                    s["status"] = ERROR
                elif s["status"] == QUEUED:
                    s["status"] = "skipped"
            job["current_step"] = None
            job["current_action"] = None
        _evict()
        return job


def _rough_size(body: dict) -> int:
    """Cheap byte estimate for the memory budget. The inline base64 file dominates by
    orders of magnitude, so measuring it and treating everything else as small is both
    accurate enough and O(1) — json.dumps of a big body would itself cost megabytes."""
    if not isinstance(body, dict):
        return 0
    inline = body.get("file_base64")
    return (len(inline) if isinstance(inline, str) else 0) + 4096


def _evict() -> None:
    """Two independent bounds; caller holds _LOCK.

    Running/queued jobs are never touched — only finished ones are forgettable."""
    max_jobs = max(1, config.MAX_JOBS)
    budget = max(0, config.MAX_JOB_RESULT_MB) * 1024 * 1024

    # 1) too many records → drop the oldest FINISHED ones entirely.
    if len(_JOBS) > max_jobs:
        for jid in list(_JOBS):
            if len(_JOBS) <= max_jobs:
                break
            if _JOBS[jid]["state"] in TERMINAL:
                del _JOBS[jid]

    # 2) too many retained bytes → drop the oldest bodies, keeping their receipts, so a
    #    caller that comes back late still learns where its file is.
    held = sum(j["body_bytes"] for j in _JOBS.values() if j["body"] is not None)
    if held > budget:
        for job in list(_JOBS.values()):
            if held <= budget:
                break
            if job["body"] is not None and job["state"] in TERMINAL:
                held -= job["body_bytes"]
                job["body"] = None
                job["body_bytes"] = 0


def get(job_id: str) -> dict | None:
    with _LOCK:
        return _JOBS.get(job_id)


def active_for_session(session_id: str) -> str | None:
    """The id of this session's live job, if any. Caller may hold _LOCK (it's an RLock)."""
    if not session_id:
        return None
    with _LOCK:
        for jid, job in _JOBS.items():
            if job["session_id"] == session_id and job["state"] not in TERMINAL:
                return jid
    return None


def snapshot(job_id: str) -> dict | None:
    """The small, JSON-safe status a poller gets. Never includes the result body — a
    status poll must stay cheap enough to run once a second next to a multi-MB result."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        total = job["total_steps"]
        done = job["completed_steps"]
        state = job["state"]
        if state in TERMINAL:
            fraction = 1.0
        elif total <= 0:
            fraction = 0.0
        else:
            # Serializing is real work, so it gets a slice: a plan of N steps is measured
            # out of N+1 units, and being INSIDE that phase counts as half of it (there is
            # no honest way to know how far through a workbook write we are). This is also
            # why finishing every step still shows < 100% — the file isn't written yet, and
            # claiming otherwise would be the same lie the old timer-driven tracker told.
            units = done + (0.5 if job["phase"] == PHASE_SAVING else 0.0)
            fraction = round(min(units / (total + 1), 0.99), 3)
        return {
            "job_id": job["id"],
            "run_id": job["id"],  # the same id: see the module docstring
            "session_id": job["session_id"],
            "state": state,
            "phase": job["phase"],
            "done": state in TERMINAL,
            "cached": job["cached"],
            "total_steps": total,
            "completed_steps": done,
            "current_step": job["current_step"],
            "current_action": job["current_action"],
            "steps": [dict(s) for s in job["steps"]],
            "progress": fraction,
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "elapsed_ms": int(
                ((job["finished_at"] or time.time()) - (job["started_at"] or job["created_at"]))
                * 1000
            ),
            "error": job["error"],
            "result_available": job["body"] is not None,
            "receipt": dict(job["receipt"]),
        }


def result(job_id: str) -> tuple[int, dict] | None:
    """The stored (http_status, body) — the exact response `/execute` would have given.
    None means the job is unknown, unfinished, or its body has been dropped; the caller
    distinguishes those from the snapshot."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job or job["state"] not in TERMINAL or job["body"] is None:
            return None
        return job["http_status"] or 200, job["body"]


def recent(limit: int = 20) -> list[dict]:
    """Most-recent-first status list, for debugging a live server."""
    with _LOCK:
        ids = list(_JOBS)[-max(0, limit):]
    return [s for s in (snapshot(j) for j in reversed(ids)) if s]


def clear() -> None:
    with _LOCK:
        _JOBS.clear()
