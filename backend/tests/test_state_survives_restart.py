"""User-created state must survive a restart. This host sleeps when idle, so "restart"
is a DAILY event, not a rare one.

WHY THIS SUITE EXISTS: a user reported "I upload the sheet and open it the next day and it
asks me to upload again". That turned out to be one instance of a CLASS — several stores
held deliberate user state in a module-level dict and were simply never persisted. Losing
them is worse than an error, because it is SILENT: someone who saved a nightly workflow
just never sees it run again, with nothing to explain why.

The important test here is the LAST one: it asserts the property for every store rather
than the three we happened to notice, so the next store added can't quietly miss it.

Run: .venv\\Scripts\\python.exe tests\\test_state_survives_restart.py
"""
from __future__ import annotations

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

_fd, _db = tempfile.mkstemp(suffix="-restart.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")
os.environ["SUMIO_PERSIST"] = "1"

# Import the whole app, not just the modules under test: a store registers itself at
# IMPORT time, so checking the registry without loading main() would measure this test's
# imports rather than the running server's — and would "pass" while real stores were
# unregistered.
from app import config, main, marketplace, store, workflow  # noqa: E402,F401
from app.db import init_db  # noqa: E402

init_db()

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("STATE SURVIVES RESTART — the host sleeps daily, so this is not a rare case\n")
check("persistence is actually on for this run", config.PERSIST is True, str(config.PERSIST))

# --- a saved workflow ----------------------------------------------------------------
wf = workflow.create_workflow(
    name="Nightly dedupe",
    steps=[{"action": "remove_duplicates"}],
    trigger={"type": "schedule", "cadence": "daily"},
)
wf_id = wf["id"]
store.save_all()

# A restart = a fresh process reading the database back. load_dict is exactly what the
# module does at import, so this reproduces it without spawning a process.
reloaded = store.load_dict("workflows")
check("a saved workflow is written to the database", wf_id in reloaded, str(list(reloaded))[:160])
check(
    "and comes back intact — name, steps AND trigger",
    reloaded.get(wf_id, {}).get("name") == "Nightly dedupe"
    and reloaded[wf_id]["steps"] == [{"action": "remove_duplicates"}]
    and (reloaded[wf_id].get("trigger") or {}).get("type") == "schedule",
    str(reloaded.get(wf_id))[:200],
)

# --- a published plugin + its installs -------------------------------------------------
plug = marketplace.publish(
    name="Clean and sort", description="tidy up",
    steps=[{"action": "remove_duplicates"}, {"action": "sort", "columns": ["Price"]}],
    author="tester",
)
marketplace.install("team-a", plug["id"])  # (team_id, plugin_id) — in that order
store.save_all()

plugins_back = store.load_dict("plugins")
installs_back = store.load_dict("plugin_installs")
check("a published plugin survives", plug["id"] in plugins_back, str(list(plugins_back))[:160])
check(
    "a team's install survives (a set round-trips)",
    plug["id"] in (installs_back.get("team-a") or set()),
    str(installs_back)[:160],
)

# --- the systemic check ---------------------------------------------------------------
# Every module-level dict that holds USER-CREATED state must be registered. Config lookup
# tables and caches are deliberately excluded — losing those costs nothing.
EXPECTED = {
    "workflows",      # saved automations
    "plugins",        # marketplace listings
    "plugin_installs",
    "apikeys",        # credentials the user pasted into their own scripts
    "workspaces",     # shared collaboration workspaces
    "connections",    # database/SaaS connections
    "schedules",      # scheduled report delivery
    "syncs",          # scheduled data syncs
}
missing = sorted(EXPECTED - set(store._REGISTRY))
check(
    "every user-created store is registered for persistence",
    not missing,
    f"NOT PERSISTED: {missing} — these would vanish on the next restart",
)

print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_db)
except Exception:
    pass
sys.exit(1 if failed else 0)
