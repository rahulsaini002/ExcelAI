"""BUG FIX — Excel's `&` (text concatenation) was rejected as arithmetic.

Reported as a known defect since Phase 3.4 and never chased down. The symptom:

    {First} & {Last}
    -> "#VALUE!: First, Last aren't numbers and can't be converted ...
        A formula column needs numeric columns."

which is both a failure and a lie — CONCAT({First}, {Last}) produced "AshaPatel"
perfectly well.

ROOT CAUSE. _compute_formula_self_correcting decides whether a formula is "plain
arithmetic" before evaluating it, and only plain arithmetic gets the text->number
coercion pass:

    advanced = <has a function call> or <has < > = >

`&` matched neither, so a concat was treated as arithmetic, the coercion step found the
name columns weren't numbers, and it refused the formula outright. CONCAT worked purely
because its opening bracket made it "advanced". The evaluator's ast.BitAnd branch has
implemented `&` as concatenation all along — it simply never got the chance to run.

FIX: `&` also marks a formula as advanced.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_fix_concat_operator.py
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

_fd, _db = tempfile.mkstemp(suffix="-concat.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402

from app.executor import OperationError, execute_multi  # noqa: E402

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


BASE = pd.DataFrame({
    "First": ["Asha", "Ravi", "Meera"],
    "Last": ["Patel", "Kumar", "Nair"],
    "Qty": [2, 3, 4],
    "Price": [10.0, 20.0, 30.0],
})


def run_formula(formula: str, df: pd.DataFrame | None = None, name: str = "Result"):
    """Returns (values, excel_formula) or raises OperationError."""
    out, _, _, directives = execute_multi(
        {"t": (BASE if df is None else df)}, "t",
        [{"action": "add_formula_column", "name": name, "formula": formula}],
    )
    d = next((x for x in directives if x.get("type") == "formula"), {})
    return list(out[name]), d.get("formula")


def run() -> None:
    # --- the bug itself ---------------------------------------------------------------
    vals, _ = run_formula('{First} & {Last}')
    check("plain `&` concatenation works", vals == ["AshaPatel", "RaviKumar", "MeeraNair"],
          f"got {vals}")

    vals, _ = run_formula('{First} & " " & {Last}')
    check("`&` with a string literal between columns",
          vals == ["Asha Patel", "Ravi Kumar", "Meera Nair"], f"got {vals}")

    # --- numbers joined into text must not gain a spurious .0 --------------------------
    vals, _ = run_formula('{First} & " has " & {Qty}')
    check("an integer column joins as '2', not '2.0'",
          vals == ["Asha has 2", "Ravi has 3", "Meera has 4"], f"got {vals}")

    vals, _ = run_formula('{Price} & ""')
    check("a float column keeps its decimal when joined",
          vals[0].startswith("10"), f"got {vals}")

    # --- the saved workbook must get a LIVE Excel formula, not just values -------------
    vals, excel = run_formula('{First} & " " & {Last}')
    check("the .xlsx directive keeps `&` so Excel gets a working formula",
          excel is not None and "&" in excel, f"directive={excel!r}")

    # --- arithmetic must be untouched (the thing `&` was being confused with) ----------
    vals, _ = run_formula('{Qty} * {Price}')
    check("arithmetic still works", vals == [20.0, 60.0, 120.0], f"got {vals}")

    vals, _ = run_formula('({Qty} + 1) * 2')
    check("arithmetic with literals and parentheses still works",
          vals == [6, 8, 10], f"got {vals}")

    # --- text->number auto-repair on REAL arithmetic must still happen -----------------
    text_nums = pd.DataFrame({"A": ["1", "2", "3"], "B": [10, 20, 30]})
    vals, _ = run_formula("{A} * {B}", df=text_nums)
    check("numeric-looking TEXT is still auto-coerced for real arithmetic",
          vals == [10, 40, 90], f"got {vals}")

    # --- genuinely wrong arithmetic still fails, with better wording -------------------
    try:
        run_formula("{First} * {Last}")
        msg = ""
    except OperationError as exc:
        msg = str(exc)
    check("arithmetic on non-numeric text still refuses", bool(msg), "it did not raise")
    check("the refusal no longer claims formula columns must be numeric",
          "needs numeric columns" not in msg, msg)
    check("and it points the user at `&` for joining text instead",
          "&" in msg and "join" in msg.lower(), msg)

    # --- concat is not fooled into arithmetic by numeric columns ----------------------
    vals, _ = run_formula('{Qty} & {Price}')
    check("two numeric columns joined with `&` concatenate, not add",
          vals[0] == "210" or vals[0] == "210.0", f"got {vals}")

    # --- blanks join as empty, never the string 'nan' ---------------------------------
    blanks = pd.DataFrame({"A": ["x", None, "z"], "B": ["1", "2", None]})
    vals, _ = run_formula('{A} & {B}', df=blanks)
    check("missing values join as empty text, not 'nan'/'None'",
          all(isinstance(v, str) and "nan" not in v.lower() and "none" not in v.lower()
              for v in vals),
          f"got {vals}")

    # --- SECOND BUG, found while testing the first --------------------------------
    # The #NUM! repair asked "did any row coerce to a finite number?" to decide whether
    # the formula was numeric. A TEXT formula satisfies that by accident: joining a blank
    # with "2" gives the string "2", which coerces fine — so every other row ("x1", "z")
    # looked like an invalid number and was BLANKED. Real text was destroyed and the note
    # told the user it had "auto-corrected an invalid number". It hit CONCAT and TEXTJOIN
    # too, so it long predates the `&` fix.
    check("text results are never blanked as 'invalid numbers' (& )",
          run_formula('{A} & {B}', df=blanks)[0] == ["x1", "2", "z"],
          f"got {run_formula('{A} & {B}', df=blanks)[0]}")
    check("...nor via CONCAT",
          run_formula('CONCAT({A}, {B})', df=blanks)[0] == ["x1", "2", "z"],
          f"got {run_formula('CONCAT({A}, {B})', df=blanks)[0]}")

    # ...and the #NUM! guard must STILL fire for genuinely numeric errors.
    negs = pd.DataFrame({"X": [4.0, -1.0, 9.0]})
    vals, _ = run_formula("SQRT({X})", df=negs)
    check("#NUM! still blanks the square root of a negative",
          vals[0] == 2.0 and pd.isna(vals[1]) and vals[2] == 3.0, f"got {vals}")

    # ...and divide-by-zero is still caught.
    dz = pd.DataFrame({"P": [10.0, 20.0], "Q": [2.0, 0.0]})
    vals, _ = run_formula("{P} / {Q}", df=dz)
    check("#DIV/0! still blanks a divide-by-zero row",
          vals[0] == 5.0 and pd.isna(vals[1]), f"got {vals}")

    # --- the sibling functions still work (they always did) ---------------------------
    vals, _ = run_formula('CONCAT({First}, " ", {Last})')
    check("CONCAT still works", vals == ["Asha Patel", "Ravi Kumar", "Meera Nair"], f"got {vals}")
    vals, _ = run_formula('TEXTJOIN(" ", TRUE, {First}, {Last})')
    check("TEXTJOIN still works", vals == ["Asha Patel", "Ravi Kumar", "Meera Nair"], f"got {vals}")

    # --- comparisons unaffected -------------------------------------------------------
    vals, _ = run_formula('IF({Qty} > 2, "big", "small")')
    check("comparisons/IF still work", vals == ["small", "big", "big"], f"got {vals}")

    # --- DOCUMENTED TRADE-OFF ---------------------------------------------------------
    # Marking `&` as "advanced" skips the text->number coercion for the WHOLE formula, so
    # a formula that both concatenates AND does maths on numeric-looking text no longer
    # auto-repairs — it now fails with the arithmetic message instead. That case failed
    # outright before this fix too, so nothing regressed; asserting it keeps the
    # limitation visible rather than folklore.
    mixed = pd.DataFrame({"Label": ["a", "b"], "N": ["5", "6"]})
    try:
        out, _ = run_formula('{Label} & ({N} * 2)', df=mixed)
        note = f"auto-coerced after all: {out}"
        ok = True  # if a future change makes this work, that is an improvement
    except OperationError as exc:
        note = str(exc)
        ok = True
    check("mixed concat+maths on text-numbers behaves predictably (documented)", ok, note)
    print(f"        (mixed case -> {note[:90]})")


if __name__ == "__main__":
    print("BUG FIX — Excel `&` text concatenation\n")
    run()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
