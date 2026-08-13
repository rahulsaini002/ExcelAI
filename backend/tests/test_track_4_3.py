"""ENHANCEMENT TRACK 4, item 3 — formula vs computed value, always disclosed.

Sumio can put two very different things in a cell: a live Excel formula that recalculates
when the user edits the sheet, or a computed value that is frozen at the moment it ran.
Both are legitimate. Leaving the user to guess is not — someone who assumes a total
recalculates, when it doesn't, will quietly ship a wrong number.

Before this, the rule existed only implicitly (in which render directive an operation
happened to emit) and the response said nothing about it beyond listing formula text.

The checks below care most about the two ways this could be WORSE than saying nothing:
  - claiming a formula when values were written (the user trusts a stale number to update)
  - claiming values when a formula was written (the user hand-edits a cell and breaks it)
so the report is derived from what the run ACTUALLY emitted, not from the declared intent.

Exercised against a large file and a multi-step chain, per Track 4's bar.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_track_4_3.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-t43.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import compute_mode, scale  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0
XL = "application/octet-stream"
BIG_ROWS = 120_000


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def big_workbook() -> bytes:
    n = BIG_ROWS
    df = pd.DataFrame({
        "Email": [f"user{i % (n // 2)}@example.com" for i in range(n)],
        "Qty": [(i % 9) + 1 for i in range(n)],
        "Price": [float((i * 7) % 500) + 1.0 for i in range(n)],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def seed(wb: bytes) -> str:
    sid = f"t43-{uuid.uuid4().hex[:8]}"
    r = client.post("/inspect", data={"session_id": sid},
                    files=[("files", ("big.xlsx", wb, XL))])
    assert r.status_code == 200, r.text
    return sid


def run_plan(sid: str, ops: list) -> dict:
    r = client.post("/execute", data={"session_id": sid,
                                      "plan": json.dumps({"operations": ops})})
    assert r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}"
    return r.json()


def run() -> None:
    print(f"  (building a {BIG_ROWS:,}-row workbook…)")
    wb = big_workbook()
    print(f"  (built, {len(wb) / 1_000_000:.1f} MB)\n")

    # --- the declared rule ------------------------------------------------------------
    check("add_formula_column is declared as writing a formula",
          compute_mode.declared_mode("add_formula_column") == compute_mode.FORMULA)
    check("lookup is declared as writing a formula",
          compute_mode.declared_mode("lookup") == compute_mode.FORMULA)
    check("sort defaults to computed values",
          compute_mode.declared_mode("sort") == compute_mode.VALUES)
    check("an unknown/new action defaults to values, the safe answer",
          compute_mode.declared_mode("some_future_op") == compute_mode.VALUES)

    # --- VALUES: a plain chain on a large file ----------------------------------------
    scale.RESULT_CACHE.clear()
    sid = seed(wb)
    body = run_plan(sid, [
        {"action": "remove_duplicates", "columns": ["Email"]},
        {"action": "sort", "columns": ["Price"], "orders": ["desc"]},
    ])
    comp = body.get("computation") or {}
    check("every run reports a `computation` block", bool(comp), f"got {body.get('computation')!r}")
    check("a value-only chain reports both steps as values",
          [s["mode"] for s in comp.get("steps", [])] == ["values", "values"],
          f"modes={[s.get('mode') for s in comp.get('steps', [])]}")
    check("a value-only chain says nothing recalculates",
          all(s["recalculates"] is False for s in comp.get("steps", [])),
          "a value step claimed it recalculates")
    check("the summary warns values will not update by themselves",
          "won't update" in comp.get("summary", "") or "will NOT update" in comp.get("summary", ""),
          f"summary={comp.get('summary')!r}")
    check("no formula columns are claimed when none were written",
          comp.get("formula_columns") == [] and comp.get("any_formulas") is False,
          f"cols={comp.get('formula_columns')}")

    # --- FORMULA: a real formula column on the same large file ------------------------
    scale.RESULT_CACHE.clear()
    sid2 = seed(wb)
    body2 = run_plan(sid2, [
        {"action": "add_formula_column", "name": "Revenue", "formula": "{Qty} * {Price}"},
    ])
    comp2 = body2.get("computation") or {}
    steps2 = comp2.get("steps", [])
    check("a formula column is reported as a formula",
          len(steps2) == 1 and steps2[0]["mode"] == "formula",
          f"modes={[s.get('mode') for s in steps2]}")
    check("and is reported as recalculating",
          bool(steps2) and steps2[0]["recalculates"] is True,
          "a formula step said it does not recalculate")
    check("the column that got the formula is named",
          "Revenue" in (comp2.get("formula_columns") or []),
          f"cols={comp2.get('formula_columns')}")
    check("the explanation tells the user it updates when they edit the inputs",
          bool(steps2) and "recalculat" in steps2[0]["explanation"],
          f"explanation={steps2[0]['explanation'] if steps2 else None!r}")

    # --- MIXED: the case most likely to mislead ---------------------------------------
    scale.RESULT_CACHE.clear()
    sid3 = seed(wb)
    body3 = run_plan(sid3, [
        {"action": "add_formula_column", "name": "Revenue", "formula": "{Qty} * {Price}"},
        {"action": "sort", "columns": ["Qty"], "orders": ["desc"]},
    ])
    comp3 = body3.get("computation") or {}
    modes3 = [s["mode"] for s in comp3.get("steps", [])]
    check("a mixed chain distinguishes the formula step from the value step",
          modes3 == ["formula", "values"], f"modes={modes3}")
    check("the mixed summary says plainly that some parts recalculate and some don't",
          "Mixed" in comp3.get("summary", ""), f"summary={comp3.get('summary')!r}")

    # --- HONESTY: the report follows what was EMITTED, not what was intended ----------
    # An operation declared FORMULA that emitted no formula directive must be reported as
    # values — the file contains values, and saying otherwise would be the harmful lie.
    only_intent = compute_mode.describe(
        [{"action": "add_formula_column", "name": "X", "formula": "{Qty}*2"}],
        render_ops=[],  # nothing was actually emitted
    )
    check("a declared formula that emitted nothing is reported as VALUES",
          only_intent["steps"][0]["mode"] == "values",
          f"mode={only_intent['steps'][0]['mode']}")
    check("and the mismatch is surfaced rather than silently reconciled",
          only_intent["steps"][0].get("declared_mode") == "formula",
          f"entry={only_intent['steps'][0]}")

    # --- the rule is inspectable without running anything -----------------------------
    r = client.get("/operations/compute-mode")
    check("GET /operations/compute-mode states the rule", r.status_code == 200,
          f"HTTP {r.status_code}")
    doc = r.json() if r.status_code == 200 else {}
    check("the rule names the formula-writing operations",
          doc.get("formula_operations", {}).get("add_formula_column") == "formula",
          f"got {doc.get('formula_operations')}")
    check("and states that everything else writes values",
          doc.get("default") == "values", f"default={doc.get('default')}")
    check("and reports the settings that change the answer",
          "lookup_style" in (doc.get("settings") or {})
          and "pivot_style" in (doc.get("settings") or {}),
          f"settings={doc.get('settings')}")

    # --- lookup wording follows the configured style ----------------------------------
    text = compute_mode._explain("lookup", compute_mode.FORMULA, ["Email"])
    expected = "XLOOKUP" if compute_mode._lookup_is_xlookup() else "INDEX/MATCH"
    check(f"the lookup explanation names the configured style ({expected})",
          expected in text, f"text={text!r}")

    # --- an empty plan says nothing rather than something wrong -----------------------
    empty = compute_mode.describe([], [])
    check("an empty plan produces no claims at all",
          empty["steps"] == [] and empty["summary"] == "", f"got {empty}")


if __name__ == "__main__":
    print("TRACK 4 item 3 — formula vs computed value (120k rows + multi-step)\n")
    run()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
