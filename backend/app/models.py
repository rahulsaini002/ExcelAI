"""Database tables (SQLAlchemy models).

Each class here is one table. They all inherit from `Base` (db.py), which is how
`init_db()` discovers them. As we move the in-memory stores into the database, each
gets its own model added to this file.

Started with `User` — the foundation for both login methods we chose:
  - email + password  -> password_hash is set.
  - Google sign-in    -> google_sub is set (password_hash stays NULL).
A single account can have both (we link by email).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    # A random hex id (not a sequential integer) so user ids aren't guessable/enumerable
    # in a public API — same spirit as the existing session ids.
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)

    # Email is the natural login + the key we link the two sign-in methods on.
    # Stored lowercased by the auth layer; unique + indexed for fast lookups.
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)

    # NULL for Google-only accounts (they have no password). Never the raw password —
    # always a bcrypt hash, set by the auth layer.
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)

    # Google's stable subject id ("sub" claim). NULL until the user links Google.
    google_sub: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)

    name: Mapped[str | None] = mapped_column(String, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # --- Two-factor auth (TOTP) -------------------------------------------------------
    # The base32 shared secret. Set when the user STARTS setup; cleared when they turn 2FA
    # off. Non-null does NOT mean 2FA is active — `totp_enabled` does (a half-finished
    # setup leaves a secret but keeps 2FA off, so a user can't be locked out mid-enrollment).
    totp_secret: Mapped[str | None] = mapped_column(String, nullable=True)

    # True only after the user confirms a code from their app; this is what gates login.
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # JSON array of BCRYPT-HASHED one-time recovery codes (never the plaintext). Each is
    # removed as it's used. NULL/[] once all are spent.
    totp_recovery_codes: Mapped[str | None] = mapped_column(String, nullable=True)

    # Bumped by "sign out of all devices" — every login token embeds the version it was
    # minted with, and current_user rejects tokens whose version is stale. This is how we
    # revoke otherwise-stateless JWTs without a server-side session store.
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:  # helpful in test output / debugging
        return f"<User {self.email}>"


class RunEvent(Base):
    """One row per successful, signed-in run of the Hands (/execute). This is the ONLY
    server-side record of what a logged-in user actually did — it exists so the weekly
    digest email has real activity to summarize (anonymous runs are never recorded).

    Deliberately tiny: no spreadsheet contents, no file, just "user X did a task titled Y
    that touched N rows at time T". The user's data never leaves their session; this is
    metadata only, the same shape the in-browser History already shows them."""
    __tablename__ = "run_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)

    # Whose run this was. Indexed because the digest groups events by user.
    user_id: Mapped[str] = mapped_column(String, index=True, nullable=False)

    # A short human title for the task ("Cleaned up dates", "Merged two sheets") — the
    # plan's ai_title when present, else a comma-joined list of the actions it ran.
    summary: Mapped[str | None] = mapped_column(String, nullable=True)

    # How many rows the result had — a simple "impact" number for the digest.
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Indexed because the digest windows on "the last 7 days".
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


class DigestLog(Base):
    """One row per user, remembering when we last emailed them a weekly digest. This is
    the cadence guard: the digest job only sends if it's been >= 7 days since last_sent_at,
    so re-running the cron (or running it every 15 min) never double-sends."""
    __tablename__ = "digest_log"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Lifetime count of digests sent to this user — handy in tests/support, harmless else.
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Organization(Base):
    """A team/organization — the layer ABOVE workspaces. Where collab.py governs who can
    edit a single spreadsheet (owner/editor/viewer), this governs who's on the team and who
    can manage it (owner/admin/member/viewer). Persisted (unlike the in-memory workspace
    collab state) because team membership is account-level, not per-file."""
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # The founding owner (also has an OrgMembership row with role 'owner').
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OrgMembership(Base):
    """One row per (org, user) — the user's role on that team. Roles, most→least power:
    owner > admin > member > viewer. Exactly one owner per org (set at creation)."""
    __tablename__ = "org_memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_org_user"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OrgInvite(Base):
    """A pending invitation to join a team, addressed to an email that has NO account yet.
    (Existing accounts are added directly — see org.add_member.) When someone signs up with
    an invited email, the invite is applied automatically and they land on the team; no link
    or token dance required. `id` doubles as the opaque token in the emailed signup link."""
    __tablename__ = "org_invites"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String, index=True, nullable=False)  # normalized (lower)
    role: Mapped[str] = mapped_column(String, nullable=False, default="member")
    invited_by: Mapped[str] = mapped_column(String, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class KVStore(Base):
    """A simple key→blob table for persisting the small runtime stores (connections,
    schedules, workspaces, syncs, webhook logs). Each row is ONE store's full snapshot,
    pickled. The stores are tiny (hundreds of small dicts), so snapshotting the whole
    thing on each change is cheap and correct even when an entry is mutated in place.
    See store.py for how it's read/written."""
    __tablename__ = "kv_store"

    namespace: Mapped[str] = mapped_column(String, primary_key=True)
    blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
