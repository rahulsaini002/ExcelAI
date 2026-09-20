"""Workflow / automation builder (Phase 4.9).

A workflow is a saved PIPELINE — an ordered list of operations (the exact same Operation
Plan the Brain emits) — plus a TRIGGER that says when it should run. Running one produces a
PER-STEP STATUS report, and a failed step STOPS THE PIPELINE CLEANLY (later steps are marked
skipped, never half-applied) — reusing the executor's MultiStepError contract from the
agentic-planning work (3.4/4.4) rather than inventing a second execution path.

Design mirrors sync.py (Phase 3.3): a small in-memory store of pure, timestamp-injectable
functions, with the actual op execution passed in as a `runner` callable so this module
stays decoupled from the executor and is trivially testable.

Trigger types:
  • manual    — only runs when explicitly invoked.
  • schedule  — due every `interval_seconds` (a scheduler/cron calls due_workflows on a tick).
  • new_file  — runs when fresh data arrives.
  • anomaly   — runs only when an upstream check reports genuinely unusual data (gated so it
                doesn't fire on every tick — keeping with the "low false alarms" ethos).

No new Operation-schema field is introduced (workflows store raw plans), so there is no
llm.py schema/serving risk.
"""
from __future__ import annotations

import time
import uuid

from . import store
from .executor import MultiStepError, OperationError

# PERSISTED. A workflow is something the user deliberately SAVED — a named pipeline with a
# trigger, often a schedule. In memory only, it vanished every time the host restarted
# (which the free tier does whenever it sleeps), and worse, it did so SILENTLY: someone who
# set up a nightly run would simply never see it run again, with nothing to indicate why.
# Same class of bug as sessions not surviving to the next day. Stores plain dicts of plan
# steps and trigger metadata, so it is cheap to snapshot.
_WORKFLOWS: dict[str, dict] = store.register("workflows", store.load_dict("workflows"))

TRIGGER_TYPES = ("manual", "schedule", "new_file", "anomaly")
_DEFAULT_INTERVAL = 24 * 3600
_MAX_HISTORY = 20


class WorkflowError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> float:
    return time.time()


def _normalize_trigger(trigger: dict | None) -> dict:
    trigger = trigger or {}
    t = str(trigger.get("type") or "manual").lower()
    if t not in TRIGGER_TYPES:
        t = "manual"
    out: dict = {"type": t}
    if t == "schedule":
        try:
            out["interval_seconds"] = max(1, int(trigger.get("interval_seconds") or _DEFAULT_INTERVAL))
        except (TypeError, ValueError):
            out["interval_seconds"] = _DEFAULT_INTERVAL
    if t == "anomaly":
        out["columns"] = [str(c) for c in (trigger.get("columns") or [])]
    return out


def create_workflow(name: str, steps: list, trigger: dict | None = None, now: float | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise WorkflowError("A workflow needs a name.")
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("A workflow needs at least one step.")
    if any(not isinstance(s, dict) or not s.get("action") for s in steps):
        raise WorkflowError("Every workflow step must be an operation with an 'action'.")
    now = now if now is not None else _now()
    trig = _normalize_trigger(trigger)
    wid = uuid.uuid4().hex[:12]
    wf = {
        "id": wid,
        "name": name,
        "steps": steps,
        "trigger": trig,
        "status": "active",
        "created_at": now,
        "last_run": None,
        "next_run": (now + trig["interval_seconds"]) if trig["type"] == "schedule" else None,
        "runs": [],
    }
    _WORKFLOWS[wid] = wf
    return wf


def get_workflow(workflow_id: str) -> dict:
    wf = _WORKFLOWS.get(workflow_id)
    if not wf:
        raise WorkflowError("No such workflow.", status=404)
    return wf


def list_workflows() -> list[dict]:
    return [_public(wf) for wf in _WORKFLOWS.values()]


def delete_workflow(workflow_id: str) -> bool:
    return _WORKFLOWS.pop(workflow_id, None) is not None


def set_status(workflow_id: str, status: str) -> dict:
    wf = get_workflow(workflow_id)
    if status not in ("active", "paused"):
        raise WorkflowError("Status must be 'active' or 'paused'.")
    wf["status"] = status
    return _public(wf)


def _public(wf: dict) -> dict:
    """A view safe to return over HTTP (no huge op payloads unless asked)."""
    return {
        "id": wf["id"], "name": wf["name"], "trigger": wf["trigger"],
        "status": wf["status"], "step_count": len(wf["steps"]),
        "created_at": wf["created_at"], "last_run": wf["last_run"],
        "next_run": wf["next_run"], "runs": wf["runs"][-5:],
    }


def _step_statuses(steps: list, failed_step: int | None, error: str | None) -> list[dict]:
    """Per-step status: everything before the failure is 'ok', the failing step is 'failed',
    everything after is 'skipped' (the pipeline stopped there — no half-applied later steps)."""
    out: list[dict] = []
    for i, op in enumerate(steps, 1):
        if failed_step is None or i < failed_step:
            status = "ok"
        elif i == failed_step:
            status = "failed"
        else:
            status = "skipped"
        entry = {"index": i, "action": op.get("action"), "status": status}
        if i == failed_step and error:
            entry["error"] = error
        out.append(entry)
    return out


def run_workflow(workflow_id: str, tables: dict, primary: str, runner, now: float | None = None):
    """Run a workflow's pipeline on `tables` using `runner` (execute_multi-compatible:
    (tables, primary, operations) -> (result, name, notes, render), raising MultiStepError
    on a later-step failure or OperationError on a first-step failure).

    Returns (report, result, result_name). `report` has per-step status, and a failed step
    stops the pipeline cleanly (later steps 'skipped'). Records the run in history and, for
    a scheduled workflow, advances next_run."""
    wf = get_workflow(workflow_id)
    now = now if now is not None else _now()
    steps = wf["steps"]

    result = None
    result_name = primary
    failed_step: int | None = None
    error: str | None = None

    try:
        result, result_name, _notes, _render = runner(tables, primary, steps)
    except MultiStepError as exc:
        result, result_name = exc.partial_result, exc.partial_name
        failed_step, error = exc.failed_step, exc.reason
    except OperationError as exc:
        # The very first step failed — nothing ran.
        failed_step, error = 1, str(exc)

    statuses = _step_statuses(steps, failed_step, error)
    completed = len(steps) if failed_step is None else failed_step - 1
    report = {
        "workflow_id": wf["id"], "name": wf["name"],
        "ok": failed_step is None,
        "steps": statuses,
        "completed_steps": completed,
        "total_steps": len(steps),
        "failed_step": failed_step,
        "error": error,
        "ran_at": now,
    }

    wf["last_run"] = now
    wf["runs"].append({k: report[k] for k in ("ok", "completed_steps", "total_steps", "failed_step", "error", "ran_at")})
    wf["runs"] = wf["runs"][-_MAX_HISTORY:]
    if wf["trigger"]["type"] == "schedule":
        wf["next_run"] = now + wf["trigger"]["interval_seconds"]

    return report, result, result_name


def due_workflows(event: dict | None, now: float | None = None) -> list[dict]:
    """Which ACTIVE workflows should fire for a trigger `event`. The tick a scheduler calls.

    event = {"type": "schedule" | "new_file" | "anomaly", "anomalies_found"?: bool}. A
    schedule fires only once its cadence has elapsed; an anomaly workflow fires only when the
    event actually reports anomalies (so it doesn't cry wolf every tick)."""
    now = now if now is not None else _now()
    etype = str((event or {}).get("type") or "").lower()
    fired: list[dict] = []
    for wf in _WORKFLOWS.values():
        if wf["status"] != "active":
            continue
        trig = wf["trigger"]
        if trig["type"] != etype:
            continue
        if etype == "schedule":
            if wf.get("next_run") is not None and now >= wf["next_run"]:
                fired.append(wf)
        elif etype == "new_file":
            fired.append(wf)
        elif etype == "anomaly":
            if (event or {}).get("anomalies_found"):
                fired.append(wf)
    return fired
