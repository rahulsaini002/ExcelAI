"""Data quality & observability (Phase 3.11).

Watches a dataset across refreshes and surfaces three things that quietly break reports:

  • Schema changes   — columns added / removed / retyped between a baseline snapshot and a
    new one. Column REORDERING is deliberately NOT a change (a common false alarm).
  • Missing-data spikes — a column's blank rate jumping sharply (e.g. 2% → 60%), gated by
    both a minimum jump AND a minimum new rate so noise doesn't trip it.
  • Staleness         — an accurate "last updated", and a flag when it's older than a
    threshold.

All logic is pure and timestamp-injectable so it's deterministic to test. `profile` builds
a lightweight snapshot; `assess` diffs two snapshots into an alarm report. Removed/retyped
columns and real spikes are alarms; ADDED columns are reported as info, not alarms — adding
a column rarely breaks anything, and treating it as an alarm just trains people to ignore them.
"""
from __future__ import annotations

import pandas as pd

from .reader import _friendly_dtype

# Spike thresholds — tuned to minimise false alarms.
_MIN_RATE_INCREASE = 0.20   # blank rate must jump at least 20 percentage points
_MIN_NEW_RATE = 0.10        # ...and end up at least 10% blank to matter
_DEFAULT_MAX_AGE = 24 * 3600  # "stale" after a day by default


def _blank_mask(series: pd.Series) -> pd.Series:
    return series.isna() | (series.astype(str).str.strip() == "")


def profile(tables: dict[str, pd.DataFrame]) -> dict:
    """A compact, comparable snapshot of every table: columns + types, row count, and the
    blank count/rate per column."""
    out: dict = {}
    for name, df in tables.items():
        n = int(len(df))
        columns = [{"name": str(c), "type": _friendly_dtype(df[c])} for c in df.columns]
        blanks = {str(c): int(_blank_mask(df[c]).sum()) for c in df.columns}
        blank_rate = {c: (blanks[c] / n if n else 0.0) for c in blanks}
        out[str(name)] = {
            "row_count": n,
            "columns": columns,
            "blanks": blanks,
            "blank_rate": blank_rate,
        }
    return out


def _schema_diff(old_cols: list[dict], new_cols: list[dict]) -> dict:
    old_map = {c["name"]: c["type"] for c in old_cols}
    new_map = {c["name"]: c["type"] for c in new_cols}
    added = [c for c in new_map if c not in old_map]
    removed = [c for c in old_map if c not in new_map]
    type_changed = [
        {"column": c, "from": old_map[c], "to": new_map[c]}
        for c in new_map
        if c in old_map and new_map[c] != old_map[c]
    ]
    return {"added": added, "removed": removed, "type_changed": type_changed}


def compare_schema(old: dict, new: dict) -> dict:
    """Diff two profiles' schemas. Reordered columns produce NO diff (compared by name)."""
    tables_added = [t for t in new if t not in old]
    tables_removed = [t for t in old if t not in new]
    per_table: dict = {}
    for t in new:
        if t in old:
            d = _schema_diff(old[t]["columns"], new[t]["columns"])
            if d["added"] or d["removed"] or d["type_changed"]:
                per_table[t] = d
    changed = bool(tables_added or tables_removed or per_table)
    return {
        "tables_added": tables_added,
        "tables_removed": tables_removed,
        "columns": per_table,
        "changed": changed,
    }


def missing_spikes(
    old: dict, new: dict,
    min_increase: float = _MIN_RATE_INCREASE, min_rate: float = _MIN_NEW_RATE,
) -> list[dict]:
    """Columns whose blank rate jumped sharply. Gated by both a minimum increase and a
    minimum resulting rate, so small fluctuations never alarm."""
    spikes: list[dict] = []
    for t in new:
        if t not in old:
            continue
        old_rate, new_rate = old[t]["blank_rate"], new[t]["blank_rate"]
        for col, nr in new_rate.items():
            orr = old_rate.get(col)
            if orr is None:  # newly added column — schema diff covers it
                continue
            if nr - orr >= min_increase and nr >= min_rate:
                spikes.append({
                    "table": t, "column": col,
                    "from": round(orr, 3), "to": round(nr, 3),
                    "increase": round(nr - orr, 3),
                })
    return spikes


def _humanize(age: float) -> str:
    age = max(0.0, age)
    if age < 60:
        return "just now" if age < 5 else f"{int(age)} seconds ago"
    if age < 3600:
        m = int(age // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if age < 86400:
        h = int(age // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = int(age // 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"


def staleness(updated_at: float, now: float, max_age_seconds: float = _DEFAULT_MAX_AGE) -> dict:
    """Accurate 'last updated' + a stale flag when older than the threshold."""
    age = max(0.0, now - updated_at)
    return {
        "last_updated": updated_at,
        "age_seconds": age,
        "human": _humanize(age),
        "max_age_seconds": max_age_seconds,
        "stale": age > max_age_seconds,
    }


def assess(
    old: dict, new: dict, updated_at: float, now: float,
    max_age_seconds: float = _DEFAULT_MAX_AGE,
) -> dict:
    """Diff baseline `old` vs current `new` into an observability report: schema changes,
    missing-data spikes, staleness, and a de-duplicated alarm list (with severities).
    Added columns are surfaced as info, not alarms — to keep alarms meaningful."""
    schema = compare_schema(old, new)
    spikes = missing_spikes(old, new)
    stale = staleness(updated_at, now, max_age_seconds)

    alarms: list[dict] = []
    info: list[str] = []

    for t in schema["tables_removed"]:
        alarms.append({"severity": "high", "kind": "schema", "message": f"Table '{t}' is gone."})
    for t in schema["tables_added"]:
        info.append(f"New table '{t}'.")
    for t, d in schema["columns"].items():
        for c in d["removed"]:
            alarms.append({"severity": "high", "kind": "schema",
                           "message": f"Column '{c}' was removed from {t}."})
        for tc in d["type_changed"]:
            alarms.append({"severity": "high", "kind": "schema",
                           "message": f"Column '{tc['column']}' in {t} changed type: {tc['from']} → {tc['to']}."})
        for c in d["added"]:
            info.append(f"New column '{c}' in {t}.")

    for s in spikes:
        alarms.append({
            "severity": "warning", "kind": "missing",
            "message": (
                f"Missing-data spike in {s['table']}.{s['column']}: "
                f"{round(s['from'] * 100)}% → {round(s['to'] * 100)}% blank."
            ),
        })

    if stale["stale"]:
        alarms.append({"severity": "warning", "kind": "staleness",
                       "message": f"Data looks stale — last updated {stale['human']}."})

    return {
        "schema_changes": schema,
        "missing_spikes": spikes,
        "staleness": stale,
        "alarms": alarms,
        "info": info,
        "ok": len(alarms) == 0,
    }
