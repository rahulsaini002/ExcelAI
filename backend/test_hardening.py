"""Pre-deploy hardening — optional API token + per-IP rate limit.

Both are OFF by default (so dev + the rest of the suite are unaffected); these tests flip
the config on, exercise the gate, and reset it. /health is always exempt.

Run from backend:  .venv\\Scripts\\python.exe test_hardening.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from fastapi.testclient import TestClient

from app import config, main

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


client = TestClient(main.app)
print("PRE-DEPLOY HARDENING\n")

# =========================================================================
# Default OFF — nothing changes for normal callers
# =========================================================================
print("Default (off)")
check("health ok with no token configured", client.get("/health").status_code == 200, "")
check("normal endpoint ok with no token", client.get("/distribution/list").status_code == 200, "")

# =========================================================================
# API token
# =========================================================================
print("\nAPI token")
config.API_TOKEN = "s3cret-key"
try:
    # No key → 401, with a friendly (non-technical) message.
    r = client.get("/distribution/list")
    check("missing key → 401", r.status_code == 401, str(r.status_code))
    check("401 message is friendly", "key" in r.json().get("error", "").lower() and "traceback" not in str(r.json()).lower(), str(r.json()))
    # Wrong key → 401.
    check("wrong key → 401", client.get("/distribution/list", headers={"X-API-Key": "nope"}).status_code == 401, "")
    # Correct key (header) → passes the gate.
    check("correct X-API-Key → 200", client.get("/distribution/list", headers={"X-API-Key": "s3cret-key"}).status_code == 200, "")
    # Bearer form also accepted.
    check("Bearer token → 200", client.get("/distribution/list", headers={"Authorization": "Bearer s3cret-key"}).status_code == 200, "")
    # /health stays open even with a token configured (deploy health checks must work).
    check("health exempt from token", client.get("/health").status_code == 200, "")
finally:
    config.API_TOKEN = ""

check("token gate fully off after reset", client.get("/distribution/list").status_code == 200, "")

# =========================================================================
# Rate limit
# =========================================================================
print("\nRate limit")
main._RATE.clear()
config.RATE_LIMIT = 3
config.RATE_WINDOW = 60
try:
    codes = [client.get("/distribution/list").status_code for _ in range(5)]
    check("first 3 requests allowed", codes[:3] == [200, 200, 200], str(codes))
    check("4th+ request rate-limited (429)", codes[3] == 429 and codes[4] == 429, str(codes))
    r = client.get("/distribution/list")
    check("429 message is friendly", "too many" in r.json().get("error", "").lower(), str(r.json()))
    # /health is never rate-limited (so platform health checks don't trip it).
    main._RATE.clear()
    health_codes = [client.get("/health").status_code for _ in range(6)]
    check("health never rate-limited", all(c == 200 for c in health_codes), str(health_codes))
finally:
    config.RATE_LIMIT = 0
    main._RATE.clear()

check("rate limit fully off after reset", all(client.get("/distribution/list").status_code == 200 for _ in range(5)), "")

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
