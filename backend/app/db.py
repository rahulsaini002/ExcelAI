"""Database foundation: one engine + session factory for the whole app.

Everything that needs to persist (users, sessions, workspaces, connections, …) goes
through here. We use SQLAlchemy so the SAME code runs on two databases:
  - local dev: a SQLite file (sumio.db) — no install, no server to run.
  - production: Postgres on Render — survives restarts (set DATABASE_URL).

How other modules use it:
  - define a table by subclassing `Base` (see the auth/persistence modules).
  - call `init_db()` once at startup to create any missing tables.
  - open a short-lived session with the `session_scope()` context manager:

        from .db import session_scope
        with session_scope() as db:
            db.add(row)            # commit happens automatically on a clean exit
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


def _normalized_url(url: str) -> str:
    # Render (and Heroku) hand out "postgres://…", but SQLAlchemy's dialect name is
    # "postgresql". Rewrite the prefix so the user can paste Render's URL unchanged.
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


_URL = _normalized_url(config.DATABASE_URL)

# SQLite needs check_same_thread=False because FastAPI may touch a connection from a
# different thread than the one that created it. (No effect on Postgres.)
_connect_args = {"check_same_thread": False} if _URL.startswith("sqlite") else {}

# pool_pre_ping avoids "server closed the connection" errors after Render's Postgres
# idles out a connection — SQLAlchemy quietly reconnects instead of erroring.
engine = create_engine(_URL, connect_args=_connect_args, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Parent class for every table model. Subclasses register themselves here, so
    `Base.metadata.create_all` (in init_db) knows about all of them."""


def init_db() -> None:
    """Create any tables that don't exist yet. Safe to call repeatedly — existing
    tables are left untouched. Imports the models so they're registered on Base first."""
    from . import models  # noqa: F401  (registers all table classes on Base.metadata)

    Base.metadata.create_all(bind=engine)
    _ensure_user_columns()


def _ensure_user_columns() -> None:
    """Add newer columns to an EXISTING `users` table. `create_all` only creates missing
    TABLES, never missing COLUMNS — so when we add a field to the User model, databases
    created before that change would be missing it and every query would error. This adds
    any missing column with a safe default. Idempotent, dialect-aware (SQLite + Postgres),
    and a no-op on a freshly-created database.

    This is a lightweight stand-in for a migration tool; when the schema grows complex,
    graduate to Alembic."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "users" not in insp.get_table_names():
        return  # brand-new DB: create_all already made it with every column
    existing = {c["name"] for c in insp.get_columns("users")}
    false_lit = "false" if engine.dialect.name == "postgresql" else "0"
    wanted = {
        "totp_secret": "VARCHAR",
        "totp_recovery_codes": "VARCHAR",
        "totp_enabled": f"BOOLEAN NOT NULL DEFAULT {false_lit}",
        "token_version": "INTEGER NOT NULL DEFAULT 0",
    }
    missing = {name: decl for name, decl in wanted.items() if name not in existing}
    if not missing:
        return
    with engine.begin() as conn:
        for name, decl in missing.items():
            conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {decl}"))


@contextmanager
def session_scope() -> Iterator[Session]:
    """A database session that commits on success and rolls back on error, then closes.
    This is the normal way to read/write — it guarantees the connection is returned to
    the pool even if something raises."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency form (use with `Depends(get_db)`). Yields a session and
    always closes it; the endpoint decides when to commit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
