"""ENGINE PHASE 4.9 — workflow / automation builder (BUILD).

A workflow = a saved PIPELINE of operations + a TRIGGER. This suite proves the four DoD
pieces end to end:

  build a pipeline         create_workflow stores an ordered Operation Plan (validated).
  triggers                 schedule (cadence) / new_file / anomaly (gated) — due_workflows
                           is the scheduler tick; paused workflows never fire.
  per-step status          run_workflow returns a status for EVERY step (ok/failed/skipped).
  failed step stops clean  a mid-pipeline failure marks that step 'failed' and every LATER
                           step 'skipped' — never half-applied — reusing the executor's
                           MultiStepError contract (no second execution path).

Offline: the executor is the runner (real ops); the HTTP legs drive the /workflow/* API.
No llm.py change (workflows store raw plans) → no schema/serving/quota risk; and — like the
other endpoint/mechanism phases (3.2/3.3/4.7/4.8) — no battery rows.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_4_9.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p49.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import workflow  # noqa: E402
from app.db import init_db  # noqa: E402
from app.executor import execute_multi  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def df():
    # Row 1 & 3 identical → one duplicate, so a dedupe step visibly changes the row count.
    return pd.DataFrame({"Region": ["N", "S", "N", "E"], "Price": [100, 200, 100, 50]})


print("ENGINE PHASE 4.9 — workflow / automation builder\n")

# ===================== BUILD a pipeline (validation) =====================
workflow._WORKFLOWS.clear()
wf = workflow.create_workflow("Clean & sort",
    [{"action": "remove_duplicates"}, {"action": "sort", "columns": ["Price"], "orders": ["desc"]}])
check("create_workflow stores an ordered 2-step pipeline", len(wf["steps"]) == 2 and wf["status"] == "active", str(wf)[:160])
for bad, label in (([], "empty steps"), ([{"foo": 1}], "step without action")):
    try:
        workflow.create_workflow("x", bad)
        check(f"create rejects {label}", False, "no error")
    except workflow.WorkflowError:
        check(f"create rejects {label}", True)
try:
    workflow.create_workflow("", [{"action": "sort"}])
    check("create rejects empty name", False)
except workflow.WorkflowError:
    check("create rejects empty name", True)

# ===================== PER-STEP STATUS: happy path =====================
rep, result, _ = workflow.run_workflow(wf["id"], {"t": df()}, "t", execute_multi)
check("happy run: ok=True, all steps 'ok'", rep["ok"] and all(s["status"] == "ok" for s in rep["steps"]), str(rep["steps"]))
check("happy run: completed == total", rep["completed_steps"] == rep["total_steps"] == 2, str(rep))
check("happy run: the pipeline actually transformed the data (dedupe → 3 rows)", len(result) == 3, str(len(result)))
check("run recorded in history", len(wf["runs"]) == 1 and wf["runs"][0]["ok"], str(wf["runs"]))

# ===================== FAILED STEP STOPS CLEANLY =====================
workflow._WORKFLOWS.clear()
wf2 = workflow.create_workflow("with a bad middle step", [
    {"action": "sort", "columns": ["Price"], "orders": ["desc"]},  # ok
    {"action": "sort", "columns": ["Ghost"]},                       # fails (no such column)
    {"action": "remove_duplicates"},                                # must be SKIPPED
])
rep, result, _ = workflow.run_workflow(wf2["id"], {"t": df()}, "t", execute_multi)
st = {s["index"]: s["status"] for s in rep["steps"]}
check("failed pipeline: ok=False, failed_step=2", rep["ok"] is False and rep["failed_step"] == 2, str(rep))
check("per-step status: step1 ok, step2 failed, step3 skipped", st == {1: "ok", 2: "failed", 3: "skipped"}, str(st))
check("failed step carries an error message", any(s.get("error") for s in rep["steps"] if s["index"] == 2), str(rep["steps"]))
check("the SKIPPED step never ran (dedupe skipped → still 4 rows)", len(result) == 4, str(len(result)))
check("file reflects the steps that completed (sorted by Price desc)", list(result["Price"]) == [200, 100, 100, 50], str(list(result["Price"])))

# first-step failure → nothing ran
wf3 = workflow.create_workflow("bad first step", [
    {"action": "sort", "columns": ["Ghost"]}, {"action": "remove_duplicates"}])
rep, _, _ = workflow.run_workflow(wf3["id"], {"t": df()}, "t", execute_multi)
check("first-step failure: step1 failed, step2 skipped, completed=0",
      rep["failed_step"] == 1 and rep["completed_steps"] == 0 and rep["steps"][1]["status"] == "skipped", str(rep))

# ===================== TRIGGERS =====================
workflow._WORKFLOWS.clear()
NOW = 1_000_000.0
sched = workflow.create_workflow("nightly", [{"action": "remove_duplicates"}],
    {"type": "schedule", "interval_seconds": 3600}, now=NOW)
check("schedule sets next_run one interval out", sched["next_run"] == NOW + 3600, str(sched["next_run"]))
check("schedule NOT due before its cadence elapses",
      workflow.due_workflows({"type": "schedule"}, now=NOW + 100) == [], "")
due = workflow.due_workflows({"type": "schedule"}, now=NOW + 3601)
check("schedule IS due once the cadence elapses", len(due) == 1 and due[0]["id"] == sched["id"], str(due))

newf = workflow.create_workflow("on upload", [{"action": "remove_duplicates"}], {"type": "new_file"})
check("new_file workflow fires on a new-file event",
      any(w["id"] == newf["id"] for w in workflow.due_workflows({"type": "new_file"})), "")
check("new_file workflow does NOT fire on a schedule tick",
      all(w["id"] != newf["id"] for w in workflow.due_workflows({"type": "schedule"}, now=NOW)), "")

anom = workflow.create_workflow("on anomaly", [{"action": "detect_anomalies"}],
    {"type": "anomaly", "columns": ["Price"]})
check("anomaly workflow fires only when anomalies are reported",
      workflow.due_workflows({"type": "anomaly", "anomalies_found": True}) and
      not workflow.due_workflows({"type": "anomaly", "anomalies_found": False}), "gating failed")

# paused workflows never fire
workflow.set_status(newf["id"], "paused")
check("a PAUSED workflow does not fire", all(w["id"] != newf["id"] for w in workflow.due_workflows({"type": "new_file"})), "")

# scheduled run advances next_run
workflow._WORKFLOWS.clear()
s2 = workflow.create_workflow("nightly2", [{"action": "remove_duplicates"}],
    {"type": "schedule", "interval_seconds": 3600}, now=NOW)
workflow.run_workflow(s2["id"], {"t": df()}, "t", execute_multi, now=NOW + 5000)
check("running a scheduled workflow advances next_run", s2["next_run"] == NOW + 5000 + 3600, str(s2["next_run"]))

# ===================== HTTP end-to-end =====================
workflow._WORKFLOWS.clear()
CSV = b"Region,Price\nN,100\nS,200\nN,100\nE,50\n"
c.post("/inspect", data={"session_id": "wf"}, files=[("files", ("d.csv", CSV, "text/csv"))])

r = c.post("/workflow/create", data={
    "name": "Clean+sort",
    "steps": json.dumps([{"action": "remove_duplicates"}, {"action": "sort", "columns": ["Price"], "orders": ["desc"]}]),
    "trigger": json.dumps({"type": "new_file"}),
}).json()
check("API create returns a workflow id", r.get("status") == "ok" and r["workflow"]["id"], str(r)[:160])
wid = r["workflow"]["id"]

run = c.post(f"/workflow/{wid}/run", data={"session_id": "wf"}).json()
check("API run: ok with full per-step status", run.get("ok") is True and len(run.get("steps", [])) == 2, str(run)[:200])
check("API run: returns the resulting file (dedupe → 3 rows)", run.get("row_count") == 3 and run.get("download_id"), str(run)[:200])

# a workflow whose middle step fails → partial, per-step status over HTTP
r2 = c.post("/workflow/create", data={"name": "bad", "steps": json.dumps([
    {"action": "sort", "columns": ["Price"], "orders": ["desc"]},
    {"action": "sort", "columns": ["Ghost"]},
    {"action": "remove_duplicates"}])}).json()
run2 = c.post(f"/workflow/{r2['workflow']['id']}/run", data={"session_id": "wf"}).json()
check("API run partial: ok=False, failed_step=2, later step skipped",
      run2.get("ok") is False and run2.get("failed_step") == 2 and run2["steps"][2]["status"] == "skipped", str(run2)[:220])

# run-due over HTTP: a new_file event surfaces the active new_file workflow
due = c.post("/workflow/run-due", data={"event": json.dumps({"type": "new_file"})}).json()
check("API run-due surfaces the due new_file workflow", any(w["id"] == wid for w in due.get("due", [])), str(due)[:200])

# pause it → it drops out of run-due
c.post(f"/workflow/{wid}/pause")
due2 = c.post("/workflow/run-due", data={"event": json.dumps({"type": "new_file"})}).json()
check("API paused workflow drops out of run-due", all(w["id"] != wid for w in due2.get("due", [])), str(due2)[:200])

lst = c.get("/workflow/list").json()
check("API list returns the workflows", lst.get("status") == "ok" and len(lst.get("workflows", [])) >= 1, str(lst)[:160])
check("API delete removes a workflow", c.post(f"/workflow/{wid}/delete").json().get("removed") is True, "")

workflow._WORKFLOWS.clear()
m._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
