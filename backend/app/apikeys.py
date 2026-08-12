"""API platform — programmatic access keys (Phase 5.11).

Issues API keys for programmatic access to a team's engine. The raw key is shown EXACTLY
ONCE at creation and never stored — only a SHA-256 hash is kept, so a leak of this store
can't reveal a usable key (the same reason password hashes exist). `verify` checks a
presented key against the hashes; `revoke` disables one immediately.

Pure in-memory store like the rest of the app's runtime state.
"""
from __future__ import annotations

import hashlib
import secrets
import time
import uuid

_KEYS: dict[str, dict] = {}  # key_id -> {id, team_id, label, hash, prefix, created_at, revoked}


def _hash(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def issue(team_id: str, label: str = "") -> dict:
    """Create a key. Returns the RAW key once (never retrievable again) plus its metadata."""
    raw = "sk_" + secrets.token_urlsafe(24)
    kid = uuid.uuid4().hex[:12]
    _KEYS[kid] = {
        "id": kid, "team_id": team_id or "default",
        "label": (label or "").strip() or "API key",
        "hash": _hash(raw), "prefix": raw[:10],
        "created_at": time.time(), "revoked": False,
    }
    return {"id": kid, "api_key": raw, "prefix": raw[:10], "label": _KEYS[kid]["label"], "team_id": _KEYS[kid]["team_id"]}


def verify(raw: str) -> str | None:
    """Return the team_id a live key belongs to, or None (unknown/revoked)."""
    if not raw:
        return None
    h = _hash(raw)
    for k in _KEYS.values():
        if not k["revoked"] and k["hash"] == h:
            return k["team_id"]
    return None


def list_keys(team_id: str) -> list[dict]:
    """A team's keys as safe metadata — never the raw key or its hash."""
    return [
        {"id": k["id"], "label": k["label"], "prefix": k["prefix"],
         "created_at": k["created_at"], "revoked": k["revoked"]}
        for k in _KEYS.values() if k["team_id"] == (team_id or "default")
    ]


def revoke(key_id: str) -> bool:
    k = _KEYS.get(key_id)
    if k and not k["revoked"]:
        k["revoked"] = True
        return True
    return False


def count(active_only: bool = True) -> int:
    return sum(1 for k in _KEYS.values() if not (active_only and k["revoked"]))
