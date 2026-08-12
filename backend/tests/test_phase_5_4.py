"""ENGINE PHASE 5.4 — knowledge graph (BUILD).

Infers how sheets relate — the foreign keys a spreadsheet has but never declares — so a user
can run a relational query WITHOUT writing a join. This suite proves:

  entities + keys     candidate_keys finds identifier columns (id-named or near-unique text),
                      and EXCLUDES unique numeric measures (Amount) that are distinct only by
                      accident of a small sample.
  relationships       A.col → B.key when A.col's values live inside B's key (a foreign-key
                      pattern), matched the same normalized way the executor's lookup joins,
                      guarded by a name-match / very-high-coverage signal so coincidental
                      overlap doesn't invent a link. No link when values don't line up.
  relational query    auto_lookup names a field in a RELATED table and returns a ready lookup
                      op with the join keys filled in — or declines honestly (no path /
                      ambiguous / already present).
  end to end          /kg/graph and /kg/query bring a customer's Email onto Sales with no join
                      specified.

No llm.py change → no schema/serving/quota risk; no battery rows (endpoint/mechanism, the KG
resolution is deterministic, not Brain routing).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_5_4.py
"""
from __future__ import annotations

import io
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

_fd, _db = tempfile.mkstemp(suffix="-p54.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import kg  # noqa: E402
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


SALES = pd.DataFrame({"Sale_ID": [1, 2, 3, 4], "Customer_ID": ["C1", "C2", "C1", "C3"], "Amount": [10, 20, 30, 40]})
CUST = pd.DataFrame({"Customer_ID": ["C1", "C2", "C3"], "Name": ["Ann", "Bob", "Cy"], "Email": ["a@x", "b@x", "c@x"]})
TABLES = {"Sales": SALES, "Customers": CUST}


print("ENGINE PHASE 5.4 — knowledge graph\n")

# ===================== entities + keys =====================
sk = kg.candidate_keys(SALES)
check("Sale_ID is a key; Amount (unique numeric measure) is NOT", "Sale_ID" in sk and "Amount" not in sk, str(sk))
check("a repeating FK column (Customer_ID in Sales) is not a key of Sales", "Customer_ID" not in sk, str(sk))
ck = kg.candidate_keys(CUST)
check("Customers keys include the id + unique text identifiers", set(ck) >= {"Customer_ID", "Email"}, str(ck))

# ===================== relationships =====================
rels = kg.relationships(TABLES)
check("exactly one relationship: Sales.Customer_ID → Customers.Customer_ID",
      len(rels) == 1 and rels[0]["from_table"] == "Sales" and rels[0]["from_column"] == "Customer_ID"
      and rels[0]["to_table"] == "Customers" and rels[0]["to_column"] == "Customer_ID", str(rels))
check("the relationship reports full coverage + name match", rels[0]["coverage"] == 1.0 and rels[0]["name_match"], str(rels[0]))

# no relationship when values don't line up (disjoint id spaces, no name match)
NOJOIN = {"A": pd.DataFrame({"AID": [1, 2, 3], "V": [9, 8, 7]}),
          "B": pd.DataFrame({"BID": [100, 200, 300], "W": [1, 2, 3]})}
check("no relationship is invented when keys don't overlap", kg.relationships(NOJOIN) == [], str(kg.relationships(NOJOIN)))

# partial-but-strong FK coverage with a name match is still detected
PART = {"Orders": pd.DataFrame({"Customer_ID": ["C1", "C2", "C9"]}),  # C9 unknown → 2/3 coverage
        "Customers": pd.DataFrame({"Customer_ID": ["C1", "C2", "C3"], "Email": ["a", "b", "c"]})}
pr = kg.relationships(PART, min_coverage=0.6)
check("a partial FK (2/3 covered) with a name match is detected", any(r["to_table"] == "Customers" for r in pr), str(pr))

# ===================== graph =====================
g = kg.graph(TABLES)
ent = {e["table"]: e for e in g["entities"]}
check("graph lists both entities with their keys", "Sales" in ent and "Customers" in ent and "Customer_ID" in ent["Customers"]["keys"], str(ent))
check("graph carries the inferred relationship", len(g["relationships"]) == 1, str(g["relationships"]))

# ===================== auto_lookup (relational query without joins) =====================
op, rel = kg.auto_lookup(TABLES, "Sales", "Email")
check("auto_lookup builds a lookup op with the join keys resolved",
      op == {"action": "lookup", "key_column": "Customer_ID", "source_sheet": "Customers",
             "source_key_column": "Customer_ID", "return_column": "Email", "new_column": "Email"}, str(op))

for bad, why in [
    (lambda: kg.auto_lookup(TABLES, "Sales", "Amount"), "already present"),
    (lambda: kg.auto_lookup(TABLES, "Sales", "Nonexistent"), "no table has it"),
    (lambda: kg.auto_lookup(TABLES, "Customers", "Amount"), "no path (Customers doesn't reference Sales)"),
]:
    try:
        bad()
        check(f"auto_lookup declines: {why}", False, "no error")
    except ValueError:
        check(f"auto_lookup declines: {why}", True)

# ambiguity: a field reachable via TWO different FK columns to two tables
AMB = {
    "Txns": pd.DataFrame({"Cust_ID": ["C1", "C2"], "Vend_ID": ["V1", "V2"]}),
    "Customers": pd.DataFrame({"Cust_ID": ["C1", "C2"], "Email": ["a", "b"]}),
    "Vendors": pd.DataFrame({"Vend_ID": ["V1", "V2"], "Email": ["x", "y"]}),
}
try:
    kg.auto_lookup(AMB, "Txns", "Email")
    check("auto_lookup declines an ambiguous field (in two related tables)", False, "no error")
except ValueError as e:
    check("auto_lookup declines an ambiguous field (in two related tables)", "which" in str(e).lower() or "several" in str(e).lower(), str(e))

# ===================== END TO END over HTTP (2-sheet workbook) =====================
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as w:
    SALES.to_excel(w, sheet_name="Sales", index=False)
    CUST.to_excel(w, sheet_name="Customers", index=False)
XLSX = buf.getvalue()
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
c.post("/inspect", data={"session_id": "kg"}, files=[("files", ("book.xlsx", XLSX, MIME))])

gr = c.post("/kg/graph", data={"session_id": "kg"}).json()
# The multi-sheet reader prefixes table names with the filename ("book - Sales"); resolve the
# real names rather than assuming them.
tables_seen = {e["table"] for e in gr.get("graph", {}).get("entities", [])}
sales_tbl = next((t for t in tables_seen if t.endswith("Sales")), None)
cust_tbl = next((t for t in tables_seen if t.endswith("Customers")), None)
check("/kg/graph returns both sheets as entities", sales_tbl and cust_tbl, str(tables_seen))
check("/kg/graph infers the Sales→Customers relationship",
      any(r["from_table"] == sales_tbl and r["to_table"] == cust_tbl for r in gr["graph"]["relationships"]), str(gr["graph"]["relationships"]))

q = c.post("/kg/query", data={"session_id": "kg", "field": "Email", "from_table": sales_tbl}).json()
check("/kg/query: brings Email into Sales (status ok)", q.get("status") == "ok", str(q)[:200])
check("/kg/query: reports the relationship it used", q.get("relationship", {}).get("to_table") == cust_tbl, str(q.get("relationship")))
sales_preview = q["preview"][0]
cols = [col["name"] if isinstance(col, dict) else col for col in sales_preview["columns"]]
check("/kg/query: the Email column now appears on Sales", "Email" in cols, str(cols))

qbad = c.post("/kg/query", data={"session_id": "kg", "field": "Nope", "from_table": sales_tbl})
check("/kg/query: an unresolvable field declines (422)", qbad.status_code == 422, f"HTTP {qbad.status_code}")

m._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
