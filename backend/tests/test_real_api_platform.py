"""The API Platform page's data must come from the engine, not from the browser.

Context: the frontend used to mint `sk_live_…` strings with Math.random, keep them in
localStorage, and show them as credentials — they authenticated nothing. It also drew a
"128,450 / 100,000 monthly quota" bar against a quota that does not exist anywhere in
this system. These tests pin the REAL contract the UI now depends on:

  * GET  /limits          — the limits actually enforced, and an explicit "no monthly quota"
  * POST /apikeys/issue   — raw key returned EXACTLY once
  * GET  /apikeys/list    — metadata only; never the raw key or its hash
  * POST /apikeys/{id}/revoke
  * apikeys is REGISTERED with `store`, so keys survive a restart

Run: .venv\\Scripts\\python.exe tests\\test_real_api_platform.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-apiplat.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

from app import apikeys, config, store  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("REAL API PLATFORM — the page's numbers must come from the engine\n")

# --- /limits -----------------------------------------------------------------------
r = client.get("/limits")
j = r.json()
check("/limits answers 200", r.status_code == 200, f"HTTP {r.status_code}")
check(
    "/limits reports the REAL configured rate limit",
    j.get("rate_limit", {}).get("requests_per_window") == config.RATE_LIMIT
    and j["rate_limit"]["window_seconds"] == config.RATE_WINDOW,
    f"got {j.get('rate_limit')} vs config {config.RATE_LIMIT}/{config.RATE_WINDOW}",
)
check(
    "/limits reports the REAL upload cap",
    j.get("upload", {}).get("max_mb") == config.MAX_UPLOAD_MB,
    f"got {j.get('upload')} vs {config.MAX_UPLOAD_MB}",
)
# The whole point: the UI must be able to learn there is no monthly quota, rather than
# inventing one. An ABSENT key would force the UI to guess; an explicit null cannot.
check(
    "/limits states explicitly that there is NO monthly quota",
    "monthly_quota" in j and j["monthly_quota"] is None,
    f"monthly_quota={j.get('monthly_quota', '<missing>')}",
)
check(
    "enabled flag matches the limit (0 would mean unlimited, not zero allowed)",
    j["rate_limit"]["enabled"] == (config.RATE_LIMIT > 0),
    str(j["rate_limit"]),
)

# --- key issuance ------------------------------------------------------------------
team = f"t-{uuid.uuid4().hex[:8]}"
r = client.post("/apikeys/issue", data={"team_id": team, "label": "Production server"})
issued = r.json()
raw = issued.get("api_key", "")
check("issue returns the raw key once", bool(raw) and raw.startswith("sk_"), str(issued)[:160])
check("issue echoes the label", issued.get("label") == "Production server", str(issued)[:160])
check(
    "the raw key is NOT a browser-generated 'sk_live_' string",
    not raw.startswith("sk_live_"),
    raw[:20],
)

# --- listing never leaks the key ---------------------------------------------------
r = client.get(f"/apikeys/list?team_id={team}")
listed = r.json().get("keys", [])
check("list returns the issued key's metadata", len(listed) == 1, str(listed)[:200])
one = listed[0] if listed else {}
check(
    "list NEVER returns the raw key or its hash",
    "api_key" not in one and "hash" not in one and raw not in str(one),
    str(one)[:200],
)
check(
    "list returns a prefix long enough to tell keys apart",
    isinstance(one.get("prefix"), str) and len(one["prefix"]) >= 8 and raw.startswith(one["prefix"]),
    str(one.get("prefix")),
)
check("a fresh key is not revoked", one.get("revoked") is False, str(one))

# --- the key actually authenticates ------------------------------------------------
# The defining difference from the old fake: verify() resolves it to a real team.
check("the issued key verifies against the engine", apikeys.verify(raw) == team, str(apikeys.verify(raw)))
check("a made-up key does NOT verify", apikeys.verify("sk_live_deadbeef") is None)

# --- revoke -------------------------------------------------------------------------
r = client.post(f"/apikeys/{one['id']}/revoke")
check("revoke confirms", r.json().get("revoked") is True, r.text[:160])
check("a revoked key stops verifying immediately", apikeys.verify(raw) is None)
after = client.get(f"/apikeys/list?team_id={team}").json()["keys"][0]
check("the revoked key is still listed, marked revoked", after.get("revoked") is True, str(after))

# --- persistence --------------------------------------------------------------------
# Keys are CREDENTIALS the user wires into their own scripts. This host restarts often
# (free tier sleeps after 15 min), and an in-memory store would drop them silently.
check(
    "apikeys is registered with the persistence layer",
    store._REGISTRY.get("apikeys") is apikeys._KEYS,
    f"registry keys: {list(store._REGISTRY)}",
)

print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_db)
except Exception:
    pass
sys.exit(1 if failed else 0)
