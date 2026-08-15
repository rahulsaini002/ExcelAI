"""PROGRAM PHASE 0.3 — the AI Brain, verified against PRD 1.3.

Two halves:

LIVE MATRIX (real Gemini calls) — 9 cases x {EN, HI, Hinglish} through /parse:
  1 clear-sort            plan with a sort op on the named column
  2 column approximation  "sort by rev" resolves to the "Revenue" column
  3 ambiguous column      "sort by price" vs Price_2024/Price_2025 -> clarify
  4 unsupported           sparklines (Stage-6 deferred) -> decline/clarify, never a plan
  5 forecast              "predict next year's sales" — SUPPORTED since engine Phase
                          4.5 (supersedes the program doc's 'unsupported' example):
                          a forecast plan or a clarifying question both pass; a
                          decline is a capability-detection FAIL
  6 non-existent column   "sort by Discount" -> clarify/decline, never a confident plan
  7 vague                 "clean it up" -> clarify OR a plan of ONLY cleanup-family ops
  8 mixed-language lookup lookup op pulling Unit_Price from Prices by Product
  9 multi-step            dedupe THEN sort, in that order

MALFORMED-BRAIN SIMULATION (no API calls) — llm.parse_instruction is monkeypatched to
return garbage (raw string / empty dict / unknown action / non-existent column) and
/process must answer with a clean message/clarify/error — NEVER status=ok, never a 500.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_0_3.py
  --offline        skip the live matrix (malformed simulation only)
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p03.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

from app import llm  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0
XL = "application/octet-stream"

# Actions an honest Brain may propose for a vague "clean it up": all non-destructive,
# reviewable-in-preview cleanup families. (find_replace/format_cells count — a
# transparent, plainly-described cleanup plan the user approves before running is safe
# behavior; only actions OUTSIDE these families — e.g. sort, aggregate — would mean the
# model invented intent.)
CLEANUP_ACTIONS = {
    "trim", "remove_duplicates", "fill_missing", "drop_missing", "drop_invalid",
    "flag_missing", "find_replace", "format_cells",
    # conditional_format = highlight cells matching a rule (e.g. shade the blanks). It
    # changes no data and is reviewable in the preview, so by the rule stated above it
    # belongs with flag_missing/format_cells — it is the visual sibling of both. Added
    # 2026-08-13 after the multilingual few-shot work, when the Brain started reaching
    # for it to highlight missing prices during a vague tidy-up. Widening the set here is
    # NOT to make a test pass: the equivalent overreach it replaced (inventing
    # add_formula_column columns) was fixed in the PROMPT, not excused here.
    "conditional_format",
}


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def seed(sid: str, path: Path) -> None:
    r = client.post("/inspect", data={"session_id": sid},
                    files=[("files", (path.name, path.read_bytes(), XL))])
    assert r.status_code == 200, f"/inspect {path.name}: {r.text[:120]}"


def _rate_limited(j: dict) -> bool:
    return j.get("status") == "error" and "rate-limit" in str(j.get("error", "")).lower()


def parse(sid: str, prompt: str) -> dict:
    """One /parse call, with pacing + retry so the free-tier per-minute quota doesn't
    masquerade as Brain failures (the engine's honest 'rate-limited' answer is correct
    behavior, but it means the row wasn't actually judged)."""
    for attempt in range(3):
        j = client.post("/parse", data={"instruction": prompt, "session_id": sid}).json()
        if not _rate_limited(j) or attempt == 2:
            return j
        # The free tier meters per MINUTE-window; a short wait lands inside the same
        # throttled window (and llm.py's internal retries amplify the burn). Wait out
        # a full window-plus before retrying.
        wait = 70 * (attempt + 1)
        print(f"        (rate-limited — waiting {wait}s, retry {attempt + 2}/3)")
        time.sleep(wait)
    return j


def ops_of(j: dict) -> list[dict]:
    return (j.get("plan") or {}).get("operations") or []


def actions_of(j: dict) -> list[str]:
    return [op.get("action") for op in ops_of(j)]


WB = TESTS / "standard_test_workbook.xlsx"
APPROX = TESTS / "files" / "approx.xlsx"
AMBIG = TESTS / "files" / "ambiguous.xlsx"

# (case, language, fixture, prompt, judge) — judge returns (ok, detail)
CASES = [
    # 1 — clear sort
    ("1 clear-sort", "EN", WB, "Sort the Sales sheet by Price from highest to lowest",
     lambda j: (j.get("status") == "plan" and "sort" in actions_of(j)
                and "price" in json.dumps(ops_of(j)).lower(), "")),
    ("1 clear-sort", "HI", WB, "Sales sheet को Price के हिसाब से घटते क्रम में लगाओ",
     lambda j: (j.get("status") == "plan" and "sort" in actions_of(j)
                and "price" in json.dumps(ops_of(j)).lower(), "")),
    ("1 clear-sort", "HG", WB, "Sales sheet ko Price ke hisab se descending sort karo",
     lambda j: (j.get("status") == "plan" and "sort" in actions_of(j)
                and "price" in json.dumps(ops_of(j)).lower(), "")),
    # 2 — column-name approximation: "rev" -> Revenue
    ("2 approx rev->Revenue", "EN", APPROX, "sort by rev from highest to lowest",
     lambda j: (j.get("status") == "plan" and "revenue" in json.dumps(ops_of(j)).lower(), "")),
    ("2 approx rev->Revenue", "HI", APPROX, "rev के हिसाब से घटते क्रम में सॉर्ट करो",
     lambda j: (j.get("status") == "plan" and "revenue" in json.dumps(ops_of(j)).lower(), "")),
    ("2 approx rev->Revenue", "HG", APPROX, "rev ke hisab se descending sort karo",
     lambda j: (j.get("status") == "plan" and "revenue" in json.dumps(ops_of(j)).lower(), "")),
    # 3 — ambiguous column: two Price_* columns -> must ask, not guess
    ("3 ambiguous price", "EN", AMBIG, "sort by price",
     lambda j: (j.get("status") == "clarify", "")),
    ("3 ambiguous price", "HI", AMBIG, "price के हिसाब से सॉर्ट करो",
     lambda j: (j.get("status") == "clarify", "")),
    ("3 ambiguous price", "HG", AMBIG, "price ke hisab se sort karo",
     lambda j: (j.get("status") == "clarify", "")),
    # 4 — genuinely unsupported (sparklines are Stage-6 deferred): decline, never a plan
    ("4 unsupported sparklines", "EN", WB, "Add sparklines next to each row of the Sales sheet",
     lambda j: (j.get("status") in ("message", "clarify"), "")),
    ("4 unsupported sparklines", "HI", WB, "Sales sheet की हर पंक्ति के बगल में sparklines जोड़ो",
     lambda j: (j.get("status") in ("message", "clarify"), "")),
    ("4 unsupported sparklines", "HG", WB, "Sales sheet ki har row ke saath sparklines add karo",
     lambda j: (j.get("status") in ("message", "clarify"), "")),
    # 5 — forecast is SUPPORTED now: plan(forecast) or clarify pass; a decline fails
    ("5 forecast supported", "EN", WB, "Predict next year's sales",
     lambda j: (j.get("status") == "clarify"
                or (j.get("status") == "plan" and "forecast" in actions_of(j)), "")),
    ("5 forecast supported", "HI", WB, "अगले साल की sales का अनुमान लगाओ",
     lambda j: (j.get("status") == "clarify"
                or (j.get("status") == "plan" and "forecast" in actions_of(j)), "")),
    ("5 forecast supported", "HG", WB, "Agle saal ki sales predict karo",
     lambda j: (j.get("status") == "clarify"
                or (j.get("status") == "plan" and "forecast" in actions_of(j)), "")),
    # 6 — non-existent column: never a confident plan on "Discount"
    ("6 non-existent column", "EN", WB, "Sort the Sales sheet by Discount from highest to lowest",
     lambda j: (j.get("status") in ("clarify", "message"), "")),
    ("6 non-existent column", "HI", WB, "Sales sheet को Discount के हिसाब से सॉर्ट करो",
     lambda j: (j.get("status") in ("clarify", "message"), "")),
    ("6 non-existent column", "HG", WB, "Sales sheet ko Discount ke hisab se sort karo",
     lambda j: (j.get("status") in ("clarify", "message"), "")),
    # 7 — vague: clarify, or a plan made ONLY of cleanup-family ops
    ("7 vague clean-it-up", "EN", WB, "clean it up",
     lambda j: (j.get("status") == "clarify"
                or (j.get("status") == "plan" and actions_of(j)
                    and set(actions_of(j)) <= CLEANUP_ACTIONS),
                f"actions={actions_of(j)}")),
    ("7 vague clean-it-up", "HI", WB, "इसे साफ़ करो",
     lambda j: (j.get("status") == "clarify"
                or (j.get("status") == "plan" and actions_of(j)
                    and set(actions_of(j)) <= CLEANUP_ACTIONS),
                f"actions={actions_of(j)}")),
    ("7 vague clean-it-up", "HG", WB, "isko clean kar do",
     lambda j: (j.get("status") == "clarify"
                or (j.get("status") == "plan" and actions_of(j)
                    and set(actions_of(j)) <= CLEANUP_ACTIONS),
                f"actions={actions_of(j)}")),
    # 8 — cross-sheet lookup, incl. mixed language
    ("8 lookup", "EN", WB, "Pull Unit_Price from the Prices sheet into the Sales sheet by matching Product",
     lambda j: (j.get("status") == "plan" and "lookup" in actions_of(j), "")),
    ("8 lookup", "HI", WB, "Prices sheet से Unit_Price को Product के आधार पर Sales sheet में जोड़ो",
     lambda j: (j.get("status") == "plan" and "lookup" in actions_of(j), "")),
    ("8 lookup", "HG", WB, "Prices sheet se Unit_Price ko Product ke hisab se Sales me le aao",
     lambda j: (j.get("status") == "plan" and "lookup" in actions_of(j), "")),
    # 9 — multi-step: dedupe THEN sort, in order
    ("9 multi-step", "EN", WB, "Remove duplicate rows, then sort by Price from highest to lowest",
     lambda j: (j.get("status") == "plan" and len(ops_of(j)) >= 2
                and "remove_duplicates" in actions_of(j) and "sort" in actions_of(j)
                and actions_of(j).index("remove_duplicates") < actions_of(j).index("sort"),
                f"actions={actions_of(j)}")),
    ("9 multi-step", "HI", WB, "पहले duplicate rows हटाओ, फिर Price के हिसाब से घटते क्रम में लगाओ",
     lambda j: (j.get("status") == "plan" and len(ops_of(j)) >= 2
                and "remove_duplicates" in actions_of(j) and "sort" in actions_of(j)
                and actions_of(j).index("remove_duplicates") < actions_of(j).index("sort"),
                f"actions={actions_of(j)}")),
    ("9 multi-step", "HG", WB, "pehle duplicate rows hatao, phir Price ke hisab se descending sort karo",
     lambda j: (j.get("status") == "plan" and len(ops_of(j)) >= 2
                and "remove_duplicates" in actions_of(j) and "sort" in actions_of(j)
                and actions_of(j).index("remove_duplicates") < actions_of(j).index("sort"),
                f"actions={actions_of(j)}")),
]


def run_live(only: str = "") -> None:
    """`only` — substring filter over 'case · lang' (e.g. '3 ambiguous' or '· HI') so
    quota-blocked rows can be retried surgically instead of re-running all 27."""
    cases = [c for c in CASES if only.lower() in f"{c[0]} · {c[1]}".lower()] if only else CASES
    print(f"LIVE MATRIX — {len(cases)} of {len(CASES)} rows (real Brain)\n")
    for i, (case, lang, fixture, prompt, judge) in enumerate(cases):
        if i:
            time.sleep(8)  # pace under the free tier's ~10-requests-per-minute cap
        sid = f"p03-{uuid.uuid4().hex[:10]}"
        seed(sid, fixture)
        t0 = time.time()
        j = parse(sid, prompt)
        ms = int((time.time() - t0) * 1000)
        ok, extra = judge(j)
        detail = extra or f"status={j.get('status')} actions={actions_of(j)}"
        check(f"[{case} · {lang}] {ms}ms", ok, detail + f"  resp={json.dumps(j, ensure_ascii=False)[:140]}")


def run_malformed() -> None:
    print("\nMALFORMED-BRAIN SIMULATION — /process must never fake a result\n")
    wb_bytes = WB.read_bytes()
    real = llm.parse_instruction
    fakes = [
        ("raw string (not a dict)", "utter garbage, not a plan"),
        ("empty dict (no operations)", {}),
        ("unknown action", {"operations": [{"action": "explode_everything"}]}),
        ("non-existent column injected", {"operations": [{"action": "sort", "columns": ["NoSuchColumn"], "orders": ["asc"]}]}),
    ]
    try:
        for name, fake in fakes:
            llm.parse_instruction = lambda *a, _fake=fake, **k: _fake
            r = client.post(
                "/process",
                data={"instruction": "sort by Price", "session_id": f"p03m-{uuid.uuid4().hex[:8]}"},
                files=[("files", ("standard_test_workbook.xlsx", wb_bytes, XL))],
            )
            try:
                j = r.json()
            except Exception:
                j = {}
            not_fake = j.get("status") in ("message", "clarify", "error")
            no_500 = r.status_code < 500
            check(f"malformed: {name} -> clean non-ok answer, no 500",
                  bool(not_fake and no_500),
                  f"HTTP {r.status_code} status={j.get('status')} body={r.text[:140]}")
    finally:
        llm.parse_instruction = real


if __name__ == "__main__":
    offline = "--offline" in sys.argv
    only = ""
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    print("PHASE 0.3 — the AI Brain (PRD 1.3)\n")
    if not offline:
        if not os.getenv("GEMINI_API_KEY"):
            print("NOTE: no GEMINI_API_KEY — live matrix will exercise the offline fallback.\n")
        run_live(only)
    if not only:
        run_malformed()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
