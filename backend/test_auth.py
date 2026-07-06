"""Step 2 — email + password auth. Tests the pure helpers AND the HTTP endpoints
(/auth/signup, /auth/login, /auth/me) through FastAPI's TestClient. No live server,
no Gemini key; uses a throwaway temp database.

Run:  .venv\\Scripts\\python.exe test_auth.py
"""
from __future__ import annotations

import os
import tempfile

# Point at a throwaway DB BEFORE importing the app (config/db read it at import time).
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP.name.replace("\\", "/")

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, config, db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)

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


# ============================================================ PURE HELPERS
print("HELPERS")
h = auth.hash_password("correct horse battery staple")
check("hash is not the plaintext", h != "correct horse battery staple")
check("hash verifies", auth.verify_password("correct horse battery staple", h))
check("wrong password rejected", not auth.verify_password("nope", h))
check("verify on None hash (google-only) is False", auth.verify_password("x", None) is False)
check("same password hashes differently (random salt)",
      auth.hash_password("abc12345") != auth.hash_password("abc12345"))
check("very long password doesn't crash", isinstance(auth.hash_password("a" * 500), str))
check("email normalized (lower+trim)", auth.normalize_email("  Alice@X.COM ") == "alice@x.com")

tok = auth.create_access_token("user-123")
check("token decodes to the user id", auth.decode_token(tok)["sub"] == "user-123")
# forged token (wrong secret) is rejected
forged = jwt.encode({"sub": "hacker"}, "some-other-secret", algorithm=config.JWT_ALGORITHM)
forged_rejected = False
try:
    auth.decode_token(forged)
except jwt.PyJWTError:
    forged_rejected = True
check("forged token rejected", forged_rejected)
# expired token is rejected
_orig_hours = config.JWT_EXPIRE_HOURS
config.JWT_EXPIRE_HOURS = -1
expired = auth.create_access_token("user-123")
config.JWT_EXPIRE_HOURS = _orig_hours
expired_rejected = False
try:
    auth.decode_token(expired)
except jwt.ExpiredSignatureError:
    expired_rejected = True
check("expired token rejected", expired_rejected)

# ============================================================ SIGNUP
print("SIGNUP")
r = client.post("/auth/signup", json={"email": "alice@example.com", "password": "s3cretpw!", "name": "Alice"})
check("signup 200", r.status_code == 200, str(r.status_code))
body = r.json()
check("signup returns a token", bool(body.get("token")))
check("signup returns the user", body.get("user", {}).get("email") == "alice@example.com")
check("signup never leaks the hash", "password_hash" not in body.get("user", {}) and "password" not in body.get("user", {}))
alice_token = body["token"]
alice_id = body["user"]["id"]

# email is case/space-insensitive -> same account -> duplicate
r = client.post("/auth/signup", json={"email": "  ALICE@example.com ", "password": "another1"})
check("duplicate email (case/space) -> 409", r.status_code == 409, str(r.status_code))

# weak password rejected
r = client.post("/auth/signup", json={"email": "short@example.com", "password": "abc"})
check("short password -> 400", r.status_code == 400, str(r.status_code))

# malformed email rejected (pydantic EmailStr -> 422 via the app's validation handler)
r = client.post("/auth/signup", json={"email": "not-an-email", "password": "longenough1"})
check("bad email format rejected", r.status_code in (400, 422), str(r.status_code))

# ============================================================ LOGIN
print("LOGIN")
r = client.post("/auth/login", json={"email": "alice@example.com", "password": "s3cretpw!"})
check("login 200", r.status_code == 200, str(r.status_code))
check("login returns a token", bool(r.json().get("token")))

# login is case-insensitive on email
r = client.post("/auth/login", json={"email": "Alice@Example.com", "password": "s3cretpw!"})
check("login email case-insensitive", r.status_code == 200, str(r.status_code))

r = client.post("/auth/login", json={"email": "alice@example.com", "password": "WRONG"})
check("wrong password -> 401", r.status_code == 401, str(r.status_code))
check("wrong password message is generic",
      "email or password" in r.json().get("detail", "").lower())

r = client.post("/auth/login", json={"email": "ghost@example.com", "password": "whatever1"})
check("unknown email -> 401 (same generic msg)", r.status_code == 401, str(r.status_code))

# ============================================================ /auth/me (protected)
print("ME")
r = client.get("/auth/me", headers={"Authorization": f"Bearer {alice_token}"})
check("me with valid token -> 200", r.status_code == 200, str(r.status_code))
check("me returns the right user", r.json().get("email") == "alice@example.com")

r = client.get("/auth/me")
check("me without token -> 401", r.status_code == 401, str(r.status_code))

r = client.get("/auth/me", headers={"Authorization": "Bearer garbage.token.here"})
check("me with bad token -> 401", r.status_code == 401, str(r.status_code))

# a fresh login token also works on /me (end-to-end: signup -> login -> me)
login_tok = client.post("/auth/login", json={"email": "alice@example.com", "password": "s3cretpw!"}).json()["token"]
r = client.get("/auth/me", headers={"Authorization": f"Bearer {login_tok}"})
check("login token works on /me", r.status_code == 200 and r.json()["email"] == "alice@example.com")

# ============================================================ GOOGLE SIGN-IN
print("GOOGLE")

# Disabled when no client id is configured.
config.GOOGLE_CLIENT_ID = ""
r = client.post("/auth/google", json={"id_token": "anything"})
check("google disabled -> 503", r.status_code == 503, str(r.status_code))

# Enable it, and stub Google's verifier so no network/real token is needed.
config.GOOGLE_CLIENT_ID = "test-client-id.apps.googleusercontent.com"
_claims = {"sub": "google-sub-001", "email": "newper@gmail.com", "email_verified": True, "name": "New Person"}
auth.verify_google_id_token = lambda token: _claims  # type: ignore[assignment]

# New Google identity -> creates an account.
r = client.post("/auth/google", json={"id_token": "valid-token"})
check("google new user -> 200", r.status_code == 200, str(r.status_code))
gbody = r.json()
check("google returns a token", bool(gbody.get("token")))
check("google created the right user", gbody.get("user", {}).get("email") == "newper@gmail.com")
new_user_id = gbody["user"]["id"]
# the returned token actually works
r = client.get("/auth/me", headers={"Authorization": f"Bearer {gbody['token']}"})
check("google token works on /me", r.status_code == 200 and r.json()["email"] == "newper@gmail.com")

# Same Google sub again -> logs into the SAME account (no duplicate).
r = client.post("/auth/google", json={"id_token": "valid-token"})
check("google repeat -> same account", r.status_code == 200 and r.json()["user"]["id"] == new_user_id)

# Google identity whose email matches an EXISTING password account -> links, same id.
_claims2 = {"sub": "google-sub-alice", "email": "alice@example.com", "email_verified": True, "name": "Alice G"}
auth.verify_google_id_token = lambda token: _claims2  # type: ignore[assignment]
r = client.post("/auth/google", json={"id_token": "valid-token"})
check("google links to existing email account (same id)",
      r.status_code == 200 and r.json()["user"]["id"] == alice_id, str(r.status_code))
# and the linked account can STILL log in with its original password
r = client.post("/auth/login", json={"email": "alice@example.com", "password": "s3cretpw!"})
check("password still works after google-link", r.status_code == 200, str(r.status_code))

# Unverified Google email -> rejected.
auth.verify_google_id_token = lambda token: {"sub": "s2", "email": "x@gmail.com", "email_verified": False}  # type: ignore[assignment]
r = client.post("/auth/google", json={"id_token": "valid-token"})
check("unverified google email -> 401", r.status_code == 401, str(r.status_code))

# Verifier rejects the token (forged/expired) -> 401, generic message.
def _raise(token):
    raise ValueError("invalid token")
auth.verify_google_id_token = _raise  # type: ignore[assignment]
r = client.post("/auth/google", json={"id_token": "bad"})
check("invalid google token -> 401", r.status_code == 401, str(r.status_code))

# ============================================================ PASSWORD RESET
print("RESET")

# Email not configured -> honest 503 (never a fake "sent!").
os.environ.pop("SUMIO_SMTP_HOST", None)
r = client.post("/auth/forgot", json={"email": "alice@example.com"})
check("forgot without SMTP -> 503", r.status_code == 503, str(r.status_code))

# Configure email + stub the send so no real SMTP is needed.
os.environ["SUMIO_SMTP_HOST"] = "smtp.test.invalid"
_sent: list[tuple[str, str]] = []
_orig_send = auth.send_reset_email
auth.send_reset_email = lambda recipient, link: _sent.append((recipient, link))  # type: ignore[assignment]
try:
    r = client.post("/auth/forgot", json={"email": "alice@example.com"})
    check("forgot known email -> 200", r.status_code == 200, str(r.status_code))
    check("reset email sent with a link", len(_sent) == 1 and "/reset-password?token=" in _sent[0][1])

    # Unknown email: SAME 200 message (no probing), and no email goes out.
    r2 = client.post("/auth/forgot", json={"email": "nobody@example.com"})
    check("forgot unknown email -> same 200", r2.status_code == 200 and r2.json() == r.json())
    check("no email sent for unknown account", len(_sent) == 1)

    reset_token = _sent[0][1].split("token=", 1)[1]
    # Bad tokens rejected.
    r = client.post("/auth/reset", json={"token": "garbage", "password": "newpass123"})
    check("reset with garbage token -> 400", r.status_code == 400, str(r.status_code))
    login_tok2 = client.post("/auth/login", json={"email": "alice@example.com", "password": "s3cretpw!"}).json()["token"]
    r = client.post("/auth/reset", json={"token": login_tok2, "password": "newpass123"})
    check("login token can't reset (kind check)", r.status_code == 400, str(r.status_code))
    # Weak password rejected.
    r = client.post("/auth/reset", json={"token": reset_token, "password": "abc"})
    check("reset short password -> 400", r.status_code == 400, str(r.status_code))

    # The real reset works, old password stops working, new one logs in.
    r = client.post("/auth/reset", json={"token": reset_token, "password": "newpass123"})
    check("reset -> 200", r.status_code == 200, str(r.status_code))
    check("old password rejected after reset",
          client.post("/auth/login", json={"email": "alice@example.com", "password": "s3cretpw!"}).status_code == 401)
    r = client.post("/auth/login", json={"email": "alice@example.com", "password": "newpass123"})
    check("new password logs in", r.status_code == 200, str(r.status_code))
finally:
    auth.send_reset_email = _orig_send  # type: ignore[assignment]
    os.environ.pop("SUMIO_SMTP_HOST", None)

# alice's password is now "newpass123" (changed by the reset above).

# ============================================================ LOGIN WALL (require-auth)
print("REQUIRE_AUTH")
config.REQUIRE_AUTH = True
try:
    # /health stays open (deploy platforms poll it).
    check("health open even when auth required", client.get("/health").status_code == 200)
    # login endpoints stay open so users CAN sign in.
    r = client.post("/auth/login", json={"email": "alice@example.com", "password": "newpass123"})
    check("login open when auth required", r.status_code == 200, str(r.status_code))
    walled_token = r.json()["token"]
    # any other path without a token is blocked by the gate (before routing).
    check("no token -> 401 gate", client.get("/some/path").status_code == 401)
    check("bad token -> 401 gate", client.get("/some/path", headers={"Authorization": "Bearer nope"}).status_code == 401)
    # a valid token passes the gate (unknown route -> 404, i.e. NOT the gate's 401).
    passed_gate = client.get("/some/path", headers={"Authorization": f"Bearer {walled_token}"}).status_code
    check("valid token passes the gate", passed_gate != 401, str(passed_gate))
    # downloads are exempt: <a href> links can't carry a header; the id is the capability.
    dl = client.get("/download/nonexistent-id")
    check("download GET exempt from wall (404 not 401)", dl.status_code != 401, str(dl.status_code))
    # ...but only for GET/HEAD — other methods/paths stay walled.
    check("POST /download still walled", client.post("/download/x").status_code == 401)
    # a server caller with the (secret) API key needs no per-user login (Sheets add-on, cron).
    config.API_TOKEN = "server-secret-key"
    try:
        r = client.get("/some/path", headers={"X-API-Key": "server-secret-key"})
        check("API key passes wall (server caller)", r.status_code != 401, str(r.status_code))
        r = client.get("/some/path", headers={"Authorization": "Bearer server-secret-key"})
        check("API key as Bearer passes wall too", r.status_code != 401, str(r.status_code))
        # browser style: API key + a valid login token together also passes both gates.
        r = client.get("/some/path", headers={"X-API-Key": "server-secret-key", "Authorization": f"Bearer {walled_token}"})
        check("api-key + JWT together passes", r.status_code != 401, str(r.status_code))
        # no credentials at all -> rejected by the API-key gate first.
        check("no creds with both gates on -> 401", client.get("/some/path").status_code == 401)
    finally:
        config.API_TOKEN = ""
finally:
    config.REQUIRE_AUTH = False
check("gate off again -> open", client.get("/some/path").status_code != 401)

# ============================================================ cleanup
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
