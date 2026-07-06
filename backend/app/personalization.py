"""Continuous learning & personalization (Phase 3.12).

Remembers, per team, the things that make a workspace feel like *theirs*:

  • definitions  — what the team means by a term, e.g. ARR = {MRR} * 12. Injected into the
    parsing context so the assistant resolves the term consistently, and (when a formula is
    given) expanded deterministically even by the offline fallback.
  • preferences  — formatting defaults (currency symbol, date format, decimals, bold headers)
    that are filled into the plan so outputs look the way the team expects, every time.
  • templates    — saved operation sequences the team reuses.

Everything is viewable, editable, and deletable through the CRUD functions (and the
/memory endpoints). State is kept in memory and persisted to a small JSON file so it
survives restarts — real "memory", not just per-session.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_MEMORY: dict[str, dict] = {}

# Persisted so learning survives restarts. Tests set _PERSIST = False to stay off disk.
_MEMORY_PATH = Path(__file__).resolve().parent.parent / "data" / "memory.json"
_PERSIST = True


def _now() -> float:
    return time.time()


def _load() -> None:
    global _MEMORY
    try:
        _MEMORY = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        _MEMORY = {}


def _persist() -> None:
    if not _PERSIST:
        return
    try:
        _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MEMORY_PATH.write_text(json.dumps(_MEMORY, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # persistence is best-effort; never fail a request over it


def _team(team_id: str) -> dict:
    return _MEMORY.setdefault(
        (team_id or "default"),
        {"definitions": {}, "preferences": {}, "templates": {}},
    )


# --------------------------------------------------------------------------- #
# Definitions
# --------------------------------------------------------------------------- #
def set_definition(team_id: str, term: str, definition: str, formula: str | None = None) -> dict:
    term = (term or "").strip()
    if not term:
        raise ValueError("A definition needs a term.")
    t = _team(team_id)
    existing = t["definitions"].get(term, {})
    t["definitions"][term] = {
        "term": term,
        "definition": (definition or "").strip(),
        "formula": (formula or "").strip() or None,
        "created_at": existing.get("created_at", _now()),
        "updated_at": _now(),
    }
    _persist()
    return t["definitions"][term]


def delete_definition(team_id: str, term: str) -> bool:
    t = _team(team_id)
    removed = t["definitions"].pop((term or "").strip(), None) is not None
    if removed:
        _persist()
    return removed


def definitions(team_id: str) -> dict:
    return dict(_team(team_id)["definitions"])


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #
_PREF_KEYS = ("currency_symbol", "date_format", "decimals", "bold_header")


def set_preferences(team_id: str, **prefs) -> dict:
    t = _team(team_id)
    for k in _PREF_KEYS:
        if k in prefs and prefs[k] is not None:
            t["preferences"][k] = prefs[k]
    _persist()
    return dict(t["preferences"])


def clear_preference(team_id: str, key: str) -> None:
    t = _team(team_id)
    if t["preferences"].pop(key, None) is not None:
        _persist()


def preferences(team_id: str) -> dict:
    return dict(_team(team_id)["preferences"])


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #
def save_template(team_id: str, name: str, operations: list, prompt: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("A template needs a name.")
    if not isinstance(operations, list) or not operations:
        raise ValueError("A template needs at least one operation.")
    t = _team(team_id)
    t["templates"][name] = {
        "name": name, "operations": operations, "prompt": (prompt or "").strip() or None,
        "updated_at": _now(),
    }
    _persist()
    return t["templates"][name]


def delete_template(team_id: str, name: str) -> bool:
    t = _team(team_id)
    removed = t["templates"].pop((name or "").strip(), None) is not None
    if removed:
        _persist()
    return removed


def templates(team_id: str) -> dict:
    return dict(_team(team_id)["templates"])


# --------------------------------------------------------------------------- #
# Read-all (for the view/edit UI)
# --------------------------------------------------------------------------- #
def get_memory(team_id: str) -> dict:
    t = _team(team_id)
    return {
        "team_id": team_id or "default",
        "definitions": dict(t["definitions"]),
        "preferences": dict(t["preferences"]),
        "templates": dict(t["templates"]),
    }


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
def context(team_id: str) -> str:
    """A glossary + preferences text block to inject into the parsing prompt, so learned
    definitions are applied consistently. Empty when nothing is remembered."""
    t = _team(team_id)
    lines: list[str] = []
    if t["definitions"]:
        lines.append("Team glossary — when the user uses these terms, apply these meanings:")
        for d in t["definitions"].values():
            formula = f" (compute as {d['formula']})" if d.get("formula") else ""
            lines.append(f"- {d['term']}: {d['definition']}{formula}")
    p = t["preferences"]
    if p:
        bits = []
        if p.get("currency_symbol"):
            bits.append(f"currency symbol '{p['currency_symbol']}'")
        if p.get("date_format"):
            bits.append(f"date format {p['date_format']}")
        if p.get("decimals") is not None:
            bits.append(f"{p['decimals']} decimal places")
        if p.get("bold_header"):
            bits.append("bold header rows")
        if bits:
            lines.append("Team formatting preferences: " + ", ".join(bits) + ".")
    return "\n".join(lines)


def apply_preferences(operations: list[dict], prefs: dict) -> list[dict]:
    """Fill the team's formatting defaults into format_cells steps that left them blank, so
    outputs are formatted consistently without the user re-specifying every time. Returns a
    new list; the input isn't mutated."""
    if not prefs or not operations:
        return operations
    out: list[dict] = []
    for op in operations:
        if op.get("action") != "format_cells":
            out.append(op)
            continue
        op = dict(op)
        if op.get("number_format") == "currency" and not op.get("currency_symbol") and prefs.get("currency_symbol"):
            op["currency_symbol"] = prefs["currency_symbol"]
        if op.get("number_format") == "date" and not op.get("date_format") and prefs.get("date_format"):
            op["date_format"] = prefs["date_format"]
        if op.get("decimals") is None and prefs.get("decimals") is not None:
            op["decimals"] = prefs["decimals"]
        if not op.get("bold_header") and prefs.get("bold_header"):
            op["bold_header"] = True
        out.append(op)
    return out


def expand_definitions(instruction: str, team_id: str) -> list[dict] | None:
    """Deterministic, offline expansion: 'add/create/calculate <term>' where <term> is a
    defined formula → the team's add_formula_column. Used by the fallback parser so learned
    definitions apply even when the model is unavailable. Returns ops or None."""
    return _expand(instruction, definitions(team_id))


def _expand(instruction: str, defs: dict) -> list[dict] | None:
    import re
    low = (instruction or "").lower()
    if not defs or not re.search(r"\b(add|create|make|new|calculate|compute)\b", low):
        return None
    ops: list[dict] = []
    for term, d in defs.items():
        if d.get("formula") and re.search(rf"\b{re.escape(term.lower())}\b", low):
            ops.append({"action": "add_formula_column", "name": term, "formula": d["formula"]})
    return ops or None


_load()
