"""Feature 1.14 — Plain-language feedback & error handling. Verifies 1.14-a..e:
clear summaries with counts, specific friendly failures, and that NO raw error code
or stack trace ever reaches the user. The Brain-routing cases (unsupported / ambiguous)
are tested through the real /process pipeline with the LLM stubbed, since the routing
(not the wording) is what the backend owns.

Run from backend:  .venv\\Scripts\\python.exe test_1_14.py
"""
from __future__ import annotations

import pandas as pd
from fastapi.testclient import TestClient

from app import main
from app.executor import OperationError, execute_plan

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


client = TestClient(main.app)
_ORIG_PARSE = main.llm.parse_instruction
_ORIG_EXEC = main.execute_multi


def stub_parse(plan):
    """Make llm.parse_instruction return a fixed plan (no live model call)."""
    main.llm.parse_instruction = lambda instruction, structure, history: plan


def restore():
    main.llm.parse_instruction = _ORIG_PARSE
    main.execute_multi = _ORIG_EXEC


def post(instruction="do it", csv=b"A,B\n1,1\n1,1\n2,2\n", session="s1"):
    return client.post(
        "/process",
        data={"instruction": instruction, "session_id": session, "rewind": "-1", "history": ""},
        files=[("files", ("t.csv", csv, "text/csv"))],
    )


def looks_technical(text: str) -> bool:
    """True if a response leaks anything stack-trace-y / raw."""
    low = text.lower()
    return any(s in low for s in ("traceback", "line ", "exception", ".py", "keyerror", "valueerror", "0x"))


print("FEATURE 1.14 — plain-language feedback & error handling\n")

# --------------------------------------------------------------------------- #
# 1.14-a  Successful operation -> plain summary with counts
# --------------------------------------------------------------------------- #
# (i) executor level: notes are plain language and include affected counts
out, notes, _ = execute_plan(
    pd.DataFrame({"Region": ["North"] * 3 + ["South"] * 2, "Rev": [5, 1, 3, 2, 4]}),
    [{"action": "filter", "conditions": [{"column": "Region", "operator": "equals", "value": "North"}]},
     {"action": "sort", "columns": ["Rev"], "orders": ["desc"]}],
)
check("1.14-a filter note has counts", any("3" in n and "5" in n.replace("Rev", "") for n in notes) or any("Kept" in n for n in notes), str(notes))
check("1.14-a sort note is plain", any("Sorted by Rev" in n for n in notes), str(notes))
check("1.14-a no technical text in notes", not looks_technical(" ".join(notes)), str(notes))

# (ii) full pipeline: a success returns an "explanation" + a downloadable file
try:
    stub_parse({"operations": [{"action": "remove_duplicates"}, {"action": "sort", "columns": ["A"], "orders": ["asc"]}], "title": "Dedupe and sort"})
    r = post()
    body = r.json()
    check("1.14-a API success status ok", r.status_code == 200 and body["status"] == "ok", str(body)[:200])
    # session-naming fields: distinct op types + the AI's short title
    check("1.14-a returns op actions for the title", body.get("actions") == ["remove_duplicates", "sort"], str(body.get("actions")))
    check("1.14-a returns the AI title", body.get("ai_title") == "Dedupe and sort", str(body.get("ai_title")))
    check("1.14-a explanation present & plain", body["explanation"] and not looks_technical(body["explanation"]), body.get("explanation"))
    check("1.14-a summary reports a count", any(ch.isdigit() for ch in body["explanation"]), body.get("explanation"))
    check("1.14-a returns a file", bool(body.get("file_base64")) and body.get("filename"))
    # result preview so the UI can SHOW the transformed data, not just download it
    pv = body.get("preview")
    check("1.14-a response includes a result preview",
          isinstance(pv, list) and pv and "columns" in pv[0] and "sample_rows" in pv[0], str(pv)[:160])
    # results-summary fields (US-007/US-009): rows before/after, time, file size
    check("1.14-a response has rows_before", body.get("rows_before") == 3, str(body.get("rows_before")))
    check("1.14-a response has file_size + elapsed_ms",
          isinstance(body.get("file_size"), int) and body["file_size"] > 0 and isinstance(body.get("elapsed_ms"), int),
          f"size={body.get('file_size')} ms={body.get('elapsed_ms')}")
finally:
    restore()

# --------------------------------------------------------------------------- #
# Undo (US-011): pops the last server state; nothing-to-undo is friendly
# --------------------------------------------------------------------------- #
try:
    stub_parse({"operations": [{"action": "remove_duplicates"}]})
    post(session="undo1")  # one step -> states = [upload, step1]
    u = client.post("/undo", data={"session_id": "undo1"}).json()
    check("undo pops the last step", u.get("status") == "ok" and u.get("steps_remaining") == 0, str(u)[:120])
    u2 = client.post("/undo", data={"session_id": "undo1"}).json()
    check("undo with only the upload left -> friendly error", u2.get("status") == "error", str(u2)[:120])
    u3 = client.post("/undo", data={"session_id": "nope"}).json()
    check("undo unknown session -> friendly error", u3.get("status") == "error")
finally:
    restore()
    main._SESSIONS.clear()

# --------------------------------------------------------------------------- #
# 1.14-b  Unsupported request -> "I can't do that yet, but I can…" (reply routing)
# --------------------------------------------------------------------------- #
try:
    stub_parse({"operations": [], "reply": "I can't do that yet — but I can sort, filter, and aggregate."})
    r = post("make me a pivot chart")
    body = r.json()
    check("1.14-b routed to a message", r.status_code == 200 and body["status"] == "message", str(body)[:200])
    check("1.14-b says can't-do-yet, offers alternatives", "can't do that yet" in body["message"], body.get("message"))
finally:
    restore()

# --------------------------------------------------------------------------- #
# 1.14-c  Ambiguous request -> ONE clarifying question (clarify routing)
# --------------------------------------------------------------------------- #
try:
    stub_parse({"operations": [], "clarification": "Which column should I sort by? Available: A, B"})
    r = post("sort it")
    body = r.json()
    check("1.14-c routed to clarify", r.status_code == 200 and body["status"] == "clarify", str(body)[:200])
    check("1.14-c asks one question listing columns", body["clarification"].count("?") == 1 and "Available" in body["clarification"], body.get("clarification"))
finally:
    restore()

# empty/garbled plan (no ops, no reply, no clarification) -> a gentle nudge, not a crash
try:
    stub_parse({"operations": []})
    body = post("asdfghjkl").json()
    check("1.14-c empty plan -> friendly nudge", body["status"] == "message" and "didn't understand" in body["message"], str(body)[:200])
finally:
    restore()

# --------------------------------------------------------------------------- #
# 1.14-d  Internal error (simulated) -> friendly message, NO stack trace
# --------------------------------------------------------------------------- #
# (i) an unexpected crash during execution
try:
    stub_parse({"operations": [{"action": "remove_duplicates"}]})

    def boom(*a, **k):
        raise RuntimeError("secret internal detail: object at 0xDEADBEEF, file executor.py line 42")

    main.execute_multi = boom
    r = post("do it")
    body = r.json()
    check("1.14-d execution crash -> 500", r.status_code == 500)
    check("1.14-d friendly message", body["status"] == "error" and body["error"] == main._INTERNAL_ERROR, str(body)[:200])
    check("1.14-d no secret/stack detail leaked", "secret" not in r.text and not looks_technical(r.text), r.text[:200])
finally:
    restore()

# (ii) an unexpected crash while SAVING the workbook
try:
    stub_parse({"operations": [{"action": "remove_duplicates"}]})
    orig_serialize = main._serialize
    main._serialize = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk gremlin traceback"))
    r = post("do it")
    body = r.json()
    check("1.14-d serialize crash -> friendly 500", r.status_code == 500 and body["error"] == main._INTERNAL_ERROR, str(body)[:200])
    check("1.14-d serialize crash hides detail", "gremlin" not in r.text and "traceback" not in r.text.lower())
    main._serialize = orig_serialize
finally:
    restore()

# --------------------------------------------------------------------------- #
# 1.14-e  Type mismatch -> specific, friendly explanation (e.g. "isn't numbers")
# --------------------------------------------------------------------------- #
# executor level: the message names the column and the problem
try:
    execute_plan(pd.DataFrame({"Name": ["Asha", "Rohan"]}), [{"action": "aggregate", "agg_func": "sum", "agg_column": "Name"}])
    check("1.14-e type mismatch raises", False, "no error")
except OperationError as e:
    check("1.14-e specific friendly type message", "Name" in str(e) and "text" in str(e).lower() and not looks_technical(str(e)), str(e))

# full pipeline: the same mismatch comes back as a friendly 422 (not a 500/stack trace)
try:
    stub_parse({"operations": [{"action": "aggregate", "agg_func": "sum", "agg_column": "A"}]})
    r = post("sum A", csv=b"A,B\nx,1\ny,2\n")  # column A is text
    body = r.json()
    check("1.14-e API type mismatch -> 422", r.status_code == 422 and body["status"] == "error", str(body)[:200])
    check("1.14-e API message specific & clean", "A" in body["error"] and "text" in body["error"].lower() and not looks_technical(body["error"]), body.get("error"))
finally:
    restore()

# a bad column name is explained, not crashed (still 422, names the column)
try:
    stub_parse({"operations": [{"action": "sort", "columns": ["Nope"], "orders": ["asc"]}]})
    r = post("sort by Nope")
    body = r.json()
    check("1.14-e bad column -> friendly 422", r.status_code == 422 and "Nope" in body["error"] and not looks_technical(body["error"]), str(body)[:200])
finally:
    restore()

# guard: never accidentally left the LLM stubbed
check("teardown: parse_instruction restored", main.llm.parse_instruction is _ORIG_PARSE)
check("teardown: execute_multi restored", main.execute_multi is _ORIG_EXEC)

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
