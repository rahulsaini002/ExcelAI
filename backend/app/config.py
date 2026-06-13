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

# Origins allowed to call the API (the Next.js frontend in dev).
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("SUMIO_CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]
