"""Engine Phase 1.6 — sheet management (Area 14).

One op, `sheet_op`, acting on the WORKBOOK's sheet namespace (not a single table):
  sheet_action  new_sheet | rename | delete | copy | move | tab_color | hide | unhide
  sheet_name    which sheet (defaults to the current working sheet)
  new_name      for new_sheet / rename / copy
  position      for move — 1-based, or "first" / "last"
  tab_color     a named color (tab_color action)

new_sheet puts the CURRENT working result into a new tab ("put the summary in a new
tab called Report"). Namespace changes make the whole workbook the result, so every
tab survives into the saved file. Styling actions (tab_color/hide/unhide) only emit a
render directive — data untouched.
"""
from __future__ import annotations

import pandas as pd

from .base import OperationError
from .conditional_format import STRONG

ACTIONS = {"new_sheet", "rename", "delete", "copy", "move", "tab_color", "hide", "unhide",
           "protect", "unprotect", "protect_workbook", "unprotect_workbook", "compare"}


def _resolve(tables: dict, want: str) -> str | None:
    low = (want or "").strip().lower()
    exact = [n for n in tables if str(n).strip().lower() == low]
    if len(exact) == 1:
        return exact[0]
    tails = [n for n in tables if str(n).split(" - ")[-1].strip().lower() == low]
    if len(tails) == 1:
        return tails[0]
    return None


def sheet_op(
    tables: dict[str, pd.DataFrame], working: str, op: dict
) -> tuple[dict[str, pd.DataFrame], str, str, dict | None, bool]:
    """Returns (tables, working, note, directive|None, namespace_changed)."""
    action = (op.get("sheet_action") or "").strip().lower()
    if action not in ACTIONS:
        raise OperationError(
            f"I don't know the sheet action '{action or '(none)'}' — I can create, "
            "rename, delete, copy, or move sheets, color their tabs, hide/unhide them, "
            "and protect/unprotect sheets or the workbook."
        )

    # Workbook COMPARE (Area 16): diff two tables → a "Comparison" result. Reuses
    # existing fields (sheet_name = file A, source_sheet = file B, key_column) so it adds
    # nothing to the response schema, which is at Gemini's serving limit.
    if action == "compare":
        from .compare import compare_tables

        a_name = _resolve(tables, (op.get("sheet_name") or "").strip()) or working
        b_raw = (op.get("source_sheet") or op.get("new_name") or "").strip()
        b_name = _resolve(tables, b_raw) if b_raw else None
        if b_name is None:
            others = [n for n in tables if n != a_name]
            if b_raw:
                raise OperationError(
                    f"There's no file/sheet called '{b_raw}' to compare against. "
                    f"Available: {', '.join(tables)}."
                )
            if len(others) == 1:
                b_name = others[0]  # only one other table — the obvious counterpart
            else:
                raise OperationError(
                    "Which two files should I compare? Upload two files (or name a "
                    f"second sheet). Available: {', '.join(tables)}."
                )
        if a_name == b_name:
            raise OperationError("I need TWO different files/sheets to compare — those are the same one.")
        key = (op.get("key_column") or "").strip() or None
        diff_df, note = compare_tables(tables[a_name], tables[b_name], a_name, b_name, key)
        tables["Comparison"] = diff_df
        return tables, "Comparison", note, None, False

    # Workbook-STRUCTURE protection (lock sheets from being added/removed/reordered).
    # No target sheet, no password (Sumio never sets or stores a file password).
    if action in ("protect_workbook", "unprotect_workbook"):
        on = action == "protect_workbook"
        directive = {"type": "workbook_protect", "lock_structure": on}
        note = ("Protected the workbook structure — sheets can't be added, deleted, or "
                "reordered until it's unprotected."
                if on else "Removed the workbook-structure protection.")
        return tables, working, note, directive, True

    name = (op.get("sheet_name") or "").strip()
    new_name = (op.get("new_name") or "").strip()
    target = _resolve(tables, name) if name else working
    if target is None:
        raise OperationError(
            f"There's no sheet called '{name}'. Available: {', '.join(tables)}."
        )

    def need_new_name(what: str) -> str:
        if not new_name:
            raise OperationError(f"What should the {what} be called?")
        if _resolve(tables, new_name) or new_name in tables:
            raise OperationError(
                f"A sheet called '{new_name}' already exists — pick another name."
            )
        return new_name

    if action == "new_sheet":
        nn = need_new_name("new sheet")
        tables[nn] = tables[target].copy()
        return tables, nn, f"Put the current result in a new sheet '{nn}'.", None, True

    if action == "rename":
        nn = need_new_name("sheet")
        out = {}
        for k, v in tables.items():  # preserve tab order
            out[nn if k == target else k] = v
        tables.clear()
        tables.update(out)
        new_working = nn if working == target else working
        return tables, new_working, f"Renamed sheet '{target}' to '{nn}'.", None, True

    if action == "delete":
        if len(tables) <= 1:
            raise OperationError(
                "That's the only sheet in the workbook — deleting it would leave "
                "nothing. Add or keep another sheet first."
            )
        tables.pop(target)
        new_working = working if working in tables else next(iter(tables))
        return tables, new_working, f"Deleted sheet '{target}'.", None, True

    if action == "copy":
        nn = new_name or f"{target} Copy"
        if _resolve(tables, nn) or nn in tables:
            raise OperationError(f"A sheet called '{nn}' already exists — pick another name.")
        out = {}
        for k, v in tables.items():
            out[k] = v
            if k == target:
                out[nn] = v.copy()  # insert the copy right after its source
        tables.clear()
        tables.update(out)
        return tables, working, f"Copied sheet '{target}' to '{nn}'.", None, True

    if action == "move":
        pos = op.get("position")
        keys = [k for k in tables if k != target]
        if isinstance(pos, str) and pos.strip().lower() == "first":
            idx = 0
        elif isinstance(pos, str) and pos.strip().lower() == "last":
            idx = len(keys)
        else:
            try:
                idx = max(0, min(len(keys), int(pos) - 1))
            except (TypeError, ValueError):
                raise OperationError(
                    "Where should the sheet go? Say a position like 1, or 'first'/'last'."
                )
        keys.insert(idx, target)
        out = {k: tables[k] for k in keys}
        tables.clear()
        tables.update(out)
        return tables, working, f"Moved sheet '{target}' to position {idx + 1}.", None, True

    if action == "tab_color":
        color = (op.get("tab_color") or op.get("color") or "").strip().lower()
        if color not in STRONG:
            raise OperationError(
                f"I don't have the tab color '{color or '(none)'}' — try "
                f"{', '.join(sorted(set(STRONG) - {'gray'}))}."
            )
        directive = {"type": "sheet_style", "sheet_name": target, "tab_color": STRONG[color]}
        # namespace_changed=True: the styled tab must EXIST in the output, so the whole
        # workbook (not just the working sheet) becomes the result.
        return tables, working, f"Colored the '{target}' tab {color}.", directive, True

    if action in ("protect", "unprotect"):
        on = action == "protect"
        # Optional "allow editing these columns" — reuses the generic `columns` field
        # (no new schema field: the Operation model is at Gemini's serving limit).
        allow = [c for c in (op.get("columns") or []) if c and c in tables[target].columns]
        directive = {"type": "sheet_protect", "sheet_name": target,
                     "protect": on, "allow_columns": allow}
        if on:
            note = f"Protected the sheet '{target}' — its cells now resist accidental edits"
            note += (f" (except the {', '.join(allow)} column"
                     f"{'s' if len(allow) != 1 else ''}, left editable)." if allow else ".")
            # Honesty + safety: Sumio never sets or stores an open/file PASSWORD. Structural
            # protection is password-less; a password to OPEN the file is user-driven.
            note += (" This prevents casual edits; to require a PASSWORD, set it yourself "
                     "in Excel/Sheets — Sumio never handles or stores passwords.")
        else:
            note = f"Removed the protection from sheet '{target}' — its cells are editable again."
        return tables, working, note, directive, True

    # hide / unhide
    directive = {"type": "sheet_style", "sheet_name": target,
                 "hidden": action == "hide"}
    verb = "Hid" if action == "hide" else "Unhid"
    if action == "hide" and len(tables) == 1:
        raise OperationError("That's the only sheet — Excel needs at least one visible sheet.")
    return tables, working, f"{verb} the sheet '{target}'.", directive, True
