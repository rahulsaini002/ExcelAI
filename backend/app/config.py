"""Runtime configuration, loaded once from environment / .env."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# The Gemini model used to turn an instruction into an operation plan.
# Parsing is an easy task, so we default to a fast, low-cost tier. Swap this
# in one place (here, or via the SUMIO_MODEL env var) to change models.
MODEL = os.getenv("SUMIO_MODEL", "gemini-2.5-flash")

# google-genai also reads GEMINI_API_KEY / GOOGLE_API_KEY from the environment,
# but we read it explicitly so we can give a clear error if it's missing.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

# Lookup formula style written into .xlsx downloads:
#   "index_match" — =IFERROR(INDEX(...,MATCH(...)),"Not found"); works in ALL Excel
#                   versions and Google Sheets (default, safest).
#   "xlookup"     — =XLOOKUP(...); needs Excel 2021/365 or Google Sheets.
LOOKUP_STYLE = os.getenv("SUMIO_LOOKUP_STYLE", "index_match").strip().lower()

# Resource limits, so a single user can't exhaust server memory.
#   MAX_UPLOAD_MB     — reject uploads whose combined size exceeds this. It's a clean
#                       "too large" message up front rather than a mid-processing crash.
#                       Raise it for bigger files via SUMIO_MAX_UPLOAD_MB (e.g. 1000).
#   MAX_SESSIONS      — cap how many sessions we keep in memory (oldest evicted).
#   MAX_STATES        — cap the undo/redo stack kept per session.
MAX_UPLOAD_MB = int(os.getenv("SUMIO_MAX_UPLOAD_MB", "250"))
MAX_SESSIONS = int(os.getenv("SUMIO_MAX_SESSIONS", "200"))
MAX_STATES = int(os.getenv("SUMIO_MAX_STATES", "30"))

# Generated result files are written here so downloads (and "continue on the result")
# survive a backend restart. Kept under MAX_RESULTS_MB and deleted after RESULTS_TTL_HOURS.
import pathlib  # noqa: E402

RESULTS_DIR = os.getenv(
    "SUMIO_RESULTS_DIR", str(pathlib.Path(__file__).resolve().parent.parent / ".sumio_results")
)
RESULTS_TTL_HOURS = int(os.getenv("SUMIO_RESULTS_TTL_HOURS", "168"))  # 7 days
MAX_RESULTS_MB = int(os.getenv("SUMIO_MAX_RESULTS_MB", "600"))

# Origins allowed to call the API (the Next.js frontend in dev).
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("SUMIO_CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]
