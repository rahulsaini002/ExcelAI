"""Durable storage for generated result files.

THE GAP THIS CLOSES: results were written only to the local filesystem. That is fine on a
server with a disk; this one has none, so every restart discarded them. A user who kept a
download link, or opened version history to restore an earlier step, got a 404 — and the
code's own comment claimed the opposite ("survives a server restart"), so nothing looked
wrong from the inside.

SHAPE OF THE FIX — disk stays the fast path, the database is the fallback:
  write : to disk (as before) AND to the database, when under the size cap
  read  : disk first; on a miss, the database, rehydrating the disk copy on the way out
This keeps every existing performance property (FileResponse streams from disk, so a big
file never sits in the process's memory) and only adds a path for the case that used to
fail outright.

BOUNDED ON PURPOSE. A database is a poor object store and the free tier's is small, so
`config.RESULT_FILE_MAX_MB` decides what gets a durable copy. Over the cap the result still
works exactly as before — it simply lives on disk only, and `save` says so by returning
False rather than pretending. Old rows are pruned by the same TTL the disk cache uses, so
this can't grow without bound.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import config
from .db import session_scope
from .models import ResultFile


def max_bytes() -> int:
    return config.RESULT_FILE_MAX_MB * 1024 * 1024


def save(result_id: str, blob: bytes, filename: str, media_type: str) -> bool:
    """Keep a durable copy. Returns False when it wasn't kept (too big, or storage is off).

    Never raises: a result the user can already download must not fail because the durable
    copy couldn't be written.
    """
    if not config.PERSIST or not result_id or not blob:
        return False
    if len(blob) > max_bytes():
        return False
    try:
        with session_scope() as db:
            row = db.get(ResultFile, result_id)
            if row is None:
                row = ResultFile(id=result_id)
                db.add(row)
            row.filename = (filename or "result.xlsx")[:200]
            row.media_type = (media_type or "application/octet-stream")[:120]
            row.size_bytes = len(blob)
            row.blob = blob
        return True
    except Exception:
        return False


def load(result_id: str) -> tuple[bytes, str, str] | None:
    """(bytes, filename, media_type) for a stored result, or None."""
    if not config.PERSIST or not result_id:
        return None
    try:
        with session_scope() as db:
            row = db.get(ResultFile, result_id)
            if row is None or not row.blob:
                return None
            return bytes(row.blob), row.filename, row.media_type
    except Exception:
        return None


def delete(result_id: str) -> None:
    if not config.PERSIST or not result_id:
        return
    try:
        with session_scope() as db:
            row = db.get(ResultFile, result_id)
            if row is not None:
                db.delete(row)
    except Exception:
        pass


def prune(ttl_seconds: int | None = None) -> int:
    """Drop rows older than the results TTL. Returns how many went.

    Mirrors the disk cache's own expiry so the two can't disagree about what still exists.
    """
    if not config.PERSIST:
        return 0
    ttl = ttl_seconds if ttl_seconds is not None else config.RESULTS_TTL_HOURS * 3600
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl)
    try:
        with session_scope() as db:
            rows = list(db.scalars(select(ResultFile).where(ResultFile.created_at < cutoff)).all())
            for row in rows:
                db.delete(row)
            return len(rows)
    except Exception:
        return 0


def touch() -> float:
    """Wall clock, isolated so tests can reason about expiry without patching time."""
    return time.time()
