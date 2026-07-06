"""Global error-handling rules — enforced for EVERY feature.

  R1  No silent wrong answers — unsure → ask/refuse (covered by parse clarify/reply +
      executor honest notes; spot-checked here).
  R2  No technical error reaches the user — stack traces / codes / library errors are
      caught and translated. (The big one: a global catch-all + per-endpoint handling.)
  R3  Validate before executing — a bad Operation Plan is rejected with a friendly error,
      and nothing is half-applied.
  R4  The original is sacred — the uploaded data is never mutated in place; the first
      state stays intact.
  R5  Ambiguity → one clear question.
  R6  Unsupported → honest "not yet" + what IS possible.

Run from backend:  .venv\\Scripts\\python.exe test_global_rules.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd
from fastapi.testclient import TestClient

from app import main

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


_TECH = ("traceback", "exception", "keyerror", "valueerror", "typeerror", "none type",
         "nonetype", "0x", "line ", '/app/', "\\app\\", "pandas", "numpy", "attributeerror")


def looks_technical(msg: str) -> bool:
    m = (msg or "").lower()
    return any(t in m for t in _TECH)


# raise_server_exceptions=False so the global 500 handler's response is returned to us
# (instead of TestClient re-raising), which is exactly what a real client would receive.
client = TestClient(main.app, raise_server_exceptions=False)
CSV = b"Region,Revenue\nNorth,100\nSouth,200\nNorth,50\n"

print("GLOBAL ERROR-HANDLING RULES\n")

# =========================================================================
# R2  No technical error reaches the user
# =========================================================================
print("R2  No technical errors reach the user")

# (a) A truly unexpected crash inside an endpoint → friendly 500, no traceback.
_orig = main.summarize_tables
try:
    def boom(*a, **k):
        raise RuntimeError("kaboom: secret internals 0xDEADBEEF at line 42")
    main.summarize_tables = boom
    client.post("/inspect", data={"session_id": "ge"}, files=[("files", ("d.csv", CSV, "text/csv"))])
    r = client.post("/parse", data={"instruction": "sort by Revenue", "session_id": "ge"})
    body = r.json()
    check("R2 unexpected crash → 500 with friendly error", r.status_code == 500 and body.get("status") == "error", str(body)[:160])
    check("R2 crash message is NOT technical", not looks_technical(body.get("error", "")), body.get("error"))
    check("R2 internal details (kaboom/0x) never leak", "kaboom" not in str(body) and "0x" not in str(body).lower(), str(body)[:160])
finally:
    main.summarize_tables = _orig

# (b) Malformed request body → friendly, not a parser dump.
bad = client.post("/sheets/plan", content=b"not json{{{", headers={"Content-Type": "application/json"})
bb = bad.json()
check("R2 bad JSON body → friendly 400", bad.status_code == 400 and not looks_technical(bb.get("error", "")), str(bb)[:160])

# (c) Missing required field → friendly validation message (not FastAPI's field dump).
miss = client.post("/parse", data={"session_id": "ge"})  # no 'instruction'
mb = miss.json()
check("R2 missing field → friendly message", miss.status_code == 400 and not looks_technical(mb.get("error", "")), str(mb)[:160])
check("R2 validation msg has no raw field paths", "body" not in str(mb).lower() or "missing" not in str(mb).lower() or not looks_technical(mb.get("error","")), str(mb)[:160])

# =========================================================================
# R3  Validate before executing — bad plan rejected, nothing half-applied
# =========================================================================
print("\nR3  Validate before executing")
client.post("/inspect", data={"session_id": "ge3"}, files=[("files", ("d.csv", CSV, "text/csv"))])
plan = '{"operations":[{"action":"sort","columns":["DoesNotExist"],"orders":["asc"]}]}'
r = client.post("/execute", data={"session_id": "ge3", "plan": plan})
body = r.json()
check("R3 unknown column rejected (422)", r.status_code == 422 and body.get("status") == "error", str(body)[:160])
check("R3 rejection names the column, stays friendly", "DoesNotExist" in body.get("error", "") and not looks_technical(body.get("error", "")), str(body)[:160])

# unknown action → friendly, not a crash
r2 = client.post("/execute", data={"session_id": "ge3", "plan": '{"operations":[{"action":"teleport"}]}'})
check("R3 unknown action rejected friendly", r2.status_code in (422, 400) and not looks_technical(r2.json().get("error", "")), str(r2.json())[:160])

# =========================================================================
# R4  The original is sacred — never mutated in place
# =========================================================================
print("\nR4  The original is sacred")
client.post("/inspect", data={"session_id": "ge4"}, files=[("files", ("d.csv", CSV, "text/csv"))])
before = main._SESSIONS["ge4"]["states"][0]
before_df = before["tables"][before["primary"]].copy(deep=True)
# run a destructive op (remove duplicates) via /execute
client.post("/execute", data={"session_id": "ge4", "plan": '{"operations":[{"action":"remove_duplicates"}]}'})
orig_now = main._SESSIONS["ge4"]["states"][0]["tables"][main._SESSIONS["ge4"]["states"][0]["primary"]]
check("R4 original state[0] row count unchanged", len(orig_now) == len(before_df), f"{len(orig_now)} vs {len(before_df)}")
check("R4 original state[0] values unchanged", orig_now.equals(before_df), "original was mutated!")
check("R4 a new state was appended (result is a new version)", len(main._SESSIONS["ge4"]["states"]) >= 2, "")

# =========================================================================
# R5 / R6  Ambiguity → one question;  Unsupported → honest "not yet" + options
# =========================================================================
print("\nR5/R6  Clarify vs honest decline")
client.post("/inspect", data={"session_id": "ge5"}, files=[("files", ("d.csv", CSV, "text/csv"))])
_origp = main.llm.parse_instruction
try:
    # R5: ambiguous → a single clarification question, no operations.
    main.llm.parse_instruction = lambda i, s, h: {
        "operations": [], "clarification": "Which column should I sort by — Region or Revenue?",
    }
    r = client.post("/parse", data={"instruction": "sort it", "session_id": "ge5"}).json()
    check("R5 ambiguous → clarify (one question, no ops)",
          r.get("status") == "clarify" and r["clarification"].count("?") == 1, str(r)[:160])

    # R6: unsupported → honest reply, with what IS possible, no fabricated ops.
    main.llm.parse_instruction = lambda i, s, h: {
        "operations": [],
        "reply": "I can't translate cells yet — but I can sort, filter, dedupe, add formulas, "
                 "aggregate, look up, chart, and more.",
    }
    r = client.post("/parse", data={"instruction": "translate to French", "session_id": "ge5"}).json()
    check("R6 unsupported → honest message, no ops", r.get("status") == "message" and "can" in r["message"].lower(), str(r)[:160])
finally:
    main.llm.parse_instruction = _origp

main._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
