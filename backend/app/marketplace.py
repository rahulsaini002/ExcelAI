"""Plugin marketplace & custom agents (Phase 5.11).

The ecosystem layer: users publish reusable "plugins" (a.k.a. custom agents) — named pipelines
of operations others can browse, install, and run on their own data.

THE SANDBOX is the safety guarantee: a plugin is NOT code. It is a list of operations drawn
only from the app's known, trusted set (SAFE_ACTIONS), validated at publish time. Running a
plugin is exactly running that Operation Plan through the same trusted executor everything
else uses — so a shared plugin can never do anything a normal instruction couldn't, and
"arbitrary code from the marketplace" is structurally impossible.

Pure in-memory registry (like the rest of the app's runtime state); main.py exposes it over
/marketplace/* and reuses the trusted execution path to run a plugin.
"""
from __future__ import annotations

import time
import uuid

# The allow-list a plugin's steps must draw from — the app's known, trusted operations. An
# unknown action (typo, or an attempt to smuggle something in) is rejected at publish time.
SAFE_ACTIONS = frozenset({
    "sort", "filter", "limit", "remove_duplicates", "fill_missing", "drop_missing",
    "flag_missing", "drop_invalid", "add_formula_column", "aggregate", "lookup", "merge",
    "combine_sheets", "rename_columns", "drop_columns", "select_columns", "find_replace",
    "format_cells", "pivot", "unpivot", "transpose", "pivot_summary", "fill_series",
    "fill_by_example", "merge_columns", "conditional_format", "data_validation",
    "excel_table", "layout_format", "name_range", "set_cells", "forecast", "what_if",
    "detect_anomalies", "statistics", "chart", "dashboard", "goal_seek", "explain_changes",
})

_PLUGINS: dict[str, dict] = {}       # id -> plugin
_INSTALLS: dict[str, set] = {}       # team_id -> {plugin_id, …}
_MAX = 500


class MarketplaceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def validate_steps(steps) -> list:
    """The sandbox check: every step must be a known operation. Returns the steps or raises."""
    if not isinstance(steps, list) or not steps:
        raise MarketplaceError("A plugin needs at least one operation.")
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or not s.get("action"):
            raise MarketplaceError(f"Step {i} isn't a valid operation.")
        if s["action"] not in SAFE_ACTIONS:
            raise MarketplaceError(
                f"Step {i} uses '{s['action']}', which isn't an allowed plugin operation "
                "(plugins are sandboxed to the app's known operations).", status=403)
    return steps


def publish(name: str, steps, description: str = "", author: str = "", kind: str = "plugin") -> dict:
    name = (name or "").strip()
    if not name:
        raise MarketplaceError("A plugin needs a name.")
    validate_steps(steps)
    pid = uuid.uuid4().hex[:12]
    _PLUGINS[pid] = {
        "id": pid, "name": name,
        "description": (description or "").strip(),
        "author": (author or "").strip() or "anonymous",
        "kind": kind if kind in ("plugin", "agent") else "plugin",
        "steps": steps, "installs": 0, "created_at": time.time(),
    }
    while len(_PLUGINS) > _MAX:
        _PLUGINS.pop(next(iter(_PLUGINS)))
    return _public(_PLUGINS[pid])


def get(plugin_id: str) -> dict:
    p = _PLUGINS.get(plugin_id)
    if not p:
        raise MarketplaceError("No such plugin.", status=404)
    return p


def _public(p: dict) -> dict:
    return {
        "id": p["id"], "name": p["name"], "description": p["description"],
        "author": p["author"], "kind": p["kind"], "installs": p["installs"],
        "created_at": p["created_at"], "step_count": len(p["steps"]),
    }


def listing() -> list[dict]:
    return [_public(p) for p in _PLUGINS.values()]


def unpublish(plugin_id: str) -> bool:
    removed = _PLUGINS.pop(plugin_id, None) is not None
    if removed:
        for s in _INSTALLS.values():
            s.discard(plugin_id)
    return removed


def install(team_id: str, plugin_id: str) -> list[str]:
    get(plugin_id)  # must exist
    s = _INSTALLS.setdefault(team_id or "default", set())
    if plugin_id not in s:
        s.add(plugin_id)
        _PLUGINS[plugin_id]["installs"] += 1
    return sorted(s)


def uninstall(team_id: str, plugin_id: str) -> bool:
    s = _INSTALLS.get(team_id or "default", set())
    if plugin_id in s:
        s.discard(plugin_id)
        if plugin_id in _PLUGINS:
            _PLUGINS[plugin_id]["installs"] = max(0, _PLUGINS[plugin_id]["installs"] - 1)
        return True
    return False


def installed(team_id: str) -> list[dict]:
    return [_public(_PLUGINS[pid]) for pid in sorted(_INSTALLS.get(team_id or "default", set())) if pid in _PLUGINS]


def steps_of(plugin_id: str) -> list:
    return list(get(plugin_id)["steps"])


def count() -> int:
    return len(_PLUGINS)
