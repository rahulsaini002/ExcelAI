"""Data lineage (Phase 5.3).

Answers "where does this value come from?" for any column, by walking the session's formula
dependency graph — the {column: formula} registry Sumio builds as add_formula_column ops run
(Phase 4.8). A derived column (Margin = {Rev} - {Cost}) traces to the columns its formula
references; each of THOSE traces further if it is itself derived (Rev = {Price} * {Qty}),
down to SOURCE columns (from the uploaded data, i.e. not a formula Sumio wrote).

Two views:
  • trace(column)          a value→source TREE (each node: derived + its formula, or a
                            source leaf), with cycle-safety so a self-referential overwrite
                            (Rev = {Rev} * 2) can't loop forever.
  • graph(formulas)        NODES + EDGES (source → derived) for a visual lineage graph.
  • source_columns(column)  the flat set of ROOT sources a column ultimately derives from.

Honest by construction: it only claims lineage Sumio actually created this session — it
never invents a source. Uploaded-file formulas aren't preserved (pandas loads values), so a
column with no known formula is reported as a source, which is the truthful statement.
"""
from __future__ import annotations

from .guardrails import _refs_in


def trace(column: str, formulas: dict, columns: set | None = None, _seen: tuple = ()) -> dict:
    """A value→source lineage tree for one column. `formulas` = {col: formula}; `columns`
    (optional) = the currently-present column names, used to flag a referenced source that
    no longer exists."""
    column = str(column)
    if column in _seen:  # cycle (e.g. an overwrite that references itself) — stop cleanly
        return {"column": column, "kind": "cycle", "formula": None, "sources": []}
    if column in formulas:
        formula = formulas[column]
        refs = sorted(_refs_in(formula))
        return {
            "column": column,
            "kind": "derived",
            "formula": formula,
            "sources": [trace(r, formulas, columns, _seen + (column,)) for r in refs],
        }
    node = {"column": column, "kind": "source", "formula": None, "sources": []}
    if columns is not None:
        node["present"] = column in columns  # a referenced source that was since dropped
    return node


def source_columns(column: str, formulas: dict) -> list[str]:
    """The flat, sorted set of ROOT source columns `column` ultimately derives from
    (transitive closure down to non-formula columns). Cycle-safe."""
    roots: set[str] = set()

    def walk(col: str, seen: frozenset) -> None:
        col = str(col)
        if col in seen:
            return
        if col in formulas:
            for r in _refs_in(formulas[col]):
                walk(r, seen | {col})
        else:
            roots.add(col)

    walk(str(column), frozenset())
    return sorted(roots)


def graph(formulas: dict, columns: set | None = None) -> dict:
    """Nodes + edges for a visual lineage graph. An edge source→derived means the source
    column feeds the derived column's formula. Nodes are every column involved (present
    columns plus any referenced source), tagged source/derived."""
    nodes: dict[str, dict] = {}

    def add_node(col: str) -> None:
        col = str(col)
        if col not in nodes:
            nodes[col] = {"id": col, "kind": "derived" if col in formulas else "source"}
            if columns is not None:
                nodes[col]["present"] = col in columns

    for col in (columns or set()):
        add_node(col)
    edges: list[dict] = []
    for col, formula in formulas.items():
        add_node(col)
        for src in sorted(_refs_in(formula)):
            add_node(src)
            edges.append({"from": src, "to": col})
    return {"nodes": list(nodes.values()), "edges": edges}
