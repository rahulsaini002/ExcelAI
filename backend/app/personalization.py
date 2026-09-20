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

from . import store

# REGISTERED WITH `store` so team memory lives in the DATABASE, not just a JSON file.
# The file alone was never durable in production: this host has no persistent disk, so a
# restart (which happens whenever it sleeps) wiped every custom term a team had taught it.
#
# ⚠️ These dicts are mutated IN PLACE everywhere below — never rebound — because `store`
# holds a reference to this exact object. Reassigning (`_MEMORY = {...}`) would silently
# detach it from the registry and stop persisting, which is exactly the kind of quiet
# failure this change exists to remove.
_MEMORY: dict[str, dict] = store.register("team_memory", {})

# Shared / org-wide AI memory (Phase 5.2): a glossary keyed by an ORG scope that MANY teams
# inherit, so terminology ("our ARR", "Runway") is consistent across the whole organization
# — not just within one team's private memory. A team's own definition of a term OVERRIDES
# the shared one (local wins), so teams can still specialize. Kept in its own store + file so
# it never disturbs the existing per-team memory.json format.
_SHARED: dict[str, dict] = store.register("shared_memory", {})

# Persisted so learning survives restarts. Tests set _PERSIST = False to stay off disk.
_MEMORY_PATH = Path(__file__).resolve().parent.parent / "data" / "memory.json"
_SHARED_PATH = Path(__file__).resolve().parent.parent / "data" / "shared_memory.json"
_PERSIST = True


def _now() -> float:
    return time.time()


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _restore(target: dict, namespace: str, path: Path) -> None:
    """Fill `target` IN PLACE from the database, falling back to the JSON file.

    Database first because it's the only durable copy in production. The file is still
    read when the database has nothing, which covers two real cases: local development
    with no DATABASE_URL, and a deployment that already had a memory.json — its contents
    migrate into the database on the first save rather than being silently discarded.
    """
    data = store.load_dict(namespace) or _read_json(path)
    target.clear()
    target.update(data)


def _load() -> None:
    _restore(_MEMORY, "team_memory", _MEMORY_PATH)


def _load_shared() -> None:
    _restore(_SHARED, "shared_memory", _SHARED_PATH)


def _write(target: dict, namespace: str, path: Path) -> None:
    if not _PERSIST:
        return
    # The database is the durable copy (no-op when persistence is off, e.g. local dev).
    try:
        store.save(namespace)
    except Exception:
        pass  # best-effort; never fail a request over persistence
    # The file is still written so local development keeps working without a database.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _persist() -> None:
    _write(_MEMORY, "team_memory", _MEMORY_PATH)


def _persist_shared() -> None:
    _write(_SHARED, "shared_memory", _SHARED_PATH)


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
# Shared / org-wide definitions (Phase 5.2)
# --------------------------------------------------------------------------- #
def _shared(scope: str) -> dict:
    return _SHARED.setdefault((scope or "").strip(), {"definitions": {}})


def set_shared_definition(scope: str, term: str, definition: str, formula: str | None = None) -> dict:
    """Add/update an ORG-shared term every team in the org inherits."""
    scope = (scope or "").strip()
    if not scope:
        raise ValueError("A shared definition needs an organization scope.")
    term = (term or "").strip()
    if not term:
        raise ValueError("A definition needs a term.")
    s = _shared(scope)
    existing = s["definitions"].get(term, {})
    s["definitions"][term] = {
        "term": term,
        "definition": (definition or "").strip(),
        "formula": (formula or "").strip() or None,
        "created_at": existing.get("created_at", _now()),
        "updated_at": _now(),
    }
    _persist_shared()
    return s["definitions"][term]


def delete_shared_definition(scope: str, term: str) -> bool:
    s = _shared(scope)
    removed = s["definitions"].pop((term or "").strip(), None) is not None
    if removed:
        _persist_shared()
    return removed


def shared_definitions(scope: str) -> dict:
    if not (scope or "").strip():
        return {}
    return dict(_shared(scope)["definitions"])


def get_shared_memory(scope: str) -> dict:
    return {"scope": (scope or "").strip(), "definitions": shared_definitions(scope)}


def effective_definitions(team_id: str, scope: str | None = None) -> dict:
    """Org-shared definitions (base) merged with the team's OWN (team overrides on a clash).
    This is what the offline fallback expands, so org terms apply even when the model is
    down — exactly like team terms do."""
    merged = dict(shared_definitions(scope)) if scope else {}
    merged.update(definitions(team_id))  # local team definition wins over the shared one
    return merged


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


def clear_preference(team_id: str, key: str) -> bool:
    """Delete ONE remembered preference (e.g. drop the team's currency default). Returns
    True if something was removed — completes view/edit/delete for preferences, matching
    definitions and templates."""
    t = _team(team_id)
    removed = t["preferences"].pop((key or "").strip(), None) is not None
    if removed:
        _persist()
    return removed


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
def context(team_id: str, scope: str | None = None) -> str:
    """A glossary + preferences text block to inject into the parsing prompt, so learned
    definitions are applied consistently. Empty when nothing is remembered.

    When `scope` (an org) is given, an ORG glossary (Phase 5.2) is prepended so shared
    terminology applies across teams; a team's own definition of the same term overrides the
    shared one, so only the team's version is shown for a clash. With no scope, the output is
    byte-identical to the team-only behaviour (backward compatible)."""
    t = _team(team_id)
    lines: list[str] = []
    shared = shared_definitions(scope) if scope else {}
    if shared:
        team_terms = set(t["definitions"])
        org_lines = [
            f"- {d['term']}: {d['definition']}" + (f" (compute as {d['formula']})" if d.get("formula") else "")
            for term, d in shared.items() if term not in team_terms  # team overrides shared
        ]
        if org_lines:
            lines.append("Organization glossary — shared terms everyone should apply:")
            lines.extend(org_lines)
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


def expand_definitions(instruction: str, team_id: str, scope: str | None = None) -> list[dict] | None:
    """Deterministic, offline expansion: 'add/create/calculate <term>' where <term> is a
    defined formula → an add_formula_column. Used by the fallback parser so learned
    definitions apply even when the model is unavailable. Includes ORG-shared terms (Phase
    5.2) when `scope` is given. Returns ops or None."""
    return _expand(instruction, effective_definitions(team_id, scope))


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
_load_shared()
