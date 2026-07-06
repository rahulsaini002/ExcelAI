"""Step 1 — database foundation. Verifies the SQLite/Postgres plumbing works:
table creation, insert, query, the unique-email constraint, and Render's
postgres:// URL normalization. Uses a throwaway temp DB (never the real sumio.db).

Run:  .venv\\Scripts\\python.exe test_db.py
"""
from __future__ import annotations

import os
import tempfile

# Point the app at a throwaway database BEFORE importing anything that reads config.
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP.name.replace("\\", "/")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app import db  # noqa: E402
from app.db import _normalized_url, session_scope  # noqa: E402
from app.models import User  # noqa: E402

passed = 0
failed = 0
fails: list[str] = []


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        fails.append(f"{name}  {detail}")
        print(f"  FAIL  {name}  {detail}")


# --- URL normalization (Render hands out postgres://, SQLAlchemy needs postgresql://) ---
check("postgres:// rewritten to postgresql://",
      _normalized_url("postgres://u:p@host/db") == "postgresql://u:p@host/db")
check("postgresql:// left unchanged",
      _normalized_url("postgresql://u:p@host/db") == "postgresql://u:p@host/db")
check("sqlite url left unchanged",
      _normalized_url("sqlite:///x.db") == "sqlite:///x.db")

# --- create tables ---------------------------------------------------------------------
db.init_db()
check("users table created", "users" in db.Base.metadata.tables)

# --- insert + read back ----------------------------------------------------------------
with session_scope() as s:
    s.add(User(email="alice@example.com", password_hash="hash123", name="Alice"))

with session_scope() as s:
    u = s.scalar(select(User).where(User.email == "alice@example.com"))
    check("user round-trips", u is not None and u.name == "Alice")
    check("id auto-generated (hex)", u is not None and isinstance(u.id, str) and len(u.id) == 32)
    check("created_at auto-set", u is not None and u.created_at is not None)
    check("is_active defaults True", u is not None and u.is_active is True)
    check("google_sub null by default", u is not None and u.google_sub is None)

# --- a Google-only user (no password) is allowed ---------------------------------------
with session_scope() as s:
    s.add(User(email="bob@example.com", google_sub="google-123", name="Bob"))
with session_scope() as s:
    b = s.scalar(select(User).where(User.email == "bob@example.com"))
    check("google-only user (no password) allowed", b is not None and b.password_hash is None)

# --- duplicate email is rejected by the unique constraint ------------------------------
dup_rejected = False
try:
    with session_scope() as s:
        s.add(User(email="alice@example.com", password_hash="x"))
except IntegrityError:
    dup_rejected = True
check("duplicate email rejected", dup_rejected)

# --- cleanup ---------------------------------------------------------------------------
db.engine.dispose()
try:
    os.unlink(_TMP.name)
except OSError:
    pass

print(f"\n{passed} passed, {failed} failed.")
if fails:
    print("FAILURES:")
    for f in fails:
        print("  -", f)
raise SystemExit(1 if failed else 0)
