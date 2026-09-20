"""A download link must still work after a restart.

THE BUG: result files were written only to the local filesystem, and this host has no
persistent disk. A link that worked yesterday returned 404 today, and version history's
"restore an earlier version" broke the same way — while the code's own comment claimed
results "survive a server restart".

The restart is simulated the way it actually happens: DELETE THE FILES AND CLEAR THE
IN-MEMORY INDEX. Both are lost together on a real restart, so a test that cleared only one
would prove nothing.

Run: .venv\\Scripts\\python.exe tests\\test_durable_results.py
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

_fd, _db = tempfile.mkstemp(suffix="-results.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")
os.environ["SUMIO_PERSIST"] = "1"
os.environ["SUMIO_RESULTS_DIR"] = tempfile.mkdtemp(prefix="sumio-results-")

from fastapi.testclient import TestClient  # noqa: E402

from app import config, resultstore  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import _RESULTS, _RESULTS_DIR, _store_result, app  # noqa: E402

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


print("DURABLE RESULTS — a download link must outlive a restart\n")
check("persistence is on for this run", config.PERSIST is True, str(config.PERSIST))

payload = b"PK\x03\x04 pretend-xlsx " + b"z" * 5000
rid = _store_result(payload, "Cleaned.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

r = client.get(f"/download/{rid}")
check("the fresh link downloads", r.status_code == 200 and r.content == payload, f"HTTP {r.status_code}")

# --- the restart -----------------------------------------------------------------------
for f in Path(_RESULTS_DIR).glob("*"):
    try:
        f.unlink()
    except Exception:
        pass
_RESULTS.clear()
check("after the restart the disk copy and index really are gone",
      not (Path(_RESULTS_DIR) / rid).exists() and rid not in _RESULTS)

r = client.get(f"/download/{rid}")
check("THE LINK STILL WORKS (served from the database)",
      r.status_code == 200 and r.content == payload, f"HTTP {r.status_code} len={len(r.content)}")
check("and the original filename survives — metadata came back too",
      "Cleaned.xlsx" in r.headers.get("content-disposition", ""),
      r.headers.get("content-disposition", ""))
check("the disk cache is rehydrated, so the next hit is fast again",
      (Path(_RESULTS_DIR) / rid).exists())

# --- honest 404 ------------------------------------------------------------------------
r = client.get("/download/definitely-not-a-real-id")
check("an unknown id is still an honest 404", r.status_code == 404, f"HTTP {r.status_code}")

# --- eviction must agree with the cache -------------------------------------------------
from app.main import _delete_result  # noqa: E402

_delete_result(rid)
r = client.get(f"/download/{rid}")
check("a deleted result does NOT come back from the database",
      r.status_code == 404, f"HTTP {r.status_code}")

# --- the size cap ------------------------------------------------------------------------
too_big = b"x" * (config.RESULT_FILE_MAX_MB * 1024 * 1024 + 1024)
big_id = _store_result(too_big, "Huge.xlsx", "application/octet-stream")
check("an over-cap result is NOT stored durably", resultstore.load(big_id) is None)
r = client.get(f"/download/{big_id}")
check("...but it still downloads normally from disk while it's there",
      r.status_code == 200, f"HTTP {r.status_code}")

# --- ttl prune ----------------------------------------------------------------------------
rid2 = _store_result(b"small", "Small.xlsx", "application/octet-stream")
check("stored durably before pruning", resultstore.load(rid2) is not None)
removed = resultstore.prune(ttl_seconds=-1)  # everything is older than "now + 1s"
check("the TTL prune removes rows, so the table can't grow forever", removed >= 1, str(removed))
check("and the pruned row is gone", resultstore.load(rid2) is None)

print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_db)
except Exception:
    pass
sys.exit(1 if failed else 0)
