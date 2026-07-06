"""Org RBAC tests — the pure role rules AND the team-management flow with its guardrails.

Proven here:
  RBAC-rank    role ordering + can()/has_at_least()/outranks() are correct.
  ORG-create   creating a team makes you owner; you can't be on two teams.
  ORG-add      invite existing accounts; unknown email → 404; already-on-a-team → 409;
               you can't grant a role at/above your own; non-managers can't invite.
  ORG-role     owner can promote to admin; an admin can't touch the owner, can't mint an
               admin, and can't change their own role.
  ORG-remove   you can remove someone strictly below you; not the owner, not a peer, not
               yourself.
  ORG-auth     endpoints require a login; non-members can't manage a team.

Run from backend:  .venv\\Scripts\\python.exe test_org.py
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

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="-org-test.db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

from app import rbac  # noqa: E402
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


def signup(tag: str) -> tuple[str, str]:
    """Create a fresh account, return (token, user_id)."""
    email = f"{tag}_{RUN}@example.com"
    r = client.post("/auth/signup", json={"email": email, "password": "s3cretpw!", "name": tag}).json()
    return r["token"], r["user"]["id"]


def hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ============================================================ RBAC-rank (unit) ===========
check("RBAC-rank orders roles", rbac.rank("owner") > rbac.rank("admin") > rbac.rank("member") > rbac.rank("viewer"))
check("RBAC-rank unknown role loses", rbac.rank("bogus") == -1)
check("RBAC has_at_least", rbac.has_at_least("admin", "member") and not rbac.has_at_least("member", "admin"))
check("RBAC can manage-members admin+", rbac.can("admin", "org:manage_members") and not rbac.can("member", "org:manage_members"))
check("RBAC can delete owner-only", rbac.can("owner", "org:delete") and not rbac.can("admin", "org:delete"))
check("RBAC outranks", rbac.outranks("owner", "admin") and not rbac.outranks("admin", "admin"))

# ============================================================ setup accounts =============
owner_t, owner_id = signup("owner")
alice_t, alice_id = signup("alice")
bob_t, bob_id = signup("bob")
carol_t, carol_id = signup("carol")
dave_t, dave_id = signup("dave")

# ============================================================ ORG-auth ===================
check("ORG-auth /org needs a login", client.get("/org").status_code == 401)
check("ORG-auth non-member sees no team", client.get("/org", headers=hdr(dave_t)).json() == {"org": None})

# ============================================================ ORG-create =================
r = client.post("/org", json={"name": "Acme"}, headers=hdr(owner_t))
check("ORG-create owner creates a team", r.status_code == 200 and r.json()["my_role"] == "owner", r.text)
check("ORG-create roster starts with just the owner",
      [m["role"] for m in r.json()["members"]] == ["owner"], r.text)
check("ORG-create can't be on two teams",
      client.post("/org", json={"name": "Other"}, headers=hdr(owner_t)).status_code == 409)

# ============================================================ ORG-add ====================
r = client.post("/org/members", json={"email": f"alice_{RUN}@example.com", "role": "admin"}, headers=hdr(owner_t))
check("ORG-add owner adds an admin", r.status_code == 200 and r.json()["member"]["role"] == "admin", r.text)
r = client.post("/org/members", json={"email": f"bob_{RUN}@example.com", "role": "member"}, headers=hdr(owner_t))
check("ORG-add owner adds a member", r.status_code == 200, r.text)
# An unknown email now creates a PENDING invite (auto-joins on signup) — see test_org_invites.
check("ORG-add unknown email → pending invite (200)",
      client.post("/org/members", json={"email": "nobody@nowhere.com", "role": "member"}, headers=hdr(owner_t)).json().get("kind") == "invite")
check("ORG-add already-on-a-team → 409",
      client.post("/org/members", json={"email": f"bob_{RUN}@example.com", "role": "member"}, headers=hdr(owner_t)).status_code == 409)

# admin (alice) can add a member…
check("ORG-add admin can add a member",
      client.post("/org/members", json={"email": f"carol_{RUN}@example.com", "role": "member"}, headers=hdr(alice_t)).status_code == 200)
# …but not another admin (can't grant a role at/above your own)
r = client.post("/org/members", json={"email": f"dave_{RUN}@example.com", "role": "admin"}, headers=hdr(alice_t))
check("ORG-add admin can't mint an admin", r.status_code == 403, r.text)
# a plain member can't invite at all
check("ORG-add member can't invite (403)",
      client.post("/org/members", json={"email": f"dave_{RUN}@example.com", "role": "member"}, headers=hdr(bob_t)).status_code == 403)

# ============================================================ ORG-role ===================
check("ORG-role admin demotes a member",
      client.post("/org/members/role", json={"user_id": bob_id, "role": "viewer"}, headers=hdr(alice_t)).status_code == 200)
check("ORG-role admin can't touch the owner",
      client.post("/org/members/role", json={"user_id": owner_id, "role": "member"}, headers=hdr(alice_t)).status_code == 403)
check("ORG-role admin can't mint an admin",
      client.post("/org/members/role", json={"user_id": bob_id, "role": "admin"}, headers=hdr(alice_t)).status_code == 403)
check("ORG-role can't change your own role",
      client.post("/org/members/role", json={"user_id": alice_id, "role": "member"}, headers=hdr(alice_t)).status_code == 400)
check("ORG-role owner promotes to admin",
      client.post("/org/members/role", json={"user_id": bob_id, "role": "admin"}, headers=hdr(owner_t)).status_code == 200)

# ============================================================ ORG-remove =================
# bob is now admin; alice (admin) can't remove a peer admin
check("ORG-remove admin can't remove a peer admin",
      client.post("/org/members/remove", json={"user_id": bob_id}, headers=hdr(alice_t)).status_code == 403)
check("ORG-remove nobody can remove the owner",
      client.post("/org/members/remove", json={"user_id": owner_id}, headers=hdr(alice_t)).status_code == 403)
check("ORG-remove can't remove yourself",
      client.post("/org/members/remove", json={"user_id": alice_id}, headers=hdr(alice_t)).status_code == 400)
check("ORG-remove owner removes an admin",
      client.post("/org/members/remove", json={"user_id": bob_id}, headers=hdr(owner_t)).status_code == 200)
# after removal, bob can create his own team (no longer on one)
check("ORG-remove frees the member to join/create again",
      client.post("/org", json={"name": "Bob Co"}, headers=hdr(bob_t)).status_code == 200)
# final roster: owner, alice(admin), carol(member) — bob gone
final = client.get("/org", headers=hdr(owner_t)).json()
check("ORG final roster is owner+admin+member (bob removed)",
      sorted(m["role"] for m in final["members"]) == ["admin", "member", "owner"], str(final["members"]))

# ============================================================ done =======================
print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_DB_PATH)
except Exception:
    pass
sys.exit(1 if failed else 0)
