"""Step 3 — persistence. With persistence ON (a temp DATABASE_URL auto-enables it),
verifies the runtime stores (connections, schedules, …) are saved to the database and
survive a simulated restart — including entries that were mutated IN PLACE.

Run:  .venv\\Scripts\\python.exe test_persistence.py
"""
from __future__ import annotations

import os
import tempfile

# Setting DATABASE_URL both points us at a throwaway DB AND auto-enables persistence.
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP.name.replace("\\", "/")

# Create the tables BEFORE importing the modules whose stores load at import time.
from app import db  # noqa: E402

db.init_db()

from app import config, connectors, distribution, store, sync  # noqa: E402

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


# --- persistence is auto-on because DATABASE_URL is set --------------------------------
check("persistence auto-enabled when DATABASE_URL set", config.PERSIST is True)

# --- store primitive round-trip --------------------------------------------------------
d = store.register("demo_ns", {})
d["a"] = {"n": 1, "tags": {"x", "y"}}     # includes a set -> proves pickle (not JSON)
store.save("demo_ns")
reloaded = store.load_dict("demo_ns")
check("store round-trips a dict", reloaded.get("a", {}).get("n") == 1)
check("store preserves non-JSON types (set)", reloaded.get("a", {}).get("tags") == {"x", "y"})

# --- connectors: create -> save_all (what the middleware does) -> reload from DB --------
view = connectors.register_connection(name="My Sample", ctype="sample", credentials={})
conn_id = view["id"]
store.save_all()
from_db = store.load_dict("connections")
check("connection saved to DB", conn_id in from_db)
check("connection secret survives (stored apart)", "_secret" in from_db[conn_id])

# --- distribution: create + IN-PLACE mutation (arm) survives ----------------------------
sched = distribution.create_schedule(
    name="Weekly KPI", created_by="alice", channel="email", recipients=["a@b.com"],
    fmt="pdf", cadence="weekly",
)
sid = sched["id"]
distribution.arm_schedule(sid, confirm=True)          # mutates the schedule dict in place
check("schedule armed in memory", distribution._SCHEDULES[sid]["armed"] is True)
store.save_all()
sched_db = store.load_dict("schedules")
check("schedule saved to DB", sid in sched_db)
check("in-place mutation (armed) persisted", sched_db[sid]["armed"] is True)
check("in-place mutation (status) persisted", sched_db[sid]["status"] == "active")

# --- simulate a RESTART: rebuild the module's store from the DB, data is still there ----
connectors._CONNECTIONS.clear()
check("memory cleared (simulating restart)", connectors.get_connection_count() == 0
      if hasattr(connectors, "get_connection_count") else len(connectors._CONNECTIONS) == 0)
# this is what happens at process startup: load_dict() repopulates from the DB
restored = store.load_dict("connections")
connectors._CONNECTIONS.update(restored)
check("connection restored after restart", conn_id in connectors._CONNECTIONS)
check("restored connection is usable", connectors.get_connection(conn_id)["name"] == "My Sample")

# --- sync: webhook log (a list-valued store) round-trips -------------------------------
sync._WEBHOOK_LOG.setdefault("ep1", []).append({"received": 1})
store.save_all()
wl = store.load_dict("webhook_log")
check("webhook log persisted", wl.get("ep1") == [{"received": 1}])

# --- a SECOND save overwrites the snapshot (no stale duplicates) ------------------------
distribution.pause_schedule(sid)
store.save_all()
sched_db2 = store.load_dict("schedules")
check("re-save reflects latest state", sched_db2[sid]["status"] == "paused")

# --- cleanup ---------------------------------------------------------------------------
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
