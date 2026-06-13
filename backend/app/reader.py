"""Reads an uploaded spreadsheet into a DataFrame and summarizes its structure.

The structure summary is what the LLM sees — it never sees the raw file. We
hand it column names, inferred types, and a few sample rows so it can map a
plain-language instruction onto real columns.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import pandas as pd


@dataclass
class LoadedSheet:
    df: pd.DataFrame
    filename: str
    ext: str  # "csv" or "xlsx"


def load_spreadsheet(data: bytes, filename: str) -> LoadedSheet:
    """Parse raw upload bytes into a DataFrame. Supports .csv and .xlsx."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(data))
        ext = "csv"
    elif name.endswith(".xlsx") or name.endswith(".xls"):
        # First sheet only for v1.
        df = pd.read_excel(io.BytesIO(data))
        ext = "xlsx"
    else:
        raise ValueError(
            "Unsupported file type. Please upload a .csv or .xlsx file."
        )

    # Normalize column names to strings and strip surrounding whitespace.
    df.columns = [str(c).strip() for c in df.columns]
    return LoadedSheet(df=df, filename=filename, ext=ext)


def summarize_structure(df: pd.DataFrame, sample_rows: int = 3) -> dict:
    """Build a compact, JSON-serializable description of the sheet for the LLM."""
    columns = []
    for col in df.columns:
        columns.append({"name": col, "type": _friendly_dtype(df[col])})

    # A few example rows so the model can see the actual data shape.
    sample = (
        df.head(sample_rows)
        .astype(object)
        .where(pd.notnull(df.head(sample_rows)), None)
        .to_dict(orient="records")
    )

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
    return "text"
