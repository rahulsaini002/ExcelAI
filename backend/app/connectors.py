"""Live database & SaaS connectors (Phase 3.2).

Connect to databases (PostgreSQL, MySQL, Snowflake, BigQuery) and tools (Salesforce,
HubSpot, Stripe, QuickBooks, Shopify) and pull data in — under guardrails:

  • Read-only by default — a connection is read-only unless explicitly created otherwise;
    for SQL sources only a single SELECT/WITH is allowed (writes are rejected before they
    ever reach the driver).
  • Credentials handled safely — secrets are stored apart and NEVER returned by the API or
    put in error messages; a connection/auth failure becomes a generic friendly error.
  • Large results paginated — every fetch is paged (capped page size) with a has_more flag.
  • Permission scopes respected — a connection can be limited to specific tables/objects;
    anything outside its scopes is refused.

The actual network/DB call is an injected `driver`, so this is fully testable offline. The
default driver refuses to run unless a real client is configured — connecting to a live
source needs both the client library and credentials.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Callable

from . import store

_DB_TYPES = {"postgres", "mysql", "snowflake", "bigquery"}
_SAAS_TYPES = {"salesforce", "hubspot", "stripe", "quickbooks", "shopify"}
_DEMO_TYPES = {"sample"}  # a built-in synthetic source so the flow is demonstrable
ALL_TYPES = _DB_TYPES | _SAAS_TYPES | _DEMO_TYPES

_MAX_PAGE_SIZE = 1000
_MAX_CONNECTIONS = 200

# A driver runs ONE fetch: (connection, query, offset, limit) -> list[dict] (rows).
Driver = Callable[[dict, str, int, int], list]

# In-memory in dev; loaded from / snapshotted to the DB when persistence is on (store.py).
_CONNECTIONS: dict[str, dict] = store.register("connections", store.load_dict("connections"))

# SQL that would modify data — rejected on read-only connections.
_WRITE_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "truncate", "grant",
    "revoke", "merge", "replace", "call", "exec", "execute", "copy", "into", "upsert",
    "vacuum", "attach", "pragma",
}


class ConnectorError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> float:
    return time.time()


def _get(conn_id: str) -> dict:
    c = _CONNECTIONS.get(conn_id)
    if c is None:
        raise ConnectorError("That connection doesn't exist.", status=404)
    return c


def kind_of(ctype: str) -> str:
    return "database" if ctype in (_DB_TYPES | _DEMO_TYPES) else "saas"


def safe_view(conn: dict) -> dict:
    """A connection WITHOUT secrets — what the API may return. Shows which credential
    fields exist, never their values."""
    return {
        "id": conn["id"],
        "name": conn["name"],
        "type": conn["type"],
        "kind": conn["kind"],
        "read_only": conn["read_only"],
        "scopes": list(conn["scopes"]),
        "config": dict(conn["config"]),
        "credential_fields": sorted(conn["_secret"].keys()),
        "created_at": conn["created_at"],
    }


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def register_connection(
    name: str, ctype: str, credentials: dict, scopes: list[str] | None = None,
    read_only: bool = True, config: dict | None = None,
) -> dict:
    """Create a connection. Read-only by default. Returns the SAFE view (no secrets)."""
    ctype = (ctype or "").lower().strip()
    if ctype not in ALL_TYPES:
        raise ConnectorError(
            f"Unsupported source '{ctype}'. Supported: {', '.join(sorted(ALL_TYPES))}.", 400
        )
    if not isinstance(credentials, dict):
        raise ConnectorError("Credentials must be a JSON object.", 400)
    if not credentials and ctype not in _DEMO_TYPES:  # the demo source needs none
        raise ConnectorError("Credentials are required to create a connection.", 400)

    conn_id = f"con_{uuid.uuid4().hex[:12]}"
    conn = {
        "id": conn_id,
        "name": (name or ctype).strip(),
        "type": ctype,
        "kind": kind_of(ctype),
        "read_only": bool(read_only),
        "scopes": [s.strip().lower() for s in (scopes or []) if s and s.strip()],
        "config": dict(config or {}),
        "_secret": dict(credentials),  # stored apart; never returned
        "created_at": _now(),
    }
    _CONNECTIONS[conn_id] = conn
    while len(_CONNECTIONS) > _MAX_CONNECTIONS:
        _CONNECTIONS.pop(next(iter(_CONNECTIONS)))
    return safe_view(conn)


def list_connections() -> list[dict]:
    return [safe_view(c) for c in _CONNECTIONS.values()]


def get_connection(conn_id: str) -> dict:
    return safe_view(_get(conn_id))


def delete_connection(conn_id: str) -> None:
    _get(conn_id)
    _CONNECTIONS.pop(conn_id, None)


# --------------------------------------------------------------------------- #
# Read-only + scope enforcement
# --------------------------------------------------------------------------- #
def _strip_sql(query: str) -> str:
    s = re.sub(r"--.*?$", " ", query, flags=re.M)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    return s.strip().rstrip(";").strip()


def is_read_only_sql(query: str) -> bool:
    """True only for a SINGLE read statement (SELECT / WITH…SELECT) with no write tokens
    and no statement chaining."""
    s = _strip_sql(query)
    if not s or ";" in s:  # an internal ';' means multiple statements
        return False
    low = s.lower()
    m = re.match(r"\s*([a-z]+)", low)
    first = m.group(1) if m else ""
    if first not in ("select", "with"):
        return False
    tokens = set(re.findall(r"[a-z_]+", low))
    return not (tokens & _WRITE_KEYWORDS)


def _referenced_tables(query: str) -> set[str]:
    low = _strip_sql(query).lower()
    refs = re.findall(r"(?:from|join)\s+([a-z_][\w.]*)", low)
    return {r.split(".")[-1] for r in refs}  # compare on the table's final component


def _check_scope(conn: dict, query: str) -> None:
    scopes = set(conn["scopes"])
    if not scopes:
        return  # no scope restriction
    if conn["kind"] == "database":
        outside = {t for t in _referenced_tables(query) if t not in scopes}
        if outside:
            raise ConnectorError(
                f"This connection isn't allowed to read: {', '.join(sorted(outside))}. "
                f"Permitted: {', '.join(sorted(scopes))}.",
                status=403,
            )
    else:  # saas: the query IS the object/resource name
        resource = query.strip().lower()
        if resource and resource not in scopes:
            raise ConnectorError(
                f"This connection isn't allowed to read '{query}'. "
                f"Permitted: {', '.join(sorted(scopes))}.",
                status=403,
            )


# --------------------------------------------------------------------------- #
# Querying
# --------------------------------------------------------------------------- #
def run_query(
    conn_id: str, query: str, driver: Driver, page: int = 1, page_size: int = 100,
) -> dict:
    """Run a read-only, scope-checked, paginated query. Returns
    {rows, page, page_size, has_more, next_page}. Credential/connection failures are
    surfaced as a generic error that never leaks the secret."""
    conn = _get(conn_id)
    query = (query or "").strip()
    if not query:
        raise ConnectorError("Provide a query (SQL) or an object/resource name.", 400)

    if conn["kind"] == "database" and conn["read_only"] and not is_read_only_sql(query):
        raise ConnectorError(
            "This connection is read-only — only a single SELECT query is allowed.", status=403
        )
    _check_scope(conn, query)

    page = max(1, int(page))
    page_size = max(1, min(int(page_size), _MAX_PAGE_SIZE))
    offset = (page - 1) * page_size

    try:
        # Fetch one extra row to know whether there's another page.
        rows = driver(conn, query, offset, page_size + 1)
    except ConnectorError:
        raise
    except Exception:
        # Never echo the driver exception — it may contain the connection string/secret.
        raise ConnectorError(
            "Couldn't reach the data source. Check the connection details and credentials.",
            status=502,
        )

    rows = list(rows or [])
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    return {
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "has_more": has_more,
        "next_page": page + 1 if has_more else None,
    }


def default_driver(connection: dict, query: str, offset: int, limit: int) -> list:
    """The shipped driver: refuses to run until a real client + credentials are wired in,
    so the app never pretends to have connected. Real drivers (psycopg2, snowflake, the
    Stripe SDK, …) are registered per type in deployment."""
    raise ConnectorError(
        f"No live driver is configured for '{connection['type']}' on this server. "
        "Install the client library and configure credentials to enable it.",
        status=501,
    )


def sample_driver(connection: dict, query: str, offset: int, limit: int) -> list:
    """A built-in synthetic source (type 'sample') so connectors/sync can be demonstrated
    end-to-end without real credentials. Deterministic, so dedup behaves correctly."""
    total = 137
    regions = ["North", "South", "East", "West"]
    statuses = ["Paid", "Pending", "Refunded"]
    rows = []
    for i in range(offset, min(total, offset + limit)):
        units = (i * 7) % 200
        price = round(((i % 90) + 10) + 0.99, 2)
        rows.append({
            "id": i + 1,
            "date": f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
            "region": regions[i % 4],
            "product": f"SKU-{1000 + (i % 50)}",
            "units": units,
            "revenue": round(units * price, 2),
            "status": statuses[i % 3],
        })
    return rows


def dispatch_driver(connection: dict, query: str, offset: int, limit: int) -> list:
    """Route to the right driver: the built-in sample source, then a real driver for the
    connection's type (Postgres/MySQL/Snowflake/Stripe — see drivers.py), else the default
    that fails safe. This is what the API uses. Real drivers themselves fail safe if their
    client library or credentials are missing, so we never pretend to have connected."""
    if connection["type"] in _DEMO_TYPES:
        return sample_driver(connection, query, offset, limit)
    from . import drivers  # lazy: avoids an import cycle (drivers imports ConnectorError)

    real = drivers.get_driver(connection["type"])
    if real is not None:
        return real(connection, query, offset, limit)
    return default_driver(connection, query, offset, limit)
