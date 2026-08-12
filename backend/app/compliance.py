"""Compliance profiles (Phase 5.5).

The base PII shield (pii.py) already masks obvious personal data — cards, SSN, PAN, Aadhaar,
email, phone — before anything reaches the AI. Compliance PROFILES layer regime-specific
knowledge on top: each regime treats extra, domain-specific columns as sensitive (a HIPAA
'MRN' or 'diagnosis'; an IRDAI 'policy number' or 'nominee') so they're shielded too when
that regime is switched on. Toggling a profile only ever WIDENS what gets masked — it never
reveals anything — so turning one on is always safe.

Pure functions over the existing pii scan; no model, no request. `sensitive_columns` is what
main.py feeds to pii.redact_structure so the profile's extra columns are masked before the
Brain, and `report` gives a plain compliance posture for the UI.
"""
from __future__ import annotations

import re

import pandas as pd

from . import pii

# Each profile adds column-name hints (normalized substring → pii mask type). Types map to
# pii.mask behaviour ("sensitive" masks keeping the last 4; "aadhaar"/"pan" use their shapes).
PROFILES: dict[str, dict] = {
    "GDPR": {
        "label": "EU personal data (GDPR)",
        "hints": {
            "name": "sensitive", "firstname": "sensitive", "lastname": "sensitive",
            "fullname": "sensitive", "address": "sensitive", "dob": "sensitive",
            "dateofbirth": "sensitive", "ip": "sensitive", "ipaddress": "sensitive",
            "nationalid": "sensitive",
        },
    },
    "HIPAA": {
        "label": "US protected health information (HIPAA)",
        "hints": {
            "mrn": "sensitive", "medicalrecord": "sensitive", "patient": "sensitive",
            "diagnosis": "sensitive", "health": "sensitive", "insuranceid": "sensitive",
        },
    },
    "SOC2": {
        "label": "security controls (SOC 2)",
        "hints": {
            "password": "sensitive", "secret": "sensitive", "apikey": "sensitive",
            "token": "sensitive", "privatekey": "sensitive",
        },
    },
    "IRDAI": {
        "label": "India insurance regulation (IRDAI)",
        "hints": {
            "policyno": "sensitive", "policynumber": "sensitive", "nominee": "sensitive",
            "proposalno": "sensitive", "aadhaar": "aadhaar", "pan": "pan",
        },
    },
}


def parse_profiles(value) -> list[str]:
    """'gdpr,hipaa' (or a list) → ['GDPR','HIPAA']; case-insensitive, unknown names dropped,
    order-preserving and de-duplicated."""
    if not value:
        return []
    tokens = value if isinstance(value, (list, tuple)) else str(value).replace(";", ",").split(",")
    out: list[str] = []
    for tok in tokens:
        key = str(tok).strip().upper()
        if key in PROFILES and key not in out:
            out.append(key)
    return out


def extra_hints(profiles: list[str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for p in profiles:
        merged.update(PROFILES.get(p, {}).get("hints", {}))
    return merged


def _hint_type(colname: str, hints: dict[str, str]) -> str | None:
    """Token-aware match (mirrors pii._name_hint) so a short hint never matches inside an
    unrelated word — e.g. 'pan' must not fire on 'Company', 'ip' must not fire on 'Zip'. A
    hint matches when it's a whole token, or (only if it's ≥5 chars, so it's specific enough)
    a substring spanning joined tokens like 'policyno' in 'Policy_No'."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", str(colname).lower()) if t]
    joined = "".join(tokens)
    tokenset = set(tokens)
    for token, typ in hints.items():
        if token in tokenset or (len(token) >= 5 and token in joined):
            return typ
    return None


def sensitive_columns(tables: dict[str, pd.DataFrame], profiles: list[str] | None = None) -> dict[str, dict[str, str]]:
    """{table: {column: pii_type}} = the base PII scan PLUS the active profiles' extra name
    hints. Fed to pii.redact_structure so these columns are masked before the AI."""
    profiles = profiles or []
    base = pii.scan_tables(tables)
    out: dict[str, dict[str, str]] = {t: dict(cols) for t, cols in base.items()}
    hints = extra_hints(profiles)
    if hints:
        for t, df in tables.items():
            for col in df.columns:
                cn = str(col)
                if cn in out.get(t, {}):
                    continue  # base scan already flagged it
                typ = _hint_type(cn, hints)
                if typ:
                    out.setdefault(t, {})[cn] = typ
    return out


def report(tables: dict[str, pd.DataFrame], profiles) -> dict:
    """A plain compliance posture: which fields are sensitive under the active profiles, and
    the fact that all of them are masked before the AI by the same shield."""
    profiles = parse_profiles(profiles) if isinstance(profiles, str) else parse_profiles(profiles or [])
    base = pii.scan_tables(tables)
    scan = sensitive_columns(tables, profiles)
    findings = []
    for t, cols in scan.items():
        for col, typ in cols.items():
            findings.append({
                "table": t, "column": col, "type": typ,
                "source": "detected" if col in base.get(t, {}) else "compliance_hint",
            })
    return {
        "profiles": [{"id": p, "label": PROFILES[p]["label"]} for p in profiles],
        "sensitive_fields": findings,
        "sensitive_count": len(findings),
        # Every sensitive field found is masked before the model — the core guarantee.
        "masked_before_ai": True,
    }


def available() -> list[dict]:
    return [{"id": p, "label": v["label"]} for p, v in PROFILES.items()]
