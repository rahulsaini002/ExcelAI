"""Cross-device session storage — a user's source files, kept against their ACCOUNT.

THE PROBLEM: `_SESSIONS` is in memory and this host sleeps when idle, so "upload a sheet,
open it tomorrow" lost the session. A browser-side copy fixes that on the SAME device but
cannot help on a different one — a second device has never seen the file.

THE SHAPE OF THE FIX: store the SOURCE FILE against the user id. A session is then
rebuilt by re-reading that file, which is exactly what the in-app recovery path already
does; nothing new has to understand DataFrames or undo history.

WHY IDENTITY IS REQUIRED, stated plainly: "the same user on another device" only means
something if there is a user. Anonymous callers get no cloud sync — not a limitation we
chose so much as the definition of the feature. The workspace is already sign-in gated,
so in practice every real user of it qualifies.

TWO BOUNDS, both deliberate, because a database is a poor object store and a free-tier one
is small:
  * per-file cap (config.CLOUD_FILE_MAX_MB) — over it, the upload still works, it simply
    is not synced, and the caller is TOLD rather than left to assume it was.
  * per-user session cap (config.CLOUD_SESSIONS_PER_USER) — oldest dropped first, so one
    account cannot crowd out everyone else.
"""
from __future__ import annotations

from sqlalchemy import select

from . import config
from .models import CloudSession


class CloudSessionError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def max_bytes() -> int:
    return config.CLOUD_FILE_MAX_MB * 1024 * 1024


def too_big(size: int) -> bool:
    return size > max_bytes()


def save(
    db,
    user_id: str,
    session_id: str,
    name: str,
    filename: str,
    media_type: str,
    blob: bytes,
) -> CloudSession:
    """Create or replace the stored file for (user, session).

    Replace rather than append: a session has ONE source file at a time, and keeping every
    version would quietly multiply storage for no stated benefit.
    """
    if not user_id:
        raise CloudSessionError("Sign in to sync sessions across devices.", status=401)
    if not session_id:
        raise CloudSessionError("A session id is required.", status=400)
    if not blob:
        raise CloudSessionError("There was no file content to save.", status=400)
    if too_big(len(blob)):
        raise CloudSessionError(
            f"This file is larger than the {config.CLOUD_FILE_MAX_MB} MB sync limit, so it "
            "stays on this device only. Everything else works normally.",
            status=413,
        )

    row = db.scalar(
        select(CloudSession).where(
            CloudSession.user_id == user_id, CloudSession.session_id == session_id
        )
    )
    if row is None:
        row = CloudSession(user_id=user_id, session_id=session_id)
        db.add(row)
    row.name = (name or "Untitled session").strip()[:200]
    row.filename = (filename or "upload.xlsx").strip()[:200]
    row.media_type = (media_type or "application/octet-stream")[:120]
    row.size_bytes = len(blob)
    row.blob = blob
    db.flush()
    _prune(db, user_id)
    return row


def _prune(db, user_id: str) -> int:
    """Keep only the newest CLOUD_SESSIONS_PER_USER sessions for this user."""
    rows = list(
        db.scalars(
            select(CloudSession)
            .where(CloudSession.user_id == user_id)
            .order_by(CloudSession.updated_at.desc())
        ).all()
    )
    extra = rows[config.CLOUD_SESSIONS_PER_USER :]
    for row in extra:
        db.delete(row)
    return len(extra)


def listing(db, user_id: str) -> list[dict]:
    """A user's saved sessions, newest first. METADATA ONLY — never the bytes, so listing
    stays cheap and a session list can't accidentally ship a megabyte per row."""
    rows = db.scalars(
        select(CloudSession)
        .where(CloudSession.user_id == user_id)
        .order_by(CloudSession.updated_at.desc())
    ).all()
    return [
        {
            "session_id": r.session_id,
            "name": r.name,
            "filename": r.filename,
            "size_bytes": r.size_bytes,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


def get(db, user_id: str, session_id: str) -> CloudSession:
    row = db.scalar(
        select(CloudSession).where(
            CloudSession.user_id == user_id, CloudSession.session_id == session_id
        )
    )
    if row is None:
        # 404 and not 403: scoping the query by user_id already makes another user's
        # session indistinguishable from one that doesn't exist, which is what we want.
        raise CloudSessionError("That saved session isn't available.", status=404)
    return row


def delete(db, user_id: str, session_id: str) -> bool:
    row = db.scalar(
        select(CloudSession).where(
            CloudSession.user_id == user_id, CloudSession.session_id == session_id
        )
    )
    if row is None:
        return False
    db.delete(row)
    return True
