"""Self-healing workbooks (Phase 5.7).

When a formula column references a column that has since been dropped or renamed, the formula
is quietly broken — a re-run would fail. Self-healing DETECTS those broken references (using
the session's formula registry from Phase 4.8) and proposes a repair, preferring the safest
fix:

  1. REMAP  — the referenced column was likely RENAMED. If a current column is a close fuzzy
     match ("Revenue" → "Revenues"), rewrite the formula to point at it. Safe: it recomputes
     from data that's still present.
  2. RESTORE — the column was DROPPED. If an earlier VERSION (Phase 3.2 history) still has it,
     bring it back from there. This is the "repair via version history" the DoD calls for.
  3. UNREPAIRABLE — no fuzzy match and not in any prior version: reported honestly, never
     guessed.

Pure functions over {column: formula} + column sets, so they're trivially testable; main.py
orchestrates the actual restore-from-history and formula re-run against the live session.
"""
from __future__ import annotations

import difflib
import re

from .guardrails import _refs_in


def broken_references(formulas: dict, current_columns) -> dict[str, list[str]]:
    """{formula_column: [referenced columns that no longer exist]} for the current data."""
    cur = {str(c) for c in current_columns}
    out: dict[str, list[str]] = {}
    for col, formula in formulas.items():
        missing = [r for r in sorted(_refs_in(formula)) if r not in cur]
        if missing:
            out[str(col)] = missing
    return out


def _find_in_history(colname: str, history: list) -> int | None:
    """`history` = [(version_index, columns_set), …] NEWEST-first among past versions. Returns
    the newest version index that still had `colname`, or None."""
    for vi, cols in history:
        if colname in cols:
            return vi
    return None


def plan_repairs(formulas: dict, current_columns, history: list | None = None, cutoff: float = 0.8) -> list[dict]:
    """One repair proposal per broken reference: remap (renamed → fuzzy match), restore (from
    a prior version), or unrepairable. Remap is preferred (recomputes from present data)."""
    history = history or []
    cur = [str(c) for c in current_columns]
    repairs: list[dict] = []
    for col, missing in broken_references(formulas, current_columns).items():
        for ref in missing:
            match = difflib.get_close_matches(ref, cur, n=1, cutoff=cutoff)
            if match:
                repairs.append({"formula_column": col, "missing": ref, "action": "remap", "to": match[0]})
                continue
            vi = _find_in_history(ref, history)
            if vi is not None:
                repairs.append({"formula_column": col, "missing": ref, "action": "restore",
                                "column": ref, "from_version": vi})
            else:
                repairs.append({"formula_column": col, "missing": ref, "action": "unrepairable",
                                "column": ref})
    return repairs


def apply_remaps(formulas: dict, repairs: list[dict]) -> tuple[dict, list[str]]:
    """Rewrite formula templates for every 'remap' repair (replace {missing} / {missing:} with
    {to} / {to:}). Returns (new_formulas, changed_formula_columns)."""
    new = dict(formulas)
    changed: list[str] = []
    for r in repairs:
        if r.get("action") != "remap":
            continue
        col = r["formula_column"]
        if col not in new:
            continue
        pat = r"\{\s*" + re.escape(r["missing"]) + r"\s*(:?)\}"
        rewritten = re.sub(pat, lambda m: "{" + r["to"] + m.group(1) + "}", new[col])
        if rewritten != new[col]:
            new[col] = rewritten
            if col not in changed:
                changed.append(col)
    return new, changed
