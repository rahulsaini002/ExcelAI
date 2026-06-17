"""Reads an uploaded spreadsheet into a DataFrame and summarizes its structure.

The structure summary is what the LLM sees — it never sees the raw file. We
hand it column names, inferred types, and a few sample rows so it can map a
plain-language instruction onto real columns.
"""
from __future__ import annotations

import io
import math
import re
import warnings
from dataclasses import dataclass

import pandas as pd

# pandas auto-names columns with no header "Unnamed: 0", "Unnamed: 1", ...
_UNNAMED = re.compile(r"^Unnamed: \d+$")


def _col_letter(i: int) -> str:
    """0-based index -> Excel column letter (0->A, 1->B, 26->AA)."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _name_blank_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Give leftover blank-header columns a friendly name like 'Column C' (1.1-d).
    Runs AFTER header recovery so it doesn't suppress the title-row fix."""
    cols = list(df.columns)
    if any(_UNNAMED.match(str(c)) for c in cols):
        df = df.copy()
        df.columns = [
            f"Column {_col_letter(i)}" if _UNNAMED.match(str(c)) else c
            for i, c in enumerate(cols)
        ]
    return df


def _json_safe(v):
    """Make a cell value safe for JSON (dates -> str, NaN -> None, numpy -> python)."""
    if v is None:
        return None
    if hasattr(v, "item"):  # numpy scalar
        try:
            v = v.item()
        except Exception:
            pass
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, (bool, int, str)):
        return v
    return str(v)


@dataclass
class LoadedSheet:
    df: pd.DataFrame  # the primary sheet (first sheet) — what operations act on
    filename: str
    ext: str  # "csv" or "xlsx"
    sheets: dict[str, pd.DataFrame]  # all sheets by name (for cross-sheet lookup)


@dataclass
class LoadedData:
    """Everything from one or more uploaded files, as named tables.

    Each sheet of each file becomes one table. `primary` is the table operations
    act on by default (the first sheet of the first file uploaded).
    """
    tables: dict[str, pd.DataFrame]
    primary: str
    exts: dict[str, str]  # table name -> "csv" or "xlsx" (for choosing output format)


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize column names and drop pandas' phantom empty columns.

    When a sheet's data doesn't start in column A (e.g. blank columns to the
    left), pandas reads those blanks as columns auto-named "Unnamed: 0", etc.
    They aren't real data, so we drop the ones that are auto-named AND entirely
    empty — otherwise those invented names would be written into the output.
    """
    df.columns = [str(c).strip() for c in df.columns]
    phantom = [c for c in df.columns if _UNNAMED.match(c) and df[c].isna().all()]
    if phantom:
        df = df.drop(columns=phantom)
    return df


def _fix_header(df: pd.DataFrame) -> pd.DataFrame:
    """Recover the real header when a file has a title/banner row above it.

    Some spreadsheets put a title in row 1 and the actual column names in row 2.
    pandas then reads the title row as the header, producing mostly "Unnamed: N"
    columns while the real names sit in the first data row. When we detect that
    pattern, we promote the first data row to be the header.
    """
    if len(df) < 2:
        return df
    cols = list(df.columns)
    unnamed = [c for c in cols if _UNNAMED.match(str(c))]
    named = [c for c in cols if not _UNNAMED.match(str(c))]
    # Only act when the header is mostly blank AND the first row looks like a
    # fuller set of real labels — that's the signature of a title row above.
    first = df.iloc[0]
    first_nonnull = int(first.notna().sum())
    if (
        len(unnamed) >= max(1, len(named))
        and first_nonnull > len(named)
        and first_nonnull >= len(cols) - 1
    ):
        new_cols = [
            str(v).strip() if pd.notna(v) else c for v, c in zip(first, cols)
        ]
        df = df.iloc[1:].copy()
        df.columns = new_cols
        df = df.reset_index(drop=True)
        df = _clean_columns(df)
    return df


def load_spreadsheet(data: bytes, filename: str) -> LoadedSheet:
    """Parse raw upload bytes into DataFrame(s). Supports .csv, .xlsx, .xls.

    The first sheet is the one operations act on by default; all sheets are kept
    so cross-sheet lookups can reach the others.
    """
    name = (filename or "").lower()
    if name.endswith(".csv"):
        try:
            raw = pd.read_csv(io.BytesIO(data))
        except Exception as exc:
            raise ValueError(
                f"Couldn't read '{filename}' — it may be empty or corrupted. "
                "Try opening it and re-saving as CSV."
            ) from exc
        df = _name_blank_columns(_fix_header(_clean_columns(raw)))
        return LoadedSheet(df=df, filename=filename, ext="csv", sheets={"Sheet1": df})

    if name.endswith(".xlsx") or name.endswith(".xlsm") or name.endswith(".xls"):
        try:
            # sheet_name=None reads every sheet into a {name: DataFrame} dict.
            all_sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
        except Exception as exc:
            raise ValueError(
                f"Couldn't read '{filename}' — it may be corrupted or not a real "
                "Excel file. Try opening it in Excel and re-saving."
            ) from exc
        sheets = {
            str(n): _name_blank_columns(_fix_header(_clean_columns(d)))
            for n, d in all_sheets.items()
        }
        first = next(iter(sheets.values()))
        return LoadedSheet(df=first, filename=filename, ext="xlsx", sheets=sheets)

    raise ValueError(
        "Unsupported file type. Please upload an Excel (.xlsx, .xlsm, .xls) or CSV file."
    )


def _base_name(filename: str) -> str:
    """Turn 'C:/path/Sales Jan.xlsx' into a clean table name 'Sales Jan'."""
    name = (filename or "table").replace("\\", "/").split("/")[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name.strip() or "table"


def _unique_name(name: str, taken: dict) -> str:
    """Ensure a table name doesn't collide with one we already have."""
    if name not in taken:
        return name
    i = 2
    while f"{name} ({i})" in taken:
        i += 1
    return f"{name} ({i})"


def load_files(files: list[tuple[str, bytes]]) -> LoadedData:
    """Load several uploaded files into one namespace of named tables.

    A single-sheet file becomes one table named after the file. A multi-sheet
    workbook becomes one table per sheet, named "<file> - <sheet>".
    """
    tables: dict[str, pd.DataFrame] = {}
    exts: dict[str, str] = {}
    primary: str | None = None

    for filename, data in files:
        loaded = load_spreadsheet(data, filename)
        base = _base_name(filename)
        multi = len(loaded.sheets) > 1
        for sheet_name, df in loaded.sheets.items():
            label = f"{base} - {sheet_name}" if multi else base
            label = _unique_name(label, tables)
            tables[label] = df
            exts[label] = loaded.ext
            if primary is None:
                primary = label

    if not tables:
        raise ValueError("No readable spreadsheet data was found in the upload.")
    return LoadedData(tables=tables, primary=primary, exts=exts)


def summarize_tables(tables: dict[str, pd.DataFrame], primary: str) -> dict:
    """Describe every table for the LLM, marking which one is the working table."""
    return {
        "primary_table": primary,
        "tables": {name: summarize_structure(df) for name, df in tables.items()},
    }


def summarize_structure(df: pd.DataFrame, sample_rows: int = 5) -> dict:
    """Build a compact, JSON-serializable description of the sheet (for the LLM and
    the upload preview). Sample values are made JSON-safe (dates->str, NaN->None)."""
    columns = [{"name": str(c), "type": _friendly_dtype(df[c])} for c in df.columns]

    head = df.head(sample_rows).to_dict(orient="records")
    sample = [{str(k): _json_safe(v) for k, v in row.items()} for row in head]

    return {
        "row_count": int(len(df)),
        "columns": columns,
        "sample_rows": sample,
    }


def _friendly_dtype(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"
    # Object/text column: sniff whether every value is really a number or a date
    # (e.g. CSV columns, where everything arrives as text). Mixed -> text (safest).
    nonnull = series.dropna()
    if len(nonnull) == 0:
        return "text"
    if pd.to_numeric(nonnull, errors="coerce").notna().all():
        return "number"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if pd.to_datetime(nonnull, errors="coerce").notna().all():
            return "date"
    return "text"
