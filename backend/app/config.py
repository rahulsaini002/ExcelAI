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

# --- Pre-deploy hardening (all OFF by default so local dev + tests are unaffected) ----
#   API_TOKEN    — when set, every request must send it (header `X-API-Key: <token>` or
#                  `Authorization: Bearer <token>`). /health is always exempt. Genuinely
#                  secret for server callers (the Sheets add-on); for the browser app it's
#                  a coarse gate (the SPA sends it via NEXT_PUBLIC_API_TOKEN).
#   RATE_LIMIT   — max requests per IP per RATE_WINDOW seconds (0 = unlimited). Protects
#                  the public AI endpoints from abuse / runaway cost.
API_TOKEN = os.getenv("SUMIO_API_TOKEN", "").strip()
RATE_LIMIT = int(os.getenv("SUMIO_RATE_LIMIT", "0"))
RATE_WINDOW = float(os.getenv("SUMIO_RATE_WINDOW", "60"))

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

# In dev we allow ANY origin so the app works from localhost, 127.0.0.1, or a LAN IP.
# In production set SUMIO_CORS_ALLOW_ALL=0 so only the origins in SUMIO_CORS_ORIGINS
# (your deployed frontend) can call the API.
CORS_ALLOW_ALL = os.getenv("SUMIO_CORS_ALLOW_ALL", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# --- Database (persistence) -----------------------------------------------------------
# One code path, two backends, chosen by the DATABASE_URL env var:
#   - unset (local dev)  -> a SQLite file next to the backend (zero setup, no install).
#   - set on Render      -> Render's managed Postgres (survives restarts/redeploys).
# Render hands out URLs starting "postgres://", but SQLAlchemy needs "postgresql://";
# db.py normalizes that so you can paste Render's value as-is.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///" + str(pathlib.Path(__file__).resolve().parent.parent / "sumio.db"),
)

# Whether the small runtime stores (connections, schedules, workspaces, syncs, webhook
# logs) are SAVED to the database so they survive a restart. Defaults ON whenever a
# DATABASE_URL is explicitly set (i.e. production/Render with Postgres) and OFF otherwise
# (local dev + the test suite use plain in-memory, exactly as before). Override with
# SUMIO_PERSIST=1/0.
_persist_default = "1" if os.getenv("DATABASE_URL") else "0"
PERSIST = os.getenv("SUMIO_PERSIST", _persist_default).strip().lower() in {"1", "true", "yes", "on"}

# --- Auth (login tokens) --------------------------------------------------------------
# JWT_SECRET signs the login tokens. Anyone who knows it can forge a login, so in
# PRODUCTION you MUST set SUMIO_JWT_SECRET to a long random string (e.g. the output of
# `python -c "import secrets; print(secrets.token_hex(32))"`). The dev default below is
# intentionally obvious and only fine for local work — auth.py warns if it's still in use.
JWT_SECRET = os.getenv("SUMIO_JWT_SECRET", "dev-only-insecure-change-me")
JWT_ALGORITHM = "HS256"
# How long a login stays valid before the user must sign in again (default 7 days).
JWT_EXPIRE_HOURS = int(os.getenv("SUMIO_JWT_EXPIRE_HOURS", "168"))

# Google sign-in: the OAuth 2.0 Client ID from Google Cloud Console
# (APIs & Services -> Credentials -> OAuth client -> Web application). When empty,
# the /auth/google endpoint is disabled with a clear message. The SAME value goes to
# the frontend as NEXT_PUBLIC_GOOGLE_CLIENT_ID so the Google button can request a token.
GOOGLE_CLIENT_ID = os.getenv("SUMIO_GOOGLE_CLIENT_ID", "").strip()

# When ON, EVERY request (except /health and the /auth/* login endpoints) must carry a
# valid login token — i.e. the whole app requires sign-in. OFF by default so local dev +
# the test suite work anonymously; turn it on in production for a login-walled product.
# The token is read from the `Authorization: Bearer <jwt>` header.
REQUIRE_AUTH = os.getenv("SUMIO_REQUIRE_AUTH", "0").strip().lower() in {"1", "true", "yes", "on"}

# Where the web app lives — used to build links we email to users (password reset).
# Defaults to the first CORS origin, which is already the frontend's URL.
FRONTEND_URL = os.getenv(
    "SUMIO_FRONTEND_URL", CORS_ORIGINS[0] if CORS_ORIGINS else "http://localhost:3000"
).rstrip("/")
