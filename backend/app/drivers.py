"""Real connector drivers — the live network/DB calls behind connectors.py.

connectors.py stays pure and testable by taking an injected `driver(connection, query,
offset, limit) -> list[dict]`. This module supplies the REAL ones:

  - SQL databases (postgres, mysql, snowflake) share one DB-API runner (_run_dbapi):
    connect -> execute a paginated read -> map rows to dicts -> close.
  - Stripe uses its REST API over HTTPS (no SDK needed).

Every driver FAILS SAFE: if the client library isn't installed or credentials are
missing, it raises a friendly ConnectorError rather than pretending to connect. The
pieces that don't need a live service (SQL pagination, credential mapping, Stripe URL/
auth building, row mapping) are separated out so they're unit-tested without a network.

The database itself is trusted to enforce read-only at the connection/user level; on top
of that connectors.run_query already blocks non-SELECT SQL before it ever reaches here.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable


def _err(message: str, status: int = 400):
    # Imported lazily so this module can be imported before connectors finishes loading.
    from .connectors import ConnectorError

    return ConnectorError(message, status)


# --------------------------------------------------------------- SQL (DB-API) shared ---
def _wrap_limit_offset(query: str, offset: int, limit: int) -> str:
    """Wrap any SELECT in an outer LIMIT/OFFSET so we page it without parsing its SQL.
    offset/limit are ints supplied by connectors.run_query (never user text), so inlining
    them is injection-safe."""
    inner = query.strip().rstrip(";").strip()
    return f"SELECT * FROM (\n{inner}\n) AS _sumio_sub LIMIT {int(limit)} OFFSET {int(offset)}"


def _run_dbapi(connect: Callable[[], object], query: str, offset: int, limit: int) -> list[dict]:
    """Run a paginated read against any PEP-249 (DB-API 2.0) connection and return the
    rows as dicts. `connect` is a zero-arg callable returning a fresh connection, so this
    is trivially testable with a fake connection."""
    sql = _wrap_limit_offset(query, offset, limit)
    conn = connect()
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            fetched = cur.fetchall() or []
        finally:
            cur.close()
    finally:
        conn.close()
    return [dict(zip(columns, row)) for row in fetched]


def _require(secret: dict, *keys: str) -> None:
    missing = [k for k in keys if not secret.get(k)]
    if missing:
        raise _err(f"This connection is missing credential(s): {', '.join(missing)}.", 400)


# --------------------------------------------------------------------------- Postgres --
def postgres_driver(connection: dict, query: str, offset: int, limit: int) -> list[dict]:
    try:
        import psycopg2
    except ImportError:
        raise _err("PostgreSQL support isn't installed here (pip install psycopg2-binary).", 501)

    secret = connection["_secret"]
    # Accept either a full DSN/URL or discrete fields.
    if secret.get("dsn") or secret.get("url"):
        connect = lambda: psycopg2.connect(secret.get("dsn") or secret.get("url"))
    else:
        _require(secret, "host", "dbname", "user", "password")
        connect = lambda: psycopg2.connect(
            host=secret["host"], port=int(secret.get("port", 5432)),
            dbname=secret["dbname"], user=secret["user"], password=secret["password"],
            connect_timeout=int(secret.get("connect_timeout", 15)),
        )
    return _run_dbapi(connect, query, offset, limit)


# ------------------------------------------------------------------------------ MySQL --
def mysql_driver(connection: dict, query: str, offset: int, limit: int) -> list[dict]:
    try:
        import pymysql
    except ImportError:
        raise _err("MySQL support isn't installed here (pip install PyMySQL).", 501)

    secret = connection["_secret"]
    _require(secret, "host", "database", "user", "password")
    connect = lambda: pymysql.connect(
        host=secret["host"], port=int(secret.get("port", 3306)),
        database=secret["database"], user=secret["user"], password=secret["password"],
        connect_timeout=int(secret.get("connect_timeout", 15)),
    )
    return _run_dbapi(connect, query, offset, limit)


# -------------------------------------------------------------------------- Snowflake --
def snowflake_driver(connection: dict, query: str, offset: int, limit: int) -> list[dict]:
    try:
        import snowflake.connector as sf
    except ImportError:
        raise _err("Snowflake support isn't installed here (pip install snowflake-connector-python).", 501)

    secret = connection["_secret"]
    _require(secret, "account", "user", "password", "warehouse", "database", "schema")
    connect = lambda: sf.connect(
        account=secret["account"], user=secret["user"], password=secret["password"],
        warehouse=secret["warehouse"], database=secret["database"], schema=secret["schema"],
        login_timeout=int(secret.get("login_timeout", 20)),
    )
    return _run_dbapi(connect, query, offset, limit)


# ---------------------------------------------------------------------- Stripe (REST) --
_STRIPE_BASE = "https://api.stripe.com/v1/"
_STRIPE_PAGE = 100  # Stripe's max page size


def _http_get_json(url: str, headers: dict, timeout: int = 20) -> dict:
    """GET a URL and parse JSON. Factored out so tests can stub the HTTP call."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https host)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 401/403 etc. — surface a clean status without echoing the body (may leak detail).
        raise _err("The data source rejected the request (check credentials/permissions).",
                   502 if exc.code >= 500 else 400)
    except Exception:
        raise _err("Couldn't reach the data source.", 502)


def stripe_driver(connection: dict, query: str, offset: int, limit: int) -> list[dict]:
    """Read a Stripe resource (e.g. 'charges', 'customers', 'invoices'). Stripe uses
    cursor pagination, so we emulate connectors' offset/limit by walking pages with
    `starting_after` until we've skipped `offset` rows and gathered `limit`."""
    secret = connection["_secret"]
    api_key = secret.get("api_key") or secret.get("secret_key")
    if not api_key:
        raise _err("This Stripe connection needs an 'api_key' credential.", 400)
    resource = (query or "").strip().strip("/").lower()
    if not resource:
        raise _err("Provide a Stripe resource name, e.g. 'charges' or 'customers'.", 400)

    headers = {"Authorization": f"Bearer {api_key}"}
    collected: list[dict] = []
    skipped = 0
    after: str | None = None
    while len(collected) < limit:
        params = {"limit": _STRIPE_PAGE}
        if after:
            params["starting_after"] = after
        url = _STRIPE_BASE + urllib.parse.quote(resource) + "?" + urllib.parse.urlencode(params)
        payload = _http_get_json(url, headers)
        batch = payload.get("data", []) or []
        if not batch:
            break
        for row in batch:
            if skipped < offset:
                skipped += 1
            else:
                collected.append(row)
                if len(collected) >= limit:
                    break
        after = batch[-1].get("id")
        if not payload.get("has_more"):
            break
    return collected


# ------------------------------------------------------------------------- registry ----
# type -> real driver. Types NOT listed fall through to connectors.default_driver (a safe
# 501). Add new integrations here.
_DRIVERS: dict[str, Callable[[dict, str, int, int], list]] = {
    "postgres": postgres_driver,
    "mysql": mysql_driver,
    "snowflake": snowflake_driver,
    "stripe": stripe_driver,
}


def get_driver(ctype: str) -> Callable[[dict, str, int, int], list] | None:
    return _DRIVERS.get(ctype)
