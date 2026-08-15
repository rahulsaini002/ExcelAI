"""PROGRAM PHASE 0.1 — file read & validation, verified at the API level (/inspect),
one check-group per row of the PRD 1.1 test table. The reader-level twin is
backend/test_1_1.py; this suite proves the same guarantees through the real
HTTP surface (upload -> /inspect), using the Standard Test Workbook + tests/files/.

PRD 1.1 rows covered:
  a normal xlsx        b csv                 c multi-sheet (standard workbook)
  d blank header       e duplicate headers   f mixed-type column
  g empty file         h wrong file type     i corrupt file
  j large file (>50k)  k non-English headers l pdf (SUPPORTED input since Phase 4.1
                                               — supersedes the doc's "wrong type
                                               (.pdf)"; wrong-type now uses .txt)

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_0_1.py
(Needs the fixtures: run tests\\make_standard_workbook.py + tests\\make_extra_files.py first.)
No AI calls — /inspect never touches the Brain.
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

_fd, _db = tempfile.mkstemp(suffix="-p01.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

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


def inspect(path: Path):
    with open(path, "rb") as f:
        return client.post("/inspect", files=[("files", (path.name, f.read(), "application/octet-stream"))])


F = TESTS / "files"
WB = TESTS / "standard_test_workbook.xlsx"

print("PHASE 0.1 — file read & validation (API level)\n")

# --- a. normal xlsx -----------------------------------------------------------------
r = inspect(F / "normal.xlsx")
j = r.json()
t = j["tables"][0] if r.status_code == 200 and j.get("tables") else {}
check("a. normal xlsx loads, 4 columns, 3 rows",
      r.status_code == 200 and t.get("row_count") == 3 and len(t.get("columns", [])) == 4,
      r.text[:160])

# --- b. csv --------------------------------------------------------------------------
r = inspect(F / "basic.csv")
j = r.json()
t = j["tables"][0] if r.status_code == 200 and j.get("tables") else {}
check("b. csv loads, 25 rows, headers correct",
      r.status_code == 200 and t.get("row_count") == 25
      and [c["name"] for c in t.get("columns", [])] == ["Region", "Product", "Qty", "Price"],
      r.text[:160])

# --- c. multi-sheet (Standard Test Workbook) -----------------------------------------
r = inspect(WB)
j = r.json()
names = [t["name"] for t in j.get("tables", [])] if r.status_code == 200 else []
check("c. multi-sheet: Sales/Customers/Prices/SingleRow all detected",
      all(any(n.endswith(s) or n == s for n in names) for s in ["Sales", "Customers", "Prices", "SingleRow"]),
      f"tables={names}")
sales = next((t for t in j.get("tables", []) if t["name"].endswith("Sales")), {})
check("c2. Sales row count = 200", sales.get("row_count") == 200, str(sales.get("row_count")))

# --- d. blank header -----------------------------------------------------------------
r = inspect(F / "blank_header.xlsx")
cols = [c["name"] for c in r.json()["tables"][0]["columns"]] if r.status_code == 200 else []
check("d. blank header auto-named (no empty column names)",
      r.status_code == 200 and len(cols) == 3 and all(str(c).strip() for c in cols),
      f"columns={cols}")

# --- e. duplicate headers ------------------------------------------------------------
r = inspect(F / "dup_headers.xlsx")
cols = [c["name"] for c in r.json()["tables"][0]["columns"]] if r.status_code == 200 else []
check("e. duplicate headers disambiguated (all names unique)",
      r.status_code == 200 and len(cols) == 3 and len(set(cols)) == 3,
      f"columns={cols}")

# --- f. mixed-type column ------------------------------------------------------------
r = inspect(F / "mixed_types.xlsx")
j = r.json()
t = j["tables"][0] if r.status_code == 200 and j.get("tables") else {}
check("f. mixed-type column loads without crash (5 rows, type inferred)",
      r.status_code == 200 and t.get("row_count") == 5
      and any(c["name"] == "Value" for c in t.get("columns", [])),
      r.text[:160])

# --- g. empty file (0 bytes) ---------------------------------------------------------
r = inspect(F / "empty.xlsx")
check("g. empty file -> clean 4xx error (no 500, no fake table)",
      400 <= r.status_code < 500 and r.json().get("status") == "error",
      f"HTTP {r.status_code}: {r.text[:160]}")

# --- h. wrong file type (.txt) -------------------------------------------------------
r = inspect(F / "wrong_type.txt")
check("h. wrong type (.txt) -> clean 4xx naming supported formats",
      400 <= r.status_code < 500 and "unsupported" in r.text.lower(),
      f"HTTP {r.status_code}: {r.text[:160]}")

# --- i. corrupt xlsx -----------------------------------------------------------------
r = inspect(F / "corrupt.xlsx")
check("i. corrupt xlsx -> clean 4xx error (no traceback leak)",
      400 <= r.status_code < 500 and r.json().get("status") == "error"
      and "Traceback" not in r.text,
      f"HTTP {r.status_code}: {r.text[:160]}")

# --- j. large file (60,001 rows) -----------------------------------------------------
r = inspect(F / "large.csv")
j = r.json()
t = j["tables"][0] if r.status_code == 200 and j.get("tables") else {}
check("j. large file loads with correct count + 'Large file' note",
      r.status_code == 200 and t.get("row_count") == 60_001
      and "large" in (t.get("note") or "").lower(),
      f"rows={t.get('row_count')} note={t.get('note')!r}")

# --- k. non-English headers ----------------------------------------------------------
r = inspect(F / "non_english.xlsx")
cols = [c["name"] for c in r.json()["tables"][0]["columns"]] if r.status_code == 200 else []
check("k. Hindi/Urdu headers preserved exactly",
      r.status_code == 200 and cols == ["नाम", "राशि", "تاریخ"],
      f"columns={cols}")

# --- l. pdf is a SUPPORTED input (engine Phase 4.1) ----------------------------------
pdf = F / "sample.pdf"
if pdf.exists():
    r = inspect(pdf)
    ok_load = r.status_code == 200 and r.json().get("tables")
    ok_honest = 400 <= r.status_code < 500 and r.json().get("status") == "error"
    check("l. pdf accepted: extracts a table OR fails honestly (never a 500)",
          bool(ok_load or ok_honest), f"HTTP {r.status_code}: {r.text[:160]}")
else:
    print("  SKIP  l. pdf (no sample.pdf fixture — pdfplumber assets not found)")

# --- m. PRD 1.1-h, images: an UNREADABLE image must fail CLEANLY, never with a 500 -----
# Sumio accepts photos/scans of tables via OCR (Phase 4.1), so the right behaviour for one
# it can't read is a clear 4xx that says what to upload instead. This regressed as an opaque
# 500 ("Something went wrong on our side") because an OCR failure escaped as an unexpected
# exception; reader._extract_or_explain now rewords it. A 500 here is a hard fail.
for fname, blob, mime in (
    ("photo.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"not a real image" * 8, "image/jpeg"),
    ("shot.png", b"\x89PNG\r\n\x1a\nbroken", "image/png"),
):
    r = client.post("/inspect", data={"session_id": "img"}, files=[("files", (fname, blob, mime))])
    body = r.json()
    # An image goes through OCR, which needs the model — so there are TWO honest outcomes
    # and which one you get depends on whether the model is reachable right now:
    #   4xx  OCR ran and the file really is unreadable -> name the formats we accept.
    #   503  the model is rate-limited/down, so we CANNOT know whether the file was
    #        readable. Saying "this file is bad" would be a guess; "the AI is busy, try
    #        again" is the truth. (/inspect used to return 500 here, blaming our server
    #        for a queue the user only has to wait out.)
    # The invariant either way — and the point of this check — is never a 500.
    unavailable = r.status_code == 503
    check(f"m. unreadable image ({fname}) -> clean 4xx or an honest 503, never a 500",
          (400 <= r.status_code < 500 or unavailable) and body.get("status") == "error",
          f"HTTP {r.status_code}: {str(body)[:140]}")
    msg = str(body.get("error", "")).lower()
    if unavailable:
        check(f"m. {fname} 503 explains the AI is busy rather than blaming the file",
              any(w in msg for w in ("rate-limit", "usage cap", "try again")),
              str(body.get("error"))[:140])
    else:
        check(f"m. {fname} message tells the user what to upload instead",
              any(w in msg for w in (".xlsx", ".csv", "spreadsheet")),
              str(body.get("error"))[:140])

print(f"\n{passed} passed, {failed} failed.")
try:
    os.unlink(_db)
except Exception:
    pass
sys.exit(1 if failed else 0)
