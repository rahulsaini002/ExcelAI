"""AI guardrails (Phase 3.10).

Before a plan runs, look at what it will actually DO to the data and flag the destructive
parts with a concrete, numeric impact — "removes 1,240 of 5,000 rows", "deletes 3 columns",
"overwrites Profit (used by 4 formula steps)". The API uses this to ask the user to confirm
before anything irreversible happens; the user can always cancel.

Impacts are computed against the current data, cheaply (a count pass at most). For a
multi-step plan the row-based numbers are best-effort estimates against the starting table,
so they read as "up to N". Nothing here mutates anything — it only inspects.
"""
from __future__ import annotations

import re

import pandas as pd

from .executor import OperationError, _filter

# Actions that can lose or overwrite data → worth a confirmation.
_DELETES_ROWS = {"drop_missing", "drop_invalid", "remove_duplicates", "filter", "limit"}
_RESHAPES = {"merge", "combine_sheets", "pivot", "unpivot", "transpose"}


def _blank_count(df: pd.DataFrame, cols: list[str]) -> int:
    sub = df[cols] if cols else df
    mask = sub.isna() | (sub.astype(str).apply(lambda s: s.str.strip() == ""))
    return int(mask.any(axis=1).sum())


def _formula_refs(operations: list[dict], column: str) -> int:
    """How many add_formula_column steps in THIS plan reference `column` (i.e. would be
    affected if it's renamed or dropped) — the basis for 'affects N formulas'."""
    token = "{" + column + "}"
    return sum(
        1 for op in operations
        if op.get("action") == "add_formula_column" and token in (op.get("formula") or "")
    )


def _refs_in(formula: str) -> set[str]:
    """Column names a formula template references. Handles {Col}, {Col:} (range) and
    {Sheet.Col:} — normalised to the bare column name for dependency matching."""
    out: set[str] = set()
    for inner in re.findall(r"\{([^{}]+)\}", formula or ""):
        text = inner.strip().rstrip(":").strip()
        if "." in text:                       # {Sheet.Col} → the column part
            text = text.split(".", 1)[1].strip()
        if text:
            out.add(text)
    return out


def _known_dependents(
    known_formulas: dict | None, target_cols, dropping: set | frozenset = frozenset()
) -> list[str]:
    """Names of EXISTING (prior-step) formula columns that reference any of `target_cols`
    — real trace-precedents across the session, not just the current plan. Skips any
    formula whose own column is itself being removed in this op (it can't be "broken" if
    it's going away). This is what powers the honest "feeds N formulas" impact."""
    if not known_formulas:
        return []
    targets = {str(c) for c in target_cols}
    dropping = {str(c) for c in dropping}
    out: list[str] = []
    for name, formula in known_formulas.items():
        if name in dropping:
            continue
        if _refs_in(formula) & targets:
            out.append(str(name))
    return out


def _feeds_clause(names: list[str]) -> str:
    """' — feeds N existing formula(s): a, b' or '' when nothing depends on it."""
    if not names:
        return ""
    return (f" — feeds {len(names)} existing formula{'s' if len(names) != 1 else ''}: "
            f"{', '.join(names)}")


def _assess_op(step: int, op: dict, df: pd.DataFrame | None, operations: list[dict],
               known_formulas: dict | None = None) -> dict | None:
    action = op.get("action")
    cols = op.get("columns") or []
    n = len(df) if df is not None else 0

    def warn(severity: str, impact: str) -> dict:
        return {"step": step, "action": action, "severity": severity, "impact": impact}

    try:
        if action == "drop_columns":
            present = [c for c in cols if df is not None and c in df.columns] or cols
            refs = sum(_formula_refs(operations, c) for c in present)
            extra = f" (used by {refs} formula step{'s' if refs != 1 else ''} in this plan)" if refs else ""
            feeds = _feeds_clause(_known_dependents(
                known_formulas, present, dropping={str(x) for x in cols} | set(present)))
            return warn("high", f"Deletes {len(present)} column{'s' if len(present) != 1 else ''}: {', '.join(present)}{extra}{feeds}.")

        if action == "select_columns":
            if df is None:
                return warn("high", f"Keeps only {len(cols)} column(s); removes the rest.")
            dropped = [c for c in df.columns if c not in cols]
            if not dropped:
                return None
            return warn("high", f"Removes {len(dropped)} column{'s' if len(dropped) != 1 else ''} ({', '.join(map(str, dropped[:6]))}{'…' if len(dropped) > 6 else ''}); keeps {len(cols)}.")

        if action == "remove_duplicates":
            if df is None:
                return warn("medium", "Removes duplicate rows.")
            subset = [c for c in cols if c in df.columns] or None
            dups = int(df.duplicated(subset=subset).sum())
            if dups == 0:
                return None
            return warn("high", f"Removes {dups:,} duplicate row{'s' if dups != 1 else ''} of {n:,}.")

        if action in ("drop_missing", "drop_invalid"):
            if df is None:
                return warn("medium", "Removes rows with missing/invalid values.")
            target = [c for c in cols if c in df.columns] or list(df.columns)
            if action == "drop_missing":
                cnt = _blank_count(df, target)
                what = "blank"
            else:
                dt = op.get("data_type") or "number"
                cnt = 0
                for c in target:
                    s = df[c]
                    nonblank = s[s.notna() & (s.astype(str).str.strip() != "")]
                    coerced = pd.to_numeric(nonblank, errors="coerce") if dt == "number" else pd.to_datetime(nonblank, errors="coerce")
                    cnt += int(coerced.isna().sum())
                what = f"invalid {dt}"
            if cnt == 0:
                return None
            return warn("high" if cnt > n * 0.2 else "medium",
                        f"Removes up to {cnt:,} row{'s' if cnt != 1 else ''} with {what} values in {', '.join(map(str, target[:4]))}.")

        if action == "filter":
            if df is None:
                return warn("medium", "Keeps only rows matching your condition.")
            try:
                kept = len(_filter(df.copy(), op)[0])
            except OperationError:
                return None  # the real run will surface the error
            removed = n - kept
            if removed <= 0:
                return None
            return warn("high" if removed > n * 0.5 else "medium",
                        f"Removes {removed:,} of {n:,} row{'s' if n != 1 else ''} (keeps {kept:,}).")

        if action == "limit":
            keep = int(op.get("count") or 0)
            if df is None or keep >= n or keep <= 0:
                return None
            return warn("medium", f"Keeps {'last' if op.get('from_end') else 'first'} {keep:,}; drops {n - keep:,} row(s).")

        if action == "find_replace":
            find = op.get("find")
            if not find:
                return None
            col = op.get("column")
            mc = bool(op.get("match_case"))
            try:
                if df is None:
                    cnt = 0
                elif col and col in df.columns:
                    cnt = int(df[col].astype(str).str.contains(re.escape(str(find)), case=mc, na=False).sum())
                else:
                    cnt = int(df.astype(str).apply(lambda s: s.str.contains(re.escape(str(find)), case=mc, na=False)).to_numpy().sum())
            except Exception:
                cnt = 0
            if cnt == 0:
                return None
            where = f"in {col}" if col else "across the sheet"
            return warn("medium", f"Replaces about {cnt:,} cell{'s' if cnt != 1 else ''} {where}.")

        if action == "add_formula_column":
            name = (op.get("name") or "").strip()
            if df is not None and name in df.columns and op.get("overwrite"):
                refs = _formula_refs(operations, name)
                extra = f" (used by {refs} formula step{'s' if refs != 1 else ''} in this plan)" if refs else ""
                feeds = _feeds_clause(_known_dependents(known_formulas, [name], dropping={name}))
                return warn("high", f"Overwrites the existing column '{name}'{extra}{feeds}.")
            return None

        if action == "rename_columns":
            pairs = list(zip(op.get("rename_from") or [], op.get("rename_to") or []))
            if not pairs:
                return None
            refs = sum(_formula_refs(operations, a) for a, _ in pairs)
            detail = ", ".join(f"{a}→{b}" for a, b in pairs)
            extra = f" — affects {refs} formula step{'s' if refs != 1 else ''} in this plan" if refs else ""
            dependents = _known_dependents(known_formulas, [a for a, _ in pairs])
            feeds = _feeds_clause(dependents)
            # Renaming a column that EXISTING formulas depend on is a real break → high.
            sev = "high" if dependents else ("medium" if refs else "low")
            return warn(sev, f"Renames {detail}{extra}{feeds}.")

        if action in _RESHAPES:
            label = {"merge": "Merges tables into one", "combine_sheets": "Combines tables onto separate sheets",
                     "pivot": "Pivots", "unpivot": "Unpivots", "transpose": "Transposes"}[action]
            return warn("medium", f"{label} — the row/column layout changes.")

        if action == "set_cells":
            edits = op.get("edits") or []
            if not edits:
                return None
            return warn("low", f"Edits {len(edits)} cell{'s' if len(edits) != 1 else ''}.")
    except Exception:
        return None  # assessment is best-effort; never block a run because it failed
    return None


def assess(operations: list[dict], tables: dict, primary: str,
           known_formulas: dict | None = None) -> dict:
    """Return {destructive, warnings, summary}. `destructive` is True when any warning is
    high/medium severity (worth a confirmation). Row-based numbers for chained plans are
    estimated against the starting table.

    `known_formulas` = {column: formula} the SESSION has already built (prior steps). It
    lets drop/rename/overwrite warnings trace real precedents — "feeds 3 existing formulas:
    Margin, GM, Runway" — not just references inside the current plan. Optional and
    defaulting to none, so every existing caller is unaffected."""
    df0 = tables.get(primary)
    working = primary
    warnings: list[dict] = []
    for i, op in enumerate(operations, 1):
        tname = op.get("table") or working
        df = tables.get(tname, df0)
        w = _assess_op(i, op, df, operations, known_formulas)
        if w:
            warnings.append(w)
        if op.get("action") not in ("merge", "combine_sheets"):
            working = tname

    blocking = [w for w in warnings if w["severity"] in ("high", "medium")]
    destructive = bool(blocking)
    summary = " ".join(w["impact"] for w in blocking) if blocking else ""
    return {"destructive": destructive, "warnings": warnings, "summary": summary}
