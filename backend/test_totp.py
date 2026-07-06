"""Two-factor auth (TOTP) tests — the crypto AND the full login/enrollment flow.

Proven here:
  TOTP-rfc       _hotp matches the official RFC 6238 Appendix-B test vectors (so our codes
                 interoperate with Google Authenticator / Authy / 1Password).
  TOTP-verify    verify() accepts the current code, tolerates ±1 step of skew, and rejects
                 junk / wrong-length / stale codes.
  2FA-enroll     setup → enable requires a correct code; a wrong code is refused.
  2FA-login      With 2FA on, the password step returns a challenge (no token); the code
                 step exchanges it for a token; a wrong code is rejected.
  2FA-recovery   A recovery code logs you in exactly once, then is spent.
  2FA-guard      The challenge token and password-reset tokens CANNOT be used as bearer
                 tokens (no 'kind'-bearing token authenticates a request).
  2FA-disable    Disabling requires re-auth (password or code); afterward login is one-step.
  2FA-google     A Google sign-in for a 2FA account also lands on the challenge.

Run from backend:  .venv\\Scripts\\python.exe test_totp.py
"""
from __future__ import annotations

import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="-totp-test.db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, totp  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()  # create tables in the fresh temp DB (we don't run the app lifespan here)
client = TestClient(app)
passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def wrong_code(secret: str) -> str:
    """A 6-digit code guaranteed to differ from the real current one."""
    real = totp.code_at(secret)
    return str((int(real[0]) + 1) % 10) + real[1:]


# ============================================================ unit: TOTP crypto ==========
# RFC 6238 Appendix B — seed "12345678901234567890", SHA1, 8 digits, T0=0, step=30.
RFC_SEED = b"12345678901234567890"
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]
for t, expected in RFC_VECTORS:
    got = totp._hotp(RFC_SEED, t // totp.STEP_SECONDS, digits=8)
    check(f"TOTP-rfc t={t}", got == expected, f"got {got}, want {expected}")

_sec = totp.generate_secret()
_at = 1_700_000_000
check("TOTP-verify accepts current code", totp.verify(_sec, totp.code_at(_sec, _at), at=_at))
check("TOTP-verify tolerates -1 step", totp.verify(_sec, totp.code_at(_sec, _at - 30), at=_at))
check("TOTP-verify tolerates +1 step", totp.verify(_sec, totp.code_at(_sec, _at + 30), at=_at))
check("TOTP-verify rejects 2-steps stale", not totp.verify(_sec, totp.code_at(_sec, _at - 90), at=_at))
check("TOTP-verify rejects junk", not totp.verify(_sec, "abcdef", at=_at))
check("TOTP-verify rejects wrong length", not totp.verify(_sec, "1234", at=_at))
check("TOTP-verify rejects empty", not totp.verify(_sec, "", at=_at))
check("provisioning_uri is otpauth", totp.provisioning_uri(_sec, "a@b.com").startswith("otpauth://totp/Sumio:"))
check("recovery codes are unique + formatted", len(set(totp.generate_recovery_codes())) == 10)
check("normalize strips dashes/case", totp.normalize_recovery_code("AB12-CD34") == "ab12cd34")

# ============================================================ flow: enroll + login =======
EMAIL, PW = "mfa@example.com", "s3cretpw!"
client.post("/auth/signup", json={"email": EMAIL, "password": PW, "name": "Mfa"})
tok = client.post("/auth/login", json={"email": EMAIL, "password": PW}).json()
check("2FA-login pre-enroll returns a token", "token" in tok, str(tok))
bearer = {"Authorization": f"Bearer {tok['token']}"}

st = client.get("/auth/2fa/status", headers=bearer).json()
check("2FA status starts disabled", st == {"enabled": False, "recovery_codes_remaining": 0}, str(st))

setup = client.post("/auth/2fa/setup", headers=bearer).json()
secret = setup.get("secret")
check("2FA setup returns secret + uri", bool(secret) and setup.get("otpauth_uri", "").startswith("otpauth://"))

# still one-step until we confirm
mid = client.post("/auth/login", json={"email": EMAIL, "password": PW}).json()
check("2FA setup alone does NOT gate login", "token" in mid, str(mid))

# enable: wrong code refused, right code accepted
bad = client.post("/auth/2fa/enable", headers=bearer, json={"code": wrong_code(secret)})
check("2FA-enroll refuses a wrong code", bad.status_code == 400, str(bad.status_code))
en = client.post("/auth/2fa/enable", headers=bearer, json={"code": totp.code_at(secret)})
recovery = en.json().get("recovery_codes", [])
check("2FA-enroll enables + returns recovery codes", en.status_code == 200 and len(recovery) == 10, en.text)
st2 = client.get("/auth/2fa/status", headers=bearer).json()
check("2FA status now enabled w/ 10 codes", st2 == {"enabled": True, "recovery_codes_remaining": 10}, str(st2))

# now login is two-step
step1 = client.post("/auth/login", json={"email": EMAIL, "password": PW}).json()
check("2FA-login password step returns challenge, NOT a token", step1.get("status") == "totp_required" and "token" not in step1, str(step1))
challenge = step1["challenge"]

# 2FA-guard: challenge token must not authenticate a request
guard = client.get("/auth/me", headers={"Authorization": f"Bearer {challenge}"})
check("2FA-guard challenge token rejected as bearer", guard.status_code == 401, str(guard.status_code))

wrong = client.post("/auth/login/totp", json={"challenge": challenge, "code": wrong_code(secret)})
check("2FA-login rejects a wrong code", wrong.status_code == 401, str(wrong.status_code))
good = client.post("/auth/login/totp", json={"challenge": challenge, "code": totp.code_at(secret)})
check("2FA-login exchanges correct code for a token", good.status_code == 200 and "token" in good.json(), good.text)
check("2FA-login response marks user totp_enabled", good.json()["user"]["totp_enabled"] is True)

# ============================================================ recovery codes =============
ch = client.post("/auth/login", json={"email": EMAIL, "password": PW}).json()["challenge"]
rec = client.post("/auth/login/totp", json={"challenge": ch, "code": recovery[0]})
check("2FA-recovery a recovery code logs in", rec.status_code == 200 and "token" in rec.json(), rec.text)
ch2 = client.post("/auth/login", json={"email": EMAIL, "password": PW}).json()["challenge"]
reuse = client.post("/auth/login/totp", json={"challenge": ch2, "code": recovery[0]})
check("2FA-recovery a spent code is rejected", reuse.status_code == 401, str(reuse.status_code))
st3 = client.get("/auth/2fa/status", headers=bearer).json()
check("2FA-recovery remaining count dropped to 9", st3["recovery_codes_remaining"] == 9, str(st3))

# ============================================================ bearer 'kind' guard ========
reset_tok = auth.create_reset_token(good.json()["user"]["id"])
rg = client.get("/auth/me", headers={"Authorization": f"Bearer {reset_tok}"})
check("2FA-guard password-reset token rejected as bearer", rg.status_code == 401, str(rg.status_code))

# ============================================================ disable =====================
nogo = client.post("/auth/2fa/disable", headers=bearer, json={"password": "WRONG"})
check("2FA-disable refuses without valid re-auth", nogo.status_code == 400, str(nogo.status_code))
yes = client.post("/auth/2fa/disable", headers=bearer, json={"password": PW})
check("2FA-disable works with the password", yes.status_code == 200, yes.text)
after = client.post("/auth/login", json={"email": EMAIL, "password": PW}).json()
check("2FA-disable returns login to one step", "token" in after, str(after))

# ============================================================ google + 2FA ================
GEMAIL = "gmfa@example.com"


def _fake_google(_token: str) -> dict:
    return {"sub": "google-mfa-sub", "email": GEMAIL, "email_verified": True, "name": "G"}


auth.verify_google_id_token = _fake_google  # stub (no network)
import app.config as _cfg  # noqa: E402
_cfg.GOOGLE_CLIENT_ID = "test-client"

g1 = client.post("/auth/google", json={"id_token": "x"}).json()
gtok = g1["token"]
gbearer = {"Authorization": f"Bearer {gtok}"}
gsecret = client.post("/auth/2fa/setup", headers=gbearer).json()["secret"]
client.post("/auth/2fa/enable", headers=gbearer, json={"code": totp.code_at(gsecret)})
g2 = client.post("/auth/google", json={"id_token": "x"}).json()
check("2FA-google gates Google sign-in too", g2.get("status") == "totp_required" and "token" not in g2, str(g2))

# ================================================================================ done ===
print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_DB_PATH)
except Exception:
    pass
sys.exit(1 if failed else 0)
