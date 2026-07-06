"""Phase 3.4 — Agentic multi-step planning tests.

Verifies:
  P34-a  Multi-step plan is sensible and ordered (filter before aggregate).
  P34-b  Each step has a non-empty label (independently verifiable).
  P34-c  /parse returns step list + plan_rationale BEFORE /execute is called.
  P34-d  Partial failure: plan runs through step N-1, fails on step N → returns
          partial=True, warning names the failing step, earlier work is preserved.

Run from backend:  .venv\\Scripts\\python.exe test_planner.py
"""
from __future__ import annotations

import json

import pandas as pd
from fastapi.testclient import TestClient

from app import main
from app.executor import MultiStepError, execute_multi

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("PHASE 3.4 — AGENTIC MULTI-STEP PLANNING\n")

client = TestClient(main.app)
_orig_parse = main.llm.parse_instruction

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upload_csv(session_id: str, csv: bytes, filename: str = "data.csv") -> None:
    client.post(
        "/inspect",
        data={"session_id": session_id},
        files=[("files", (filename, csv, "text/csv"))],
    )

CSV = b"Region,Revenue,Category\nNorth,500,A\nSouth,300,B\nNorth,700,A\nEast,100,C\n"

# ---------------------------------------------------------------------------
# P34-a  Plan is sensible and ordered (filter → aggregate)
# ---------------------------------------------------------------------------
print("P34-a  Plan is sensible and ordered")

ORDERED_PLAN = {
    "operations": [
        {"action": "filter", "conditions": [{"column": "Region", "operator": "equals", "value": "North"}], "combine": "and"},
        {"action": "aggregate", "agg_func": "sum", "agg_column": "Revenue"},
    ],
    "title": "Sum North Revenue",
    "translation": "Filter to North region, then sum Revenue",
    "confidence": 95,
    "steps": [
        {"label": "Filter rows where Region equals North", "rationale": "Narrows to the target region before aggregating."},
        {"label": "Sum the Revenue column", "rationale": "Computes the total after filtering."},
    ],
    "plan_rationale": "Filter first to reduce the dataset, then aggregate for a focused result.",
}

try:
    main.llm.parse_instruction = lambda i, s, h: ORDERED_PLAN
    _upload_csv("p34a", CSV)
    r = client.post("/parse", data={"instruction": "sum revenue for North", "session_id": "p34a", "history": ""})
    body = r.json()

    check("P34-a /parse returns plan status", body.get("status") == "plan", str(body)[:200])
    plan = body.get("plan", {})
    check("P34-a plan has 2 operations", len(plan.get("operations", [])) == 2, str(plan))
    steps = plan.get("steps") or []
    check("P34-a steps list returned", len(steps) == 2, str(steps))
    check("P34-a step 1 is filter", "filter" in steps[0].get("label", "").lower() or "region" in steps[0].get("label", "").lower(), str(steps[0]))
    check("P34-a step 2 is aggregate", any(w in steps[1].get("label", "").lower() for w in ("sum", "revenue", "aggregat")), str(steps[1]))
    check("P34-a plan_rationale present", bool(plan.get("plan_rationale")), str(plan.get("plan_rationale")))
finally:
    main.llm.parse_instruction = _orig_parse
    main._SESSIONS.clear()

# ---------------------------------------------------------------------------
# P34-b  Each sub-step is independently verifiable (has a non-empty label)
# ---------------------------------------------------------------------------
print("\nP34-b  Each step has a non-empty label")

LONG_PLAN = {
    "operations": [
        {"action": "trim"},
        {"action": "remove_duplicates", "columns": ["Region"]},
        {"action": "sort", "columns": ["Revenue"], "orders": ["desc"]},
        {"action": "limit", "count": 3},
    ],
    "title": "Clean sort top",
    "translation": "Trim, dedupe, sort descending, keep top 3",
    "confidence": 90,
    "steps": [
        {"label": "Trim whitespace from all text columns", "rationale": "Removes hidden spaces before deduplication."},
        {"label": "Remove duplicate rows on Region"},
        {"label": "Sort by Revenue, highest first", "rationale": "Orders results before limiting."},
        {"label": "Keep the top 3 rows", "rationale": "Returns only the highest-revenue regions."},
    ],
    "plan_rationale": "Clean first, then dedupe and sort, then limit to a manageable top list.",
}

try:
    main.llm.parse_instruction = lambda i, s, h: LONG_PLAN
    _upload_csv("p34b", CSV)
    r = client.post("/parse", data={"instruction": "clean sort top 3", "session_id": "p34b", "history": ""})
    body = r.json()
    plan = body.get("plan", {})
    steps = plan.get("steps") or []

    check("P34-b all 4 steps returned", len(steps) == 4, str(len(steps)))
    all_have_labels = all(bool(s.get("label", "").strip()) for s in steps)
    check("P34-b every step has a non-empty label", all_have_labels, str(steps))
    ops_count = len(plan.get("operations", []))
    check("P34-b steps count matches operations count", len(steps) == ops_count, f"steps={len(steps)} ops={ops_count}")
finally:
    main.llm.parse_instruction = _orig_parse
    main._SESSIONS.clear()

# ---------------------------------------------------------------------------
# P34-c  User can review the plan BEFORE it runs
#        /parse returns the plan with steps; /execute is a separate call.
# ---------------------------------------------------------------------------
print("\nP34-c  /parse returns plan before /execute")

TWO_STEP_PLAN = {
    "operations": [
        {"action": "remove_duplicates", "columns": ["Region"]},
        {"action": "sort", "columns": ["Revenue"], "orders": ["desc"]},
    ],
    "title": "Dedupe sort",
    "translation": "Remove duplicate regions, then sort by Revenue descending",
    "confidence": 92,
    "steps": [
        {"label": "Remove duplicate rows on Region", "rationale": "Keeps one row per region."},
        {"label": "Sort by Revenue, highest first", "rationale": "Orders the unique regions by revenue."},
    ],
    "plan_rationale": "Deduplicate first, then sort so the result is clean and ordered.",
}

try:
    main.llm.parse_instruction = lambda i, s, h: TWO_STEP_PLAN
    _upload_csv("p34c", CSV)

    # Phase 1: parse only — no execution yet
    r_parse = client.post("/parse", data={"instruction": "dedupe then sort", "session_id": "p34c", "history": ""})
    parse_body = r_parse.json()

    check("P34-c /parse status is plan", parse_body.get("status") == "plan", str(parse_body)[:200])
    check("P34-c /parse has translation", bool(parse_body.get("translation")), str(parse_body.get("translation")))
    check("P34-c /parse has confidence", isinstance(parse_body.get("confidence"), int), str(parse_body.get("confidence")))
    plan_from_parse = parse_body.get("plan", {})
    check("P34-c plan returned before execution", len(plan_from_parse.get("operations", [])) == 2, str(plan_from_parse))
    check("P34-c steps in plan-review response", len(plan_from_parse.get("steps") or []) == 2, str(plan_from_parse.get("steps")))

    # Phase 2: user reviews and approves → /execute
    r_exec = client.post(
        "/execute",
        data={
            "session_id": "p34c",
            "plan": json.dumps(plan_from_parse),
            "rewind": "-1",
        },
    )
    exec_body = r_exec.json()

    check("P34-c /execute succeeds after review", exec_body.get("status") == "ok", str(exec_body)[:200])
    check("P34-c not partial (all steps succeeded)", not exec_body.get("partial"), str(exec_body.get("partial")))
    check("P34-c row count reduced by dedupe", exec_body.get("row_count", 0) <= 3, str(exec_body.get("row_count")))
finally:
    main.llm.parse_instruction = _orig_parse
    main._SESSIONS.clear()

# ---------------------------------------------------------------------------
# P34-d  Partial failure: step N fails → steps 1..N-1 preserved, warning clear
# ---------------------------------------------------------------------------
print("\nP34-d  Partial failure handling")

# Direct executor test
DF = pd.DataFrame({"Region": ["North", "North", "South"], "Revenue": [500, 700, 300]})

try:
    execute_multi(
        {"t": DF}, "t",
        [
            {"action": "remove_duplicates", "columns": ["Region"]},
            {"action": "sort", "columns": ["NoSuchColumn"], "orders": ["asc"]},
        ],
    )
    check("P34-d raises MultiStepError on step-2 fail", False, "no error raised")
except MultiStepError as e:
    check("P34-d raises MultiStepError", True)
    check("P34-d failed_step is 2", e.failed_step == 2, f"step={e.failed_step}")
    check("P34-d partial_result has step-1 data (dedupe)", len(e.partial_result) == 2, f"rows={len(e.partial_result)}")
    check("P34-d reason mentions bad column", "NoSuchColumn" in e.reason, e.reason)
    check("P34-d notes only from completed step", len(e.notes) == 1, str(e.notes))

# API-level partial failure: should return ok + partial=True + warning
try:
    main.llm.parse_instruction = lambda i, s, h: {
        "operations": [
            {"action": "remove_duplicates", "columns": ["Region"]},
            {"action": "sort", "columns": ["NoSuchColumn"], "orders": ["asc"]},
        ],
        "steps": [
            {"label": "Remove duplicate regions"},
            {"label": "Sort by NoSuchColumn"},
        ],
    }
    r = client.post(
        "/process",
        data={"instruction": "dedupe then sort bad col", "session_id": "p34d", "rewind": "-1", "history": ""},
        files=[("files", ("d.csv", b"Region,Revenue\nNorth,500\nNorth,700\nSouth,300\n", "text/csv"))],
    )
    body = r.json()
    check("P34-d API status ok (partial result returned)", r.status_code == 200 and body.get("status") == "ok", str(body)[:200])
    check("P34-d API partial=True", body.get("partial") is True, str(body.get("partial")))
    check("P34-d API warning present", bool(body.get("warning")), str(body.get("warning")))
    check("P34-d API warning mentions Step 2", "Step 2" in (body.get("warning") or ""), body.get("warning"))
    check("P34-d API notes show completed step", len(body.get("notes", [])) >= 1, str(body.get("notes")))
    check("P34-d API file returned for partial result", bool(body.get("file_base64") or body.get("download_id")), "no file")
    # Per-step outcome fields drive the UI checklist (✓ step 1, ✗ step 2).
    check("P34-d API completed_steps is 1", body.get("completed_steps") == 1, str(body.get("completed_steps")))
    check("P34-d API failed_step is 2", body.get("failed_step") == 2, str(body.get("failed_step")))
finally:
    main.llm.parse_instruction = _orig_parse
    main._SESSIONS.clear()

# ---------------------------------------------------------------------------
# P34-e  Steps are auto-synthesized when the Brain omits them (offline fallback,
#        or an LLM that returned no / mismatched steps) — so every multi-step plan
#        is still reviewable and each step maps 1:1 to an operation.
# ---------------------------------------------------------------------------
print("\nP34-e  Auto-synthesized steps")

# (1) LLM returns operations but NO steps
try:
    main.llm.parse_instruction = lambda i, s, h: {
        "operations": [
            {"action": "remove_duplicates", "columns": ["Region"]},
            {"action": "sort", "columns": ["Revenue"], "orders": ["desc"]},
        ],
        "title": "Dedupe sort",
        # no "steps", no "translation"
    }
    _upload_csv("p34e1", CSV)
    r = client.post("/parse", data={"instruction": "dedupe then sort", "session_id": "p34e1", "history": ""})
    body = r.json()
    plan = body.get("plan", {})
    steps = plan.get("steps") or []
    check("P34-e steps synthesized when LLM omits them", len(steps) == 2, str(steps))
    check("P34-e every synthesized step has a label", all((s.get("label") or "").strip() for s in steps), str(steps))
    check("P34-e translation synthesized too", bool(body.get("translation")), str(body.get("translation")))
finally:
    main.llm.parse_instruction = _orig_parse
    main._SESSIONS.clear()

# (2) LLM returns steps whose length does NOT match operations → re-synthesized 1:1
try:
    main.llm.parse_instruction = lambda i, s, h: {
        "operations": [
            {"action": "trim"},
            {"action": "remove_duplicates", "columns": ["Region"]},
            {"action": "sort", "columns": ["Revenue"], "orders": ["desc"]},
        ],
        "steps": [{"label": "only one step"}],  # mismatched length
    }
    _upload_csv("p34e2", CSV)
    r = client.post("/parse", data={"instruction": "clean dedupe sort", "session_id": "p34e2", "history": ""})
    plan = r.json().get("plan", {})
    steps = plan.get("steps") or []
    check("P34-e mismatched steps re-synthesized to match op count", len(steps) == 3, str(steps))
finally:
    main.llm.parse_instruction = _orig_parse
    main._SESSIONS.clear()

# (3) Single-operation plan: no step list needed (UI shows the simple translation)
try:
    main.llm.parse_instruction = lambda i, s, h: {
        "operations": [{"action": "remove_duplicates", "columns": ["Region"]}],
        "title": "Dedupe",
    }
    _upload_csv("p34e3", CSV)
    r = client.post("/parse", data={"instruction": "remove duplicates", "session_id": "p34e3", "history": ""})
    plan = r.json().get("plan", {})
    check("P34-e single-op plan has no synthesized step list", not plan.get("steps"), str(plan.get("steps")))
finally:
    main.llm.parse_instruction = _orig_parse
    main._SESSIONS.clear()

# (4) Successful multi-step run reports completed_steps == op count, failed_step None
try:
    main.llm.parse_instruction = lambda i, s, h: TWO_STEP_PLAN
    _upload_csv("p34e4", CSV)
    r_parse = client.post("/parse", data={"instruction": "dedupe then sort", "session_id": "p34e4", "history": ""})
    plan = r_parse.json().get("plan", {})
    r_exec = client.post("/execute", data={"session_id": "p34e4", "plan": json.dumps(plan), "rewind": "-1"})
    eb = r_exec.json()
    check("P34-e success: completed_steps == 2", eb.get("completed_steps") == 2, str(eb.get("completed_steps")))
    check("P34-e success: failed_step is None", eb.get("failed_step") is None, str(eb.get("failed_step")))
finally:
    main.llm.parse_instruction = _orig_parse
    main._SESSIONS.clear()

# ---------------------------------------------------------------------------
print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
