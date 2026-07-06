"""OIDC SSO tests — the ID-token verification (the security core) AND the full code flow.

A self-signed RSA key plays the IdP; discovery / token-exchange / JWKS are monkeypatched, so
the whole suite runs offline and deterministically.

Proven here:
  OIDC-verify   validate_id_token accepts a correctly-signed token and REJECTS: wrong audience,
                wrong issuer, expired, nonce mismatch, and a token signed by the wrong key.
  OIDC-state    create_state/read_state round-trip; a tampered/expired/wrong-kind state → None.
  OIDC-email    email_from_claims requires a verified email.
  OIDC-flow     /auth/oidc/start redirects to the IdP with state+nonce; /auth/oidc/callback
                validates state, exchanges the code, verifies the ID token, creates+links the
                user, and bounces back to the app with a login token. Bad state / unverified
                email / no id_token → an error redirect, never a session.
  OIDC-gate     Endpoints report 'off' when SSO isn't configured.

Run from backend:  .venv\\Scripts\\python.exe test_oidc.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="-oidc-test.db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, oidc  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402

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


def raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


# A self-signed RSA key stands in for the IdP's signing key.
_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIV = _key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
PUB = _key.public_key()
_wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
WRONG_PRIV = _wrong.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)

ISSUER = "https://idp.example.com"
CLIENT_ID = "sumio-client"


def make_id_token(*, priv=PRIV, iss=ISSUER, aud=CLIENT_ID, nonce="n1",
                  email="worker@corp.com", email_verified=True, name="Worker",
                  exp_delta=300) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": iss, "aud": aud, "sub": "idp-sub-123",
            "iat": now, "exp": now + timedelta(seconds=exp_delta),
            "nonce": nonce, "email": email, "email_verified": email_verified, "name": name,
        },
        priv, algorithm="RS256",
    )


# ------------------------------------------------------------------ OIDC-verify ----------
check("OIDC-verify accepts a valid token",
      oidc.validate_id_token(make_id_token(), PUB, ISSUER, CLIENT_ID, "n1")["email"] == "worker@corp.com")
check("OIDC-verify rejects wrong audience",
      raises(lambda: oidc.validate_id_token(make_id_token(aud="someone-else"), PUB, ISSUER, CLIENT_ID, "n1")))
check("OIDC-verify rejects wrong issuer",
      raises(lambda: oidc.validate_id_token(make_id_token(iss="https://evil.example"), PUB, ISSUER, CLIENT_ID, "n1")))
check("OIDC-verify rejects an expired token",
      raises(lambda: oidc.validate_id_token(make_id_token(exp_delta=-10), PUB, ISSUER, CLIENT_ID, "n1")))
check("OIDC-verify rejects a nonce mismatch",
      raises(lambda: oidc.validate_id_token(make_id_token(nonce="n1"), PUB, ISSUER, CLIENT_ID, "DIFFERENT")))
check("OIDC-verify rejects a token signed by the wrong key",
      raises(lambda: oidc.validate_id_token(make_id_token(priv=WRONG_PRIV), PUB, ISSUER, CLIENT_ID, "n1")))

# ------------------------------------------------------------------- OIDC-state ----------
st = oidc.create_state("abc")
check("OIDC-state round-trips the nonce", oidc.read_state(st) == "abc")
check("OIDC-state rejects a tampered token", oidc.read_state(st + "x") is None)
check("OIDC-state rejects a wrong-kind token",
      oidc.read_state(jwt.encode({"kind": "pwreset", "nonce": "x",
                                  "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                                 __import__("app").config.JWT_SECRET, algorithm="HS256")) is None)
_expired = jwt.encode({"kind": "oidc", "nonce": "x",
                       "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
                      __import__("app").config.JWT_SECRET, algorithm="HS256")
check("OIDC-state rejects an expired state", oidc.read_state(_expired) is None)

# ------------------------------------------------------------------- OIDC-email ----------
check("OIDC-email accepts a verified email",
      oidc.email_from_claims({"email": "a@b.com", "email_verified": True}) == "a@b.com")
check("OIDC-email blocks an unverified email",
      oidc.email_from_claims({"email": "a@b.com", "email_verified": False}) is None)
check("OIDC-email blocks a missing email", oidc.email_from_claims({"email_verified": True}) is None)

# ------------------------------------------------------------------- OIDC-gate -----------
for k in ("SUMIO_OIDC_ISSUER", "SUMIO_OIDC_CLIENT_ID", "SUMIO_OIDC_CLIENT_SECRET"):
    os.environ.pop(k, None)
check("OIDC-gate status disabled when unconfigured", client.get("/auth/oidc/status").json() == {"enabled": False})
check("OIDC-gate start → 503 when unconfigured",
      client.get("/auth/oidc/start", follow_redirects=False).status_code == 503)

# ------------------------------------------------------------------- OIDC-flow -----------
os.environ["SUMIO_OIDC_ISSUER"] = ISSUER
os.environ["SUMIO_OIDC_CLIENT_ID"] = CLIENT_ID
os.environ["SUMIO_OIDC_CLIENT_SECRET"] = "shhh"
oidc._DISCO_CACHE.clear()
oidc.discover = lambda issuer: {
    "authorization_endpoint": f"{issuer}/authorize",
    "token_endpoint": f"{issuer}/token",
    "jwks_uri": f"{issuer}/jwks",
}

check("OIDC-gate status enabled when configured", client.get("/auth/oidc/status").json() == {"enabled": True})

# start → 302 to the IdP with our state + nonce
r = client.get("/auth/oidc/start", follow_redirects=False)
loc = r.headers.get("location", "")
q = parse_qs(urlparse(loc).query)
check("OIDC-flow start redirects to the IdP authorize endpoint", loc.startswith(f"{ISSUER}/authorize"), loc[:60])
check("OIDC-flow start includes client_id + scope + state + nonce",
      q.get("client_id") == [CLIENT_ID] and "openid" in q.get("scope", [""])[0] and q.get("state") and q.get("nonce"),
      str(q))
state = q["state"][0]
nonce = q["nonce"][0]
check("OIDC-flow state carries the same nonce", oidc.read_state(state) == nonce)

# callback: mock the token exchange (returns an id_token bound to our nonce) + JWKS key
oidc.exchange_code = lambda *a, **k: {"id_token": make_id_token(nonce=nonce, email="worker@corp.com", name="Worker")}
oidc.signing_key_for_token = lambda jwks_uri, id_token: PUB

r = client.get(f"/auth/oidc/callback?code=abc123&state={state}", follow_redirects=False)
loc = r.headers.get("location", "")
check("OIDC-flow callback redirects to the app with a token",
      loc.startswith(f"{auth.config.FRONTEND_URL}/auth/callback#token="), loc[:80])
sso_token = loc.split("#token=", 1)[1] if "#token=" in loc else ""
decoded = auth.decode_token(sso_token)
check("OIDC-flow issued a valid login token (no 'kind')", "kind" not in decoded and decoded.get("sub"))
with session_scope() as db:
    created = db.query(User).filter(User.email == "worker@corp.com").one_or_none()
check("OIDC-flow created + linked the user by verified email", created is not None and created.name == "Worker")

# second login for the SAME email links to the SAME account (no duplicate)
r2 = client.get("/auth/oidc/start", follow_redirects=False)
q2 = parse_qs(urlparse(r2.headers["location"]).query)
state2, nonce2 = q2["state"][0], q2["nonce"][0]
oidc.exchange_code = lambda *a, **k: {"id_token": make_id_token(nonce=nonce2, email="worker@corp.com")}
client.get(f"/auth/oidc/callback?code=x&state={state2}", follow_redirects=False)
with session_scope() as db:
    count = db.query(User).filter(User.email == "worker@corp.com").count()
check("OIDC-flow is idempotent per email (no duplicate account)", count == 1, f"count={count}")

# tampered state → error redirect, no session
r = client.get("/auth/oidc/callback?code=x&state=not-a-valid-state", follow_redirects=False)
check("OIDC-flow bad state → error redirect", "#error=invalid_state" in r.headers.get("location", ""), r.headers.get("location", ""))

# unverified email → error redirect
r3 = client.get("/auth/oidc/start", follow_redirects=False)
q3 = parse_qs(urlparse(r3.headers["location"]).query)
oidc.exchange_code = lambda *a, **k: {"id_token": make_id_token(nonce=q3["nonce"][0], email="x@corp.com", email_verified=False)}
r = client.get(f"/auth/oidc/callback?code=x&state={q3['state'][0]}", follow_redirects=False)
check("OIDC-flow unverified email → error redirect", "#error=email_unverified" in r.headers.get("location", ""), r.headers.get("location", ""))

# ---------------------------------------------------------------------------- done ------
for k in ("SUMIO_OIDC_ISSUER", "SUMIO_OIDC_CLIENT_ID", "SUMIO_OIDC_CLIENT_SECRET"):
    os.environ.pop(k, None)
print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_DB_PATH)
except Exception:
    pass
sys.exit(1 if failed else 0)
