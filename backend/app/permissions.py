"""User roles & range-level permissions (Phase 5.6).

The canonical DATA-access role model for a workspace, and the range-level grants that scope
who may edit which columns. (This complements org-management RBAC in rbac.py, which governs
team administration, and the collaboration roles in collab.py.)

Roles and what each may do:
  owner    everything, including managing people and viewing the audit trail.
  admin    like owner for day-to-day: edit + manage members + audit.
  editor   edit the data (and comment) — but not manage people or read audit logs.
  viewer   read-only, may comment.
  auditor  read-only + may read the audit trail — but may NOT edit or comment. A compliance
           reviewer who can see everything (incl. the security log) and change nothing.

Range-level permissions layer on top, per table:
  • an ALLOW grant elevates a view-only member to edit SPECIFIC columns (e.g. a viewer who
    may only update a "Status" column).
  • a RESTRICT grant caps an editor to specific columns (edit these, nothing else) — a hard
    boundary that wins over everything.

Everything here is a pure function of role + grants — no DB, no request — so it's trivially
testable and can't drift per-endpoint. authorize_plan() enforces it against an actual
Operation Plan, so the range rules bite on real edits, not just in theory.
"""
from __future__ import annotations

# Capability sets per role. Ordered most→least powerful for display.
_CAPS: dict[str, set[str]] = {
    "owner":   {"view", "edit", "comment", "manage", "audit"},
    "admin":   {"view", "edit", "comment", "manage", "audit"},
    "editor":  {"view", "edit", "comment"},
    "viewer":  {"view", "comment"},
    "auditor": {"view", "audit"},
}
ROLES = tuple(_CAPS)  # owner, admin, editor, viewer, auditor

# Ops that change specific COLUMNS → additionally subject to column range-permissions. Any
# OTHER op still requires the "edit" capability (fail-closed: a read-only role runs nothing),
# it just isn't column-scoped.
_COLUMN_OPS = {
    "drop_columns": ("columns",),
    "rename_columns": ("rename_from",),
    "format_cells": ("format_columns", "columns"),
    "find_replace": ("column",),
    "add_formula_column": ("name",),   # only when overwriting an existing column
    "set_cells": (),                    # columns pulled from its edits
}


def is_role(role: str) -> bool:
    return role in _CAPS


def capabilities(role: str) -> set[str]:
    return set(_CAPS.get(role, set()))


def can(role: str, capability: str) -> bool:
    """True if `role` has `capability` (view/edit/comment/manage/audit)."""
    return capability in _CAPS.get(role, set())


def roles_summary() -> list[dict]:
    return [{"role": r, "capabilities": sorted(_CAPS[r])} for r in ROLES]


# --------------------------------------------------------------------------- #
# Range-level permissions
# --------------------------------------------------------------------------- #
def _table_match(pattern, table: str) -> bool:
    return pattern in (None, "", "*", table)


def _grant_columns(grant: dict, all_cols: set[str]) -> set[str]:
    cols = grant.get("columns")
    if cols in (None, "*", []) or cols == "*":
        return set(all_cols)
    return {str(c) for c in cols}


def editable_columns(role: str, table: str, all_columns, grants: list[dict] | None = None) -> set[str]:
    """The set of columns the subject may EDIT in `table`, given their role and any range
    grants. Base = all columns if the role can edit, else none. ALLOW grants add columns
    (elevate a viewer on a range); a RESTRICT grant caps the result (an editor confined to a
    range). RESTRICT always wins."""
    grants = grants or []
    all_cols = {str(c) for c in all_columns}
    editable = set(all_cols) if can(role, "edit") else set()
    allow_extra: set[str] = set()
    restrict_to: set[str] | None = None
    for g in grants:
        if not _table_match(g.get("table"), table):
            continue
        cols = _grant_columns(g, all_cols) & all_cols
        if g.get("mode") == "restrict":
            restrict_to = (restrict_to or set()) | cols
        else:  # "allow" (default)
            allow_extra |= cols
    editable |= allow_extra
    if restrict_to is not None:
        editable &= restrict_to
    return editable


def can_edit(role: str, table: str, column: str, all_columns, grants: list[dict] | None = None) -> bool:
    return str(column) in editable_columns(role, table, all_columns, grants)


def _op_columns(op: dict) -> list[str]:
    action = op.get("action")
    if action == "set_cells":
        return [str(e.get("column")) for e in (op.get("edits") or []) if e.get("column")]
    if action == "add_formula_column":
        # Only an OVERWRITE touches an existing column; a brand-new column is fine.
        return [str(op.get("name"))] if op.get("overwrite") and op.get("name") else []
    cols: list[str] = []
    for key in _COLUMN_OPS.get(action, ()):
        v = op.get(key)
        if isinstance(v, list):
            cols += [str(x) for x in v]
        elif v:
            cols.append(str(v))
    return cols


def authorize_plan(role: str, operations: list[dict], tables: dict, grants: list[dict] | None = None) -> dict:
    """Check an Operation Plan against the subject's role + range grants. Returns
    {allowed, blocked:[{step, action, reason}]}. A role without 'edit' can run nothing that
    changes data; an editor is checked per-column for column-targeted ops."""
    grants = grants or []
    cols_by_table = {t: {str(c) for c in df.columns} for t, df in tables.items()}
    primary = next(iter(tables), None)
    blocked: list[dict] = []
    may_edit = can(role, "edit")

    for i, op in enumerate(operations, 1):
        action = op.get("action")
        target = op.get("table") or primary
        existing = cols_by_table.get(target, set())
        # Columns THIS op edits that already exist → the range check applies to them.
        touched = [col for col in _op_columns(op) if col in existing]
        if touched:
            # Column-scoped: allowed iff EVERY touched existing column is editable. Range
            # grants can elevate an otherwise read-only role for exactly these columns.
            allowed_cols = editable_columns(role, target, existing, grants)
            denied = [col for col in touched if col not in allowed_cols]
            if denied:
                blocked.append({
                    "step": i, "action": action,
                    "reason": f"Not permitted to edit column(s) {', '.join(denied)} in {target}.",
                })
        elif not may_edit:
            # A table-wide edit (sort/filter/lookup, adding a new column, …) isn't column-
            # scoped, so it needs the global edit capability — a read-only role can't (fail-
            # closed). A range-elevated viewer may edit their granted cells but not this.
            blocked.append({"step": i, "action": action,
                            "reason": f"A {role} can't change the dataset — this is a read-only role."})
    return {"allowed": not blocked, "blocked": blocked}
