"""ENGINE PHASE 4.4 — agentic multi-step planning (verify & HARDEN).

Most of the machinery pre-existed and is verified here against the DoD:

  "sensible ORDERED plan"        execute_multi runs steps in order; a step may depend on an
                                 earlier step's output (add a column, then sort by it).
  "user REVIEW before run"       the two-phase flow: /parse returns status 'plan' (ordered
                                 steps + translation + confidence) and NEVER touches the
                                 file; /execute runs the approved plan with NO model call.
  "PARTIAL-FAILURE stops cleanly" a later step's failure stops the plan there, keeps the
                                 file at the last good step, and reports which step failed —
                                 later steps never run. A first-step failure is a plain error
                                 (nothing partial to keep).
  "ambiguous sub-step PAUSES"    an ambiguous instruction yields a clarify at plan time —
                                 the plan pauses for the user before anything runs.

HARDENED here (Phase 4.4): the /parse review now carries a `review` block — destructive
steps flagged with concrete, per-step impact ("removes 1 duplicate of 4") computed against
the real data — so the user weighs the whole ordered plan-of-plans BEFORE approving, not
only after clicking run. Each reviewable step is annotated with its impact/severity.

Offline: /parse legs monkeypatch llm.parse_instruction with canned plans; execution legs
call execute_multi / the pre-approved /execute path directly. No llm.py schema change.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_4_4.py
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p44.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app.db import init_db  # noqa: E402
from app.executor import MultiStepError, OperationError, execute_multi  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0

# Row 1 and row 3 are identical → exactly one duplicate, so guardrails impacts are concrete.
CSV = b"Region,Price,Qty\nNorth,100,1\nSouth,200,2\nNorth,100,1\nEast,50,4\n"
MIME = "text/csv"


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def df():
    return pd.DataFrame({"Region": ["North", "South", "North", "East"],
                         "Price": [100, 200, 100, 50], "Qty": [1, 2, 3, 4]})


print("ENGINE PHASE 4.4 — agentic multi-step planning (verify & harden)\n")

# ================= 1. ORDERED execution: a step depends on an earlier one =================
res, name, notes, render = execute_multi(
    {"t": df()}, "t",
    [{"action": "add_formula_column", "name": "Total", "formula": "{Price} * {Qty}"},
     {"action": "sort", "columns": ["Total"], "orders": ["desc"]}])
check("ordered plan: add-then-sort produces a column sorted by the new column",
      list(res["Total"]) == sorted(res["Total"], reverse=True) and "Total" in res.columns, str(list(res["Total"])))

# ================= 2. PARTIAL-FAILURE stops cleanly (later step never runs) =================
# Step 1 sorts (ok); step 2 sorts a phantom column (fails); step 3 dedupe must NOT run.
try:
    execute_multi({"t": df()}, "t",
                  [{"action": "sort", "columns": ["Price"], "orders": ["desc"]},
                   {"action": "sort", "columns": ["Ghost"]},
                   {"action": "remove_duplicates"}])
    check("multi-step failure raises MultiStepError", False, "no exception raised")
except MultiStepError as exc:
    check("multi-step failure raises MultiStepError", True)
    check("failed_step is the 2nd step (1-based)", exc.failed_step == 2, str(exc.failed_step))
    partial = exc.partial_result
    check("partial keeps step 1's work (sorted by Price desc)", list(partial["Price"]) == [200, 100, 100, 50], str(list(partial["Price"])))
    check("the LATER step never ran (no dedupe → still 4 rows)", len(partial) == 4, str(len(partial)))
except Exception as exc:
    check("multi-step failure raises MultiStepError", False, f"got {type(exc).__name__}: {exc}")

# ================= 3. FIRST-step failure is a plain error (nothing partial) =================
first_fail_kind = None
try:
    execute_multi({"t": df()}, "t",
                  [{"action": "sort", "columns": ["Ghost"]}, {"action": "remove_duplicates"}])
except MultiStepError:
    first_fail_kind = "multi"
except OperationError:
    first_fail_kind = "operation"
check("first-step failure raises a plain OperationError (not MultiStepError)", first_fail_kind == "operation", str(first_fail_kind))

# ================= 4. /parse = REVIEW before run (offline, canned plans) =================
_real = m.llm.parse_instruction


def _plan(*ops):
    def fake(instruction, structure, context=""):
        return {"operations": list(ops), "title": "Test plan"}
    return fake


def _seed(sid):
    m._SESSIONS.pop(sid, None)
    c.post("/inspect", data={"session_id": sid}, files=[("files", ("s.csv", CSV, MIME))])


# 4a. a 2-step plan previews as an ORDERED, reviewable step list; file untouched.
m.llm.parse_instruction = _plan(
    {"action": "remove_duplicates"},
    {"action": "sort", "columns": ["Price"], "orders": ["desc"]})
_seed("rev")
r = c.post("/parse", data={"instruction": "dedupe then sort by price", "session_id": "rev"}).json()
check("/parse returns a plan (not a result)", r.get("status") == "plan", str(r)[:160])
steps = (r.get("plan") or {}).get("steps") or []
check("plan has an ordered 2-step review list", len(steps) == 2, str(steps))
check("plan carries a translation + confidence for review",
      bool(r.get("translation")) and isinstance(r.get("confidence"), int), str((r.get("translation"), r.get("confidence"))))
check("review before run: /parse did NOT touch the file (no new state pushed)",
      len(m._SESSIONS["rev"]["states"]) == 1, str(len(m._SESSIONS["rev"]["states"])))

# 4b. HARDENING: the review block flags destructive steps with concrete per-step impact.
m.llm.parse_instruction = _plan(
    {"action": "remove_duplicates"},
    {"action": "drop_columns", "columns": ["Qty"]})
_seed("rev2")
r = c.post("/parse", data={"instruction": "dedupe and drop Qty", "session_id": "rev2"}).json()
review = r.get("review") or {}
check("review flags the plan as destructive", review.get("destructive") is True, str(review)[:200])
warns = review.get("warnings") or []
check("review has a per-step warning for each destructive step (2)", len(warns) == 2, str(warns))
check("review impact is concrete (names the duplicate count)",
      any("duplicate" in (w.get("impact") or "").lower() for w in warns), str(warns))
# steps annotated inline with impact/severity
steps = (r.get("plan") or {}).get("steps") or []
check("reviewable step 1 (dedupe) is annotated with its impact + severity",
      len(steps) == 2 and steps[0].get("impact") and steps[0].get("severity") == "high", str(steps[0]))

# 4c. a NON-destructive plan reviews clean (no false alarms).
m.llm.parse_instruction = _plan(
    {"action": "sort", "columns": ["Price"], "orders": ["desc"]},
    {"action": "add_formula_column", "name": "Total", "formula": "{Price} * {Qty}"})
_seed("rev3")
r = c.post("/parse", data={"instruction": "sort then add total", "session_id": "rev3"}).json()
check("a non-destructive plan is NOT flagged destructive (no false alarm)",
      (r.get("review") or {}).get("destructive") is False, str(r.get("review"))[:160])

# 4d. ambiguous instruction PAUSES at plan time (clarify), file untouched.
def _clarify(instruction, structure, context=""):
    return {"clarification": "Which column should I sort by — Price or Qty?"}


m.llm.parse_instruction = _clarify
_seed("amb")
r = c.post("/parse", data={"instruction": "sort it", "session_id": "amb"}).json()
check("ambiguous plan pauses with a clarify (no plan, no run)", r.get("status") == "clarify" and r.get("clarification"), str(r)[:160])
check("ambiguous pause did NOT touch the file", len(m._SESSIONS["amb"]["states"]) == 1, "")

# ================= 5. /execute runs the APPROVED ordered plan (no model call) =================
m.llm.parse_instruction = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Brain call in /execute"))
_seed("exec")
plan = {"operations": [{"action": "remove_duplicates"},
                       {"action": "sort", "columns": ["Price"], "orders": ["desc"]}]}
r = c.post("/execute", data={"session_id": "exec", "plan": json.dumps(plan)}).json()
check("/execute runs the approved plan (status ok)", r.get("status") == "ok", str(r)[:160])
check("/execute applied BOTH ordered steps (dedupe → 3 rows)", r.get("row_count") == 3, str(r.get("row_count")))
check("/execute needs no model (both actions present)", set(r.get("actions") or []) == {"remove_duplicates", "sort"}, str(r.get("actions")))

# 5b. partial failure end-to-end over /execute: file reflects step 1, later step skipped.
_seed("execpf")
plan = {"operations": [{"action": "sort", "columns": ["Price"], "orders": ["desc"]},
                       {"action": "sort", "columns": ["Ghost"]},
                       {"action": "remove_duplicates"}]}
r = c.post("/execute", data={"session_id": "execpf", "plan": json.dumps(plan)}).json()
check("/execute partial failure still returns a usable result (ok + partial flag)",
      r.get("status") == "ok" and r.get("partial") is True, str(r)[:160])
check("/execute reports which step failed (step 2, 1 completed)",
      r.get("failed_step") == 2 and r.get("completed_steps") == 1, str((r.get("failed_step"), r.get("completed_steps"))))
check("/execute partial: later dedupe step never ran (still 4 rows)", r.get("row_count") == 4, str(r.get("row_count")))

# 5c. guard=true makes a destructive plan REQUIRE confirmation before running (review gate).
_seed("guard")
plan = {"operations": [{"action": "remove_duplicates"}]}
r = c.post("/execute", data={"session_id": "guard", "plan": json.dumps(plan), "guard": "true"}).json()
check("guarded destructive plan asks for confirmation before running", r.get("status") == "confirm_required", str(r)[:160])
r2 = c.post("/execute", data={"session_id": "guard", "plan": json.dumps(plan), "guard": "true", "confirm": "true"}).json()
check("confirming then runs it", r2.get("status") == "ok" and r2.get("row_count") == 3, str(r2)[:160])

m.llm.parse_instruction = _real  # restore

# ================= 6. battery coverage: agentic multi-step rows in 4 languages =================
recs = list(csv.DictReader(open(TESTS / "prompt_battery.csv", encoding="utf-8-sig", newline="")))
ag = [r for r in recs if r["capability"] == "agentic"]
by_lang = defaultdict(int)
for r in ag:
    by_lang[r["language"]] += 1
check("battery has agentic multi-step rows in all four languages",
      all(by_lang.get(l, 0) > 0 for l in ("EN", "HI", "UR", "Hinglish")), dict(by_lang))
check("agentic battery rows expect a MULTI-op plan (two action tokens)",
      all(";" in (r.get("expected_plan") or "") for r in ag) and len(ag) >= 4, str([(r["language"], r["expected_plan"]) for r in ag]))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
