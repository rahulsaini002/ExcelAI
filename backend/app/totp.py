"""Time-based one-time passwords (TOTP) — the second factor, implemented from the RFC
with only the Python standard library (no third-party dependency to install or audit).

This is real 2FA, not a placeholder: the codes here interoperate with Google
Authenticator, Authy, 1Password, etc., because they follow the same standards those apps
implement:
  - RFC 4226 (HOTP): HMAC-SHA1 of a counter, then "dynamic truncation" to N digits.
  - RFC 6238 (TOTP): the counter is the number of 30-second steps since the Unix epoch.

Everything here is pure (no database, no bcrypt, no clock except the `now` you pass in),
so it's exhaustively testable — the suite pins `_hotp` to the official RFC 6238 test
vectors. Recovery-code *hashing* lives in auth.py (it owns bcrypt); this module only mints
the plaintext codes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6          # what authenticator apps show
STEP_SECONDS = 30   # the TOTP time step
_SECRET_BYTES = 20  # 160-bit shared secret (RFC-recommended for SHA1)


# --------------------------------------------------------------------------- secrets ----
def generate_secret() -> str:
    """A fresh base32 shared secret (no '=' padding, the form authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    """Base32-decode a secret, tolerating missing padding and lower-case (apps vary)."""
    s = secret.strip().replace(" ", "").upper()
    s += "=" * (-len(s) % 8)  # re-pad to a multiple of 8 for the stdlib decoder
    return base64.b32decode(s, casefold=True)


# ----------------------------------------------------------------------------- codes ----
def _hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    """RFC 4226 HOTP: HMAC-SHA1(key, counter) → dynamic-truncation → zero-padded digits.
    Operates on raw key bytes so it can be checked directly against the RFC test vectors."""
    msg = struct.pack(">Q", counter)  # 8-byte big-endian counter
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F                      # low 4 bits pick the slice start
    chunk = digest[offset:offset + 4]
    code_int = struct.unpack(">I", chunk)[0] & 0x7FFFFFFF  # mask off the sign bit
    return str(code_int % (10 ** digits)).zfill(digits)


def code_at(secret: str, at: float | None = None, digits: int = DIGITS) -> str:
    """The current TOTP code for a base32 secret at time `at` (defaults to now)."""
    at = time.time() if at is None else at
    counter = int(at // STEP_SECONDS)
    return _hotp(_decode_secret(secret), counter, digits)


def verify(secret: str, code: str, at: float | None = None, window: int = 1) -> bool:
    """True if `code` is valid for `secret` around time `at`. Checks ±`window` steps so a
    code entered a little late (or a slightly skewed clock) still works. Constant-time
    comparison so a wrong code can't be timed. Never raises on junk input — returns False."""
    if not code:
        return False
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == DIGITS):
        return False
    try:
        key = _decode_secret(secret)
    except Exception:
        return False
    at = time.time() if at is None else at
    base = int(at // STEP_SECONDS)
    for drift in range(-window, window + 1):
        if hmac.compare_digest(_hotp(key, base + drift), code):
            return True
    return False


# --------------------------------------------------------------- provisioning (QR) ------
def provisioning_uri(secret: str, account: str, issuer: str = "Sumio") -> str:
    """The otpauth:// URI an authenticator app reads (usually via a QR code) to add the
    account. The client renders the QR; we just build the standard URI."""
    # Standard label is "Issuer:account" with a LITERAL separating colon; encode the two
    # parts but not the separator (what Google Authenticator et al. expect).
    label = f"{quote(issuer)}:{quote(account)}"
    params = (
        f"secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )
    return f"otpauth://totp/{label}?{params}"


# ------------------------------------------------------------------ recovery codes ------
def generate_recovery_codes(n: int = 10) -> list[str]:
    """One-time backup codes for when the authenticator is lost. Human-friendly
    (lower-case, grouped) and high-entropy. Returned once; only their HASHES are stored."""
    codes = []
    for _ in range(n):
        raw = secrets.token_hex(5)  # 10 hex chars = 40 bits each
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    """Canonical form for comparison: lower-case, spaces/dashes stripped."""
    return (code or "").strip().lower().replace("-", "").replace(" ", "")
