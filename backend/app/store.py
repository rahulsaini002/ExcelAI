"""Optional database persistence for the small runtime stores.

Modules like connectors/distribution/collab/sync keep their data in a plain dict (e.g.
`_CONNECTIONS`). On their own those vanish when the server restarts. This module lets
each such dict be SNAPSHOTTED to the database after a change and RELOADED on startup,
so connections/schedules/workspaces/syncs survive restarts in production.

How a module opts in (one line, replacing its `= {}`):

    from . import store
    _CONNECTIONS = store.register("connections", store.load_dict("connections"))

`register` remembers the dict so a single `save_all()` (called by a middleware after
each write request) can snapshot every store. When persistence is OFF (no DATABASE_URL,
i.e. local dev + tests), `load_dict` returns a fresh `{}` and the saves are no-ops — so
behaviour is identical to before.

We pickle the WHOLE dict per store. The stores are small, so this is cheap, and it's
correct even when code mutates an entry in place (we always save the current snapshot).
Pickle (not JSON) so datetimes, sets, and credential dicts round-trip unchanged. The
data is our own — never untrusted input — so unpickling is safe here.
"""
from __future__ import annotations

import pickle
import threading
import traceback

from . import config
from .db import engine, session_scope
from .models import KVStore

# Serializes snapshot writes. Endpoint threads may mutate a store while another request's
# middleware is pickling it; the lock plus the shallow copy in _save_one keep saves
# consistent without blocking the endpoints themselves.
_SAVE_LOCK = threading.Lock()

# namespace -> the live dict object a module handed us. We hold the SAME object the
# module mutates, so save_all() always serializes its current contents.
_REGISTRY: dict[str, dict] = {}

# The stores load at import time — which can run BEFORE init_db() has created the tables
# (e.g. on a fresh production boot). So we create just the kv_store table on first use,
# once, so reads/writes never hit a "no such table" error.
_table_ready = False


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    try:
        KVStore.__table__.create(bind=engine, checkfirst=True)
        _table_ready = True
    except Exception:
        traceback.print_exc()


def register(namespace: str, obj: dict) -> dict:
    """Remember `obj` under `namespace` and return it (so the caller can assign it)."""
    _REGISTRY[namespace] = obj
    return obj


def load_dict(namespace: str) -> dict:
    """Return the persisted snapshot for `namespace`, or a fresh empty dict. Tolerant:
    if persistence is off, the table doesn't exist yet, or the blob is unreadable, it
    just returns `{}` (never raises) so startup can't be blocked by storage problems."""
    if not config.PERSIST:
        return {}
    _ensure_table()
    try:
        with session_scope() as db:
            row = db.get(KVStore, namespace)
            if row and row.blob:
                data = pickle.loads(row.blob)
                if isinstance(data, dict):
                    return data
    except Exception:
        traceback.print_exc()
    return {}


def _save_one(db, namespace: str, obj: dict) -> None:
    # dict(obj) takes a quick shallow snapshot so a concurrent mutation during the
    # (slower) pickle can't raise "dictionary changed size during iteration".
    blob = pickle.dumps(dict(obj))
    row = db.get(KVStore, namespace)
    if row is None:
        db.add(KVStore(namespace=namespace, blob=blob))
    else:
        row.blob = blob


def save(namespace: str) -> None:
    """Persist a single registered store immediately."""
    if not config.PERSIST:
        return
    obj = _REGISTRY.get(namespace)
    if obj is None:
        return
    _ensure_table()
    with _SAVE_LOCK, session_scope() as db:
        _save_one(db, namespace, obj)


def save_all() -> None:
    """Persist every registered store. Called after each write request (and callable
    directly). No-op when persistence is off."""
    if not config.PERSIST or not _REGISTRY:
        return
    _ensure_table()
    with _SAVE_LOCK, session_scope() as db:
        for namespace, obj in _REGISTRY.items():
            _save_one(db, namespace, obj)
