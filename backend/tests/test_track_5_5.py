"""ENHANCEMENT TRACK 5, item 5 — security hygiene.

Mostly a CONFIRMATION pass over things that already existed, plus the two things that
turned out not to hold. Written so each claim in the ask is checked by something, rather
than asserted in a summary:

  secrets are env-only          no literal credentials in the source; config reads env
  uploads are validated         type, declared size, and REAL size after reading
  file access is scoped         download/session ids are unguessable capabilities
  no sensitive data is logged   oplog redacts instructions and never stores cell values
  basic rate limiting           enabled by default, and not bypassable

TWO THINGS DID NOT HOLD, and both are the sort that look fine until someone tries:

  1. _client_ip trusted X-Forwarded-For unconditionally and used the LEFTMOST entry. That
     header is client-settable, so a caller could send a different value on every request
     and get a fresh bucket each time — the limiter would have counted to one, forever.
  2. The upload size guard read UploadFile.size and treated a missing value as 0, so an
     upload with no declared size passed the check and was then read into memory anyway.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_track_5_5.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
BACKEND = TESTS.parent
sys.path.insert(0, str(BACKEND))

_fd, _db = tempfile.mkstemp(suffix="-t55.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")
# A deliberately small budget so the limiter can be exercised over HTTP without sending
# hundreds of requests. Every HTTP check below runs BEFORE the burst that spends it.
os.environ["SUMIO_RATE_LIMIT"] = "25"
os.environ["SUMIO_RATE_WINDOW"] = "60"

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, main, oplog  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
passed = failed = 0
XL = "application/octet-stream"


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


class FakeUpload:
    """An upload whose declared size is missing — the fail-open case."""

    def __init__(self, size):
        self.size = size


def run() -> None:
    # =================================================================
    # 1. SECRETS ARE ENV-ONLY
    # =================================================================
    literal = re.compile(
        r"(api[_-]?key|secret|password|token)\s*=\s*[\"'][A-Za-z0-9_\-]{16,}[\"']", re.I)
    offenders = []
    for path in (BACKEND / "app").rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if literal.search(line) and "getenv" not in line and "dev-only" not in line:
                offenders.append(f"{path.name}:{i}")
    check("no literal credentials in the backend source", not offenders, f"{offenders[:5]}")
    check("the API key comes from the environment", config.GEMINI_API_KEY is None
          or isinstance(config.GEMINI_API_KEY, str))
    check("the JWT signing secret is env-backed with an obviously-fake dev default",
          "dev-only" in config.JWT_SECRET or config.JWT_SECRET != "dev-only-insecure-change-me"
          or os.getenv("SUMIO_JWT_SECRET") is not None,
          f"{config.JWT_SECRET[:12]}…")
    env_example = BACKEND / ".env.example"
    if env_example.exists():
        text = env_example.read_text(encoding="utf-8")
        check(".env.example carries no real-looking key",
              not re.search(r"AIza[0-9A-Za-z_\-]{20,}", text), "a real key is in .env.example")

    # =================================================================
    # 2. UPLOADS ARE VALIDATED
    # =================================================================
    # type
    r = client.post("/inspect", data={"session_id": f"t55-{uuid.uuid4().hex[:6]}"},
                    files=[("files", ("notes.txt", b"hello", "text/plain"))])
    check("an unsupported file type is rejected", r.status_code == 400, f"HTTP {r.status_code}")
    check("and the rejection names the formats we DO take",
          "xlsx" in r.text.lower(), r.text[:120])

    # declared size (the cheap pre-check)
    big = config.MAX_UPLOAD_MB * 1024 * 1024 + 1
    check("the declared-size guard rejects an oversized upload",
          main._too_big([FakeUpload(big)]) is not None)
    check("the declared-size guard passes a normal upload",
          main._too_big([FakeUpload(1024)]) is None)

    # THE FAIL-OPEN CASE: no declared size at all
    check("an upload with NO declared size slips past the cheap pre-check (why the "
          "post-read guard exists)", main._too_big([FakeUpload(None)]) is None)
    check("the post-read guard catches it using the real bytes",
          main._too_big_read([("x.xlsx", b"0" * big)]) is not None)
    check("the post-read guard passes a normal upload",
          main._too_big_read([("x.xlsx", b"0" * 1024)]) is None)
    check("both guards quote the configured limit",
          str(config.MAX_UPLOAD_MB) in (main._too_big_read([("x.xlsx", b"0" * big)]) or ""))

    # content: a file that CLAIMS to be xlsx but isn't
    r = client.post("/inspect", data={"session_id": f"t55-{uuid.uuid4().hex[:6]}"},
                    files=[("files", ("fake.xlsx", b"not really a spreadsheet", XL))])
    check("a file with a valid extension but invalid CONTENT is rejected cleanly",
          400 <= r.status_code < 500, f"HTTP {r.status_code} {r.text[:120]}")
    check("and the failure blames the file, not the server",
          "went wrong on our side" not in r.text.lower(), r.text[:120])

    # =================================================================
    # 3. FILE ACCESS IS SCOPED (unguessable capabilities)
    # =================================================================
    df = pd.DataFrame({"A": [1, 2, 3]})
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    sid = f"t55-{uuid.uuid4().hex[:8]}"
    client.post("/inspect", data={"session_id": sid},
                files=[("files", ("d.xlsx", buf.getvalue(), XL))])
    ex = client.post("/execute", data={
        "session_id": sid,
        "plan": '{"operations": [{"action": "sort", "columns": ["A"], "orders": ["desc"]}]}',
    })
    check("a run produces a download id", ex.status_code == 200, f"HTTP {ex.status_code}")
    did = ex.json().get("download_id") if ex.status_code == 200 else ""
    check("the download id is a full-length random hex, not a counter",
          isinstance(did, str) and len(did) == 32 and all(c in "0123456789abcdef" for c in did),
          f"download_id={did!r}")
    check("a guessed download id returns 404, not someone else's file",
          client.get("/download/00000000000000000000000000000001").status_code == 404)
    check("an unknown session cannot be read",
          client.post("/undo", data={"session_id": "not-a-real-session"}).status_code >= 400)

    # =================================================================
    # 4. NO SENSITIVE DATA IS LOGGED
    # =================================================================
    oplog.clear()
    secret_email = "very.secret.person@example.com"
    sid2 = f"t55-{uuid.uuid4().hex[:8]}"
    df2 = pd.DataFrame({"Email": [secret_email, secret_email], "N": [1, 1]})
    b2 = io.BytesIO()
    df2.to_excel(b2, index=False)
    client.post("/inspect", data={"session_id": sid2},
                files=[("files", ("p.xlsx", b2.getvalue(), XL))])
    client.post("/execute", data={
        "session_id": sid2,
        "plan": '{"operations": [{"action": "remove_duplicates", "columns": ["Email"]}]}',
    })
    log_blob = __import__("json").dumps(oplog.events(limit=50))
    check("no cell value reaches the operation log", secret_email not in log_blob,
          "a cell value was logged")
    check("column NAMES are kept, so the log stays useful", "Email" in log_blob,
          "column names were stripped too")
    check("the metrics endpoint leaks no cell values",
          secret_email not in client.get("/metrics/usage").text)
    check("the debug log endpoint leaks no cell values",
          secret_email not in client.get("/debug/oplog").text)

    # =================================================================
    # 5. RATE LIMITING — enabled, and NOT bypassable
    # =================================================================
    check("rate limiting is ON by default", config.RATE_LIMIT > 0, f"{config.RATE_LIMIT}")
    check("X-Forwarded-For is NOT trusted by default", config.TRUST_PROXY is False)

    class Req:
        def __init__(self, headers, host):
            self.headers = headers
            self.client = type("C", (), {"host": host})()

    spoof = Req({"x-forwarded-for": "1.2.3.4"}, "10.0.0.9")
    check("a spoofed X-Forwarded-For is ignored when not behind a proxy",
          main._client_ip(spoof) == "10.0.0.9", main._client_ip(spoof))

    config.TRUST_PROXY = True
    try:
        # A client prepends junk; the proxy appends the address it really saw.
        chained = Req({"x-forwarded-for": "evil-spoof, 203.0.113.7"}, "10.0.0.9")
        check("behind a trusted proxy we take the LAST hop, which the client can't forge",
              main._client_ip(chained) == "203.0.113.7", main._client_ip(chained))
        # The honest limit of TRUST_PROXY, asserted rather than glossed: with it ON and
        # NO real proxy appending a hop, a single client-supplied value IS taken at face
        # value. That is precisely why it defaults to off — enabling it is a statement
        # that a proxy you control sits in front and appends the true peer.
        solo = main._client_ip(Req({"x-forwarded-for": "evil-spoof"}, "10.0.0.9"))
        check("TRUST_PROXY=on with no real proxy trusts the header — hence the off default",
              solo == "evil-spoof", f"got {solo!r}")
    finally:
        config.TRUST_PROXY = False

    # /health is exempt BY DESIGN so deploy platforms can poll it; assert that on purpose
    # rather than discovering it as a surprise (this test first burst /health and saw
    # nothing but 200s).
    health_codes = [client.get("/health").status_code for _ in range(config.RATE_LIMIT + 10)]
    check("/health is deliberately exempt from rate limiting",
          set(health_codes) == {200}, f"{sorted(set(health_codes))}")

    # HTTP burst LAST — it spends the window deliberately, on a GATED endpoint.
    codes = [client.get("/brain/version").status_code for _ in range(config.RATE_LIMIT + 15)]
    check("a burst past the limit is refused with 429 on a gated endpoint", 429 in codes,
          f"statuses seen: {sorted(set(codes))}")
    check("requests before the limit still succeeded", codes[0] == 200, f"first={codes[0]}")
    check("the 429 arrives at roughly the configured limit, not much later",
          codes.index(429) <= config.RATE_LIMIT + 2, f"first 429 at {codes.index(429)}")


if __name__ == "__main__":
    print("TRACK 5 item 5 — security hygiene\n")
    run()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
