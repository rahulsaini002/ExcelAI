"""Phase 3.3 — Scheduled sync & webhooks tests.

PRD criteria:
  SY-fires    Schedules fire reliably — run_due fires due jobs and only due jobs, and is
              idempotent per window (advances next_run).
  SY-retry    Failed syncs are retried (with backoff) and logged; failed for good after the
              retry budget is exhausted.
  SY-dedup    Duplicate data is deduped (same content hash → skipped); duplicate webhook
              pushes (same idempotency key) are acknowledged but not reprocessed.

Run from backend:  .venv\\Scripts\\python.exe test_sync.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from fastapi.testclient import TestClient

from app import main
from app import sync as sy

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def reset():
    sy._SYNCS.clear()
    sy._WEBHOOK_SEEN.clear()
    sy._WEBHOOK_LOG.clear()


print("PHASE 3.3 — SCHEDULED SYNC & WEBHOOKS\n")

# =========================================================================
# SY-fires  Reliable firing + idempotent window
# =========================================================================
print("SY-fires  Schedule fires reliably")
reset()

ROWS = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]
fetch_calls = {"n": 0}


def fetch_ok(_s):
    fetch_calls["n"] += 1
    return ROWS


job = sy.create_sync("Daily orders", "con_1", "SELECT * FROM orders", "daily", now=1000.0)
check("new sync is active", job["status"] == "active", str(job["status"]))
check("recurring sync is scheduled (next_run set)", job["next_run"] == 1000.0, str(job["next_run"]))

# not due yet
check("not fired before due", sy.run_due(fetch_ok, now=999.0) == [], "")
# due now
reports = sy.run_due(fetch_ok, now=1000.0)
check("fires when due", len(reports) == 1 and reports[0]["status"] == "synced", str(reports))
check("rows synced", reports[0]["rows"] == 2, str(reports))
check("next_run advanced by a day", sy.get_sync(job["id"])["next_run"] == 1000.0 + 86400, str(sy.get_sync(job["id"])["next_run"]))
# idempotent: same instant won't fire again
check("idempotent — no re-fire same instant", sy.run_due(fetch_ok, now=1000.0) == [], "")

# manual jobs never auto-fire (other active jobs may; assert the manual one specifically doesn't)
m = sy.create_sync("Manual", "con_1", "Account", "manual", now=1000.0)
check("manual sync has no next_run", m["next_run"] is None, "")
fired = sy.run_due(fetch_ok, now=9_999_999)
check("manual never auto-fires", all(r["sync_id"] != m["id"] for r in fired), str(fired))

# =========================================================================
# SY-dedup  Identical data deduped
# =========================================================================
print("\nSY-dedup  Duplicate data deduped")
reset()
j = sy.create_sync("Dedup", "con_1", "q", "daily", now=0.0)
r1 = sy.run_due(fetch_ok, now=0.0)[0]
check("first run syncs", r1["status"] == "synced", str(r1))
# advance time to next window; same data → deduped
r2 = sy.run_due(fetch_ok, now=86400.0)[0]
check("identical data is deduped (not re-applied)", r2["status"] == "deduped", str(r2))


# changing data → synced again
def fetch_changed(_s):
    return [{"id": 1, "v": "CHANGED"}]


r3 = sy.run_due(fetch_changed, now=86400.0 * 2)[0]
check("changed data syncs again", r3["status"] == "synced", str(r3))

# =========================================================================
# SY-retry  Failures retried + logged, then fail for good
# =========================================================================
print("\nSY-retry  Failures retried + logged")
reset()
attempts = {"n": 0}


def fetch_flaky(_s):
    attempts["n"] += 1
    if attempts["n"] <= 2:
        raise RuntimeError("connection reset")
    return ROWS


jr = sy.create_sync("Retry", "con_1", "q", "daily", max_retries=3, now=0.0)
r = sy.run_due(fetch_flaky, now=0.0)[0]
check("failure → retry scheduled", r["status"] == "retry" and r["attempt"] == 1, str(r))
check("retry uses backoff (next_run pushed out)", sy.get_sync(jr["id"])["next_run"] == 0.0 + sy._RETRY_BACKOFF, str(sy.get_sync(jr["id"])["next_run"]))
check("failure is logged", any(h["status"] == "retry" for h in sy.get_sync(jr["id"])["history"]), "")

# second attempt (due at backoff) fails again, third succeeds
r2 = sy.run_due(fetch_flaky, now=sy._RETRY_BACKOFF)[0]
check("second failure → retry 2", r2["status"] == "retry" and r2["attempt"] == 2, str(r2))
r3 = sy.run_due(fetch_flaky, now=sy._RETRY_BACKOFF * 2)[0]
check("recovers on the third attempt", r3["status"] == "synced", str(r3))
check("retry_count reset after success", sy.get_sync(jr["id"])["retry_count"] == 0, "")


# exhausting the retry budget → failed + paused
def always_fail(_s):
    raise RuntimeError("down")


reset()
jf = sy.create_sync("Fails", "con_1", "q", "daily", max_retries=2, now=0.0)
sy.run_due(always_fail, now=0.0)              # attempt 1 → retry
sy.run_due(always_fail, now=sy._RETRY_BACKOFF)  # attempt 2 → retry
final = sy.run_due(always_fail, now=sy._RETRY_BACKOFF * 2)[0]  # attempt 3 → failed
check("fails for good after the retry budget", final["status"] == "failed", str(final))
check("failed job is paused (won't keep hammering)", sy.get_sync(jf["id"])["status"] == "failed", "")
check("every attempt is in the log", len([h for h in sy.get_sync(jf["id"])["history"] if h["status"] in ("retry", "failed")]) == 3, str(sy.get_sync(jf["id"])["history"]))

# =========================================================================
# SY-webhook  Webhook dedup + log
# =========================================================================
print("\nSY-webhook  Webhook dedup")
reset()
processed = {"n": 0}


def process(_endpoint, _payload):
    processed["n"] += 1


a = sy.ingest_webhook("ep1", {"order": 1}, "evt_123", process=process)
check("first webhook accepted", a["status"] == "accepted", str(a))
dup = sy.ingest_webhook("ep1", {"order": 1}, "evt_123", process=process)
check("duplicate (same key) deduped", dup["status"] == "deduped", str(dup))
check("dedup means it's processed once", processed["n"] == 1, str(processed))

# no key → dedup by payload hash
sy.ingest_webhook("ep2", {"x": 1}, process=process)
hsh = sy.ingest_webhook("ep2", {"x": 1}, process=process)
check("identical payload deduped by hash", hsh["status"] == "deduped", str(hsh))
# different payload → accepted
diff = sy.ingest_webhook("ep2", {"x": 2}, process=process)
check("different payload accepted", diff["status"] == "accepted", str(diff))

# a processing failure is NOT marked seen (so a real re-delivery can retry)
def boom(_e, _p):
    raise RuntimeError("bad")


err = sy.ingest_webhook("ep3", {"y": 1}, "k1", process=boom)
check("processing failure reported", err["status"] == "error", str(err))
retry = sy.ingest_webhook("ep3", {"y": 1}, "k1", process=process)
check("failed delivery can be retried (not stuck as deduped)", retry["status"] == "accepted", str(retry))
check("webhook receipts logged", len(sy.webhook_log("ep1")) >= 2, str(sy.webhook_log("ep1")))

# =========================================================================
# API
# =========================================================================
print("\nSY-api  HTTP endpoints")
reset()
client = TestClient(main.app)

created = client.post("/sync/create", data={
    "name": "API sync", "connection_id": "con_x", "query": "SELECT * FROM t", "cadence": "daily",
}).json()
check("API create sync", created["status"] == "ok" and created["sync"]["status"] == "active", str(created)[:120])
sid = created["sync"]["id"]
# run-due via API uses the unconfigured connector → fetch fails → retried + logged (no crash)
rd = client.post("/sync/run-due").json()
check("API run-due handles an unconfigured connector safely", rd["status"] == "ok", str(rd)[:120])
check("API run-due logs the attempt as retry/failed",
      rd["ran"] and rd["ran"][0]["status"] in ("retry", "failed"), str(rd["ran"]))
client.post(f"/sync/{sid}/pause")
check("API pause", client.get(f"/sync/{sid}").json()["sync"]["status"] == "paused", "")

# webhook over HTTP with dedup
w1 = client.post("/webhook/orders", json={"id": 1}, headers={"X-Idempotency-Key": "abc"}).json()
check("API webhook accepted", w1["status"] == "accepted", str(w1))
w2 = client.post("/webhook/orders", json={"id": 1}, headers={"X-Idempotency-Key": "abc"}).json()
check("API webhook duplicate deduped", w2["status"] == "deduped", str(w2))
log = client.get("/webhook/orders/log").json()
check("API webhook log available", log["status"] == "ok" and len(log["log"]) == 2, str(log)[:120])

reset()
print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
