"""Formula vs computed value — the rule, and telling the user which was used.

ENHANCEMENT TRACK 4, item 3.

THE DISTINCTION THAT MATTERS TO A USER. Sumio can put two very different things in a
cell:

  a live Excel FORMULA   `=B2*C2`. Recalculates when they edit the sheet. Depends on the
                         columns it references still being there, and on their Excel
                         being new enough for the function used.
  a computed VALUE       the number Sumio worked out. Frozen: correct as of the moment it
                         ran, and it will NOT update if they change the inputs.

Both are legitimate; neither is "better". What is NOT legitimate is leaving the user to
guess, because the two behave completely differently the moment they touch the file — and
someone who assumes a total recalculates, when it doesn't, will quietly ship a wrong
number.

THE RULE, per operation:
  add_formula_column   FORMULA — the point of the feature is a working formula
  lookup               FORMULA — writes INDEX/MATCH (or XLOOKUP, per SUMIO_LOOKUP_STYLE)
                       so the join keeps working in Excel
  pivot_summary        DEPENDS on SUMIO_PIVOT_STYLE: "static" (default) writes values that
                       work everywhere; "live" writes a GROUPBY/PIVOTBY spill formula that
                       needs Microsoft 365
  everything else      VALUES — sorting, filtering, dedupe, aggregation, statistics,
                       forecasts and the rest are computed by trusted Python and written
                       as results

REPORTED FROM WHAT ACTUALLY HAPPENED. The declared rule above is the intent; the report is
built from the render directives the run really emitted, so a mismatch surfaces as the
truth rather than the intention. If an operation claims FORMULA but emitted no formula
directive, this says values were written — because that is what the file contains.
"""
from __future__ import annotations

from . import config

FORMULA = "formula"
VALUES = "values"

# Render-directive types that mean "a live Excel formula was written into the sheet".
# This is the ground truth: these directives are what the serializer turns into formulas.
_FORMULA_DIRECTIVES = {"formula", "lookup", "pivot_formula"}

# The declared rule. Anything absent is VALUES — the safe default, and true of the great
# majority of operations.
_DECLARED: dict[str, str] = {
    "add_formula_column": FORMULA,
    "lookup": FORMULA,
}

_CONDITIONAL = {"pivot_summary"}


def declared_mode(action: str) -> str:
    """What this operation is DESIGNED to write, before a run happens."""
    if action in _CONDITIONAL:
        return FORMULA if _pivot_is_live() else VALUES
    return _DECLARED.get(action, VALUES)


def _pivot_is_live() -> bool:
    return str(getattr(config, "PIVOT_STYLE", "static")).strip().lower() == "live"


def _formula_columns(render_ops: list[dict] | None) -> dict[str, list[str]]:
    """Which columns actually got a formula, grouped by the directive that made them."""
    out: dict[str, list[str]] = {}
    for d in render_ops or []:
        if not isinstance(d, dict):
            continue
        kind = d.get("type")
        if kind not in _FORMULA_DIRECTIVES:
            continue
        col = d.get("column") or d.get("new_column") or d.get("name")
        out.setdefault(kind, [])
        if col:
            out[kind].append(str(col))
    return out


def describe(operations: list[dict] | None, render_ops: list[dict] | None) -> dict:
    """Per-step disclosure plus a one-line summary, for the run's response.

    `recalculates` is the field a user actually cares about: does this update when they
    change the data? It is stated plainly rather than left implicit in the word "formula".
    """
    made = _formula_columns(render_ops)
    formula_cols = [c for cols in made.values() for c in cols]
    any_formula = bool(made)

    steps: list[dict] = []
    for i, op in enumerate(operations or [], 1):
        action = (op or {}).get("action") if isinstance(op, dict) else None
        declared = declared_mode(action or "")
        # Ground truth wins: only claim a formula if one was really emitted for this run.
        actual = FORMULA if (declared == FORMULA and any_formula) else VALUES
        entry = {
            "step": i,
            "action": action,
            "mode": actual,
            "recalculates": actual == FORMULA,
            "explanation": _explain(action or "", actual, formula_cols),
        }
        if declared != actual:
            # Visible rather than silently reconciled — if this ever appears, either the
            # operation changed or the registry is stale, and both are worth noticing.
            entry["declared_mode"] = declared
        steps.append(entry)

    return {
        "steps": steps,
        "any_formulas": any_formula,
        "formula_columns": formula_cols,
        "summary": _summary(steps, formula_cols),
    }


def _explain(action: str, mode: str, formula_cols: list[str]) -> str:
    if mode == FORMULA:
        where = f" in {', '.join(formula_cols)}" if formula_cols else ""
        if action == "lookup":
            style = "XLOOKUP" if _lookup_is_xlookup() else "INDEX/MATCH"
            return (
                f"Wrote a live {style} formula{where}, so the lookup keeps working in "
                "Excel and updates if the source data changes."
            )
        if action == "pivot_summary":
            return (
                f"Wrote a live spill formula{where}. It recalculates in Excel, but needs "
                "Microsoft 365 — older Excel will show an error."
            )
        return (
            f"Wrote a live Excel formula{where}, so it recalculates when you edit the "
            "numbers it depends on."
        )
    return (
        "Wrote computed values. They are correct as of this run and will NOT update by "
        "themselves if you change the data afterwards."
    )


def _lookup_is_xlookup() -> bool:
    return str(getattr(config, "LOOKUP_STYLE", "index_match")).strip().lower() == "xlookup"


def _summary(steps: list[dict], formula_cols: list[str]) -> str:
    """One line the UI can show without the user opening anything."""
    if not steps:
        return ""
    n_formula = sum(1 for s in steps if s["mode"] == FORMULA)
    if n_formula == 0:
        return (
            "Everything here is computed values — correct as of now, and they won't "
            "update by themselves if the data changes."
        )
    cols = ", ".join(formula_cols)
    if n_formula == len(steps):
        return f"Live Excel formulas ({cols}) — these recalculate when you edit the data."
    return (
        f"Mixed: live Excel formulas in {cols} (these recalculate), and computed values "
        "elsewhere (these don't)."
    )
