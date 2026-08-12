"""ENGINE PHASE 5.6 — user management & roles + range-level permissions (BUILD).

Adds the DoD's role vocabulary and range-scoped permissions as a canonical data-access layer
(app/permissions.py), complementing org RBAC (rbac.py) and collaboration roles (collab.py):

  roles + caps    owner / admin / editor / viewer / auditor. auditor is read-only but may
                  read the audit trail (a compliance reviewer who changes nothing); viewer
                  may comment but not audit.
  range perms     ALLOW grants elevate a viewer to edit specific columns; a RESTRICT grant
                  confines an editor to specific columns (and wins over everything).
  enforcement     authorize_plan checks an actual Operation Plan: a read-only role runs
                  nothing (fail-closed); an editor is checked per-column against its grants.
  API             GET /permissions/roles; POST /permissions/authorize.

No llm.py change → no schema/serving/quota risk; no battery rows (endpoint/mechanism).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_5_6.py
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

_fd, _db = tempfile.mkstemp(suffix="-p56.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import permissions as P  # noqa: E402
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


TABLES = {"Data": pd.DataFrame({"A": [1, 2], "B": [3, 4], "C": [5, 6]})}
ALL = ["A", "B", "C"]


print("ENGINE PHASE 5.6 — roles & range-level permissions\n")

# ===================== roles + capabilities =====================
check("the five DoD roles exist", set(P.ROLES) == {"owner", "admin", "editor", "viewer", "auditor"}, str(P.ROLES))
check("owner + admin have every capability", P.capabilities("owner") == P.capabilities("admin") == {"view", "edit", "comment", "manage", "audit"}, str(P.capabilities("owner")))
check("editor can edit + comment but not manage/audit", P.can("editor", "edit") and P.can("editor", "comment") and not P.can("editor", "manage") and not P.can("editor", "audit"), "")
check("viewer can view + comment but NOT edit or audit", P.can("viewer", "view") and P.can("viewer", "comment") and not P.can("viewer", "edit") and not P.can("viewer", "audit"), "")
check("auditor can view + AUDIT but NOT edit or comment", P.can("auditor", "view") and P.can("auditor", "audit") and not P.can("auditor", "edit") and not P.can("auditor", "comment"), "")
check("an unknown role has no capabilities", P.capabilities("stranger") == set() and not P.can("stranger", "view"), "")

# ===================== range-level permissions =====================
check("editor edits all columns by default", P.editable_columns("editor", "Data", ALL) == {"A", "B", "C"}, "")
check("viewer edits nothing by default", P.editable_columns("viewer", "Data", ALL) == set(), "")
# ALLOW grant elevates a viewer on a specific column
check("an ALLOW grant lets a viewer edit ONLY the granted column",
      P.editable_columns("viewer", "Data", ALL, [{"columns": ["B"], "mode": "allow"}]) == {"B"}, "")
# RESTRICT grant confines an editor
check("a RESTRICT grant confines an editor to specific columns",
      P.editable_columns("editor", "Data", ALL, [{"table": "Data", "columns": ["A", "B"], "mode": "restrict"}]) == {"A", "B"}, "")
check("RESTRICT wins over ALLOW (hard cap)",
      P.editable_columns("editor", "Data", ALL, [{"columns": ["C"], "mode": "allow"}, {"columns": ["A"], "mode": "restrict"}]) == {"A"}, "")
check("a grant for a DIFFERENT table doesn't apply",
      P.editable_columns("viewer", "Data", ALL, [{"table": "Other", "columns": ["A"], "mode": "allow"}]) == set(), "")
check("can_edit reflects the grant", P.can_edit("viewer", "Data", "B", ALL, [{"columns": ["B"], "mode": "allow"}]) and not P.can_edit("viewer", "Data", "A", ALL, [{"columns": ["B"], "mode": "allow"}]), "")

# ===================== authorize_plan (enforcement) =====================
# read-only roles run nothing (fail-closed) — even a sort
for ro in ("viewer", "auditor"):
    res = P.authorize_plan(ro, [{"action": "sort", "columns": ["A"]}], TABLES)
    check(f"{ro} is blocked from any data change (read-only)", res["allowed"] is False and res["blocked"][0]["action"] == "sort", str(res))
# a data-adding op (lookup) is also gated for a viewer (fail-closed, not just column ops)
check("a viewer can't run a lookup either (fail-closed)", P.authorize_plan("viewer", [{"action": "lookup", "key_column": "A"}], TABLES)["allowed"] is False, "")

# editor: general ops + new columns are fine
check("editor may sort", P.authorize_plan("editor", [{"action": "sort", "columns": ["A"]}], TABLES)["allowed"], "")
check("editor may add a NEW column", P.authorize_plan("editor", [{"action": "add_formula_column", "name": "D", "formula": "{A}+{B}"}], TABLES)["allowed"], "")

# editor RESTRICTED to A,B: editing C is blocked, editing A is allowed
restrict = [{"table": "Data", "columns": ["A", "B"], "mode": "restrict"}]
r_drop_c = P.authorize_plan("editor", [{"action": "drop_columns", "columns": ["C"]}], TABLES, restrict)
check("restricted editor: dropping an out-of-range column is blocked (with a reason)",
      r_drop_c["allowed"] is False and "C" in r_drop_c["blocked"][0]["reason"], str(r_drop_c))
check("restricted editor: dropping an in-range column is allowed",
      P.authorize_plan("editor", [{"action": "drop_columns", "columns": ["A"]}], TABLES, restrict)["allowed"], "")
check("restricted editor: renaming an out-of-range column is blocked",
      P.authorize_plan("editor", [{"action": "rename_columns", "rename_from": ["C"], "rename_to": ["Z"]}], TABLES, restrict)["allowed"] is False, "")
check("restricted editor: OVERWRITING an out-of-range column is blocked",
      P.authorize_plan("editor", [{"action": "add_formula_column", "name": "C", "formula": "{A}", "overwrite": True}], TABLES, restrict)["allowed"] is False, "")
check("restricted editor: set_cells on an out-of-range column is blocked",
      P.authorize_plan("editor", [{"action": "set_cells", "edits": [{"column": "C", "row": 0, "value": 9}]}], TABLES, restrict)["allowed"] is False, "")
# a multi-step plan reports EACH blocked step
multi = P.authorize_plan("editor", [
    {"action": "sort", "columns": ["A"]},                              # ok
    {"action": "drop_columns", "columns": ["C"]},                       # blocked
    {"action": "format_cells", "format_columns": ["A"]},               # ok
], TABLES, restrict)
check("multi-step: only the out-of-range step is blocked, by index", multi["allowed"] is False and [b["step"] for b in multi["blocked"]] == [2], str(multi))

# owner is never blocked
check("owner may do anything, including dropping any column", P.authorize_plan("owner", [{"action": "drop_columns", "columns": ["C"]}], TABLES)["allowed"], "")
# a viewer ELEVATED on B may edit B (via set_cells) but not C
elev = [{"table": "Data", "columns": ["B"], "mode": "allow"}]
check("elevated viewer may edit the granted column", P.authorize_plan("viewer", [{"action": "set_cells", "edits": [{"column": "B", "row": 0, "value": 9}]}], TABLES, elev)["allowed"], "")
check("elevated viewer still can't edit an ungranted column", P.authorize_plan("viewer", [{"action": "set_cells", "edits": [{"column": "C", "row": 0, "value": 9}]}], TABLES, elev)["allowed"] is False, "")

# ===================== API =====================
roles = c.get("/permissions/roles").json()
check("/permissions/roles lists all five roles with capabilities",
      {r["role"] for r in roles["roles"]} == {"owner", "admin", "editor", "viewer", "auditor"} and "audit" in dict((r["role"], r["capabilities"]) for r in roles["roles"])["auditor"], str(roles)[:200])

c.post("/inspect", data={"session_id": "pr"}, files=[("files", ("d.csv", b"A,B,C\n1,3,5\n2,4,6\n", "text/csv"))])
auth_ok = c.post("/permissions/authorize", data={"session_id": "pr", "role": "editor",
                 "plan": json.dumps({"operations": [{"action": "sort", "columns": ["A"]}]})}).json()
check("/permissions/authorize: editor sort allowed", auth_ok.get("allowed") is True, str(auth_ok)[:160])
auth_block = c.post("/permissions/authorize", data={"session_id": "pr", "role": "editor",
                    "plan": json.dumps({"operations": [{"action": "drop_columns", "columns": ["C"]}]}),
                    "grants": json.dumps([{"table": "d", "columns": ["A", "B"], "mode": "restrict"}])}).json()
check("/permissions/authorize: restricted editor drop C blocked", auth_block.get("allowed") is False and auth_block["blocked"][0]["action"] == "drop_columns", str(auth_block)[:200])
auth_view = c.post("/permissions/authorize", data={"session_id": "pr", "role": "viewer",
                   "plan": json.dumps({"operations": [{"action": "sort", "columns": ["A"]}]})}).json()
check("/permissions/authorize: viewer blocked from any change", auth_view.get("allowed") is False, str(auth_view)[:160])
bad = c.post("/permissions/authorize", data={"session_id": "pr", "role": "wizard",
             "plan": json.dumps({"operations": []})})
check("/permissions/authorize: unknown role 400s", bad.status_code == 400, f"HTTP {bad.status_code}")

m._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
