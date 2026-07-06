"""Phase 3.9 — PII / sensitive-data shield tests.

PRD criteria proven here:
  PII-a  Known PII patterns are detected and masked (card / ID / SSN / PAN / email / phone).
  PII-b  Masking is reversible ONLY for authorized users (vault round-trip).
  PII-c  Nothing sensitive reaches the AI in the clear (structure + history are redacted
         before llm.parse_instruction is ever called).
  Plus: column scanning (value evidence + name hints, no over-masking), redact_text,
        and end-to-end shielding via /process and /parse.

Run from backend:  .venv\\Scripts\\python.exe test_pii.py
"""
from __future__ import annotations

import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd
from fastapi.testclient import TestClient

from app import main, pii

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


# Standard test values (the cards are Luhn-valid public test numbers).
CARD = "4111111111111111"
CARD2 = "5555555555554444"
CARD_SPACED = "4111 1111 1111 1111"
SSN = "123-45-6789"
AADHAAR = "1234 5678 9012"
PAN = "ABCDE1234F"
EMAIL = "rahul.saini@gmail.com"
PHONE = "+91 98765 43210"

print("PHASE 3.9 — PII / SENSITIVE-DATA SHIELD\n")

# =========================================================================
# PII-a  Detection + masking of known patterns
# =========================================================================
print("PII-a  Detection + masking")

check("PII-a detect credit card (Luhn)", pii.detect(CARD) == "credit_card", pii.detect(CARD))
check("PII-a detect spaced card", pii.detect(CARD_SPACED) == "credit_card", pii.detect(CARD_SPACED))
check("PII-a detect SSN", pii.detect(SSN) == "ssn", pii.detect(SSN))
check("PII-a detect Aadhaar (12 digits)", pii.detect(AADHAAR) == "aadhaar", pii.detect(AADHAAR))
check("PII-a detect PAN", pii.detect(PAN) == "pan", pii.detect(PAN))
check("PII-a detect email", pii.detect(EMAIL) == "email", pii.detect(EMAIL))
check("PII-a detect phone", pii.detect(PHONE) == "phone", pii.detect(PHONE))

# Non-PII must NOT be flagged (no over-masking of ordinary values).
check("PII-a plain number not a card", pii.detect("12345") is None, pii.detect("12345"))
check("PII-a random 16-digit failing Luhn isn't a card", pii.detect("1234567812345678") is None, pii.detect("1234567812345678"))
check("PII-a word isn't PII", pii.detect("Acme Corp") is None, pii.detect("Acme Corp"))
check("PII-a 10-digit quantity isn't a phone", pii.detect("1000000000") is None, pii.detect("1000000000"))

# Masking keeps only the last 4 (or the email domain) and is irreversible by shape.
check("PII-a card masked to last 4", pii.mask(CARD) == "************1111", pii.mask(CARD))
check("PII-a spaced card masked", pii.mask(CARD_SPACED) == "**** **** **** 1111", pii.mask(CARD_SPACED))
check("PII-a SSN masked", pii.mask(SSN) == "***-**-6789", pii.mask(SSN))
check("PII-a PAN masked", pii.mask(PAN).endswith("234F") and pii.mask(PAN).startswith("*"), pii.mask(PAN))
check("PII-a email masks the local part, keeps domain", pii.mask(EMAIL) == "r**********@gmail.com", pii.mask(EMAIL))
check("PII-a masked card has no original digits run", CARD not in pii.mask(CARD), pii.mask(CARD))

# =========================================================================
# Column scanning — value evidence + name hints, without over-masking
# =========================================================================
print("\nPII-scan  Column detection")

df = pd.DataFrame({
    "Name": ["Alice", "Bob", "Carol"],
    "Card": [CARD, CARD2, "4111111111111111"],
    "Email": [EMAIL, "bob@acme.io", "carol@x.org"],
    "Company": ["Pantheon", "Span Co", "Panacea"],   # contains 'pan' as a substring — must NOT flag
    "Account Number": ["1", "2", "3"],               # name hint -> sensitive even if values are plain
    "Qty": [10, 20, 30],
})
scan = pii.scan_frame(df)
check("PII-scan flags Card column", scan.get("Card") == "credit_card", str(scan))
check("PII-scan flags Email column", scan.get("Email") == "email", str(scan))
check("PII-scan name-hints Account Number", scan.get("Account Number") == "sensitive", str(scan))
check("PII-scan does NOT flag Name", "Name" not in scan, str(scan))
check("PII-scan does NOT flag Company ('pan' substring)", "Company" not in scan, str(scan))
check("PII-scan does NOT flag Qty", "Qty" not in scan, str(scan))

# =========================================================================
# PII-b  Reversible ONLY for authorized users
# =========================================================================
print("\nPII-b  Reversible only for authorized users")

masked_df, vault = pii.mask_frame(df)
check("PII-b masked frame hides the card", masked_df.loc[0, "Card"] == "************1111", str(masked_df.loc[0, "Card"]))
check("PII-b vault retained originals", any(v == CARD for v in vault.values()), str(list(vault.values()))[:120])

# Authorized -> originals restored.
restored = pii.unmask_frame(masked_df, vault, authorized=True)
check("PII-b authorized user recovers the original card", restored.loc[0, "Card"] == CARD, str(restored.loc[0, "Card"]))
check("PII-b authorized user recovers the original email", restored.loc[0, "Email"] == EMAIL, str(restored.loc[0, "Email"]))

# Unauthorized -> stays masked (cannot reverse).
still_masked = pii.unmask_frame(masked_df, vault, authorized=False)
check("PII-b unauthorized user stays masked", still_masked.loc[0, "Card"] == "************1111", str(still_masked.loc[0, "Card"]))
check("PII-b unauthorized can't see any original card", not (still_masked["Card"].astype(str) == CARD).any(), str(list(still_masked["Card"])))

# =========================================================================
# redact_structure / redact_text
# =========================================================================
print("\nPII-redact  Structure + free text")

from app.reader import summarize_tables  # noqa: E402

structure = summarize_tables({"People": df}, "People")
safe, shielded = pii.redact_structure(structure, pii.scan_tables({"People": df}))
blob = json.dumps(safe)
check("PII-redact no raw card in structure", CARD not in blob and CARD2 not in blob, blob[:160])
check("PII-redact no raw email in structure", EMAIL not in blob, blob[:160])
check("PII-redact masked marker present", "************1111" in blob, blob[:160])
check("PII-redact reports shielded columns", "People::Card" in shielded and "People::Email" in shielded, str(shielded))
# Shape preserved so the AI still understands the data.
cols = [c["name"] for c in safe["tables"]["People"]["columns"]]
check("PII-redact keeps column names", "Card" in cols and "Name" in cols, str(cols))
check("PII-redact keeps row count", safe["tables"]["People"]["row_count"] == 3, str(safe["tables"]["People"]["row_count"]))
check("PII-redact leaves non-PII values intact", any(r.get("Name") == "Alice" for r in safe["tables"]["People"]["sample_rows"]), "")

txt = f"earlier the user pasted card {CARD_SPACED} and email {EMAIL}"
red = pii.redact_text(txt)
check("PII-redact_text hides raw card", CARD_SPACED not in red and CARD not in red, red)
check("PII-redact_text hides raw email", EMAIL not in red, red)
check("PII-redact_text keeps surrounding words", "earlier the user pasted" in red, red)

# =========================================================================
# PII-c  Nothing sensitive reaches the AI in the clear (end-to-end)
# =========================================================================
print("\nPII-c  Nothing sensitive sent to the AI")

client = TestClient(main.app)
CSV = (
    "Name,Card,Email,Qty\n"
    f"Alice,{CARD},{EMAIL},10\n"
    f"Bob,{CARD2},bob@acme.io,20\n"
    "Carol,4111111111111111,carol@x.org,30\n"
).encode()

_orig = main.llm.parse_instruction
captured: dict = {}


def _capture(instruction, structure, history):
    captured["instruction"] = instruction
    captured["structure"] = json.dumps(structure)
    captured["history"] = history
    return {
        "operations": [{"action": "remove_duplicates"}],
        "title": "Dedupe",
        "translation": "Remove duplicate rows",
        "confidence": 95,
    }


# ---- /process ----
try:
    main.llm.parse_instruction = _capture
    r = client.post(
        "/process",
        data={
            "instruction": "clean it up",
            "session_id": "pii_proc",
            "rewind": "-1",
            "history": f"user mentioned card {CARD_SPACED}",
        },
        files=[("files", ("p.csv", CSV, "text/csv"))],
    )
    body = r.json()
    check("PII-c /process ok", r.status_code == 200 and body.get("status") == "ok", str(body)[:160])
    check("PII-c AI structure had NO raw card", CARD not in captured["structure"] and CARD2 not in captured["structure"], captured["structure"][:160])
    check("PII-c AI structure had NO raw email", EMAIL not in captured["structure"], captured["structure"][:160])
    check("PII-c AI structure WAS masked", "************1111" in captured["structure"], captured["structure"][:160])
    check("PII-c AI history was redacted", CARD_SPACED not in captured["history"] and CARD not in captured["history"], captured["history"])
    check("PII-c response lists shielded columns", set(["Card", "Email"]).issubset(set(body.get("shielded_columns", []))), str(body.get("shielded_columns")))
    check("PII-c response notes the shield", any("Shielded" in n for n in body.get("notes", [])), str(body.get("notes"))[:160])
finally:
    main.llm.parse_instruction = _orig
    main._SESSIONS.clear()

# ---- /parse ----
captured.clear()
try:
    main.llm.parse_instruction = _capture
    client.post("/inspect", data={"session_id": "pii_parse"}, files=[("files", ("p.csv", CSV, "text/csv"))])
    r = client.post("/parse", data={"instruction": "remove duplicates", "session_id": "pii_parse", "history": ""})
    body = r.json()
    check("PII-c /parse returns a plan", body.get("status") == "plan", str(body)[:160])
    check("PII-c /parse AI structure had no raw card", CARD not in captured["structure"], captured["structure"][:160])
    check("PII-c /parse reports shielded columns", set(["Card", "Email"]).issubset(set(body.get("shielded_columns", []))), str(body.get("shielded_columns")))
finally:
    main.llm.parse_instruction = _orig
    main._SESSIONS.clear()

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
