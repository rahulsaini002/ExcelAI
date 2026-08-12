"""Engine Phase 1.8 — Goal Seek / inverse what-if (Area 11).

Solve for ONE unknown, written {var} in the Phase-1.1 formula grammar, so the formula
hits a target:  "what price gives 1,000,000 revenue at current volume?"  ->
formula "{var} * SUM({Qty:})", target 1000000.

Trusted, server-side numeric solve (no model math):
  1. secant from two probes — exact for linear formulas, which most goal-seeks are;
  2. else expanding-bracket search (|X| up to 1e9) + bisection on a sign change;
  3. no sign change anywhere -> an HONEST "the target may be unreachable" decline.

The data is never changed: the answer lands in the note, and the working (formula,
target, found value, achieved value, method) is saved on a small "Goal Seek" sheet.
Series results are summed (a row-wise formula's natural aggregate); v1 is strictly
single-variable — two unknowns get a clear decline.
"""
from __future__ import annotations

import math
import re

import pandas as pd

from .base import OperationError

_VAR = re.compile(r"\{\s*var\s*\}", re.IGNORECASE)
_OTHER_UNKNOWN = re.compile(r"\{\s*var\d+\s*\}", re.IGNORECASE)


def goal_seek(df: pd.DataFrame, op: dict, tables: dict, eval_formula) -> tuple[pd.DataFrame, str, dict]:
    formula = (op.get("formula") or "").strip()
    target = op.get("target")
    var_label = (op.get("variable_name") or "the value").strip()

    if not formula:
        raise OperationError(
            'Goal Seek needs a formula with {var} as the unknown — e.g. '
            '"{var} * SUM({Qty:})" with a target.'
        )
    if _OTHER_UNKNOWN.search(formula) or len(set(m.lower() for m in re.findall(r"\{\s*(var\w*)\s*\}", formula, re.I))) > 1:
        raise OperationError(
            "Goal Seek can solve for ONE unknown at a time (v1). Pick a single variable "
            "to solve for and fix the others, or run two goal seeks."
        )
    if not _VAR.search(formula):
        raise OperationError(
            "The formula needs {var} marking the value to solve for — e.g. "
            '"{var} * SUM({Qty:})".'
        )
    try:
        target = float(target)
    except (TypeError, ValueError):
        raise OperationError("What target should the formula reach? Give me a number.")

    def f(x: float) -> float:
        got = eval_formula(formula, df, tables, None, {"var": x})
        if isinstance(got, pd.Series):
            nums = pd.to_numeric(got, errors="coerce")
            # A per-row formula sums its contributions — but a BROADCAST CONSTANT
            # (every row identical, e.g. POWER({var},2) * SUM({Qty:})) IS the value;
            # summing it would multiply by the row count.
            got = float(nums.iloc[0]) if nums.nunique(dropna=True) <= 1 else float(nums.sum())
        val = float(got)
        if not math.isfinite(val):
            raise OperationError(
                f"The formula doesn't give a usable number when {var_label} is {x:g} — "
                "check the columns it uses are numeric."
            )
        return val

    def g(x: float) -> float:
        return f(x) - target

    tol = max(1e-9, abs(target) * 1e-9)
    method = None
    solution = None

    # 1) Secant from two probes — exact when the formula is linear in {var}.
    g1, g2 = g(1.0), g(2.0)
    if g2 != g1:
        x = 1.0 - g1 * (2.0 - 1.0) / (g2 - g1)
        if math.isfinite(x):
            for _ in range(60):  # polish (handles mild nonlinearity too)
                gx = g(x)
                if abs(gx) <= tol:
                    solution, method = x, "secant"
                    break
                x2 = x + max(1e-6, abs(x) * 1e-4)
                gx2 = g(x2)
                if gx2 == gx:
                    break
                x = x - gx * (x2 - x) / (gx2 - gx)
                if not math.isfinite(x):
                    break

    # 2) Bracket + bisection: scan magnitudes for a sign change.
    if solution is None:
        probes = [0.0] + [s * 10.0**k for k in range(0, 10) for s in (1, -1)]
        vals = []
        for p in probes:
            try:
                vals.append((p, g(p)))
            except OperationError:
                continue
        vals.sort(key=lambda t: t[0])
        bracket = None
        for (a, ga), (b, gb) in zip(vals, vals[1:]):
            if ga == 0:
                solution, method = a, "exact probe"
                break
            if ga * gb < 0:
                bracket = (a, b, ga)
                break
        if solution is None and bracket:
            a, b, ga = bracket
            for _ in range(200):
                mid = (a + b) / 2
                gm = g(mid)
                if abs(gm) <= tol or (b - a) / 2 < 1e-12:
                    solution, method = mid, "bisection"
                    break
                if ga * gm < 0:
                    b = mid
                else:
                    a, ga = mid, gm
        if solution is None and not bracket:
            lo = min(v for _, v in vals) + target
            hi = max(v for _, v in vals) + target
            raise OperationError(
                f"I couldn't find any value of {var_label} (searched |x| up to 1e9) that "
                f"makes the formula reach {target:,.2f} — over that range the formula "
                f"stays between {lo:,.2f} and {hi:,.2f}. The target may be unreachable "
                "with the current data."
            )
    if solution is None:
        raise OperationError(
            f"The search for {var_label} didn't converge — the formula may be too "
            "irregular around the target. Try a different formulation."
        )

    achieved = f(solution)
    directive = {
        "type": "goal_seek_sheet",
        "rows": [
            ["Goal Seek", ""],
            ["Solving for", var_label],
            ["Formula", formula],
            ["Target", target],
            ["Found value", solution],
            ["Achieved", achieved],
            ["Method", method],
        ],
    }
    note = (f"Goal Seek: {var_label} = {solution:,.4f} makes the formula reach "
            f"{achieved:,.2f} (target {target:,.2f}, {method}). The working is on the "
            "'Goal Seek' sheet; your data is unchanged.")
    return df, note, directive
