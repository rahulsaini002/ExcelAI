"""Security audit log (Phase 5.5).

An append-only trail of security-relevant events — PII shielded before the AI, a compliance
scan run, an authorized reveal — so a team can answer "what happened to sensitive data, and
when?". Deliberately tiny and pure (a bounded in-memory list, most-recent-first reads) so it
is trivially testable; like the rest of the app's runtime state it lives in memory.

Events are DATA about what the system did — never instructions — and carry no secret values
themselves (only which fields/counts), so the log is safe to surface.
"""
from __future__ import annotations

import time
import uuid

_EVENTS: list[dict] = []
_MAX = 1000


def record(action: str, detail: str = "", actor: str | None = None, meta: dict | None = None) -> dict:
    """Append one event. `action` is a short slug (e.g. 'pii_shielded', 'compliance_scan');
    `meta` holds structured context (column names, counts, active profiles) — never raw
    sensitive values."""
    ev = {
        "id": uuid.uuid4().hex[:12],
        "at": time.time(),
        "action": action,
        "actor": actor,
        "detail": detail,
        "meta": meta or {},
    }
    _EVENTS.append(ev)
    if len(_EVENTS) > _MAX:
        del _EVENTS[: len(_EVENTS) - _MAX]
    return ev


def events(limit: int = 100, action: str | None = None, actor: str | None = None) -> list[dict]:
    """Recent events, most-recent-first, optionally filtered by action and/or actor."""
    evs = [
        e for e in _EVENTS
        if (action is None or e["action"] == action) and (actor is None or e["actor"] == actor)
    ]
    return list(reversed(evs))[:max(0, limit)]


def clear() -> None:
    """Reset the log (tests)."""
    _EVENTS.clear()
