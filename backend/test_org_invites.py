"""Pending org-invite tests — inviting people who don't have accounts yet, and the
auto-join-on-signup behaviour.

Proven here:
  INV-create   inviting a NEW email makes a pending invite (kind 'invite'); it shows in /org.
  INV-existing inviting an EXISTING account adds them immediately (kind 'member').
  INV-join     signing up with an invited email auto-joins the team with the invited role;
               the invite stops being pending.
  INV-dedupe   re-inviting the same email replaces the prior pending invite (role updates).
  INV-revoke   a revoked invite disappears AND no longer auto-joins on signup.
  INV-perm     non-managers can't invite or revoke; you can't grant a role at/above your own.

Run from backend:  .venv\\Scripts\\python.exe test_org_invites.py
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

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="-orginv-test.db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")
os.environ.pop("SUMIO_SMTP_HOST", None)  # email off → 'emailed' is False, invites still work

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


def email(tag: str) -> str:
    return f"{tag}_{RUN}@example.com"


def signup(tag: str) -> tuple[str, str]:
    r = client.post("/auth/signup", json={"email": email(tag), "password": "s3cretpw!", "name": tag}).json()
    return r["token"], r["user"]["id"]


def hdr(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


owner_t, _ = signup("invowner")
client.post("/org", json={"name": "Invite Co"}, headers=hdr(owner_t))

# ---------------------------------------------------------------- INV-create -------------
r = client.post("/org/members", json={"email": email("newbie"), "role": "member"}, headers=hdr(owner_t))
j = r.json()
check("INV-create new email → pending invite", r.status_code == 200 and j.get("kind") == "invite", r.text)
check("INV-create email not sent when SMTP off (no error)", j.get("emailed") is False)
snap = client.get("/org", headers=hdr(owner_t)).json()
check("INV-create invite shows in /org", [i["email"] for i in snap["invites"]] == [email("newbie")], str(snap.get("invites")))

# ---------------------------------------------------------------- INV-join ---------------
# The invited person signs up → should land on the team as a member.
newbie_t, newbie_id = signup("newbie")
mine = client.get("/org", headers=hdr(newbie_t)).json()
check("INV-join invited signup auto-joins the team", mine.get("org") and mine.get("my_role") == "member", str(mine))
snap = client.get("/org", headers=hdr(owner_t)).json()
check("INV-join invite is consumed (no longer pending)", snap["invites"] == [], str(snap["invites"]))
check("INV-join member now in roster", email("newbie") in [m["email"] for m in snap["members"]])

# ---------------------------------------------------------------- INV-existing -----------
bob_t, bob_id = signup("invbob")  # existing account BEFORE being invited
r = client.post("/org/members", json={"email": email("invbob"), "role": "member"}, headers=hdr(owner_t))
check("INV-existing existing account added directly (kind member)",
      r.status_code == 200 and r.json().get("kind") == "member", r.text)

# ---------------------------------------------------------------- INV-dedupe -------------
client.post("/org/members", json={"email": email("carol"), "role": "member"}, headers=hdr(owner_t))
client.post("/org/members", json={"email": email("carol"), "role": "viewer"}, headers=hdr(owner_t))
snap = client.get("/org", headers=hdr(owner_t)).json()
carol_invites = [i for i in snap["invites"] if i["email"] == email("carol")]
check("INV-dedupe re-invite keeps ONE invite, updated role", len(carol_invites) == 1 and carol_invites[0]["role"] == "viewer", str(carol_invites))

# ---------------------------------------------------------------- INV-revoke -------------
invite_id = carol_invites[0]["id"]
r = client.post("/org/invites/revoke", json={"invite_id": invite_id}, headers=hdr(owner_t))
check("INV-revoke removes the pending invite", r.status_code == 200 and all(i["email"] != email("carol") for i in r.json()["invites"]), r.text)
# now carol signs up → should NOT be on a team
carol_t, _ = signup("carol")
check("INV-revoke revoked invite does NOT auto-join", client.get("/org", headers=hdr(carol_t)).json() == {"org": None})

# ---------------------------------------------------------------- INV-perm ---------------
# make bob a member already (added above); a member can't invite or revoke
check("INV-perm member can't invite (403)",
      client.post("/org/members", json={"email": email("x"), "role": "member"}, headers=hdr(bob_t)).status_code == 403)
check("INV-perm member can't revoke (403)",
      client.post("/org/invites/revoke", json={"invite_id": "whatever"}, headers=hdr(bob_t)).status_code == 403)
# promote alice to admin; admin can invite a member but not grant admin
client.post("/org/members", json={"email": email("invbob"), "role": "member"}, headers=hdr(owner_t))  # bob already member
alice_t, alice_id = signup("invalice")
client.post("/org/members", json={"email": email("invalice"), "role": "admin"}, headers=hdr(owner_t))
check("INV-perm admin can invite a member",
      client.post("/org/members", json={"email": email("dave"), "role": "member"}, headers=hdr(alice_t)).status_code == 200)
check("INV-perm admin can't invite an admin (403)",
      client.post("/org/members", json={"email": email("eve"), "role": "admin"}, headers=hdr(alice_t)).status_code == 403)

print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_DB_PATH)
except Exception:
    pass
sys.exit(1 if failed else 0)
