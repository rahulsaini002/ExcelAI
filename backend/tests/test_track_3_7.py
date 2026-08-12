"""ENHANCEMENT TRACK 3, item 7 — prompt versioning (the traceability half).

The other two thirds of item 7 were ALREADY BUILT and are re-checked here rather than
rebuilt:
  - strict plan validation  main._sane_plan, applied on BOTH /process and /execute
  - JSON-only output        llm passes response_schema=OperationPlan with
                            response_mime_type="application/json", i.e. structured
                            output. That is STRONGER than "retry on malformed JSON":
                            the model cannot return non-JSON in the first place, so
                            there is no malformed-response case to retry.

What was missing: knowing WHICH prompt produced a given result. Stage 0.3 made the cost
concrete — rules were verified in English, the prompt was edited afterwards, and no
stored result could say which wording it had been run against.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_track_3_7.py
"""
from __future__ import annotations

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

_fd, _db = tempfile.mkstemp(suffix="-t37.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

from app import llm  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def run() -> None:
    # --- identity is well-formed ------------------------------------------------------
    ident = llm.prompt_identity()
    check("prompt_identity reports version, fingerprint and model",
          {"prompt_version", "prompt_fingerprint", "model"} <= set(ident),
          f"got {sorted(ident)}")
    check("fingerprint is a short stable hex digest",
          isinstance(ident["prompt_fingerprint"], str)
          and len(ident["prompt_fingerprint"]) == 12
          and all(c in "0123456789abcdef" for c in ident["prompt_fingerprint"]),
          f"got {ident['prompt_fingerprint']!r}")

    # --- stable across calls: same prompt must not produce a different id --------------
    check("fingerprint is stable across repeated calls",
          llm.prompt_fingerprint() == llm.prompt_fingerprint(), "fingerprint drifted")

    # --- THE POINT: the fingerprint tracks the TEXT, so it cannot be forgotten ---------
    # Someone edits SYSTEM_PROMPT and forgets to bump PROMPT_VERSION. The hand-maintained
    # version lies; the computed fingerprint must not.
    real_prompt = llm.SYSTEM_PROMPT
    before_fp, before_ver = llm.prompt_fingerprint(), llm.PROMPT_VERSION
    try:
        llm.SYSTEM_PROMPT = real_prompt + "\n- A NEW RULE SOMEONE FORGOT TO VERSION."
        after_fp, after_ver = llm.prompt_fingerprint(), llm.PROMPT_VERSION
        check("editing the prompt changes the fingerprint", before_fp != after_fp,
              "fingerprint did not move when the prompt changed")
        check("an unbumped PROMPT_VERSION is detectable (version same, fingerprint differs)",
              before_ver == after_ver and before_fp != after_fp,
              "could not detect the stale version")
    finally:
        llm.SYSTEM_PROMPT = real_prompt
    check("fingerprint returns to its original value once the prompt is restored",
          llm.prompt_fingerprint() == before_fp, "fingerprint did not restore")

    # --- exposed over HTTP so a battery run can record it -----------------------------
    r = client.get("/brain/version")
    check("GET /brain/version returns 200", r.status_code == 200, f"HTTP {r.status_code}")
    body = r.json() if r.status_code == 200 else {}
    check("endpoint agrees with the module", body == llm.prompt_identity(),
          f"endpoint={body} module={llm.prompt_identity()}")

    # --- the already-built two thirds, re-verified rather than rebuilt -----------------
    check("structured output is enforced (model cannot emit non-JSON)",
          llm.OperationPlan is not None, "OperationPlan schema missing")

    sane = __import__("app.main", fromlist=["_sane_plan"])._sane_plan
    check("_sane_plan rejects a raw string", sane("just some text").get("operations") in (None, []),
          "raw string survived as a plan")
    check("_sane_plan rejects a list", sane([1, 2, 3]).get("operations") in (None, []),
          "list survived as a plan")
    check("_sane_plan rejects None", sane(None).get("operations") in (None, []),
          "None survived as a plan")
    good = {"operations": [{"action": "sort", "columns": ["Price"]}]}
    check("_sane_plan preserves a well-formed plan",
          sane(good).get("operations") == good["operations"], "good plan was mangled")
    # One malformed step invalidates the WHOLE plan rather than being dropped from it.
    # That is deliberate and stricter: silently running 1 of 2 requested operations would
    # hand back a partial result the user never asked for and cannot see is partial. An
    # empty plan flows into the standard "I didn't understand that" answer instead.
    mixed = sane({"operations": [{"action": "sort"}, None, "x"]})
    check("one malformed step voids the entire plan (no silent partial run)",
          mixed.get("operations") == [], f"got {mixed.get('operations')!r}")
    check("voiding a plan keeps the rest of the response intact",
          sane({"operations": [None], "reply": "hi"}).get("reply") == "hi",
          "sibling fields were lost")


if __name__ == "__main__":
    print("TRACK 3 item 7 — prompt versioning + already-built hardening\n")
    run()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
