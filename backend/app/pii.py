"""PII / sensitive-data shield (Phase 3.9).

The AI (the Brain) only ever needs the SHAPE of the data — column names, types, and a
few example rows — to plan an operation. Those example rows, though, can carry real card
numbers, government IDs, emails and the like. This module detects that PII and masks it
*before* anything is sent to the model, so nothing sensitive leaves the trusted backend
in the clear.

Two layers:
  • Detection + display masking (irreversible): `detect`, `mask`, `redact_structure`,
    `redact_text`. Used on everything bound for the AI. A masked value like "****1111"
    can't be turned back into the original — that's the point.
  • Reversible masking (authorized only): `mask_frame` returns a masked DataFrame plus a
    private vault mapping each masked cell back to its original. `unmask_frame(..., authorized)`
    restores the originals ONLY when the caller is authorized; everyone else just keeps
    the masks. The vault never leaves the backend and is never shown to the AI.

Detection favours catching real PII over avoiding the odd false positive (over-masking is
safe; under-masking leaks). Credit cards are Luhn-checked to keep plain long numbers from
being mistaken for cards.
"""
from __future__ import annotations

import copy
import re
from collections import Counter

import pandas as pd

# Known sensitive types we can recognise and mask.
TYPES = {"credit_card", "ssn", "aadhaar", "pan", "email", "phone", "sensitive"}

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PAN = re.compile(r"[A-Za-z]{5}\d{4}[A-Za-z]")          # India PAN, e.g. ABCDE1234F
SSN = re.compile(r"\d{3}[- ]\d{2}[- ]\d{4}")           # US SSN, e.g. 123-45-6789
# A run of digits (with spaces/dashes) long enough to be a card or national ID.
_LONG_NUM = re.compile(r"\d[\d \-]{9,}\d")


def _luhn(digits: str) -> bool:
    """Standard Luhn checksum — real credit-card numbers pass it."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _blank(v) -> bool:
    if v is None:
        return True
    try:
        if isinstance(v, float) and pd.isna(v):
            return True
    except Exception:
        pass
    return isinstance(v, str) and v.strip() == ""


def detect(value) -> str | None:
    """Return the PII type of a single value, or None. Operates on the whole value."""
    if _blank(value):
        return None
    s = str(value).strip()
    if not s:
        return None
    if EMAIL.fullmatch(s):
        return "email"
    if PAN.fullmatch(s):
        return "pan"
    digits = re.sub(r"\D", "", s)
    if re.fullmatch(r"[\d \-]+", s) and 13 <= len(digits) <= 19 and _luhn(digits):
        return "credit_card"
    if re.fullmatch(r"[\d ]+", s) and len(digits) == 12:
        return "aadhaar"
    if SSN.fullmatch(s):
        return "ssn"
    # Phone: require a separator/“+” or 11+ digits so plain 10-digit IDs aren't swept up.
    if re.fullmatch(r"[\d\-\s()+]+", s) and 10 <= len(digits) <= 15 and (
        any(c in s for c in "+-() ") or len(digits) >= 11
    ):
        return "phone"
    return None


def _mask_keep_last4(s: str) -> str:
    """Replace every alphanumeric char except the last four with '*', keeping separators
    so the shape is still recognisable (e.g. '**** **** **** 1111')."""
    chars = list(s)
    alnum = [i for i, c in enumerate(chars) if c.isalnum()]
    keep = set(alnum[-4:])
    for i in alnum:
        if i not in keep:
            chars[i] = "*"
    return "".join(chars)


def mask(value, ptype: str | None = None) -> str:
    """Mask a value for display. Irreversible — the original can't be recovered from it."""
    s = str(value)
    ptype = ptype or detect(s)
    if ptype is None:
        return s
    if ptype == "email":
        local, at, domain = s.partition("@")
        if at:
            masked_local = (local[0] + "*" * (len(local) - 1)) if len(local) > 1 else "*"
            return f"{masked_local}@{domain}"
    return _mask_keep_last4(s)


def redact_text(text: str) -> str:
    """Mask any PII found INSIDE a free-text string (used on the conversation history sent
    to the AI). Leaves the surrounding text intact."""
    if not text:
        return text
    t = EMAIL.sub(lambda m: mask(m.group(0), "email"), text)
    t = PAN.sub(lambda m: mask(m.group(0), "pan"), t)
    t = SSN.sub(lambda m: mask(m.group(0), "ssn"), t)

    def _num(m: re.Match) -> str:
        s = m.group(0)
        digits = re.sub(r"\D", "", s)
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return mask(s, "credit_card")
        if len(digits) == 12:
            return mask(s, "aadhaar")
        return s

    return _LONG_NUM.sub(_num, t)


def contains_pii(text) -> bool:
    """True if `text` has any detectable PII (handy as a guard/assertion)."""
    s = str(text)
    return redact_text(s) != s or detect(s) is not None


# Column-name hints — token-based so "company" never matches "pan", etc.
def _name_hint(name: str) -> str | None:
    tokens = [t for t in re.split(r"[^a-z0-9]+", str(name).lower()) if t]
    joined = "".join(tokens)
    tokenset = set(tokens)
    for key, ptype in (
        ("creditcard", "credit_card"), ("cardnumber", "credit_card"), ("cardno", "credit_card"),
        ("socialsecurity", "ssn"), ("ssn", "ssn"),
        ("aadhaar", "aadhaar"), ("aadhar", "aadhaar"),
        ("passport", "sensitive"), ("accountnumber", "sensitive"), ("accountno", "sensitive"),
        ("iban", "sensitive"), ("cvv", "sensitive"), ("cvc", "sensitive"),
    ):
        if key in joined:
            return ptype
    if "pan" in tokenset:
        return "pan"
    if "card" in tokenset:
        return "credit_card"
    if {"password", "secret", "apikey"} & tokenset:
        return "sensitive"
    if {"phone", "mobile"} & tokenset:
        return "phone"
    if "email" in tokenset:
        return "email"
    return None


def scan_series(series: pd.Series, name: str = "") -> str | None:
    """Classify a column as a PII type, by VALUE evidence first (≥50% of non-blank values
    match one type) then by column-NAME hint. Returns the type or None."""
    nonblank = [str(v).strip() for v in series.tolist() if not _blank(v)]
    nonblank = [v for v in nonblank if v]
    if nonblank:
        matched = [t for t in (detect(v) for v in nonblank) if t]
        if matched and len(matched) >= max(1, int(0.5 * len(nonblank))):
            return Counter(matched).most_common(1)[0][0]
    return _name_hint(name)


def scan_frame(df: pd.DataFrame) -> dict[str, str]:
    """{column -> pii type} for every column that looks sensitive."""
    out: dict[str, str] = {}
    for col in df.columns:
        ptype = scan_series(df[col], str(col))
        if ptype:
            out[str(col)] = ptype
    return out


def scan_tables(tables: dict[str, pd.DataFrame]) -> dict[str, dict[str, str]]:
    """{table -> {column -> pii type}} across every table."""
    return {name: scan_frame(df) for name, df in tables.items()}


def redact_structure(structure: dict, scan_by_table: dict | None = None) -> tuple[dict, list[str]]:
    """Return a COPY of an AI-bound structure with PII masked in its sample rows, plus the
    list of "table::column" fields that were masked. Column names, types and row counts are
    untouched (the model still understands the shape). Columns flagged by `scan_by_table`
    are masked wholesale; every other sample value is still value-checked as a safety net."""
    out = copy.deepcopy(structure)
    masked: set[str] = set()
    for tname, tinfo in (out.get("tables") or {}).items():
        colscan = (scan_by_table or {}).get(tname, {})
        for row in tinfo.get("sample_rows") or []:
            for col, val in list(row.items()):
                if _blank(val):
                    continue
                ptype = colscan.get(col) or detect(val)
                if ptype:
                    row[col] = mask(val, ptype)
                    masked.add(f"{tname}::{col}")
    return out, sorted(masked)


# --------------------------------------------------------------------------- #
# Reversible masking — for OUTPUT, recoverable only by authorized users.
# --------------------------------------------------------------------------- #
_SEP = "\x00"  # internal vault-key separator (never appears in column names)


def mask_frame(
    df: pd.DataFrame, scan: dict[str, str] | None = None
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Return (masked_df, vault). Sensitive cells are display-masked in `masked_df`; the
    `vault` maps each masked cell back to its original. The vault stays on the backend and
    is the ONLY way to reverse the masking — see `unmask_frame`."""
    scan = scan if scan is not None else scan_frame(df)
    masked = df.copy()
    vault: dict[str, object] = {}
    for col, ptype in scan.items():
        if col not in masked.columns:
            continue
        new_vals = []
        for idx, val in masked[col].items():
            if _blank(val):
                new_vals.append(val)
                continue
            vault[f"{col}{_SEP}{idx}"] = val
            new_vals.append(mask(val, ptype))
        masked[col] = new_vals
    return masked, vault


def unmask_frame(masked_df: pd.DataFrame, vault: dict[str, object], authorized: bool) -> pd.DataFrame:
    """Restore originals from the vault — but ONLY for an authorized caller. An
    unauthorized caller gets the masked data back unchanged (the masks are irreversible
    to them)."""
    out = masked_df.copy()
    if not authorized:
        return out
    for key, original in vault.items():
        col, _, idx_str = key.partition(_SEP)
        if col not in out.columns:
            continue
        idx: object = idx_str
        if idx_str.lstrip("-").isdigit():
            idx = int(idx_str)
        try:
            out.at[idx, col] = original
        except Exception:
            pass
    return out
