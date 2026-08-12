"""ENGINE PHASE 5.3 — data lineage (BUILD).

"Where does this value come from?" — traced from the session's formula registry (Phase 4.8:
{column: formula} that Sumio builds as add_formula_column ops run). This suite proves:

  value→source TREE   trace(col) walks a derived column to the columns its formula uses, and
                       recurses (Rev = {Price}*{Qty}) down to SOURCE columns (uploaded data,
                       no known formula).
  flat root sources    source_columns(col) = the transitive set of roots it derives from.
  visual GRAPH         graph() = nodes (source/derived) + edges (source → derived).
  honesty              a column with no known formula is reported as a SOURCE (never an
                       invented origin); uploaded-file formulas aren't preserved, so that's
                       the truthful statement.
  cycle-safe           a self-referential overwrite (Rev = {Rev}*2) doesn't loop.
  end to end           /lineage and /lineage/graph over a real session built via /execute.

No llm.py change (reads the existing registry) → no schema/serving/quota risk; no battery
rows (endpoint/mechanism, like 3.2/4.8/4.9).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_5_3.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p53.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import lineage  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("ENGINE PHASE 5.3 — data lineage\n")

# A two-level dependency chain:
#   Price, Qty, Cost  = sources (uploaded)
#   Rev    = {Price} * {Qty}      (derived, one level)
#   Margin = {Rev} - {Cost}       (derived, two levels: Rev is itself derived)
FORMULAS = {"Rev": "{Price} * {Qty}", "Margin": "{Rev} - {Cost}"}
COLS = {"Price", "Qty", "Cost", "Rev", "Margin"}

# ===================== value→source TREE =====================
t = lineage.trace("Margin", FORMULAS, COLS)
check("Margin is derived with its formula", t["kind"] == "derived" and t["formula"] == "{Rev} - {Cost}", str(t))
kids = {n["column"]: n for n in t["sources"]}
check("Margin's direct sources are Rev and Cost", set(kids) == {"Rev", "Cost"}, str(set(kids)))
check("Cost is a SOURCE leaf (no formula)", kids["Cost"]["kind"] == "source" and kids["Cost"]["sources"] == [], str(kids["Cost"]))
check("Rev is itself DERIVED and expands further", kids["Rev"]["kind"] == "derived" and {g["column"] for g in kids["Rev"]["sources"]} == {"Price", "Qty"}, str(kids["Rev"]))

# ===================== flat root sources =====================
check("Margin ultimately derives from Price, Qty, Cost (roots only)",
      lineage.source_columns("Margin", FORMULAS) == ["Cost", "Price", "Qty"], str(lineage.source_columns("Margin", FORMULAS)))
check("a source column's roots are just itself", lineage.source_columns("Price", FORMULAS) == ["Price"], "")
check("Rev's roots are Price, Qty", lineage.source_columns("Rev", FORMULAS) == ["Price", "Qty"], "")

# ===================== a pure source traces to itself =====================
s = lineage.trace("Price", FORMULAS, COLS)
check("tracing a source column reports kind=source, present=True", s["kind"] == "source" and s.get("present") is True, str(s))
# a referenced source that was since dropped is flagged not-present
d = lineage.trace("Cost", {"Cost": "{Gone}"}, {"Cost"})
check("a referenced-but-dropped source is flagged present=False", d["sources"][0]["present"] is False, str(d["sources"][0]))

# ===================== visual GRAPH =====================
g = lineage.graph(FORMULAS, COLS)
kinds = {n["id"]: n["kind"] for n in g["nodes"]}
check("graph tags derived vs source nodes", kinds["Margin"] == "derived" and kinds["Rev"] == "derived" and kinds["Price"] == "source", str(kinds))
edges = {(e["from"], e["to"]) for e in g["edges"]}
check("graph has source→derived edges", {("Price", "Rev"), ("Qty", "Rev"), ("Rev", "Margin"), ("Cost", "Margin")} <= edges, str(edges))
check("graph includes every involved column as a node", {n["id"] for n in g["nodes"]} >= COLS, str([n["id"] for n in g["nodes"]]))

# ===================== cycle safety =====================
cyc = lineage.trace("Rev", {"Rev": "{Rev} * 2"})
check("a self-referential overwrite doesn't loop (marked 'cycle')",
      cyc["kind"] == "derived" and cyc["sources"][0]["kind"] == "cycle", str(cyc))
check("source_columns is cycle-safe too", lineage.source_columns("Rev", {"Rev": "{Rev} * 2"}) == [], str(lineage.source_columns("Rev", {"Rev": "{Rev} * 2"})))

# ===================== END TO END over HTTP =====================
CSV = b"Price,Qty,Cost\n10,2,5\n20,3,8\n"
c.post("/inspect", data={"session_id": "ln"}, files=[("files", ("d.csv", CSV, "text/csv"))])
# Build the derived columns via the trusted executor (registry learns them — Phase 4.8).
c.post("/execute", data={"session_id": "ln", "plan": json.dumps(
    {"operations": [{"action": "add_formula_column", "name": "Rev", "formula": "{Price} * {Qty}"}]})})
c.post("/execute", data={"session_id": "ln", "plan": json.dumps(
    {"operations": [{"action": "add_formula_column", "name": "Margin", "formula": "{Rev} - {Cost}"}]})})

r = c.post("/lineage", data={"session_id": "ln", "column": "Margin"}).json()
check("/lineage: Margin is derived", r.get("status") == "ok" and r.get("derived") is True, str(r)[:200])
check("/lineage: Margin traces to Cost, Price, Qty", r.get("sources") == ["Cost", "Price", "Qty"], str(r.get("sources")))
check("/lineage: the tree expands Rev one level deeper",
      any(n["column"] == "Rev" and n["kind"] == "derived" for n in r["trace"]["sources"]), str(r["trace"]))

r_src = c.post("/lineage", data={"session_id": "ln", "column": "Price"}).json()
check("/lineage: a source column reports itself as its only source", r_src.get("derived") is False and r_src.get("sources") == ["Price"], str(r_src)[:160])

r_missing = c.post("/lineage", data={"session_id": "ln", "column": "Ghost"})
check("/lineage: an unknown column 404s", r_missing.status_code == 404, f"HTTP {r_missing.status_code}")

gr = c.post("/lineage/graph", data={"session_id": "ln"}).json()
gedges = {(e["from"], e["to"]) for e in gr["graph"]["edges"]}
check("/lineage/graph: full source→derived edge set", {("Price", "Rev"), ("Qty", "Rev"), ("Rev", "Margin"), ("Cost", "Margin")} <= gedges, str(gedges))

m._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
