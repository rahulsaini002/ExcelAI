"""Tests the two-phase /parse route (Brain-only: translate an instruction into a plan
WITHOUT executing). The live model is stubbed, so this verifies the ROUTING + response
shape the new Workspace UI relies on (plan / clarify / message), not the model wording.

Run from backend:  .venv\\Scripts\\python.exe test_parse.py
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import main

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
CSV = b"Region,Amount\nNorth,5\nSouth,3\nNorth,1\n"


def stub_parse(plan):
    main.llm.parse_instruction = lambda instruction, structure, history: plan


def restore():
    main.llm.parse_instruction = _ORIG_PARSE


def load_session(sid="p1"):
    return client.post(
        "/inspect",
        data={"session_id": sid},
        files=[("files", ("t.csv", CSV, "text/csv"))],
    )


def parse(instruction="do it", sid="p1", history=""):
    return client.post(
        "/parse",
        data={"instruction": instruction, "session_id": sid, "history": history},
    )


print("ROUTE /parse — two-phase Brain-only parsing\n")

# --- /inspect with a session_id remembers the upload (so /parse can reuse it) ---
r = load_session("p1")
check("/inspect ok", r.status_code == 200 and r.json()["status"] == "ok", str(r.json())[:120])
check("/inspect remembered the session", "p1" in main._SESSIONS)

# --- plan: operations + the model's translation + confidence flow through ---
stub_parse(
    {
        "operations": [
            {"action": "filter", "conditions": [{"column": "Region", "operator": "equals", "value": "North"}]},
            {"action": "sort", "columns": ["Amount"], "orders": ["desc"]},
        ],
        "translation": "Filter Region equals North, then sort Amount high to low",
        "confidence": 94,
        "title": "Filter and sort",
    }
)
b = parse("North rows sorted by amount desc").json()
check("plan: status is 'plan'", b.get("status") == "plan", str(b)[:160])
check("plan: real translation passed through", b.get("translation") == "Filter Region equals North, then sort Amount high to low", b.get("translation"))
check("plan: confidence passed through", b.get("confidence") == 94, str(b.get("confidence")))
check("plan: carries the operations for /execute", len(b.get("plan", {}).get("operations", [])) == 2, str(b.get("plan")))
check("plan: carries the title", b.get("plan", {}).get("title") == "Filter and sort", str(b.get("plan")))

# --- when the model gives no translation/confidence, we derive a deterministic one ---
stub_parse({"operations": [{"action": "remove_duplicates", "columns": ["Region"]}]})
b = parse("dedupe on region").json()
check("derived translation when missing", b.get("status") == "plan" and isinstance(b.get("translation"), str) and len(b["translation"]) > 0, str(b)[:160])
check("default confidence when missing", b.get("confidence") == 80, str(b.get("confidence")))

# --- ambiguous -> clarify (question shown, no plan) ---
stub_parse({"operations": [], "clarification": "Which column should I sort by? Available: Region, Amount"})
b = parse("sort it").json()
check("clarify: status is 'clarify'", b.get("status") == "clarify", str(b)[:160])
check("clarify: carries the question", "Available" in (b.get("clarification") or ""), b.get("clarification"))

# --- unsupported -> message ("can't do that yet") ---
stub_parse({"operations": [], "reply": "I can't do that yet — but I can sort, filter, remove duplicates…"})
b = parse("forecast next year's sales").json()
check("unsupported: status is 'message'", b.get("status") == "message", str(b)[:160])
check("unsupported: carries the reply", "can't do that yet" in (b.get("message") or ""), b.get("message"))

# --- empty plan with no reply/clarify -> a friendly nudge (still a 'message') ---
stub_parse({"operations": []})
b = parse("asdfghjkl").json()
check("empty plan -> nudge message", b.get("status") == "message" and "didn't understand" in (b.get("message") or ""), str(b)[:160])

# --- guardrails: unknown session and empty instruction are rejected friendly ---
r = parse("sort by Amount", sid="does-not-exist")
check("unknown session -> 400", r.status_code == 400 and "upload" in r.json().get("error", "").lower(), str(r.json())[:120])

r = parse("   ", sid="p1")
check("empty instruction -> 400", r.status_code == 400, str(r.json())[:120])

# --- backward compat: /inspect WITHOUT a session_id still works (and stores nothing) ---
r = client.post("/inspect", files=[("files", ("t.csv", CSV, "text/csv"))])
check("/inspect (no session) still ok", r.status_code == 200 and r.json()["status"] == "ok", str(r.json())[:120])

restore()
print(f"\n{passed} passed, {failed} failed.")
