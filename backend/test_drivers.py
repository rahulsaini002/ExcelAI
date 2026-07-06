"""#3 — real connector drivers. Unit-tests the testable logic (SQL pagination wrap,
DB-API row mapping, credential checks, Stripe pagination/auth, dispatch routing, and
fail-safe when a client library is missing) using fakes — no live DB or network.

Run:  .venv\\Scripts\\python.exe test_drivers.py
"""
from __future__ import annotations

import urllib.parse

from app import connectors, drivers
from app.connectors import ConnectorError

passed = 0
failed = 0
fails: list[str] = []


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        fails.append(f"{name}  {detail}")
        print(f"  FAIL  {name}  {detail}")


def raises(fn, status=None):
    try:
        fn()
        return False
    except ConnectorError as e:
        return status is None or e.status == status
    except Exception:
        return False


# ===================================================== SQL pagination wrap
print("SQL WRAP")
sql = drivers._wrap_limit_offset("SELECT id, name FROM users;", offset=20, limit=10)
check("wrap adds LIMIT/OFFSET", "LIMIT 10 OFFSET 20" in sql)
check("wrap strips trailing semicolon", "users;" not in sql and "users" in sql)
check("wrap uses a subquery alias", "_sumio_sub" in sql)


# ===================================================== DB-API runner (fake connection)
print("DB-API RUNNER")


class FakeCursor:
    def __init__(self, rows, description):
        self.rows = rows
        self.description = description
        self.executed = None
        self.closed = False

    def execute(self, sql):
        self.executed = sql

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


cur = FakeCursor([(1, "Alice"), (2, "Bob")], [("id",), ("name",)])
conn = FakeConn(cur)
rows = drivers._run_dbapi(lambda: conn, "SELECT id, name FROM t", offset=5, limit=100)
check("rows mapped to dicts", rows == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}])
check("pagination applied to executed SQL", "LIMIT 100 OFFSET 5" in cur.executed)
check("cursor closed", cur.closed is True)
check("connection closed", conn.closed is True)

# empty result set + no description doesn't crash
cur2 = FakeCursor([], None)
rows2 = drivers._run_dbapi(lambda: FakeConn(cur2), "SELECT 1", 0, 10)
check("empty result -> empty list", rows2 == [])


# ===================================================== credential checks + fail-safe
print("CREDENTIALS / FAIL-SAFE")
check("_require flags missing keys", raises(lambda: drivers._require({"host": "h"}, "host", "user"), 400))
check("_require passes when present", drivers._require({"a": 1, "b": 2}, "a", "b") is None)

# client libraries not installed here -> friendly 501 (never a raw ImportError)
check("mysql without PyMySQL -> 501", raises(lambda: drivers.mysql_driver({"_secret": {}}, "select 1", 0, 10), 501))
check("snowflake without connector -> 501", raises(lambda: drivers.snowflake_driver({"_secret": {}}, "select 1", 0, 10), 501))
# postgres: either psycopg2 is absent (501) or present-but-no-creds (400) — both are clean
check("postgres fails cleanly with no creds", raises(lambda: drivers.postgres_driver({"_secret": {}}, "select 1", 0, 10)))


# ===================================================== Stripe (fake HTTP)
print("STRIPE")
ALL = [{"id": f"c{i}", "amount": i} for i in range(250)]
_seen_headers = {}


def fake_http(url, headers, timeout=20):
    _seen_headers.update(headers)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    after = q.get("starting_after", [None])[0]
    if after is None:
        start = 0
    else:
        start = next(i for i, r in enumerate(ALL) if r["id"] == after) + 1
    page = ALL[start:start + drivers._STRIPE_PAGE]
    return {"data": page, "has_more": start + drivers._STRIPE_PAGE < len(ALL)}


drivers._http_get_json = fake_http  # type: ignore[assignment]

conn_stripe = {"_secret": {"api_key": "sk_test_123"}}
r = drivers.stripe_driver(conn_stripe, "charges", offset=0, limit=5)
check("stripe first page slice", [x["id"] for x in r] == ["c0", "c1", "c2", "c3", "c4"])
check("stripe sends bearer auth", _seen_headers.get("Authorization") == "Bearer sk_test_123")

r = drivers.stripe_driver(conn_stripe, "charges", offset=120, limit=10)
check("stripe offset crosses page boundary", [x["id"] for x in r] == [f"c{i}" for i in range(120, 130)])

r = drivers.stripe_driver(conn_stripe, "charges", offset=245, limit=50)
check("stripe stops at end (has_more False)", [x["id"] for x in r] == [f"c{i}" for i in range(245, 250)])

check("stripe needs api_key", raises(lambda: drivers.stripe_driver({"_secret": {}}, "charges", 0, 10), 400))
check("stripe needs resource", raises(lambda: drivers.stripe_driver(conn_stripe, "  ", 0, 10), 400))


# ===================================================== registry + dispatch routing
print("REGISTRY / DISPATCH")
check("get_driver known type", drivers.get_driver("postgres") is drivers.postgres_driver)
check("get_driver unknown type -> None", drivers.get_driver("bigquery") is None)

# sample demo source still works through dispatch
srows = connectors.dispatch_driver({"type": "sample", "_secret": {}}, "", 0, 5)
check("dispatch sample -> rows", len(srows) == 5 and "revenue" in srows[0])

# a real-but-unconfigured type (no driver registered) -> safe 501
check("dispatch unknown real type -> 501",
      raises(lambda: connectors.dispatch_driver({"type": "bigquery", "_secret": {}}, "x", 0, 10), 501))

# a registered type routes to its driver (stripe with no key -> 400, proving it wasn't the 501 default)
check("dispatch routes stripe to its driver",
      raises(lambda: connectors.dispatch_driver({"type": "stripe", "_secret": {}}, "charges", 0, 10), 400))


print(f"\n{passed} passed, {failed} failed.")
if fails:
    print("FAILURES:")
    for f in fails:
        print("  -", f)
raise SystemExit(1 if failed else 0)
