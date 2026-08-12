"""Engine Phase 1.3 — split a column, merge columns, and fill-by-example (Areas 3-4).

split_column   {"column", "new_columns", "delimiter" | "widths" | "pattern", "keep_original"}
merge_columns  {"columns", "name", "separator", "keep_original"}  (also emits a live
               TEXTJOIN formula via the Phase-1.1 directive when the sources are kept)
fill_by_example {"column", "name", "examples": [{"input","output"}, ...]}

Fill-by-example is TRUSTED-CODE induction (Flash-Fill style), never model guessing:
the input is tokenized, the outputs are parsed into a template of [token | token-prefix |
literal] pieces with case transforms, and the template must reproduce EVERY example
exactly — otherwise we ask for better examples instead of inventing values.
"""
from __future__ import annotations

import re

import pandas as pd

from .base import OperationError

_TOKEN_SPLIT = re.compile(r"[\s,;:@._/\-]+")
_CASES = {
    "same": lambda s: s,
    "lower": str.lower,
    "upper": str.upper,
    "title": lambda s: s[:1].upper() + s[1:].lower(),
}


# ---------------------------------------------------------------- split_column ----------
def split_column(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    col = (op.get("column") or "").strip()
    new_cols = [c.strip() for c in (op.get("new_columns") or []) if str(c).strip()]
    if col not in df.columns:
        raise OperationError(f"I couldn't find the column '{col}'.")
    if len(new_cols) < 2:
        raise OperationError("Splitting needs at least two new column names (e.g. First, Last).")
    clash = [c for c in new_cols if c in df.columns]
    if clash:
        raise OperationError(
            f"The column{'s' if len(clash) != 1 else ''} {', '.join(clash)} already exist"
            f"{'' if len(clash) != 1 else 's'} — pick different names for the split parts."
        )

    s = df[col].astype("string")
    widths = op.get("widths")
    pattern = op.get("pattern")
    delimiter = op.get("delimiter")

    if widths:
        if len(widths) != len(new_cols):
            raise OperationError("Fixed-width split needs one width per new column.")
        parts = pd.DataFrame(index=df.index)
        start = 0
        for i, w in enumerate(widths):
            parts[i] = s.str.slice(start, start + int(w)).str.strip()
            start += int(w)
        how = f"at fixed widths {widths}"
    elif pattern:
        try:
            rx = re.compile(str(pattern))
        except re.error as exc:
            raise OperationError(f"That pattern isn't a valid regular expression ({exc}).")
        if rx.groups != len(new_cols):
            raise OperationError(
                f"The pattern captures {rx.groups} group{'s' if rx.groups != 1 else ''} "
                f"but there are {len(new_cols)} new columns — they must match."
            )
        parts = s.str.extract(rx)
        parts.columns = range(len(new_cols))
        how = f"with the pattern {pattern}"
    else:
        if delimiter is None or str(delimiter) == "":
            # Infer: try the common delimiters and take the one present in most rows.
            best, best_hits = None, 0
            for cand in [", ", ",", " ", ";", "|", "-", "/", "\t"]:
                hits = int(s.str.contains(re.escape(cand), na=False).sum())
                if hits > best_hits:
                    best, best_hits = cand, hits
            nonblank = int((s.str.strip() != "").sum())
            if not best or best_hits < max(1, nonblank // 2):
                raise OperationError(
                    f"I couldn't tell what separates the parts in '{col}' — tell me the "
                    "delimiter (e.g. a space, a comma, or a dash)."
                )
            delimiter = best
        parts = s.str.split(re.escape(str(delimiter)), n=len(new_cols) - 1, expand=True, regex=True)
        # A one-part row yields fewer columns; make sure the frame is wide enough.
        for i in range(len(new_cols)):
            if i not in parts.columns:
                parts[i] = pd.NA
        parts = parts[range(len(new_cols))].apply(lambda c: c.str.strip())
        how = f"on '{delimiter}'"

    # Rows whose source has text but whose LAST part came out empty didn't have all the
    # pieces (works for all three modes — split gives NA, slicing gives "").
    last_part = parts[len(new_cols) - 1].astype("string")
    src_ok = s.str.strip().fillna("") != ""
    missed = int((src_ok & (last_part.isna() | (last_part.str.strip() == ""))).sum())
    out = df.copy()
    insert_at = list(df.columns).index(col) + 1
    for i, name in enumerate(new_cols):
        out.insert(insert_at + i, name, parts[i])
    if not op.get("keep_original", True):
        out = out.drop(columns=[col])

    note = f"Split '{col}' {how} into {', '.join(new_cols)}."
    if missed > 0:
        note += (f" {missed:,} row{'s' if missed != 1 else ''} didn't have all the parts "
                 "— the missing pieces are left blank.")
    return out, note


# --------------------------------------------------------------- merge_columns ----------
def merge_columns(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, dict | None]:
    cols = [c for c in (op.get("columns") or []) if str(c).strip()]
    name = (op.get("name") or "").strip() or "Combined"
    sep = op.get("separator")
    sep = ", " if sep is None else str(sep)
    if len(cols) < 2:
        raise OperationError("Combining needs at least two columns.")
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise OperationError(f"I couldn't find the column{'s' if len(missing) != 1 else ''} "
                             f"{', '.join(missing)}.")
    if name in df.columns and not op.get("overwrite"):
        raise OperationError(
            f"A column called '{name}' already exists. Use a different name, "
            "or confirm you want to overwrite it."
        )

    def txt(c):
        s = df[c]
        if pd.api.types.is_numeric_dtype(s):
            return s.map(lambda x: "" if pd.isna(x)
                         else (str(int(x)) if float(x).is_integer() else str(x)))
        return s.astype("string").fillna("").str.strip()

    pieces = [txt(c) for c in cols]
    joined = [sep.join(p for p in row if p != "") for row in zip(*[p.tolist() for p in pieces])]
    out = df.copy()
    out[name] = joined

    directive = None
    if op.get("keep_original", True):
        # Bonus from Phase 1.1: the saved file gets a LIVE TEXTJOIN formula (blanks
        # skipped, same as the computed values). Sources dropped? Then values only.
        refs = ", ".join("{" + c + "}" for c in cols)
        sep_escaped = sep.replace('"', '""')
        directive = {"type": "formula", "column": name,
                     "formula": f'TEXTJOIN("{sep_escaped}", TRUE, {refs})', "spill": False}
    else:
        out = out.drop(columns=cols)

    note = f"Combined {', '.join(cols)} into '{name}' separated by '{sep}' (blanks skipped)."
    return out, note, directive


# ------------------------------------------------------------- fill_by_example ----------
def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(str(text).strip()) if t]


def _parse_template(inp: str, outp: str) -> list | None:
    """Parse `outp` as a sequence of pieces referencing `inp`'s tokens:
    ("tok", i, case) | ("pre", i, length, case) | ("lit", char). Greedy, longest-first.
    Returns None if the output can't be explained at all (pure literals don't count as
    an explanation unless nothing else exists)."""
    toks = _tokens(inp)
    pieces: list = []
    pos = 0
    while pos < len(outp):
        best = None  # (consumed_len, piece)
        for i, tok in enumerate(toks):
            for case, fn in _CASES.items():
                cand = fn(tok)
                if cand and outp.startswith(cand, pos):
                    if best is None or len(cand) > best[0]:
                        best = (len(cand), ("tok", i, case))
                # token prefixes, 1-4 chars (initials, short codes like "NOR")
                for plen in range(min(4, len(tok)) - 1, 0, -1):
                    c2 = fn(tok[:plen])
                    if c2 and outp.startswith(c2, pos):
                        if best is None or len(c2) > best[0]:
                            best = (len(c2), ("pre", i, plen, case))
        if best and best[0] > 0:
            pieces.append(best[1])
            pos += best[0]
        else:
            pieces.append(("lit", outp[pos]))
            pos += 1
    return pieces


def _apply_template(pieces: list, inp: str) -> str | None:
    toks = _tokens(inp)
    out = []
    for p in pieces:
        if p[0] == "lit":
            out.append(p[1])
            continue
        idx = p[1]
        if idx >= len(toks):
            return None  # this row doesn't have enough parts
        tok = toks[idx]
        if p[0] == "tok":
            out.append(_CASES[p[2]](tok))
        else:  # prefix
            if p[2] > len(tok):
                return None
            out.append(_CASES[p[3]](tok[: p[2]]))
    return "".join(out)


def fill_by_example(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    col = (op.get("column") or "").strip()
    name = (op.get("name") or "").strip()
    examples = op.get("examples") or []
    if col not in df.columns:
        raise OperationError(f"I couldn't find the column '{col}'.")
    if not name:
        raise OperationError("What should the new column be called?")
    if name in df.columns and not op.get("overwrite"):
        raise OperationError(
            f"A column called '{name}' already exists. Use a different name, "
            "or confirm you want to overwrite it."
        )
    pairs = [(str(e.get("input", "")).strip(), str(e.get("output", "")).strip())
             for e in examples if str(e.get("input", "")).strip() and str(e.get("output", "")).strip()]
    if not pairs:
        raise OperationError(
            'Fill-by-example needs at least one example, like {"input": "Asha Sharma", '
            '"output": "asha.sharma"}.'
        )

    template = _parse_template(*pairs[0])
    token_pieces = [p for p in (template or []) if p[0] != "lit"]
    # Evidence guard: the output must be MEANINGFULLY derived from the input — pieces
    # spanning at least two different tokens, or one piece of 2+ characters. Otherwise a
    # stray matching letter ("banana" sharing an 'a' with "Asha") would "explain" junk.
    toks0 = _tokens(pairs[0][0])
    piece_len = lambda p: len(toks0[p[1]]) if p[0] == "tok" else p[2]  # noqa: E731
    distinct = {p[1] for p in token_pieces}
    if (template is None or not token_pieces
            or sum(piece_len(p) for p in token_pieces) < 2
            or (len(distinct) < 2 and max(piece_len(p) for p in token_pieces) < 2)):
        raise OperationError(
            f"I couldn't see how '{pairs[0][1]}' comes from '{pairs[0][0]}' — "
            "the output doesn't seem to use any part of the input. Could you re-check "
            "the example?"
        )
    # The template must reproduce EVERY example exactly — otherwise we'd be guessing.
    for inp, outp in pairs:
        got = _apply_template(template, inp)
        if got != outp:
            raise OperationError(
                "Your examples don't follow one pattern I can find "
                f"('{inp}' would give '{got or '?'}', not '{outp}'). Could you give me "
                "two examples that follow the same rule?"
            )

    s = df[col].astype("string")
    results = [(_apply_template(template, v) if pd.notna(v) and str(v).strip() else None)
               for v in s.tolist()]
    unmatched = sum(1 for v, r in zip(s.tolist(), results)
                    if pd.notna(v) and str(v).strip() and r is None)

    out = df.copy()
    out[name] = results
    note = (f"Filled '{name}' from '{col}' using the pattern in your "
            f"example{'s' if len(pairs) != 1 else ''}.")
    if len(pairs) == 1:
        note += " (Inferred from a single example — worth a quick check; a second example makes it certain.)"
    if unmatched:
        note += (f" {unmatched:,} row{'s' if unmatched != 1 else ''} didn't fit the "
                 "pattern and were left blank.")
    return out, note
