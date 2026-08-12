"""ENGINE PHASE 5.11 — ecosystem: plugin marketplace, custom agents, API platform, admin (BUILD).

The final phase. Three ecosystem capabilities, all built on the app's existing safety model:

  SANDBOXED PLUGINS   a plugin/agent is a validated pipeline of KNOWN operations — never
                      code. Publishing an unknown action is rejected (403). Running a plugin
                      is just running that Operation Plan through the SAME trusted executor,
                      so a shared plugin can do nothing a normal instruction couldn't.
  MARKETPLACE         publish / list / install / uninstall / run / unpublish.
  API PLATFORM        issue a key (raw shown ONCE, only a hash stored), verify, list (no raw),
                      revoke.
  ADMIN CONSOLE       a lightweight ecosystem overview.

No llm.py change → no schema/serving/quota risk; no battery rows (endpoint/mechanism).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_5_11.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p511.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import apikeys as K  # noqa: E402
from app import marketplace as M  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def reset():
    M._PLUGINS.clear(); M._INSTALLS.clear(); K._KEYS.clear()


print("ENGINE PHASE 5.11 — ecosystem (marketplace / agents / API / admin)\n")

# ===================== SANDBOX: publish validation =====================
reset()
p = M.publish("Clean & Sort", [{"action": "remove_duplicates"}, {"action": "sort", "columns": ["A"]}],
              "tidy up", "alice", kind="agent")
check("a valid plugin publishes", p["name"] == "Clean & Sort" and p["step_count"] == 2 and p["kind"] == "agent", str(p))
for bad, why in [
    (lambda: M.publish("Evil", [{"action": "exec_shell", "cmd": "rm -rf /"}]), "unknown action (sandbox)"),
    (lambda: M.publish("Empty", []), "no steps"),
    (lambda: M.publish("", [{"action": "sort"}]), "no name"),
    (lambda: M.publish("Malformed", ["not-a-dict"]), "malformed step"),
]:
    try:
        bad()
        check(f"sandbox rejects {why}", False, "no error")
    except M.MarketplaceError as e:
        # the unknown-action case is specifically a 403 (forbidden operation)
        ok = (e.status == 403) if "sandbox" in why else True
        check(f"sandbox rejects {why}", ok, f"status={e.status}")

# ===================== marketplace lifecycle =====================
check("plugin appears in the listing", any(x["id"] == p["id"] for x in M.listing()), "")
M.install("team1", p["id"])
check("install adds it to the team + bumps the install count", [x["id"] for x in M.installed("team1")] == [p["id"]] and M.get(p["id"])["installs"] == 1, str(M.installed("team1")))
M.install("team1", p["id"])  # idempotent
check("re-install is idempotent (count stays 1)", M.get(p["id"])["installs"] == 1, "")
check("uninstall removes it", M.uninstall("team1", p["id"]) is True and M.installed("team1") == [], "")
check("a team without it doesn't see it", M.installed("team2") == [], "")
# unpublish clears installs too
M.install("team1", p["id"])
check("unpublish removes the plugin and its installs", M.unpublish(p["id"]) is True and M.installed("team1") == [], "")

# ===================== API platform =====================
reset()
issued = K.issue("team1", "CI key")
check("issue returns the RAW key once + a prefix", issued["api_key"].startswith("sk_") and issued["prefix"] == issued["api_key"][:10], str(issued["prefix"]))
check("a live key verifies to its team", K.verify(issued["api_key"]) == "team1", "")
check("a wrong key doesn't verify", K.verify("sk_wrong") is None, "")
listed = K.list_keys("team1")
check("listing keys never exposes the raw key or its hash", listed and "hash" not in listed[0] and "api_key" not in listed[0] and listed[0]["prefix"] == issued["prefix"], str(listed[0]))
check("revoke disables the key immediately", K.revoke(issued["id"]) is True and K.verify(issued["api_key"]) is None, "")

# ===================== ENDPOINTS =====================
reset()
# publish over HTTP (valid) + sandbox reject over HTTP
pub = c.post("/marketplace/publish", data={"name": "Dedupe+Sort", "author": "bob",
             "steps": json.dumps([{"action": "remove_duplicates"}, {"action": "sort", "columns": ["A"], "orders": ["desc"]}])}).json()
check("/marketplace/publish returns the plugin", pub.get("status") == "ok" and pub["plugin"]["id"], str(pub)[:160])
pid = pub["plugin"]["id"]
bad = c.post("/marketplace/publish", data={"name": "X", "steps": json.dumps([{"action": "os_system"}])})
check("/marketplace/publish sandbox-rejects an unknown op (403)", bad.status_code == 403, f"HTTP {bad.status_code}")
lst = c.get("/marketplace/list").json()
check("/marketplace/list shows the plugin", any(x["id"] == pid for x in lst["plugins"]), str(lst)[:160])
c.post(f"/marketplace/{pid}/install", data={"team_id": "team1"})
inst = c.get("/marketplace/installed", params={"team_id": "team1"}).json()
check("/marketplace/installed lists it for the team", any(x["id"] == pid for x in inst["plugins"]), str(inst)[:160])

# RUN the sandboxed plugin through the trusted executor
c.post("/inspect", data={"session_id": "mk"}, files=[("files", ("d.csv", b"A,B\n3,9\n1,8\n3,9\n2,7\n", "text/csv"))])
run = c.post(f"/marketplace/{pid}/run", data={"session_id": "mk"}).json()
check("/marketplace/{id}/run executes the plugin's pipeline (dedupe → 3 rows)", run.get("status") == "ok" and run.get("row_count") == 3, str(run)[:200])
check("the run went through the trusted executor (both actions present)", set(run.get("actions") or []) == {"remove_duplicates", "sort"}, str(run.get("actions")))
run_bad = c.post("/marketplace/nope/run", data={"session_id": "mk"})
check("/marketplace/{unknown}/run 404s", run_bad.status_code == 404, f"HTTP {run_bad.status_code}")

# API-key endpoints
iss = c.post("/apikeys/issue", data={"team_id": "team1", "label": "prod"}).json()
check("/apikeys/issue returns a raw key once", iss.get("status") == "ok" and iss["api_key"].startswith("sk_"), str(iss.get("prefix")))
kl = c.get("/apikeys/list", params={"team_id": "team1"}).json()
check("/apikeys/list returns metadata without the raw key", kl["keys"] and "api_key" not in kl["keys"][0], str(kl)[:160])
rev = c.post(f"/apikeys/{iss['id']}/revoke").json()
check("/apikeys/{id}/revoke works", rev.get("revoked") is True, str(rev))

# Admin overview
ov = c.get("/admin/overview").json()
check("/admin/overview reports ecosystem counts", ov.get("status") == "ok" and ov["plugins"] >= 1 and ov["sessions"] >= 1 and "active_api_keys" in ov, str(ov))

reset()
m._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
