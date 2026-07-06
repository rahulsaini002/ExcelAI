"""Role-based access control for organizations — the pure permission rules.

Roles, most→least power:  owner > admin > member > viewer.
  owner   the founder: everything, incl. deleting the org (there is exactly one).
  admin   can manage the team (invite/remove/change roles) — but not touch the owner,
          not grant/alter the admin role (only the owner does that), and not delete the org.
  member  a normal teammate: uses the app, creates work; can't manage the team.
  viewer  read-only.

Everything here is a pure function of roles — no database, no request — so the rules are
trivially unit-tested and can't drift per-endpoint. Endpoints call `require()`; org.py and
tests call `can()` / `has_at_least()`.
"""
from __future__ import annotations

# Ascending power. Index = rank.
ROLES = ("viewer", "member", "admin", "owner")
_RANK = {r: i for i, r in enumerate(ROLES)}

# Minimum role that may perform each action.
_REQUIRES = {
    "org:view": "viewer",
    "resource:create": "member",
    "org:manage_members": "admin",  # invite, remove, change roles
    "org:delete": "owner",
    "org:transfer": "owner",
}


def is_role(role: str) -> bool:
    return role in _RANK


def rank(role: str) -> int:
    """Numeric power of a role; -1 for an unknown role (so it loses every comparison)."""
    return _RANK.get(role, -1)


def has_at_least(role: str, minimum: str) -> bool:
    return rank(role) >= rank(minimum)


def can(role: str, action: str) -> bool:
    """True if `role` is allowed to perform `action`."""
    required = _REQUIRES.get(action)
    return required is not None and has_at_least(role, required)


def outranks(actor_role: str, target_role: str) -> bool:
    """True if actor is strictly more powerful than target — the guard that stops an admin
    from removing/demoting an owner (or another admin)."""
    return rank(actor_role) > rank(target_role)
