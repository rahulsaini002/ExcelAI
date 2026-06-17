"""Phase 1 cross-cutting — multi-step instructions (MS-a..c). The Brain returns an
ordered list of operations; the Hands run them in sequence, report each step, and on a
LATER-step failure keep the file from the steps that completed (PRD MS-b). MS-c
(pause-to-clarify mid-plan) is Brain-driven; its routing is tested via the API with the
model stubbed.

Run from backend:  .venv\\Scripts\\python.exe test_multistep.py
"""
from __future__ import annotations

import pandas as pd
from fastapi.testclient import TestClient

from app import main
from app.executor import MultiStepError, OperationError, execute_multi

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("MULTI-STEP INSTRUCTIONS (MS-a..c)\n")

DF = pd.DataFrame({
    "Email": ["a@x", "a@x", "b@x", "c@x"],
    "Date": ["2026-03-01", "2026-03-01", "2026-01-09", "2026-02-02"],
    "Amount": [10, 10, 30, 20],
})

# --------------------------------------------------------------------------- #
# MS-a  Two valid steps -> both run in order, combined summary
# --------------------------------------------------------------------------- #
out, name, notes, render = execute_multi(
    {"t": DF}, "t",
    [{"action": "remove_duplicates", "columns": ["Email"]},
     {"action": "sort", "columns": ["Date"], "orders": ["asc"]}],
)
check("MS-a both steps ran", len(out) == 3, f"rows={len(out)}")
check("MS-a ran in order (dedupe then sort by Date)", list(out["Date"]) == ["2026-01-09", "2026-02-02", "2026-03-01"], str(list(out["Date"])))
check("MS-a combined summary (one note per step)", len(notes) == 2, str(notes))
check("MS-a summary mentions both actions", any("dupl" in n.lower() for n in notes) and any("sorted" in n.lower() for n in notes), str(notes))

# --------------------------------------------------------------------------- #
# MS-b  Second step invalid -> first applied; clear "step 2 failed and why"
# --------------------------------------------------------------------------- #
try:
    execute_multi(
        {"t": DF}, "t",
        [{"action": "remove_duplicates", "columns": ["Email"]},
         {"action": "sort", "columns": ["Ghost"], "orders": ["asc"]}],
    )
    check("MS-b raises MultiStepError", False, "no error")
except MultiStepError as e:
    check("MS-b raises MultiStepError", True)
    check("MS-b reports the failing step number", e.failed_step == 2, f"step={e.failed_step}")
    check("MS-b explains why (names the bad column)", "Ghost" in e.reason, e.reason)
    check("MS-b keeps step 1's result (dedupe applied)", len(e.partial_result) == 3, f"rows={len(e.partial_result)}")
    check("MS-b notes cover only the completed step", len(e.notes) == 1, str(e.notes))

# a bad FIRST step has nothing to keep -> plain OperationError (not MultiStepError)
try:
    execute_multi({"t": DF}, "t", [{"action": "sort", "columns": ["Ghost"], "orders": ["asc"]},
                                   {"action": "remove_duplicates"}])
    check("MS-b first-step failure is plain error", False, "no error")
except MultiStepError:
    check("MS-b first-step failure is plain error", False, "got MultiStepError, expected OperationError")
except OperationError as e:
    check("MS-b first-step failure is plain error", "Ghost" in str(e), str(e))

# three steps, the THIRD fails -> first two kept, step 3 reported
try:
    execute_multi(
        {"t": DF}, "t",
        [{"action": "remove_duplicates", "columns": ["Email"]},
         {"action": "sort", "columns": ["Amount"], "orders": ["desc"]},
         {"action": "drop_columns", "columns": ["Nope"]}],
    )
    check("MS-b three-step, third fails", False, "no error")
except MultiStepError as e:
    check("MS-b three-step, third fails", e.failed_step == 3 and len(e.notes) == 2, f"step={e.failed_step} notes={len(e.notes)}")
    check("MS-b partial reflects steps 1+2 (sorted desc)", list(e.partial_result["Amount"]) == [30, 20, 10], str(list(e.partial_result["Amount"])))

# --------------------------------------------------------------------------- #
# MS-b end-to-end through the API: partial result returns a FILE + a warning,
# status stays "ok" (the user gets the file), no stack trace.
# --------------------------------------------------------------------------- #
client = TestClient(main.app)
_orig = main.llm.parse_instruction
try:
    main.llm.parse_instruction = lambda i, s, h: {"operations": [
        {"action": "remove_duplicates", "columns": ["Email"]},
        {"action": "sort", "columns": ["Ghost"], "orders": ["asc"]}]}
    r = client.post("/process",
                    data={"instruction": "dedupe then sort by ghost", "session_id": "ms", "rewind": "-1", "history": ""},
                    files=[("files", ("t.csv", b"Email\na@x\na@x\nb@x\n", "text/csv"))])
    body = r.json()
    check("MS-b API returns ok with a file", r.status_code == 200 and body["status"] == "ok" and bool(body.get("file_base64")), str(body)[:160])
    check("MS-b API flags partial", body.get("partial") is True, str(body.get("partial")))
    check("MS-b API warning names the step + reason", body.get("warning") and "Step 2" in body["warning"] and "Ghost" in body["warning"], body.get("warning"))
    check("MS-b API notes show the completed step", any("dupl" in n.lower() for n in body["notes"]), str(body.get("notes")))
finally:
    main.llm.parse_instruction = _orig
    main._SESSIONS.clear()

# --------------------------------------------------------------------------- #
# MS-c  Middle step ambiguous -> the Brain pauses to ASK (clarify), then on the
# answer it emits the full plan and all steps run. (Routing tested with a stub.)
# --------------------------------------------------------------------------- #
try:
    # 1st call: the Brain can't tell which column -> clarification, no operations
    main.llm.parse_instruction = lambda i, s, h: {"operations": [], "clarification": "Which date column should I sort by? Available: Date"}
    r1 = client.post("/process",
                     data={"instruction": "dedupe, then sort by date, then keep top rows", "session_id": "msc", "rewind": "-1", "history": ""},
                     files=[("files", ("t.csv", b"Email,Date\na@x,2026-01-01\na@x,2026-01-01\n", "text/csv"))])
    b1 = r1.json()
    check("MS-c pauses to ask (clarify)", b1["status"] == "clarify" and "?" in b1["clarification"], str(b1)[:160])

    # 2nd call: with the answer, the Brain emits the full multi-step plan -> all run
    main.llm.parse_instruction = lambda i, s, h: {"operations": [
        {"action": "remove_duplicates", "columns": ["Email"]},
        {"action": "sort", "columns": ["Date"], "orders": ["asc"]}]}
    r2 = client.post("/process",
                     data={"instruction": "dedupe, then sort by date ... answered: Date", "session_id": "msc", "rewind": "-1", "history": "Sumio asked: which date column"},
                     files=[("files", ("t.csv", b"Email,Date\na@x,2026-01-01\na@x,2026-01-01\nb@x,2026-02-02\n", "text/csv"))])
    b2 = r2.json()
    check("MS-c continues after the answer (all steps run)", b2["status"] == "ok" and not b2.get("partial") and b2["row_count"] == 2, str(b2)[:160])
finally:
    main.llm.parse_instruction = _orig
    main._SESSIONS.clear()

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
