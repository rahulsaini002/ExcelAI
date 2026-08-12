"""Voice input & spoken feedback (Engine Phase 3.6).

The engine never records or transcribes audio itself — speech-to-text happens at the
edge (the browser's Web Speech API or a client STT provider) and arrives here as plain
TEXT, exactly like a typed instruction. So the ENGINE's job for voice is narrow and
honest:

  1. NORMALIZE a raw transcript — spoken instructions carry filler words ("um", "uh",
     "मतलब") and stray whitespace that a typed one wouldn't. We strip those so the same
     instruction routes identically whether typed or spoken. We do NOT rewrite meaning,
     invent punctuation, or "fix" words — that would risk changing what the user asked.

  2. ASSESS a transcript before acting — noisy audio produces empty / low-confidence /
     garbled text. Rather than feed junk to the Brain (which might confidently do the
     wrong thing), we decline gracefully and ask the user to repeat. A wrong-but-
     confident action on a misheard command is the worst outcome; silence-then-ask is
     the safe one.

  3. SHAPE spoken feedback to the user's chosen verbosity — silent / step / summary.
     This is a pure view over the executor's real per-step notes; it never generates a
     fresh description that could drift from what actually happened.

No new Operation-schema field is introduced (the llm.py schema has zero headroom): a
spoken instruction is normalized to text and goes through the exact same parse pipeline.
Feedback mode and transcript confidence are request-level knobs, not plan fields.

Language-agnostic: filler lists cover EN + common Hindi/Urdu/Hinglish fillers, and the
"has any letters" garbled check counts letters in ANY script (Latin, Devanagari, Arabic).
"""
from __future__ import annotations

import re
import unicodedata

# How much the app should SPEAK back after doing the work.
FEEDBACK_MODES = ("silent", "step", "summary")
DEFAULT_MODE = "summary"

# Standalone filler words to drop from a transcript. Only removed when they stand ALONE
# as a token (so "umbrella" or a column literally named "Um" is never touched). Kept
# deliberately short and unambiguous — we never strip a word that could carry meaning.
_FILLERS = {
    # English / Hinglish
    "um", "umm", "uh", "uhh", "uhm", "er", "erm", "hmm", "hmmm", "ah", "eh",
    # Hindi / Urdu conversational fillers (rough spoken equivalents of "um/like")
    "मतलब", "यानी", "वो", "अरे",
    "مطلب", "یعنی", "ارے",
}

_WS = re.compile(r"\s+")


def _is_letter(ch: str) -> bool:
    """True for a letter in ANY script (Latin, Devanagari, Arabic, …)."""
    return unicodedata.category(ch).startswith("L")


def normalize_transcript(text: str) -> str:
    """Clean spoken-form noise WITHOUT changing meaning.

    Drops standalone filler words and collapses whitespace. Everything else is left
    exactly as spoken — we do not add punctuation, correct spelling, or reorder words.
    """
    if not text:
        return ""
    tokens = _WS.sub(" ", text.strip()).split(" ")
    kept = [t for t in tokens if t and t.strip(".,!?।").lower() not in _FILLERS]
    # If stripping fillers removed everything (e.g. the whole utterance was "um uh"),
    # fall back to the original tokens so `assess_transcript` can report it honestly
    # rather than us silently manufacturing an empty string from real (if useless) audio.
    if not kept:
        return _WS.sub(" ", text.strip())
    return " ".join(kept)


def assess_transcript(text: str, confidence: float | None = None,
                      min_confidence: float = 0.5) -> tuple[bool, str | None]:
    """Decide whether a transcript is usable. Returns (ok, message).

    Graceful noisy-audio handling: on empty, low-confidence, or garbled (no real words)
    input we return (False, <a friendly ask-to-repeat>). We NEVER guess at what a noisy
    transcript meant. When ok, message is None.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return False, "I didn't catch that — could you say that again?"
    # Confidence comes from the STT layer (0..1). Low means the audio was noisy/unclear.
    if confidence is not None and confidence < min_confidence:
        return False, ("I didn't hear that clearly — there may be background noise. "
                       "Could you repeat it?")
    # Garbled: a transcript with no letters in any script is noise, not a command
    # (e.g. "…", "??", "12"-only). Numbers alone aren't an instruction either.
    if not any(_is_letter(ch) for ch in cleaned):
        return False, "I didn't quite get that — could you say the instruction again?"
    return True, None


def spoken_feedback(notes: list[str] | None, mode: str = DEFAULT_MODE) -> str | None:
    """Render the executor's real notes into speech at the requested verbosity.

    silent  -> None            (do the work, say nothing)
    step    -> narrate each step ("Step 1: … Step 2: …")
    summary -> one wrap-up line ("All done. …")

    Pure view over `notes` (which come from real execution) — no fabrication.
    """
    mode = (mode or DEFAULT_MODE).strip().lower()
    if mode not in FEEDBACK_MODES:
        mode = DEFAULT_MODE
    if mode == "silent":
        return None
    clean = [n.strip() for n in (notes or []) if n and n.strip()]
    if not clean:
        return "Done."
    if len(clean) == 1:
        # One step: step and summary read the same — just speak it.
        return clean[0]
    if mode == "step":
        return " ".join(f"Step {i}: {n}" for i, n in enumerate(clean, 1))
    # summary
    return "All done. " + " ".join(clean)
