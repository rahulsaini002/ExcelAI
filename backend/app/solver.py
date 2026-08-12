"""Optimization / Solver (Phase 5.10).

Excel-Solver-equivalent constrained optimization: maximize or minimize a linear objective
over decision variables, subject to ≤ / ≥ / = constraints and per-variable bounds — the
classic product-mix / resource-allocation / blending problem. Optional integer variables make
it a MILP (Solver's "integer" option). Backed by scipy.optimize.linprog (HiGHS).

Honest by construction: it reports the TRUE outcome — optimal / infeasible / unbounded — and
never fabricates a solution when none exists. The returned objective value is recomputed from
the solution (not read back from the solver), so it always matches the reported variables.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog

_LE = {"<=", "<", "le", "leq"}
_GE = {">=", ">", "ge", "geq"}
_EQ = {"==", "=", "eq"}


class SolverError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def solve_linear(objective: dict, constraints: list | None = None, bounds: dict | None = None,
                 sense: str = "max", integer: list | None = None) -> dict:
    """Solve a linear program.

    objective   {var: coefficient} to maximize/minimize.
    constraints [{"coeffs": {var: c}, "op": "<="|">="|"==", "rhs": number}, …]
    bounds      {var: [low, high]} (high null = ∞). Default per variable: [0, ∞) — the usual
                "assume non-negative" Solver default.
    sense       "max" (default) or "min".
    integer     variables constrained to whole numbers (→ MILP).
    """
    if not objective:
        raise SolverError("An objective needs at least one variable.")
    constraints = constraints or []
    sense = (sense or "max").strip().lower()
    if sense not in ("max", "min"):
        raise SolverError("Sense must be 'max' or 'min'.")

    # Stable variable order: objective first, then any new ones from constraints/bounds.
    variables: list[str] = list(objective.keys())
    for con in constraints:
        for v in (con.get("coeffs") or {}):
            if v not in variables:
                variables.append(v)
    for v in (bounds or {}):
        if v not in variables:
            variables.append(v)
    idx = {v: i for i, v in enumerate(variables)}
    nvar = len(variables)

    c = np.zeros(nvar)
    for v, coef in objective.items():
        c[idx[v]] = float(coef)
    if sense == "max":
        c = -c  # linprog minimizes; maximize by minimizing the negative

    A_ub, b_ub, A_eq, b_eq = [], [], [], []
    for con in constraints:
        row = np.zeros(nvar)
        for v, coef in (con.get("coeffs") or {}).items():
            # A variable that appears only in constraints is valid LP (0 objective coeff) —
            # it was already collected into `variables` above, so it has an index here.
            row[idx[v]] = float(coef)
        op = str(con.get("op") or "<=").strip().lower()
        rhs = float(con.get("rhs", 0))
        if op in _LE:
            A_ub.append(row); b_ub.append(rhs)
        elif op in _GE:
            A_ub.append(-row); b_ub.append(-rhs)   # a ≥ b  ⇔  -a ≤ -b
        elif op in _EQ:
            A_eq.append(row); b_eq.append(rhs)
        else:
            raise SolverError(f"Unknown constraint operator '{op}'.")

    bnds = []
    for v in variables:
        b = (bounds or {}).get(v)
        lo = 0 if not b or b[0] is None else b[0]
        hi = None if not b or len(b) < 2 or b[1] is None else b[1]
        bnds.append((lo, hi))

    kwargs: dict = {"c": c, "bounds": bnds, "method": "highs"}
    if A_ub:
        kwargs["A_ub"], kwargs["b_ub"] = np.array(A_ub), np.array(b_ub)
    if A_eq:
        kwargs["A_eq"], kwargs["b_eq"] = np.array(A_eq), np.array(b_eq)
    if integer:
        want = set(integer)
        kwargs["integrality"] = np.array([1 if v in want else 0 for v in variables])

    try:
        res = linprog(**kwargs)
    except Exception as exc:  # bad problem shape → a clean 400, never a 500 traceback
        raise SolverError(f"Couldn't set up the problem: {exc}")

    return _result(res, variables, objective, sense, bool(integer))


def _result(res, variables: list[str], objective: dict, sense: str, integer: bool) -> dict:
    if res.status == 0 and res.x is not None:
        sol = {v: round(float(res.x[i]), 6) for i, v in enumerate(variables)}
        # Recompute the objective from the solution so it can never disagree with the vars.
        obj = sum(float(objective.get(v, 0)) * sol[v] for v in variables)
        return {
            "outcome": "optimal", "sense": sense, "integer": integer,
            "solution": sol, "objective_value": round(obj, 6),
            "message": "Optimal solution found.",
        }
    if res.status == 2:
        return {"outcome": "infeasible", "solution": None, "objective_value": None,
                "message": "No solution satisfies all of the constraints."}
    if res.status == 3:
        return {"outcome": "unbounded", "solution": None, "objective_value": None,
                "message": "The objective can improve without limit — add a bounding constraint."}
    return {"outcome": "no_solution", "solution": None, "objective_value": None,
            "message": str(getattr(res, "message", "") or "The solver couldn't find a solution.")}
