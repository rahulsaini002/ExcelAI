"""ENGINE PHASE 5.5 — security & compliance + PII shield (verify & BUILD).

The PII shield (pii.py) and RBAC (rbac.py) pre-existed (test_pii 49/49, test_security 26/26):
sensitive values are masked BEFORE the model sees them, and role permissions are enforced.
This phase re-verifies that and BUILDS the two missing DoD pieces:

  compliance TOGGLES   GDPR / HIPAA / SOC2 / IRDAI profiles WIDEN the shield with regime-
                       specific fields (HIPAA 'MRN'/'diagnosis', IRDAI 'policy no'/'nominee')
                       so they're masked too. Toggling only ever masks MORE, never less.
                       Token-aware so a short hint never misfires ('pan' ≠ 'Company').
  AUDIT trail          an append-only log of security events (what was shielded/scanned, when,
                       under which regime) — never the sensitive values themselves.

Wiring: /process takes compliance_mode and widens the shield + records a pii_shielded audit
event; /compliance/scan reports posture; /audit surfaces the trail. Base behaviour (no
compliance_mode) is unchanged. No llm.py change → no schema/serving/quota risk; no battery
rows (endpoint/mechanism).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_5_5.py
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

_fd, _db = tempfile.mkstemp(suffix="-p55.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import audit, compliance, pii  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


# Email = base PII; MRN = HIPAA-only; Policy_No = IRDAI-only; Amount = not sensitive.
TABLES = {"t": pd.DataFrame({
    "Email": ["a@x.com", "b@y.com"],
    "MRN": ["MR001", "MR002"],
    "Policy_No": ["POL9", "POL8"],
    "Amount": [100, 200],
})}


print("ENGINE PHASE 5.5 — security & compliance + PII shield (verify & build)\n")

# ===================== VERIFY: mask before the Brain (base shield) =====================
scan = pii.scan_tables(TABLES)
check("base shield detects Email as PII", scan["t"].get("Email") == "email", str(scan))
check("base shield does NOT catch MRN / Policy_No (regime-specific)", "MRN" not in scan["t"] and "Policy_No" not in scan["t"], str(scan))
struct = {"tables": {"t": {"sample_rows": [{"Email": "a@x.com", "MRN": "MR001", "Amount": 100}]}}}
red, masked = pii.redact_structure(struct, scan)
check("mask-before-Brain: Email is masked in the AI-bound sample", red["tables"]["t"]["sample_rows"][0]["Email"] != "a@x.com", str(red))
check("mask-before-Brain: a non-sensitive value is untouched", red["tables"]["t"]["sample_rows"][0]["Amount"] == 100, str(red))

# ===================== BUILD: compliance profiles WIDEN the shield =====================
check("parse_profiles is case-insensitive + drops unknowns", compliance.parse_profiles("gdpr, hipaa, bogus") == ["GDPR", "HIPAA"], str(compliance.parse_profiles("gdpr, hipaa, bogus")))
check("parse_profiles handles a list", compliance.parse_profiles(["IRDAI"]) == ["IRDAI"], "")

hipaa = compliance.sensitive_columns(TABLES, ["HIPAA"])
check("HIPAA widens the scan to include MRN (base missed it)", hipaa["t"].get("MRN") == "sensitive", str(hipaa))
check("HIPAA still keeps the base Email finding", hipaa["t"].get("Email") == "email", str(hipaa))
check("HIPAA does NOT flag the IRDAI-only Policy_No", "Policy_No" not in hipaa["t"], str(hipaa))
irdai = compliance.sensitive_columns(TABLES, ["IRDAI"])
check("IRDAI widens the scan to include Policy_No", irdai["t"].get("Policy_No") == "sensitive", str(irdai))
# only ever widens: base findings are a subset of every profile's findings
check("a profile only ADDS to the base scan (never removes)", set(scan["t"]) <= set(compliance.sensitive_columns(TABLES, ["GDPR", "HIPAA", "IRDAI"])["t"]), "")
# token-aware: no false positives
NOFALSE = {"t": pd.DataFrame({"Company": ["Acme"], "Zip": ["560001"], "Ship_Date": ["2026-01-01"]})}
check("token-aware hints don't misfire ('pan'∉Company, 'ip'∉Zip)", compliance.sensitive_columns(NOFALSE, ["IRDAI", "GDPR"])["t"] == {}, str(compliance.sensitive_columns(NOFALSE, ["IRDAI", "GDPR"])))
check("no profile → base scan only", compliance.sensitive_columns(TABLES, []) == {t: dict(cols) for t, cols in scan.items()}, "")

# report
rep = compliance.report(TABLES, "HIPAA,IRDAI")
fields = {(f["column"], f["source"]) for f in rep["sensitive_fields"]}
check("report marks base vs compliance-hint sources", ("Email", "detected") in fields and ("MRN", "compliance_hint") in fields, str(fields))
check("report affirms everything is masked before the AI", rep["masked_before_ai"] is True and rep["sensitive_count"] >= 3, str(rep))

# ===================== BUILD: audit log =====================
audit.clear()
audit.record("pii_shielded", actor="team1", detail="2 fields", meta={"columns": ["Email", "MRN"]})
audit.record("compliance_scan", actor="team1", detail="scan")
evs = audit.events()
check("audit records events, most-recent-first", len(evs) == 2 and evs[0]["action"] == "compliance_scan", str([e["action"] for e in evs]))
check("audit can filter by action", len(audit.events(action="pii_shielded")) == 1, "")
check("audit event carries structured meta, not raw values", audit.events(action="pii_shielded")[0]["meta"]["columns"] == ["Email", "MRN"], "")

# ===================== END TO END: /process widened shield + audit =====================
audit.clear()
CSV = b"Email,MRN,Amount\na@x.com,MR001,100\nb@y.com,MR002,200\n"
_real = m.llm.parse_instruction
m.llm.parse_instruction = lambda i, s, h="": {"operations": [{"action": "sort", "columns": ["Amount"], "orders": ["desc"]}], "title": "x"}
try:
    # without compliance: MRN is NOT shielded
    r0 = c.post("/process", data={"instruction": "sort by amount", "session_id": "s0"},
                files=[("files", ("d.csv", CSV, "text/csv"))]).json()
    check("without compliance: Email shielded, MRN NOT", "Email" in (r0.get("shielded_columns") or []) and "MRN" not in (r0.get("shielded_columns") or []), str(r0.get("shielded_columns")))
    # with HIPAA: MRN is ALSO shielded
    r1 = c.post("/process", data={"instruction": "sort by amount", "session_id": "s1", "compliance_mode": "HIPAA"},
                files=[("files", ("d.csv", CSV, "text/csv"))]).json()
    check("with HIPAA: MRN is now shielded before the AI", "MRN" in (r1.get("shielded_columns") or []) and "Email" in (r1.get("shielded_columns") or []), str(r1.get("shielded_columns")))
finally:
    m.llm.parse_instruction = _real

# the shield events reached the audit log; the HIPAA one records the regime
shield_events = audit.events(action="pii_shielded")
check("shielding is recorded in the audit trail", len(shield_events) >= 2, str(len(shield_events)))
check("the audit event records the active compliance regime", any(e["meta"].get("compliance") == ["HIPAA"] for e in shield_events), str([e["meta"].get("compliance") for e in shield_events]))

# ===================== endpoints =====================
prof = c.get("/compliance/profiles").json()
check("/compliance/profiles lists the regimes", {p["id"] for p in prof["profiles"]} == {"GDPR", "HIPAA", "SOC2", "IRDAI"}, str(prof))
c.post("/inspect", data={"session_id": "sc"}, files=[("files", ("d.csv", CSV, "text/csv"))])
sc = c.post("/compliance/scan", data={"session_id": "sc", "compliance_mode": "HIPAA"}).json()
check("/compliance/scan reports MRN under HIPAA", any(f["column"] == "MRN" for f in sc["sensitive_fields"]), str(sc)[:200])
al = c.get("/audit", params={"action": "compliance_scan"}).json()
check("/audit surfaces the compliance_scan event", al.get("status") == "ok" and len(al["events"]) >= 1, str(al)[:160])

m._SESSIONS.clear()
audit.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
