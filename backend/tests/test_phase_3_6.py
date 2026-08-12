"""ENGINE PHASE 3.6 — voice input & spoken feedback (OFFLINE part).

Speech-to-text itself lives at the edge (browser/STT provider) and reaches the engine as
plain TEXT, so the LIVE DoD ("STT on accented/mixed speech") is a client concern. What the
ENGINE owns — and what this suite proves WITHOUT the model — is three things:

  1. NORMALIZE a raw transcript: strip spoken fillers (EN + HI/UR), collapse whitespace,
     and — critically — never change meaning (no invented punctuation, no "corrected"
     words, meaningful lookalikes like "umbrella"/a column named "Um" left intact).
  2. ASSESS before acting: empty / low-confidence / garbled audio is declined gracefully
     with an ask-to-repeat — the engine NEVER guesses a misheard command. Proven to short-
     circuit BEFORE the Brain is ever called.
  3. SHAPE spoken feedback to silent / step / summary from the executor's REAL notes.

The /process legs monkeypatch llm.parse_instruction with a canned plan so the whole voice
path (normalize -> assess -> execute -> speech) runs fully offline and deterministically.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_3_6.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p36.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import voice  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0

CSV = b"Region,Price,Qty\nNorth,100,1\nSouth,200,2\nNorth,100,1\nEast,50,4\n"
XLSX_MIME = "text/csv"


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("ENGINE PHASE 3.6 — voice input & spoken feedback (offline)\n")

# =============================== 1. normalize_transcript ===============================
check("strips English fillers + collapses whitespace",
      voice.normalize_transcript("um, sort by  revenue uh descending") == "sort by revenue descending",
      voice.normalize_transcript("um, sort by  revenue uh descending"))
check("strips a Hindi discourse-marker filler (मतलब)",
      voice.normalize_transcript("मतलब कीमत घटाओ") == "कीमत घटाओ",
      voice.normalize_transcript("मतलब कीमत घटाओ"))
check("strips an Urdu filler (یعنی)",
      voice.normalize_transcript("یعنی قیمت کم کریں") == "قیمت کم کریں",
      voice.normalize_transcript("یعنی قیمت کم کریں"))
# HONESTY: a meaningful word that merely CONTAINS a filler must survive untouched.
check("does NOT strip 'umbrella' (only standalone fillers)",
      voice.normalize_transcript("count the umbrella column") == "count the umbrella column",
      voice.normalize_transcript("count the umbrella column"))
check("does NOT touch a column literally named 'Um' mid-sentence when meaningful",
      "Um" in voice.normalize_transcript("sort by Umbrella"), "")
# all-filler utterance falls back to original text (so assess can honestly reject it)
check("all-filler utterance is not silently emptied (falls back for honest rejection)",
      voice.normalize_transcript("um uh er") != "" , repr(voice.normalize_transcript("um uh er")))
check("empty input -> empty string", voice.normalize_transcript("") == "", "")

# =============================== 2. assess_transcript ==================================
ok, msg = voice.assess_transcript("sort by Price descending")
check("clear transcript is accepted (ok=True, no message)", ok and msg is None, str((ok, msg)))
ok, msg = voice.assess_transcript("")
check("empty transcript is declined gracefully", (not ok) and "again" in (msg or "").lower(), str((ok, msg)))
ok, msg = voice.assess_transcript("123 456")
check("garbled/no-letters transcript is declined (numbers alone aren't a command)", not ok, str((ok, msg)))
ok, msg = voice.assess_transcript("sort it", confidence=0.3)
check("low-confidence transcript is declined (noisy audio)", (not ok) and "noise" in (msg or "").lower(), str((ok, msg)))
ok, msg = voice.assess_transcript("sort it", confidence=0.95)
check("high-confidence transcript is accepted", ok, str((ok, msg)))
# single-word / non-Latin commands must NOT be over-rejected
ok, _ = voice.assess_transcript("undo")
check("a one-word command is accepted", ok, "")
ok, _ = voice.assess_transcript("हटाओ")  # Hindi 'remove'
check("a non-Latin one-word command is accepted (letters in any script)", ok, "")

# =============================== 3. spoken_feedback ====================================
NOTES2 = ["Sorted 10 rows by Price.", "Removed 2 duplicate rows."]
check("silent mode speaks nothing (None)", voice.spoken_feedback(NOTES2, "silent") is None, "")
step = voice.spoken_feedback(NOTES2, "step")
check("step mode enumerates each step", step.startswith("Step 1:") and "Step 2:" in step, step)
summ = voice.spoken_feedback(NOTES2, "summary")
check("summary mode gives one wrap-up line (differs from step)", summ.startswith("All done.") and summ != step, summ)
check("single-note: step and summary read the same",
      voice.spoken_feedback(["Sorted 10 rows."], "step") == voice.spoken_feedback(["Sorted 10 rows."], "summary") == "Sorted 10 rows.", "")
check("empty notes -> 'Done.'", voice.spoken_feedback([], "summary") == "Done.", "")
check("None notes is safe", voice.spoken_feedback(None, "step") == "Done.", "")
check("unknown mode falls back to summary", voice.spoken_feedback(NOTES2, "bogus") == summ, "")

# =============================== 4. /process voice path (offline) ======================
# Canned plans so the whole path runs without the Brain. parse_instruction(instruction,
# structure, context) is what main.process calls.
_real_parse = m.llm.parse_instruction


def _plan_sort(instruction, structure, context=""):
    return {"operations": [{"action": "sort", "columns": ["Price"], "orders": ["desc"]}]}


def _plan_two(instruction, structure, context=""):
    return {"operations": [
        {"action": "sort", "columns": ["Price"], "orders": ["desc"]},
        {"action": "remove_duplicates"},
    ]}


def _boom(*a, **k):
    raise AssertionError("the Brain must NOT be called when the transcript is rejected")


# 4a. GRACEFUL FAIL short-circuits BEFORE the model — empty voice transcript.
m.llm.parse_instruction = _boom
r = c.post("/process", data={"instruction": "   ", "input_source": "voice",
                             "feedback_mode": "summary"},
           files=[("files", ("s.csv", CSV, XLSX_MIME))]).json()
check("empty voice transcript -> clarify (Brain never called)", r.get("status") == "clarify", str(r)[:200])
check("empty voice transcript speaks the ask-to-repeat", bool(r.get("speech")) and "again" in r["speech"].lower(), str(r.get("speech")))

# 4b. low-confidence voice -> clarify, still before the model.
r = c.post("/process", data={"instruction": "sort by price", "input_source": "voice",
                             "transcript_confidence": "0.2"},
           files=[("files", ("s.csv", CSV, XLSX_MIME))]).json()
check("low-confidence voice -> clarify (Brain never called)", r.get("status") == "clarify", str(r)[:160])

# 4c. garbled voice -> clarify.
r = c.post("/process", data={"instruction": "999 ...", "input_source": "voice"},
           files=[("files", ("s.csv", CSV, XLSX_MIME))]).json()
check("garbled voice -> clarify (Brain never called)", r.get("status") == "clarify", str(r)[:160])

# 4d. GOOD voice transcript with fillers -> normalized, executed, spoken back (summary).
m.llm.parse_instruction = _plan_sort
r = c.post("/process", data={"instruction": "um, sort by Price, uh, descending",
                             "input_source": "voice", "feedback_mode": "summary",
                             "transcript_confidence": "0.9"},
           files=[("files", ("s.csv", CSV, XLSX_MIME))]).json()
check("good voice transcript executes (status ok)", r.get("status") == "ok", str(r)[:200])
check("ok voice response includes spoken feedback", bool(r.get("speech")), str(r.get("speech")))

# 4e. silent mode: work happens, nothing is spoken.
r = c.post("/process", data={"instruction": "sort by Price desc", "input_source": "voice",
                             "feedback_mode": "silent", "transcript_confidence": "0.9"},
           files=[("files", ("s.csv", CSV, XLSX_MIME))]).json()
check("silent mode still executes (status ok)", r.get("status") == "ok", str(r)[:160])
check("silent mode speaks nothing (speech is null)", r.get("speech") is None, str(r.get("speech")))

# 4f. step mode on a two-step plan enumerates the steps.
m.llm.parse_instruction = _plan_two
r = c.post("/process", data={"instruction": "sort then dedupe", "input_source": "voice",
                             "feedback_mode": "step", "transcript_confidence": "0.9"},
           files=[("files", ("s.csv", CSV, XLSX_MIME))]).json()
check("step mode enumerates steps in speech", r.get("status") == "ok" and "Step 1:" in (r.get("speech") or ""), str(r.get("speech")))

# 4g. BACKWARD COMPATIBILITY: text input (defaults) still works and now also carries speech.
m.llm.parse_instruction = _plan_sort
r = c.post("/process", data={"instruction": "sort by Price descending"},
           files=[("files", ("s.csv", CSV, XLSX_MIME))]).json()
check("text input (no voice params) still works", r.get("status") == "ok", str(r)[:160])
check("text input also gets summary speech (additive field)", bool(r.get("speech")), str(r.get("speech")))

m.llm.parse_instruction = _real_parse  # restore

# =============================== 5. battery coverage ==================================
recs = list(csv.DictReader(open(TESTS / "prompt_battery.csv", encoding="utf-8-sig", newline="")))
voice_rows = [r for r in recs if r["capability"] == "voice"]
by_lang = defaultdict(int)
for r in voice_rows:
    by_lang[r["language"]] += 1
check("battery has voice rows in all four languages",
      all(by_lang.get(l, 0) > 0 for l in ("EN", "HI", "UR", "Hinglish")), dict(by_lang))
check("voice battery rows carry spoken-form fillers (um/matlab/मतलब/arre/…)",
      any(any(t in r["prompt"].lower() for t in ("um", "matlab", "arre", "yaar")) or
          any(t in r["prompt"] for t in ("मतलब", "यानी", "अरे", "مطلب", "یعنی"))
          for r in voice_rows), str(len(voice_rows)))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
