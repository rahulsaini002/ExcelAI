"""Feature 1.2 — Plain-language instruction box. Covers the DETERMINISTIC backend/API
half (empty submit, gibberish fallback, language pass-through, two-tasks routing, long
input). The pure-frontend bits (toast, maxLength counter, example chips) and the model's
multilingual UNDERSTANDING (1.2-a/b/c) are verified in the UI / test_brain_live.py.

Run from backend:  .venv\\Scripts\\python.exe test_1_2.py
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import main

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


client = TestClient(main.app)
_ORIG = main.llm.parse_instruction


def stub(plan, capture=None):
    """Stub the Brain. If `capture` is a list, append the instruction it received."""
    def fake(instruction, structure, history):
        if capture is not None:
            capture.append(instruction)
        return plan
    main.llm.parse_instruction = fake


def post(instruction, csv=b"Revenue\n30\n10\n20\n", session="f12"):
    return client.post(
        "/process",
        data={"instruction": instruction, "session_id": session, "rewind": "-1", "history": ""},
        files=[("files", ("t.csv", csv, "text/csv"))],
    )


print("FEATURE 1.2 — plain-language instruction box\n")

# --------------------------------------------------------------------------- #
# 1.2-e  Empty submit -> blocked with a gentle message (no model call)
# --------------------------------------------------------------------------- #
try:
    r = post("")
    body = r.json()
    check("1.2-e empty submit blocked (400)", r.status_code == 400 and body["status"] == "error", str(body)[:120])
    check("1.2-e gentle message", "describe what" in body["error"].lower() or "type" in body["error"].lower(), body.get("error"))
    # whitespace-only is also treated as empty
    r2 = post("    \n\t  ")
    check("1.2-e whitespace-only blocked", r2.status_code == 400, str(r2.json())[:80])
finally:
    main.llm.parse_instruction = _ORIG

# --------------------------------------------------------------------------- #
# 1.2-f  Gibberish -> friendly "I didn't understand that" (empty plan fallback)
# --------------------------------------------------------------------------- #
try:
    stub({"operations": []})  # Brain returns nothing actionable
    body = post("asdfghjkl qwerty zxcvbn").json()
    check("1.2-f gibberish -> message status", body["status"] == "message", str(body)[:120])
    check("1.2-f friendly 'didn't understand'", "didn't understand" in body["message"], body.get("message"))
finally:
    main.llm.parse_instruction = _ORIG

# --------------------------------------------------------------------------- #
# 1.2-a/b/c  Language pass-through: any UTF-8 instruction reaches the Brain intact
#            (English / Hindi / mixed), and a valid plan routes to execution.
# --------------------------------------------------------------------------- #
for label, text in [
    ("English", "sort by Revenue descending"),
    ("Hindi", "रेवेन्यू के हिसाब से घटते क्रम में सॉर्ट करो"),
    ("Mixed", "Revenue ke hisaab se descending sort karo"),
]:
    try:
        seen: list[str] = []
        stub({"operations": [{"action": "sort", "columns": ["Revenue"], "orders": ["desc"]}]}, capture=seen)
        body = post(text).json()
        check(f"1.2 {label} reaches Brain intact", seen and seen[0].startswith(text), repr(seen[:1]))
        check(f"1.2 {label} routes to a result", body["status"] == "ok" and body["row_count"] == 3, str(body)[:100])
    finally:
        main.llm.parse_instruction = _ORIG

# --------------------------------------------------------------------------- #
# 1.2-d  Two tasks in one sentence -> both captured (ordered multi-step plan)
# --------------------------------------------------------------------------- #
try:
    stub({"operations": [
        {"action": "remove_duplicates"},
        {"action": "sort", "columns": ["Revenue"], "orders": ["asc"]}]})
    body = post("remove duplicates and then sort by revenue", csv=b"Revenue\n10\n10\n30\n").json()
    check("1.2-d both tasks ran", body["status"] == "ok" and body["row_count"] == 2, str(body)[:120])
    check("1.2-d summary covers both steps", len(body["notes"]) >= 2, str(body.get("notes")))
finally:
    main.llm.parse_instruction = _ORIG

# --------------------------------------------------------------------------- #
# 1.2-g  Extremely long input -> accepted by the backend (no crash); the UI caps
#        length at MAX_INSTRUCTION with a counter (frontend concern).
# --------------------------------------------------------------------------- #
try:
    seen2: list[str] = []
    stub({"operations": [{"action": "sort", "columns": ["Revenue"], "orders": ["desc"]}]}, capture=seen2)
    long_text = "sort by Revenue descending " + ("x" * 4000)
    body = post(long_text).json()
    check("1.2-g long input accepted (no crash)", body["status"] == "ok", str(body)[:100])
    check("1.2-g full long input reaches Brain", seen2 and len(seen2[0]) >= 4000, str(len(seen2[0]) if seen2 else 0))
finally:
    main.llm.parse_instruction = _ORIG
    main._SESSIONS.clear()

# guard: Brain restored
check("teardown: parse_instruction restored", main.llm.parse_instruction is _ORIG)

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
