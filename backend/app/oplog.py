"""Operation logging (Enhancement Track 4, item 5).

Every Operation Plan, its execution, and its outcome — so a bug report like "it dropped
my rows" can be answered from evidence instead of guesswork.

Two sinks, because they serve different readers:

  Python `logging` ("sumio.ops")  the operator's sink. Goes wherever the deployment
                                  sends logs; survives the process. Nothing here
                                  configures handlers — libraries that hijack the root
                                  logger are a menace — so a plain uvicorn run shows
                                  these at INFO and an embedding app can route them.
  bounded in-memory ring          the debugger's sink, readable over HTTP at
                                  /debug/oplog while the server is still up. Same
                                  pattern as audit.py.

The unit is a RUN, identified by a `run_id` that ties the plan to its execution and its
outcome. Without that correlation you get three disconnected streams and cannot tell
which outcome belongs to which plan when two requests overlap.

PRIVACY. This log records what the system DID, never what the data CONTAINED:
  logged      action names, column names, table names, row counts, durations, statuses,
              error messages
  never       cell values, sample rows, or the raw instruction

The instruction is stored redacted (pii.redact_text) because a prompt can carry personal
data — "delete the row where email is a@b.com" names a real person. Column NAMES are
kept: they are the vocabulary of the plan and you cannot debug an operation without
knowing what it acted on.
"""
from __future__ import annotations

import logging
import time
import uuid

from . import pii

log = logging.getLogger("sumio.ops")

_RUNS: list[dict] = []
_MAX = 500


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _plan_shape(operations: list | None) -> list[dict]:
    """A plan reduced to its debuggable skeleton: what each step does and which columns
    it touches. Deliberately drops every value-bearing field (fill_value, find/replace,
    conditions) — those can hold user data."""
    shape: list[dict] = []
    for op in operations or []:
        if not isinstance(op, dict):
            shape.append({"action": "<malformed>"})
            continue
        entry: dict = {"action": op.get("action")}
        for key in ("table", "name", "new_column", "key_column", "source_sheet"):
            if op.get(key):
                entry[key] = op[key]
        cols = op.get("columns") or op.get("format_columns") or op.get("y_columns")
        if isinstance(cols, list) and cols:
            entry["columns"] = [str(c) for c in cols]
        shape.append(entry)
    return shape


def record_plan(
    run_id: str,
    *,
    session_id: str = "",
    instruction: str = "",
    operations: list | None = None,
    source: str = "brain",
    status: str = "plan",
    confidence: int | None = None,
) -> dict:
    """The Brain (or the fallback parser, or a saved workflow) proposed this plan.

    `source` says who produced it — 'brain', 'fallback', 'workflow', 'plugin', 'user'
    (a hand-edited plan from the UI) — which is the first thing you want when a plan
    looks wrong.
    """
    ev = {
        "run_id": run_id,
        "at": time.time(),
        "phase": "plan",
        "session_id": session_id,
        "source": source,
        "status": status,
        "confidence": confidence,
        "instruction": pii.redact_text(instruction or ""),
        "plan": _plan_shape(operations),
    }
    _append(ev)
    actions = [s.get("action") for s in ev["plan"]]
    log.info(
        "run=%s plan source=%s status=%s steps=%d actions=%s",
        run_id, source, status, len(actions), actions,
    )
    return ev


def record_outcome(
    run_id: str,
    *,
    status: str,
    rows_before: int | None = None,
    rows_after: int | None = None,
    duration_ms: int | None = None,
    cached: bool = False,
    completed_steps: int | None = None,
    failed_step: int | None = None,
    error: str = "",
) -> dict:
    """How the run actually ended. `status` is 'ok', 'partial', 'error' or 'timeout'.

    rows_before/after are the single most useful debugging pair — "it deleted my data"
    is answered by the row delta plus the plan shape recorded above.
    """
    ev = {
        "run_id": run_id,
        "at": time.time(),
        "phase": "outcome",
        "status": status,
        "rows_before": rows_before,
        "rows_after": rows_after,
        "row_delta": (
            rows_after - rows_before
            if isinstance(rows_before, int) and isinstance(rows_after, int)
            else None
        ),
        "duration_ms": duration_ms,
        "cached": cached,
        "completed_steps": completed_steps,
        "failed_step": failed_step,
        "error": error,
    }
    _append(ev)
    logfn = log.info if status in ("ok", "partial") else log.warning
    logfn(
        "run=%s outcome=%s rows=%s->%s (%s) %sms cached=%s%s",
        run_id, status, rows_before, rows_after,
        _fmt_delta(ev["row_delta"]), duration_ms, cached,
        f" error={error!r}" if error else "",
    )
    return ev


def _fmt_delta(delta: int | None) -> str:
    if delta is None:
        return "?"
    return f"{delta:+d}"


def _append(ev: dict) -> None:
    _RUNS.append(ev)
    if len(_RUNS) > _MAX:
        del _RUNS[: len(_RUNS) - _MAX]


def events(limit: int = 100, run_id: str | None = None, phase: str | None = None) -> list[dict]:
    """Recent entries, most-recent-first, optionally narrowed to one run or phase."""
    evs = [
        e for e in _RUNS
        if (run_id is None or e["run_id"] == run_id)
        and (phase is None or e["phase"] == phase)
    ]
    return list(reversed(evs))[: max(0, limit)]


def run(run_id: str) -> list[dict]:
    """One run's entries in the order they happened — plan first, then outcome. This is
    the view you actually want when debugging a single complaint."""
    return [e for e in _RUNS if e["run_id"] == run_id]


def clear() -> None:
    _RUNS.clear()
