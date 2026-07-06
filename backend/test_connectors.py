"""Phase 3.2 — Live database & SaaS connectors tests.

PRD criteria:
  CN-readonly    Read-only by default; writes rejected before reaching the driver.
  CN-creds       Credentials never returned; connection/auth failures handled safely
                 (no secret leak).
  CN-paginate    Large results paginated with a has_more flag and capped page size.
  CN-scopes      Permission scopes respected — out-of-scope tables/objects refused.

Run from backend:  .venv\\Scripts\\python.exe test_connectors.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from fastapi.testclient import TestClient

from app import connectors as cn
from app import main

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


cn._CONNECTIONS.clear()
print("PHASE 3.2 — CONNECTORS\n")

# A fake driver: returns `limit` synthetic rows, and records what it was asked.
calls = []


def fake_driver(connection, query, offset, limit):
    calls.append({"query": query, "offset": offset, "limit": limit})
    return [{"id": offset + i, "v": f"row{offset + i}"} for i in range(limit)]


# =========================================================================
# CN-readonly
# =========================================================================
print("CN-readonly  Read-only by default")

c = cn.register_connection("PG", "postgres", {"host": "db", "password": "s3cret"})
check("new connection is read-only by default", c["read_only"] is True, str(c))

ok = cn.run_query(c["id"], "SELECT * FROM orders", fake_driver, page=1, page_size=5)
check("SELECT allowed on read-only", len(ok["rows"]) == 5, str(ok)[:80])

for bad in ("DELETE FROM orders", "UPDATE orders SET x=1", "DROP TABLE orders",
            "INSERT INTO orders VALUES (1)", "SELECT 1; DROP TABLE orders"):
    try:
        cn.run_query(c["id"], bad, fake_driver)
        check(f"write rejected: {bad[:18]}", False, "allowed!")
    except cn.ConnectorError as e:
        check(f"write rejected: {bad[:18]}", e.status == 403, str(e))

check("is_read_only_sql accepts a CTE SELECT", cn.is_read_only_sql("WITH t AS (SELECT 1) SELECT * FROM t"))
check("is_read_only_sql rejects sneaky write", not cn.is_read_only_sql("WITH t AS (SELECT 1) DELETE FROM x"))

# =========================================================================
# CN-creds  Secrets never exposed; failures safe
# =========================================================================
print("\nCN-creds  Credential safety")

view = cn.get_connection(c["id"])
check("safe view has no _secret", "_secret" not in view and "password" not in view, str(view))
check("safe view lists credential FIELDS only", view["credential_fields"] == ["host", "password"], str(view["credential_fields"]))
check("list never leaks secrets", all("_secret" not in v for v in cn.list_connections()), "")


def leaky_driver(connection, query, offset, limit):
    raise RuntimeError(f"auth failed: password={connection['_secret']['password']}")


try:
    cn.run_query(c["id"], "SELECT * FROM orders", leaky_driver)
    check("driver failure raises", False, "no error")
except cn.ConnectorError as e:
    check("connection failure is a friendly error", e.status == 502, str(e))
    check("secret NOT leaked in the error", "s3cret" not in str(e), str(e))

# =========================================================================
# CN-paginate
# =========================================================================
print("\nCN-paginate  Pagination")

calls.clear()
p1 = cn.run_query(c["id"], "SELECT * FROM orders", fake_driver, page=1, page_size=10)
check("returns exactly page_size rows", len(p1["rows"]) == 10, str(len(p1["rows"])))
check("has_more flagged", p1["has_more"] is True and p1["next_page"] == 2, str(p1)[:80])
check("driver asked for page_size+1 (to detect more)", calls[-1]["limit"] == 11, str(calls[-1]))
p2 = cn.run_query(c["id"], "SELECT * FROM orders", fake_driver, page=2, page_size=10)
check("page 2 uses the right offset", calls[-1]["offset"] == 10, str(calls[-1]))
check("page size is capped", cn.run_query(c["id"], "SELECT 1", fake_driver, page=1, page_size=99999)["page_size"] == cn._MAX_PAGE_SIZE)


# A driver that returns fewer than asked → last page (no more).
def short_driver(connection, query, offset, limit):
    return [{"id": i} for i in range(3)]


last = cn.run_query(c["id"], "SELECT * FROM orders", short_driver, page=1, page_size=10)
check("no has_more on a short final page", last["has_more"] is False and last["next_page"] is None, str(last)[:80])

# =========================================================================
# CN-scopes
# =========================================================================
print("\nCN-scopes  Permission scopes")

scoped = cn.register_connection("PG2", "postgres", {"u": "x"}, scopes=["orders", "customers"])
check("in-scope table allowed", len(cn.run_query(scoped["id"], "SELECT * FROM orders", fake_driver, page_size=2)["rows"]) == 2)
check("join within scope allowed",
      len(cn.run_query(scoped["id"], "SELECT * FROM orders JOIN customers USING(id)", fake_driver, page_size=2)["rows"]) == 2)
try:
    cn.run_query(scoped["id"], "SELECT * FROM salaries", fake_driver)
    check("out-of-scope table refused", False, "allowed!")
except cn.ConnectorError as e:
    check("out-of-scope table refused (403)", e.status == 403 and "salaries" in str(e), str(e))

# SaaS scopes: the query IS the object name
saas = cn.register_connection("SF", "salesforce", {"token": "t"}, scopes=["account", "contact"])
check("saas connection classified as saas", cn.get_connection(saas["id"])["kind"] == "saas", "")
check("in-scope object allowed", len(cn.run_query(saas["id"], "Account", fake_driver, page_size=2)["rows"]) == 2)
try:
    cn.run_query(saas["id"], "Salary", fake_driver)
    check("out-of-scope object refused", False, "allowed!")
except cn.ConnectorError as e:
    check("out-of-scope object refused", e.status == 403, str(e))

# =========================================================================
# Defaults + validation + API
# =========================================================================
print("\nCN-misc  Defaults, validation, API")

check("default driver refuses (not configured)", True)
try:
    cn.default_driver({"type": "postgres"}, "SELECT 1", 0, 10)
    check("default driver raises", False)
except cn.ConnectorError as e:
    check("default driver raises 501", e.status == 501, str(e))

try:
    cn.register_connection("X", "oracle", {"u": "x"})
    check("unsupported type rejected", False)
except cn.ConnectorError:
    check("unsupported type rejected", True)
try:
    cn.register_connection("X", "postgres", {})
    check("empty credentials rejected", False)
except cn.ConnectorError:
    check("empty credentials rejected", True)

client = TestClient(main.app)
cr = client.post("/connectors/create", data={
    "name": "Prod", "type": "postgres",
    "credentials": '{"host":"db","password":"hunter2"}', "scopes": "orders,customers",
}).json()
check("API create returns safe view (secret value absent)", cr["status"] == "ok" and "hunter2" not in str(cr), str(cr)[:120])
cid = cr["connection"]["id"]
got = client.get(f"/connectors/{cid}").json()
check("API get is masked (value absent, fields listed)",
      "hunter2" not in str(got) and got["connection"]["credential_fields"] == ["host", "password"], str(got)[:120])
# query via API uses the default (unconfigured) driver → safe 501, not a crash
q = client.post(f"/connectors/{cid}/query", data={"query": "SELECT * FROM orders"})
check("API query without a real driver returns 501 (not configured)", q.status_code == 501, str(q.json()))
# write rejected at API
w = client.post(f"/connectors/{cid}/query", data={"query": "DELETE FROM orders"})
check("API rejects a write (403)", w.status_code == 403, str(w.json()))
client.post(f"/connectors/{cid}/delete")
check("API delete works", all(c["id"] != cid for c in client.get("/connectors/list").json()["connections"]), "")

# Built-in 'sample' source — works end-to-end without credentials (for demos).
print("\nCN-sample  Built-in demo source")
samp = cn.register_connection("Demo", "sample", {})
check("sample connection needs no credentials", samp["type"] == "sample", str(samp))
res = cn.run_query(samp["id"], "SELECT * FROM sales", cn.dispatch_driver, page=1, page_size=50)
check("sample driver returns a full page", len(res["rows"]) == 50 and res["has_more"] is True, str(res)[:80])
check("sample rows have real columns", set(res["rows"][0]) >= {"id", "region", "revenue"}, str(res["rows"][0]))
last = cn.run_query(samp["id"], "SELECT * FROM sales", cn.dispatch_driver, page=3, page_size=50)
check("sample paginates to a final partial page", last["has_more"] is False and len(last["rows"]) == 37, str(len(last["rows"])))
try:
    cn.run_query(c["id"], "SELECT 1", cn.dispatch_driver)
    check("dispatch still 501 for an unconfigured real type", False)
except cn.ConnectorError as e:
    check("dispatch still 501 for an unconfigured real type", e.status == 501, str(e))

# API import of the sample source into a session
sc = client.post("/connectors/create", data={"name": "Demo", "type": "sample", "credentials": "{}"}).json()
check("API create sample connection", sc["status"] == "ok", str(sc)[:100])
scid = sc["connection"]["id"]
imp = client.post(f"/connectors/{scid}/import",
                  data={"query": "SELECT * FROM sales", "session_id": "connimp", "page_size": "50"}).json()
check("API import returns grid tables", imp["status"] == "ok" and imp["tables"][0]["row_count"] == 50, str(imp)[:140])
check("API import created a usable session", "connimp" in main._SESSIONS, "")

main._SESSIONS.clear()
cn._CONNECTIONS.clear()
print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
