"""Session management tests — "sign out of all devices" via the token-version epoch.

Proven here:
  SES-revoke   after /auth/logout-all, tokens minted earlier are rejected (revoked)…
  SES-current  …but the fresh token returned to the calling device still works…
  SES-independent  …and OTHER accounts are unaffected.
  SES-auth     logout-all requires a login.

Run from backend:  .venv\\Scripts\\python.exe test_session.py
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

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="-session-test.db")
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


def hdr(t):
    return {"Authorization": f"Bearer {t}"}


RUN = int(time.time())
a = client.post("/auth/signup", json={"email": f"sa_{RUN}@ex.com", "password": "s3cretpw!", "name": "A"}).json()
b = client.post("/auth/signup", json={"email": f"sb_{RUN}@ex.com", "password": "s3cretpw!", "name": "B"}).json()
old_a, tok_b = a["token"], b["token"]

check("SES-auth logout-all needs a login", client.post("/auth/logout-all").status_code == 401)
check("setup: A's original token works", client.get("/auth/me", headers=hdr(old_a)).status_code == 200)

r = client.post("/auth/logout-all", headers=hdr(old_a))
check("logout-all returns a fresh token", r.status_code == 200 and "token" in r.json(), r.text)
new_a = r.json()["token"]

check("SES-revoke A's OLD token is now rejected (401)",
      client.get("/auth/me", headers=hdr(old_a)).status_code == 401)
check("SES-current A's NEW token still works",
      client.get("/auth/me", headers=hdr(new_a)).status_code == 200)
check("SES-independent B is unaffected",
      client.get("/auth/me", headers=hdr(tok_b)).status_code == 200)

# a second logout-all revokes the first "new" token too (epoch keeps advancing)
r2 = client.post("/auth/logout-all", headers=hdr(new_a))
newer_a = r2.json()["token"]
check("SES-revoke each logout-all advances the epoch",
      client.get("/auth/me", headers=hdr(new_a)).status_code == 401
      and client.get("/auth/me", headers=hdr(newer_a)).status_code == 200)

print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_DB_PATH)
except Exception:
    pass
sys.exit(1 if failed else 0)
