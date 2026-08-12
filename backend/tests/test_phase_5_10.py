"""ENGINE PHASE 5.10 — optimization / Solver-class (BUILD).

Excel-Solver-equivalent constrained optimization via scipy.optimize.linprog (HiGHS):
maximize/minimize a linear objective over decision variables subject to ≤/≥/= constraints
and bounds, with optional integer variables (MILP). This suite proves the math AND the
honesty:

  optimal      the classic product-mix LP returns the textbook optimum (x=2, y=6 → 36).
  min / eq     minimization and equality constraints solve correctly.
  bounds       per-variable bounds are respected.
  integer      an integer restriction yields a whole-number optimum (MILP).
  honest       infeasible → 'infeasible', unbounded → 'unbounded' (never a fabricated
               solution); the objective value is recomputed from the solution so it always
               matches the variables.
  end to end   POST /solve.

Requires scipy (added to requirements). No llm.py change → no schema/serving/quota risk; no
battery rows (pure-math endpoint).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_5_10.py
"""
from __future__ import annotations

import json
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

_fd, _db = tempfile.mkstemp(suffix="-p510.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import solver as S  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def approx(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


print("ENGINE PHASE 5.10 — optimization / Solver\n")

# ===================== classic product-mix LP =====================
# max 3x + 5y  s.t.  x ≤ 4,  2y ≤ 12,  3x + 2y ≤ 18  →  x=2, y=6, obj=36
lp = S.solve_linear(
    {"x": 3, "y": 5},
    [{"coeffs": {"x": 1}, "op": "<=", "rhs": 4},
     {"coeffs": {"y": 2}, "op": "<=", "rhs": 12},
     {"coeffs": {"x": 3, "y": 2}, "op": "<=", "rhs": 18}],
    sense="max")
check("LP: optimal outcome", lp["outcome"] == "optimal", str(lp))
check("LP: textbook solution x=2, y=6", approx(lp["solution"]["x"], 2) and approx(lp["solution"]["y"], 6), str(lp["solution"]))
check("LP: objective value = 36", approx(lp["objective_value"], 36), str(lp["objective_value"]))
check("LP: objective value is recomputed from the solution (self-consistent)",
      approx(lp["objective_value"], 3 * lp["solution"]["x"] + 5 * lp["solution"]["y"]), str(lp))

# ===================== minimization + equality =====================
mn = S.solve_linear({"x": 1, "y": 1}, [{"coeffs": {"x": 1, "y": 1}, "op": ">=", "rhs": 10}], sense="min")
check("min: objective reaches the ≥ bound (10)", mn["outcome"] == "optimal" and approx(mn["objective_value"], 10), str(mn))
eq = S.solve_linear({"x": 1, "y": 1}, [{"coeffs": {"x": 1, "y": 1}, "op": "==", "rhs": 7},
                                       {"coeffs": {"x": 1}, "op": "<=", "rhs": 5}], sense="max")
check("equality constraint is honored (x+y == 7)", eq["outcome"] == "optimal" and approx(eq["solution"]["x"] + eq["solution"]["y"], 7), str(eq))

# ===================== bounds =====================
bd = S.solve_linear({"x": 1}, [], bounds={"x": [2, 5]}, sense="max")
check("upper bound is respected (x ≤ 5)", bd["outcome"] == "optimal" and approx(bd["solution"]["x"], 5), str(bd))
bd2 = S.solve_linear({"x": 1}, [], bounds={"x": [2, 5]}, sense="min")
check("lower bound is respected (x ≥ 2)", approx(bd2["solution"]["x"], 2), str(bd2))

# ===================== integer (MILP) =====================
milp = S.solve_linear({"x": 1, "y": 1}, [{"coeffs": {"x": 1, "y": 1}, "op": "<=", "rhs": 3.5}],
                      sense="max", integer=["x", "y"])
check("MILP: integer optimum is whole-numbered (3, not 3.5)", approx(milp["objective_value"], 3) and milp["integer"] is True, str(milp))
check("MILP: the solution variables are whole numbers", all(float(v).is_integer() for v in milp["solution"].values()), str(milp["solution"]))

# ===================== honesty: infeasible / unbounded =====================
inf = S.solve_linear({"x": 1}, [{"coeffs": {"x": 1}, "op": ">=", "rhs": 5}, {"coeffs": {"x": 1}, "op": "<=", "rhs": 3}])
check("contradictory constraints → 'infeasible', no fabricated solution", inf["outcome"] == "infeasible" and inf["solution"] is None, str(inf))
unb = S.solve_linear({"x": 1}, [], sense="max")  # x unbounded above (default [0, ∞))
check("no bounding constraint → 'unbounded', no fabricated solution", unb["outcome"] == "unbounded" and unb["solution"] is None, str(unb))

# ===================== input validation =====================
for bad, why in [
    (lambda: S.solve_linear({}), "empty objective"),
    (lambda: S.solve_linear({"x": 1}, sense="sideways"), "bad sense"),
    (lambda: S.solve_linear({"x": 1}, [{"coeffs": {"x": 1}, "op": "≈", "rhs": 1}]), "unknown operator"),
]:
    try:
        bad()
        check(f"rejects {why}", False, "no error")
    except S.SolverError:
        check(f"rejects {why}", True)
# a variable appearing ONLY in a constraint (0 objective coefficient) is valid LP, not an error
solo = S.solve_linear({"x": 1}, [{"coeffs": {"x": 1, "z": 1}, "op": "<=", "rhs": 5}], sense="max")
check("a constraint-only variable is accepted (valid LP)", solo["outcome"] == "optimal" and "z" in solo["solution"], str(solo))

# ===================== END TO END =====================
prob = {"objective": {"x": 3, "y": 5}, "sense": "max",
        "constraints": [{"coeffs": {"x": 1}, "op": "<=", "rhs": 4},
                        {"coeffs": {"y": 2}, "op": "<=", "rhs": 12},
                        {"coeffs": {"x": 3, "y": 2}, "op": "<=", "rhs": 18}]}
r = c.post("/solve", data={"problem": json.dumps(prob)}).json()
check("/solve returns the optimal solution", r.get("status") == "ok" and r.get("outcome") == "optimal" and approx(r["objective_value"], 36), str(r)[:200])
ri = c.post("/solve", data={"problem": json.dumps({"objective": {"x": 1}, "constraints": [{"coeffs": {"x": 1}, "op": ">=", "rhs": 5}, {"coeffs": {"x": 1}, "op": "<=", "rhs": 3}]})}).json()
check("/solve reports infeasible honestly", ri.get("outcome") == "infeasible", str(ri)[:160])
rbad = c.post("/solve", data={"problem": "not json"})
check("/solve rejects malformed JSON (400)", rbad.status_code == 400, f"HTTP {rbad.status_code}")
rempty = c.post("/solve", data={"problem": json.dumps({"objective": {}})})
check("/solve rejects an empty objective (400)", rempty.status_code == 400, f"HTTP {rempty.status_code}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
