"""Deterministic fallback parser for common, UNAMBIGUOUS commands.

Runs ONLY when the LLM ("the Brain") is unavailable (rate limit / quota / outage),
so basic operations still work without the AI. It is intentionally conservative:
it matches clear English patterns against the file's REAL column names and returns
an OperationPlan-shaped dict, or None when it isn't confident (then the user sees
the honest "AI unavailable" message instead of a wrong guess).

This is a safety net, not a replacement for the Brain — no Hindi/mixed language,
no fuzzy intent, no multi-clause reasoning. When in doubt, it defers (returns None).
"""
from __future__ import annotations

import re

_DESC = ("descending", "desc", "high to low", "highest", "largest", "biggest",
         "z-a", "z to a", "decreasing", "reverse", "top first", "newest")
_SORT_PHRASES = ("high to low", "low to high", "highest first", "lowest first",
                 "largest first", "smallest first", "a-z", "z-a", "newest first",
                 "oldest first", "ascending order", "descending order")


def _tables(structure: dict) -> tuple[str | None, dict[str, list[str]]]:
    raw = structure.get("tables") or {}
    primary = structure.get("primary_table") or (next(iter(raw)) if raw else None)
    cols = {name: [c["name"] for c in t.get("columns", []) if c.get("name")]
            for name, t in raw.items()}
    return primary, cols


def _columns(structure: dict) -> list[str]:
    primary, cols = _tables(structure)
    return cols.get(primary, []) if primary else (next(iter(cols.values())) if cols else [])


def _find_column(low: str, columns: list[str]) -> str | None:
    """The LONGEST real column name that appears in the instruction (case-insensitive)."""
    for col in sorted(columns, key=len, reverse=True):
        if col and col.lower() in low:
            return col
    return None


def _columns_in_order(low: str, columns: list[str]) -> list[str]:
    """Real column names mentioned, in order of appearance (longest-match, no overlap)."""
    spans, taken = [], []
    for col in sorted(columns, key=len, reverse=True):
        for m in re.finditer(re.escape(col.lower()), low):
            if not any(s <= m.start() < e or s < m.end() <= e for s, e in taken):
                spans.append((m.start(), col))
                taken.append((m.start(), m.end()))
    return [c for _, c in sorted(spans)]


def _wrap_columns(expr: str, columns: list[str]) -> tuple[str, list[str]]:
    """Wrap each referenced column in {braces} for a formula, without double-wrapping."""
    masked, mapping = expr, {}
    for i, col in enumerate(sorted(columns, key=len, reverse=True)):
        pat = re.compile(re.escape(col), re.I)
        if pat.search(masked):
            token = f"\x00{i}\x00"
            masked = pat.sub(token, masked)
            mapping[token] = col
    used = list(mapping.values())
    for token, col in mapping.items():
        masked = masked.replace(token, "{" + col + "}")
    return masked, used


def _split(text: str) -> list[str]:
    """Break a multi-instruction message into separate commands. Handles newlines,
    semicolons, "then"/"and then", and numbered/bulleted lists — so "Filter A > 5 /
    Sort A desc / Create Tax = A*0.1" becomes three commands run in order."""
    # split on newlines, semicolons, "then", and inline list markers ("... 2. ...").
    # The "\d+[.)] " marker needs surrounding spaces, so decimals like 0.18 are safe.
    parts = re.split(r"[\n;]+|\bthen\b|\s+\d+[.)]\s+", text)
    out: list[str] = []
    for p in parts:
        p = re.sub(r"^(?:\d+[.)]|[-*•])\s*", "", p.strip()).strip()  # list markers
        p = re.sub(r"^and\b\s*", "", p, flags=re.I).strip()          # "...and then" leftover
        p = re.sub(r"\s*\band$", "", p, flags=re.I).strip()
        if p:
            out.append(p)
    return out


def _parse_segment(text: str, structure: dict, columns: list[str]) -> list[dict]:
    low = text.lower()
    if not low:
        return []
    matchers = (
        _m_remove_duplicates, _m_fill_missing, _m_drop_missing, _m_drop_invalid,
        _m_flag_missing, _m_trim, _m_rename, _m_drop_columns, _m_select_columns,
        _m_find_replace, _m_format, _m_formula, _m_aggregate, _m_merge, _m_lookup,
        _m_sort, _m_limit, _m_filter,
    )
    for matcher in matchers:
        op = matcher(text, low, columns, structure)
        if op is not None:
            return op if isinstance(op, list) else [op]
    return []


def parse(instruction: str, structure: dict) -> dict | None:
    """Return an OperationPlan-shaped dict for one OR MORE simple commands, else None.

    A multi-line / multi-clause message is split and each command parsed independently,
    so several actions run in one go. Commands it can't confidently parse are skipped
    (so e.g. "Create Tax column" with no formula doesn't block the others)."""
    text = (instruction or "").strip()
    columns = _columns(structure)
    if not text or not columns:
        return None

    ops: list[dict] = []
    for segment in _split(text):
        ops.extend(_parse_segment(segment, structure, columns))
    return {"operations": ops} if ops else None


# --------------------------------------------------------------------------- #
# Individual matchers — each returns one op (or a list), or None to defer.
# --------------------------------------------------------------------------- #
def _m_remove_duplicates(text, low, columns, structure):
    if (re.search(r"\b(remove|drop|delete|clear)\b.{0,15}\bduplicat", low)
            or "dedup" in low or re.search(r"\bduplicate (rows|records|entries)\b", low)):
        return {"action": "remove_duplicates"}
    return None


def _m_fill_missing(text, low, columns, structure):
    if not (re.search(r"\bfill\b", low) and re.search(r"\b(blanks?|missing|empty|nulls?|na)\b", low)):
        return None
    col = _find_column(low, columns)
    if "previous" in low or "carry forward" in low or "above" in low or "ffill" in low:
        op = {"action": "fill_missing", "fill_method": "previous"}
    elif "next" in low or "below" in low or "bfill" in low:
        op = {"action": "fill_missing", "fill_method": "next"}
    else:
        m = re.search(r"\bwith\s+(.+)", text, re.I)
        if not m:
            return None
        val = m.group(1).strip().strip("'\"")
        val = re.split(r"\s+in\s+", val, maxsplit=1)[0].strip()
        # statistical fills aren't supported here — let the AI decline properly
        if re.search(r"\b(average|mean|median|mode|interpolat)\b", val.lower()):
            return None
        if not val:
            return None
        op = {"action": "fill_missing", "fill_value": val}
    if col:
        op["columns"] = [col]
    return op


def _m_drop_missing(text, low, columns, structure):
    if (re.search(r"\b(remove|drop|delete)\b", low)
            and re.search(r"\b(blanks?|missing|empty|nulls?)\b", low)
            and re.search(r"\b(rows?|records?|entries)\b", low)):
        op = {"action": "drop_missing"}
        col = _find_column(low, columns)
        if col:
            op["columns"] = [col]
        return op
    return None


def _m_drop_invalid(text, low, columns, structure):
    if re.search(r"\b(remove|drop|delete)\b.{0,20}\b(invalid|bad|garbage|non[- ]?numeric|wrong)\b", low):
        col = _find_column(low, columns)
        if col:
            op = {"action": "drop_invalid", "columns": [col]}
            if "date" in low:
                op["data_type"] = "date"
            return op
    return None


def _m_flag_missing(text, low, columns, structure):
    if re.search(r"\b(highlight|flag|mark|show)\b.{0,15}\b(blanks?|missing|empty|nulls?)\b", low):
        op = {"action": "flag_missing"}
        col = _find_column(low, columns)
        if col:
            op["columns"] = [col]
        return op
    return None


def _m_trim(text, low, columns, structure):
    if re.search(r"\btrim\b", low) or re.search(r"\b(remove|clean)\b.{0,15}\bspaces?\b", low):
        return {"action": "trim"}
    return None


def _m_rename(text, low, columns, structure):
    if "rename" not in low:
        return None
    m = re.search(r"rename\s+(?:the\s+)?(?:column\s+)?(.+?)\s+to\s+(.+)", text, re.I)
    if not m:
        return None
    old = _find_column(m.group(1).lower(), columns)
    new = m.group(2).strip().strip("'\".")
    if old and new:
        return {"action": "rename_columns", "rename_from": [old], "rename_to": [new]}
    return None


def _m_drop_columns(text, low, columns, structure):
    if re.search(r"\b(remove|drop|delete)\b.{0,12}\bcolumns?\b", low):
        cols = _columns_in_order(low, columns)
        if cols:
            return {"action": "drop_columns", "columns": cols}
    return None


def _m_select_columns(text, low, columns, structure):
    # require the word "column" so "keep only North region" stays a filter, not a select
    if re.search(r"\b(keep only|only keep|select|show only|keep just)\b", low) and "column" in low:
        cols = _columns_in_order(low, columns)
        if cols:
            return {"action": "select_columns", "columns": cols}
    return None


def _m_find_replace(text, low, columns, structure):
    m = re.search(r"\b(?:replace|change)\s+(?:all\s+)?(.+?)\s+(?:with|to|by)\s+(.+)", text, re.I)
    if not m:
        return None
    find = m.group(1).strip().strip("'\"")
    rest = m.group(2).strip()
    col = None
    mcol = re.search(r"\s+in\s+(.+)$", rest, re.I)
    if mcol:
        c = _find_column(mcol.group(1).lower(), columns)
        if c:
            col = c
            rest = rest[: mcol.start()].strip()
    replace = rest.strip().strip("'\"")
    if not find:
        return None
    op = {"action": "find_replace", "find": find, "replace": replace}
    if col:
        op["column"] = col
    return op


def _m_format(text, low, columns, structure):
    fmt = None
    if "currency" in low or "rupee" in low or "dollar" in low or "₹" in text or "$" in text:
        fmt = "currency"
    elif "percent" in low or "percentage" in low or "%" in text:
        fmt = "percent"
    elif re.search(r"\bdate\b", low) and re.search(r"\bformat|as\b", low):
        fmt = "date"
    bold = bool(re.search(r"\bbold\b", low) and re.search(r"\bheader", low))
    if not fmt and not bold:
        return None
    op = {"action": "format_cells"}
    if fmt:
        cols = _columns_in_order(low, columns)
        if not cols:
            return None if not bold else {"action": "format_cells", "bold_header": True}
        op["format_columns"] = cols
        op["number_format"] = fmt
    if bold:
        op["bold_header"] = True
    return op


def _m_aggregate(text, low, columns, structure):
    if re.search(r"\b(sum|total)\b", low):
        func = "sum"
    elif re.search(r"\b(average|avg|mean)\b", low):
        func = "average"
    elif re.search(r"\bcount\b", low):
        func = "count"
    elif re.search(r"\bminimum\b", low):
        func = "min"
    elif re.search(r"\bmaximum\b", low):
        func = "max"
    else:
        return None
    op = {"action": "aggregate", "agg_func": func}
    # group by ... / per ... / for each ...
    mg = re.search(r"\b(?:grouped by|group by|by|per|for each)\s+(.+)", low)
    group = _find_column(mg.group(1), columns) if mg else None
    before = low[: mg.start()] if mg else low
    col = _find_column(before, columns)
    if func != "count" and not col:
        return None
    if col:
        op["agg_column"] = col
    if group and group != col:
        op["group_by"] = [group]
    return op


def _m_formula(text, low, columns, structure):
    m = re.search(r"^(?:create|add|make|new|insert|calculate)?\s*(?:a\s+|an\s+)?(?:new\s+)?"
                  r"(?:column\s+)?(.+?)\s*(?:=|\bas\b)\s+(.+)$", text, re.I)
    if not m:
        return None
    name = re.sub(r"\s+column$", "", m.group(1).strip().strip("'\""), flags=re.I).strip()
    expr, used = _wrap_columns(m.group(2).strip().rstrip("."), columns)
    # only a formula if it references at least one real column and has math/an operator
    if not name or not used or not re.search(r"[-+*/(){}]", expr):
        return None
    return {"action": "add_formula_column", "name": name, "formula": expr}


def _m_merge(text, low, columns, structure):
    primary, allcols = _tables(structure)
    if len(allcols) < 2:
        return None
    if not re.search(r"\b(merge|combine|join|stack)\b", low):
        return None
    names = list(allcols.keys())
    if re.search(r"\b(separate|own|different|each).{0,10}\b(sheet|tab)", low) or "combine sheets" in low:
        return {"action": "combine_sheets", "sheet_tables": names}
    return {"action": "merge", "merge_tables": names}


def _m_lookup(text, low, columns, structure):
    primary, allcols = _tables(structure)
    if len(allcols) < 2 or not re.search(r"\b(lookup|vlookup|xlookup|bring|fetch|pull|get)\b", low):
        return None
    here = allcols.get(primary, [])
    # find a return column that lives in ANOTHER table and is named in the instruction
    for src, scols in allcols.items():
        if src == primary:
            continue
        ret = next((c for c in sorted(scols, key=len, reverse=True)
                    if c.lower() in low and c not in here), None)
        if not ret:
            continue
        # a key column present in BOTH tables (case/space-insensitive)
        def norm(s):
            return re.sub(r"[^a-z0-9]", "", s.lower())
        here_norm = {norm(c): c for c in here}
        key = next((here_norm[norm(c)] for c in scols if norm(c) in here_norm), None)
        if key:
            skey = next(c for c in scols if norm(c) == norm(key))
            return {"action": "lookup", "key_column": key, "source_sheet": src,
                    "source_key_column": skey, "return_column": ret}
    return None


def _m_sort(text, low, columns, structure):
    if not (re.search(r"\bsort\b|\border by\b|\barrange\b", low) or any(p in low for p in _SORT_PHRASES)):
        return None
    col = _find_column(low, columns)
    if not col:
        return None
    order = "desc" if any(k in low for k in _DESC) else "asc"
    ops = [{"action": "sort", "columns": [col], "orders": [order]}]
    top = re.search(r"\b(?:top|first|keep|only)\s+(\d+)", low)
    if top:
        ops.append({"action": "limit", "count": int(top.group(1))})
    return ops


def _m_limit(text, low, columns, structure):
    top = re.search(r"\b(?:top|first|keep)\s+(\d+)\b", low)
    if top:
        return {"action": "limit", "count": int(top.group(1))}
    return None


def _m_filter(text, low, columns, structure):
    col = _find_column(low, columns)
    if not col:
        return None
    after = text[low.index(col.lower()) + len(col):].strip(" :")
    m = re.match(r"(>=|<=|>|<)\s*(-?\d[\d,]*\.?\d*)", after)
    if m:
        op = {">": "greater_than", "<": "less_than",
              ">=": "greater_or_equal", "<=": "less_or_equal"}[m.group(1)]
        return {"action": "filter", "conditions": [{"column": col, "operator": op, "value": m.group(2).replace(",", "")}]}
    m = re.match(r"=\s*(.+)", after)
    if m:
        val = m.group(1).strip().strip("'\"").split(" and ")[0].strip()
        if val:
            return {"action": "filter", "conditions": [{"column": col, "operator": "equals", "value": val}]}
    m = re.match(r"(greater than or equal|less than or equal|at least|at most|greater than|"
                 r"more than|less than|equals?|is|contains?)\s+(.+)", after, re.I)
    if m:
        phrase = m.group(1).lower()
        val = m.group(2).strip().strip("'\"").split(" and ")[0].split(" then ")[0].strip()
        if not val:
            return None
        op = {"greater than": "greater_than", "more than": "greater_than", "less than": "less_than",
              "greater than or equal": "greater_or_equal", "at least": "greater_or_equal",
              "less than or equal": "less_or_equal", "at most": "less_or_equal",
              "contain": "contains", "contains": "contains"}.get(phrase, "equals")
        return {"action": "filter", "conditions": [{"column": col, "operator": op, "value": val}]}
    return None
