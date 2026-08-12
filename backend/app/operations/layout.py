"""Engine Phase 1.4 — sheet layout & formatting polish (Areas 5, 14).

One op, several optional features (at least one required):
  freeze        "header" | "first_column" | "both" | a cell like "B3"
  autofit       true — column widths computed from the data (openpyxl has no native autofit)
  borders       "all" (grid) | "outline" (box around the used range)
  header_fill   a named color for the header row (+ bold, via the serializer)
  title         inserts a merged, centered title row ABOVE the headers in the saved file
                (the serializer shifts already-written live formulas and CF rules down)
  merge_range   merge an arbitrary range like "A10:D10" — allowed only where it doesn't
                cover existing data (the title feature is the safe way to add one on top)

The DataFrame itself is unchanged — everything lands in the saved .xlsx via a "layout"
render directive, and the note says exactly what the saved file will contain.
"""
from __future__ import annotations

import re

import pandas as pd

from .base import OperationError
from .conditional_format import COLORS

_CELL = re.compile(r"^[A-Za-z]{1,3}[1-9]\d{0,6}$")
_RANGE = re.compile(r"^([A-Za-z]{1,3})([1-9]\d{0,6}):([A-Za-z]{1,3})([1-9]\d{0,6})$")
FREEZE_NAMES = {"header", "first_column", "both"}
_AREA = re.compile(r"([A-Za-z]{1,3}\d{1,7})\s*:\s*([A-Za-z]{1,3}\d{1,7})")


def _parse_print_setup(text: str) -> dict:
    """Turn a free-text print request ("landscape, fit to one page, repeat the header
    row, narrow margins") into structured page-setup settings. A single free-text field
    (not a nested model) keeps the response schema under Gemini's serving limit."""
    t = " " + text.lower() + " "
    ps: dict = {}
    if "landscape" in t:
        ps["orientation"] = "landscape"
    elif "portrait" in t:
        ps["orientation"] = "portrait"
    # fit to page (optionally "fit to N pages wide")
    if any(k in t for k in ("fit to", "fit on", "one page", "1 page", "single page",
                            "onto one", "on one page", "fit the")):
        m = re.search(r"(\d+)\s*page", t)
        ps["fit_wide"] = int(m.group(1)) if m else 1
    # repeat the header/title row on every page
    if (any(k in t for k in ("repeat", "every page", "each page", "on all pages")) and
            any(k in t for k in ("header", "title", "top row", "heading", "column name"))):
        ps["repeat_header"] = True
    # explicit print area range
    m = _AREA.search(text)
    if m and "repeat" not in t:  # a range after "repeat" is unusual; keep area for print-area intent
        ps["print_area"] = (m.group(1) + ":" + m.group(2)).upper()
    elif m:
        ps["print_area"] = (m.group(1) + ":" + m.group(2)).upper()
    # margins
    if "narrow" in t:
        ps["margins"] = "narrow"
    elif "wide margin" in t or "wide margins" in t:
        ps["margins"] = "wide"
    elif "normal margin" in t or "default margin" in t:
        ps["margins"] = "normal"
    # page numbers in the footer (very common); or explicit footer/header text
    if "page number" in t or "&p" in text.lower() or ("page" in t and " of " in t):
        ps["footer_text"] = "Page &P of &N"
    mh = re.search(r"header(?:\s+text)?\s*[:=]\s*(.+?)\s*$", text, re.I)
    if mh:
        ps["header_text"] = mh.group(1).strip().strip("\"'")
    mf = re.search(r"footer(?:\s+text)?\s*[:=]\s*(.+?)\s*$", text, re.I)
    if mf:
        ps["footer_text"] = mf.group(1).strip().strip("\"'")
    return ps


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n  # 1-based


def layout_format(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, dict]:
    freeze = (str(op.get("freeze")).strip() if op.get("freeze") else None)
    autofit = bool(op.get("autofit"))
    borders = (op.get("borders") or "").strip().lower() or None
    title = (op.get("title") or "").strip() or None
    merge_range = (op.get("merge_range") or "").strip().upper() or None
    header_fill = (op.get("header_fill") or "").strip().lower() or None
    print_text = (op.get("print_setup") or "").strip() or None
    print_ps = _parse_print_setup(print_text) if print_text else {}
    if print_text and not print_ps:
        raise OperationError(
            "I couldn't tell what print setting you meant — try 'landscape', 'fit to "
            "one page', 'repeat the header row', 'narrow margins', or a print area like "
            "A1:F50."
        )

    if not any([freeze, autofit, borders, title, merge_range, header_fill, print_ps]):
        raise OperationError(
            "Tell me what to change about the layout — e.g. freeze the header row, "
            "autofit the columns, add borders, color the header, add a title, or set up "
            "printing (landscape / fit to one page / repeat the header row)."
        )
    if freeze and freeze.lower() in FREEZE_NAMES:
        freeze = freeze.lower()
    elif freeze and not _CELL.match(freeze):
        raise OperationError(
            f"I didn't understand the freeze position '{freeze}' — say 'header', "
            "'first column', 'both', or a cell like B3."
        )
    if borders and borders not in ("all", "outline"):
        raise OperationError("Borders can be 'all' (a grid) or 'outline' (a box around the data).")
    if header_fill and header_fill not in COLORS:
        raise OperationError(
            f"I don't have the color '{header_fill}' — try "
            f"{', '.join(sorted(set(COLORS) - {'gray'}))}."
        )
    if merge_range:
        m = _RANGE.match(merge_range)
        if not m:
            raise OperationError(f"'{merge_range}' isn't a range I can merge — use the form A10:D10.")
        c1, r1, c2, r2 = _col_index(m.group(1)), int(m.group(2)), _col_index(m.group(3)), int(m.group(4))
        if c2 < c1 or r2 < r1:
            raise OperationError(f"The range '{merge_range}' is backwards — start at the top-left.")
        # Merging destroys every value except the top-left — refuse to cover data.
        # Sheet row 1 = headers, sheet row r>=2 = df row r-2 (before any title shift).
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                if r == r1 and c == c1:
                    continue
                if r == 1 and c <= len(df.columns):
                    raise OperationError(
                        f"Merging {merge_range} would wipe the header "
                        f"'{df.columns[c - 1]}'. To put a heading ABOVE the data, ask "
                        "for a title instead — I'll add a row for it."
                    )
                if 2 <= r <= len(df) + 1 and c <= len(df.columns):
                    v = df.iloc[r - 2, c - 1]
                    if pd.notna(v) and str(v).strip() != "":
                        raise OperationError(
                            f"Merging {merge_range} would wipe data in "
                            f"{df.columns[c - 1]} row {r - 1}. To put a heading ABOVE "
                            "the data, ask for a title instead."
                        )

    # Validate the print-area range shape early (so a bad one is a friendly message).
    if print_ps.get("print_area") and not _RANGE.match(print_ps["print_area"]):
        raise OperationError(
            f"'{print_ps['print_area']}' isn't a print area I can use — give a range like A1:F50."
        )

    directive = {
        "type": "layout", "freeze": freeze, "autofit": autofit, "borders": borders,
        "title": title, "merge_range": merge_range, "header_fill": header_fill,
        "print": print_ps or None,
    }
    bits = []
    if print_ps:
        pbits = []
        if print_ps.get("orientation"):
            pbits.append(print_ps["orientation"])
        if print_ps.get("fit_wide"):
            n = print_ps["fit_wide"]
            pbits.append("fit to one page wide" if n == 1 else f"fit to {n} pages wide")
        if print_ps.get("repeat_header"):
            pbits.append("the header row repeated on every page")
        if print_ps.get("print_area"):
            pbits.append(f"print area {print_ps['print_area']}")
        if print_ps.get("margins"):
            pbits.append(f"{print_ps['margins']} margins")
        if print_ps.get("header_text"):
            pbits.append(f"header “{print_ps['header_text']}”")
        if print_ps.get("footer_text"):
            pbits.append(f"footer “{print_ps['footer_text']}”")
        bits.append("print setup (" + ", ".join(pbits) + ")")
    if title:
        bits.append(f"a merged title row '{title}'")
    if freeze:
        bits.append({"header": "the header row frozen", "first_column": "the first column frozen",
                     "both": "the header row and first column frozen"}.get(freeze, f"panes frozen at {freeze}"))
    if autofit:
        bits.append("column widths auto-fitted")
    if borders:
        bits.append("a full border grid" if borders == "all" else "an outline border")
    if header_fill:
        bits.append(f"a {header_fill} header row")
    if merge_range:
        bits.append(f"{merge_range} merged")
    note = "Laid out the sheet: " + ", ".join(bits) + ". You'll see it in the saved Excel file."
    return df, note, directive
