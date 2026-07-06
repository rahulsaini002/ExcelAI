"""Account tests — the real auth-methods exposure + real account deletion.

Proven here:
  ACC-methods  /auth/me reports the account's REAL linked sign-in methods
               (has_password / has_google), so Settings shows the truth, not chips.
  ACC-delete   DELETE /auth/account permanently removes the account AND its data —
               and dissolves a team the user owns (members lose it too).
  ACC-auth     deletion requires a login.

Run from backend:  .venv\\Scripts\\python.exe test_account.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="-account-test.db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


RUN = int(time.time())


def signup(tag: str):
    email = f"{tag}_{RUN}@example.com"
    r = client.post("/auth/signup", json={"email": email, "password": "s3cretpw!", "name": tag}).json()
    return email, r["token"]


def hdr(t):
    return {"Authorization": f"Bearer {t}"}


owner_email, owner_t = signup("acowner")
member_email, member_t = signup("acmember")

# ---------------------------------------------------------------- ACC-methods ------------
me = client.get("/auth/me", headers=hdr(owner_t)).json()
check("ACC-methods email account → has_password True, has_google False",
      me.get("has_password") is True and me.get("has_google") is False, str(me))
check("ACC-methods reports totp_enabled False by default", me.get("totp_enabled") is False)

# ---------------------------------------------------------------- ACC-auth ---------------
check("ACC-auth delete requires a login", client.delete("/auth/account").status_code == 401)

# ---------------------------------------------------------------- ACC-delete -------------
# owner builds a team with the member on it
client.post("/org", json={"name": "Acme"}, headers=hdr(owner_t))
client.post("/org/members", json={"email": member_email, "role": "member"}, headers=hdr(owner_t))
check("setup: member is on the team",
      client.get("/org", headers=hdr(member_t)).json().get("my_role") == "member")

# owner deletes their account
r = client.delete("/auth/account", headers=hdr(owner_t))
check("ACC-delete returns 200", r.status_code == 200, r.text)
# the account is gone: login fails
check("ACC-delete account is gone (login 401)",
      client.post("/auth/login", json={"email": owner_email, "password": "s3cretpw!"}).status_code == 401)
# the owned team was dissolved: the member is no longer on a team
check("ACC-delete owned team dissolved (member freed)",
      client.get("/org", headers=hdr(member_t)).json() == {"org": None})
# the member's own account still works
check("ACC-delete only the deleter is affected (member still valid)",
      client.get("/auth/me", headers=hdr(member_t)).status_code == 200)

print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_DB_PATH)
except Exception:
    pass
sys.exit(1 if failed else 0)
