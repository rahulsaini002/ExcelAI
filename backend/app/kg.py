"""Knowledge graph (Phase 5.4).

Treats each sheet as an ENTITY and infers the RELATIONSHIPS between them — the foreign-key
links a spreadsheet has but never declares — so a user can ask a relational question ("bring
in each sale's customer email") without ever writing a join. Sumio figures out which columns
connect the tables and does the lookup for them.

  entities        each table + its columns + its candidate KEY columns (near-unique, or
                  id-named with high uniqueness).
  relationships   A.col → B.key when A.col's values are largely contained in B's key (a
                  foreign-key pattern), matched the SAME normalized way the executor's lookup
                  joins (via _norm_key), so a detected link always actually resolves — plus a
                  name-match signal (Customer_ID→Customer_ID) to avoid coincidental overlap.
  auto_lookup     "relational query without joins": name a field in a RELATED table and get a
                  ready-to-run lookup op with the join keys filled in — or an honest, specific
                  decline when there's no path or it's ambiguous.

Honest by construction: it only asserts a relationship when the key values genuinely line up;
it never fabricates a join, and it refuses ambiguous ones rather than guessing.
"""
from __future__ import annotations

import pandas as pd

from .executor import _norm_key

# Column-name fragments that hint "this is an identifier".
_ID_HINTS = ("id", "code", "key", "no", "number", "email", "sku", "uuid")


def _norm_col(name) -> str:
    return str(name).strip().lower().replace(" ", "_")


def _key_ratio(df: pd.DataFrame, col) -> float:
    s = df[col].dropna()
    if len(s) == 0:
        return 0.0
    return s.nunique() / len(s)


def _id_like(col) -> bool:
    nm = _norm_col(col)
    return nm in _ID_HINTS or nm == "id" or any(nm.endswith("_" + h) or nm == h for h in _ID_HINTS)


def candidate_keys(df: pd.DataFrame) -> list[str]:
    """Columns that identify a row: id-named columns with high uniqueness, or any near-unique
    column — EXCEPT a unique numeric column that isn't id-named, which is almost always a
    measure (Amount, Price) that happens to be distinct in a small sample, not an identifier.
    Excluding those keeps inferred relationships from latching onto coincidental number
    overlaps. These are the columns other tables can point AT."""
    keys: list[str] = []
    for col in df.columns:
        ratio = _key_ratio(df, col)
        if _id_like(col):
            if ratio >= 0.8:
                keys.append(str(col))
            continue
        is_numeric_measure = pd.api.types.is_numeric_dtype(df[col])
        if ratio >= 0.99 and not is_numeric_measure:
            keys.append(str(col))
    return keys


def _valset(df: pd.DataFrame, col) -> set:
    """Normalized non-blank values of a column, matching how the executor's lookup joins."""
    return {k for k in _norm_key(df[col]) if k is not None and str(k).strip() != ""}


def relationships(tables: dict[str, pd.DataFrame], min_coverage: float = 0.6) -> list[dict]:
    """Foreign-key-style links A.col → B.key: A.col's values are largely found in B's key.
    Accepts a link when coverage clears the bar AND either the column names align or coverage
    is very high (so coincidental value overlap doesn't invent a relationship). At most one
    link per source column (the best-scoring target)."""
    tkeys = {t: set(candidate_keys(df)) for t, df in tables.items()}
    cache: dict[tuple, set] = {}

    def vset(t, col):
        key = (t, col)
        if key not in cache:
            cache[key] = _valset(tables[t], col)
        return cache[key]

    best_per_source: dict[tuple, dict] = {}
    for ta, da in tables.items():
        for ca in da.columns:
            avals = vset(ta, ca)
            if not avals:
                continue
            for tb in tables:
                if tb == ta:
                    continue
                for cb in tkeys[tb]:
                    bvals = vset(tb, cb)
                    if not bvals:
                        continue
                    coverage = len(avals & bvals) / len(avals)
                    name_match = _norm_col(ca) == _norm_col(cb)
                    if coverage >= min_coverage and (name_match or coverage >= 0.9):
                        score = coverage + (0.5 if name_match else 0.0)
                        cur = best_per_source.get((ta, str(ca)))
                        if cur is None or score > cur["_score"]:
                            best_per_source[(ta, str(ca))] = {
                                "from_table": ta, "from_column": str(ca),
                                "to_table": tb, "to_column": str(cb),
                                "coverage": round(coverage, 3),
                                "name_match": name_match,
                                "kind": "many_to_one", "_score": score,
                            }
    rels = []
    for r in best_per_source.values():
        r.pop("_score", None)
        rels.append(r)
    return rels


def graph(tables: dict[str, pd.DataFrame]) -> dict:
    """The knowledge graph: entities (tables + columns + keys) and their relationships."""
    entities = [
        {
            "table": t,
            "row_count": int(len(df)),
            "columns": [str(c) for c in df.columns],
            "keys": candidate_keys(df),
        }
        for t, df in tables.items()
    ]
    return {"entities": entities, "relationships": relationships(tables)}


def _has_col(df: pd.DataFrame, field: str) -> bool:
    return field in {str(c) for c in df.columns}


def field_holders(tables: dict[str, pd.DataFrame], field: str, exclude: str | None = None) -> list[str]:
    return [t for t, df in tables.items() if t != exclude and _has_col(df, field)]


def auto_lookup(tables: dict[str, pd.DataFrame], from_table: str, field: str) -> tuple[dict, dict]:
    """Relational query without a join: return a lookup op that brings `field` from a RELATED
    table into `from_table`, with the join keys resolved automatically, plus the relationship
    used. Raises ValueError with a specific reason when it can't be done honestly."""
    if from_table not in tables:
        raise ValueError(f"There's no table named '{from_table}'.")
    if _has_col(tables[from_table], field):
        raise ValueError(f"'{field}' is already in '{from_table}'.")
    holders = field_holders(tables, field, exclude=from_table)
    if not holders:
        raise ValueError(f"No table has a '{field}' column to bring in.")
    rels = [r for r in relationships(tables) if r["from_table"] == from_table and r["to_table"] in holders]
    if not rels:
        raise ValueError(f"I can't see how '{from_table}' relates to a table containing '{field}'.")
    targets = {r["to_table"] for r in rels}
    if len(targets) > 1:
        raise ValueError(
            f"'{field}' exists in several tables related to '{from_table}' "
            f"({', '.join(sorted(targets))}) — tell me which one to use."
        )
    r = max(rels, key=lambda x: x["coverage"])
    op = {
        "action": "lookup",
        "key_column": r["from_column"],
        "source_sheet": r["to_table"],
        "source_key_column": r["to_column"],
        "return_column": field,
        "new_column": field,
    }
    return op, {k: r[k] for k in ("from_table", "from_column", "to_table", "to_column", "coverage")}
