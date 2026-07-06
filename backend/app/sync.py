"""Scheduled sync & webhooks (Phase 3.3).

Keeps data fresh from external systems two ways:

  • Scheduled pull — a sync job fetches from a connector on a cadence. `run_due` is the tick
    a scheduler/cron calls. It is reliable and self-healing:
      - fires only jobs that are active and due, then advances next_run (idempotent window);
      - on a fetch failure it RETRIES with backoff up to a limit, logging every attempt,
        and marks the job failed (and pauses) only after exhausting retries;
      - if the fetched data is identical to last time (same content hash) it's DEDUPED — no
        redundant downstream work.

  • Webhook push — external systems POST fresh data. `ingest_webhook` DEDUPES by idempotency
    key (or payload hash), so a re-delivered push is acknowledged but not processed twice,
    and logs every receipt.

`fetch`/`apply` are injected so this is fully testable offline.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Callable

from . import store

_CADENCE_SECONDS = {"hourly": 3600, "daily": 86_400, "weekly": 7 * 86_400}
_RETRY_BACKOFF = 300          # seconds added before the next retry
_DEFAULT_MAX_RETRIES = 3
_MAX_SEEN_KEYS = 1000         # per-endpoint dedup memory bound
_MAX_LOG = 200

# fetch(sync) -> list[dict] rows. apply(sync, rows) -> None (optional, e.g. load to a session).
Fetch = Callable[[dict], list]
Apply = Callable[[dict, list], None]

# Loaded from / snapshotted to the DB when persistence is on (store.py).
_SYNCS: dict[str, dict] = store.register("syncs", store.load_dict("syncs"))
_WEBHOOK_SEEN: dict[str, list[str]] = store.register("webhook_seen", store.load_dict("webhook_seen"))   # endpoint_id -> recent dedup keys (ordered)
_WEBHOOK_LOG: dict[str, list[dict]] = store.register("webhook_log", store.load_dict("webhook_log"))   # endpoint_id -> receipts


class SyncError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> float:
    return time.time()


def _get(sync_id: str) -> dict:
    s = _SYNCS.get(sync_id)
    if s is None:
        raise SyncError("That sync job doesn't exist.", status=404)
    return s


def _hash_rows(rows: list) -> str:
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _log(s: dict, entry: dict) -> None:
    s["history"].append(entry)
    if len(s["history"]) > _MAX_LOG:
        del s["history"][: len(s["history"]) - _MAX_LOG]


# --------------------------------------------------------------------------- #
# Sync jobs
# --------------------------------------------------------------------------- #
def create_sync(
    name: str, connection_id: str, query: str, cadence: str,
    target_session: str | None = None, max_retries: int = _DEFAULT_MAX_RETRIES,
    now: float | None = None,
) -> dict:
    cadence = (cadence or "manual").lower()
    if cadence not in (set(_CADENCE_SECONDS) | {"manual"}):
        raise SyncError(f"Cadence must be one of: hourly, daily, weekly, manual.", 400)
    if not connection_id:
        raise SyncError("A sync needs a connection.", 400)
    if not (query or "").strip():
        raise SyncError("A sync needs a query or resource to pull.", 400)
    now = _now() if now is None else now
    sid = f"syn_{uuid.uuid4().hex[:12]}"
    sync = {
        "id": sid,
        "name": (name or "Data sync").strip(),
        "connection_id": connection_id,
        "query": query.strip(),
        "cadence": cadence,
        "target_session": target_session or None,
        "status": "active",                       # read-only pulls are safe to enable
        "next_run": None if cadence == "manual" else now,
        "last_run": None,
        "retry_count": 0,
        "max_retries": int(max_retries),
        "last_hash": None,
        "history": [],
    }
    _SYNCS[sid] = sync
    return sync


def pause_sync(sync_id: str) -> dict:
    s = _get(sync_id)
    s["status"] = "paused"
    s["next_run"] = None
    return s


def resume_sync(sync_id: str, now: float | None = None) -> dict:
    s = _get(sync_id)
    now = _now() if now is None else now
    s["status"] = "active"
    s["retry_count"] = 0
    s["next_run"] = None if s["cadence"] == "manual" else now
    return s


def delete_sync(sync_id: str) -> None:
    _get(sync_id)
    _SYNCS.pop(sync_id, None)


def list_syncs() -> list[dict]:
    return list(_SYNCS.values())


def get_sync(sync_id: str) -> dict:
    return _get(sync_id)


def _advance(sync: dict, now: float) -> None:
    sync["next_run"] = now + _CADENCE_SECONDS[sync["cadence"]] if sync["cadence"] in _CADENCE_SECONDS else None


def _run_one(sync: dict, fetch: Fetch, apply: Apply | None, now: float) -> dict:
    try:
        rows = list(fetch(sync) or [])
    except Exception as exc:
        # Retry with backoff, logging each attempt; fail (and pause) once exhausted.
        sync["retry_count"] += 1
        if sync["retry_count"] <= sync["max_retries"]:
            sync["next_run"] = now + _RETRY_BACKOFF
            entry = {"at": now, "status": "retry", "attempt": sync["retry_count"], "error": str(exc)}
        else:
            sync["status"] = "failed"
            sync["next_run"] = None
            entry = {"at": now, "status": "failed", "attempt": sync["retry_count"], "error": str(exc)}
            sync["retry_count"] = 0
        sync["last_run"] = now
        _log(sync, entry)
        return {"sync_id": sync["id"], **entry}

    sync["retry_count"] = 0
    sync["last_run"] = now
    digest = _hash_rows(rows)
    if digest == sync["last_hash"]:
        entry = {"at": now, "status": "deduped", "rows": len(rows)}
    else:
        if apply is not None:
            try:
                apply(sync, rows)
            except Exception as exc:
                entry = {"at": now, "status": "apply_failed", "rows": len(rows), "error": str(exc)}
                _advance(sync, now)
                _log(sync, entry)
                return {"sync_id": sync["id"], **entry}
        sync["last_hash"] = digest
        entry = {"at": now, "status": "synced", "rows": len(rows)}
    _advance(sync, now)
    _log(sync, entry)
    return {"sync_id": sync["id"], **entry}


def run_due(fetch: Fetch, now: float | None = None, apply: Apply | None = None) -> list[dict]:
    """Fire every active, due sync. Idempotent per window (next_run advances). Failed
    fetches retry with backoff and are logged; identical data is deduped."""
    now = _now() if now is None else now
    reports: list[dict] = []
    for s in list(_SYNCS.values()):
        if s["status"] != "active" or s["next_run"] is None or s["next_run"] > now:
            continue
        reports.append(_run_one(s, fetch, apply, now))
    return reports


def run_now(sync_id: str, fetch: Fetch, apply: Apply | None = None, now: float | None = None) -> dict:
    """Run one sync immediately (manual trigger)."""
    s = _get(sync_id)
    return _run_one(s, fetch, apply, _now() if now is None else now)


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
def ingest_webhook(
    endpoint_id: str, payload, idempotency_key: str | None = None,
    process: Callable[[str, object], None] | None = None, now: float | None = None,
) -> dict:
    """Accept a webhook push, DEDUPED by idempotency key (or payload hash). A repeat
    delivery is acknowledged but not processed again. Every receipt is logged."""
    now = _now() if now is None else now
    key = (idempotency_key or "").strip() or _hash_rows(payload)
    seen = _WEBHOOK_SEEN.setdefault(endpoint_id, [])
    log = _WEBHOOK_LOG.setdefault(endpoint_id, [])

    if key in seen:
        entry = {"at": now, "status": "deduped", "key": key}
        log.append(entry)
        return entry

    try:
        if process is not None:
            process(endpoint_id, payload)
        entry = {"at": now, "status": "accepted", "key": key}
    except Exception as exc:
        # Don't mark as seen on failure, so a genuine re-delivery can be retried.
        entry = {"at": now, "status": "error", "key": key, "error": str(exc)}
        log.append(entry)
        return entry

    seen.append(key)
    if len(seen) > _MAX_SEEN_KEYS:
        del seen[: len(seen) - _MAX_SEEN_KEYS]
    log.append(entry)
    if len(log) > _MAX_LOG:
        del log[: len(log) - _MAX_LOG]
    return entry


def webhook_log(endpoint_id: str) -> list[dict]:
    return list(_WEBHOOK_LOG.get(endpoint_id, []))
