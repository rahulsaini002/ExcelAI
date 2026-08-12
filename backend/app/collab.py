"""Collaborative workspace layer (Phase 3.8).

Lets several people — and the AI — work on ONE dataset at once, with three guarantees
the rest of the app didn't need when it was single-user:

  • optimistic concurrency — every change carries the data revision it was based on. If
    the data moved on since then, the change is REJECTED with a clear message instead of
    silently overwriting someone else's work (the classic "lost update" problem).

  • approval gates — when a workspace requires approval, every proposed change (from any
    member, including the AI) waits in a queue until a DIFFERENT member with approval
    rights accepts it. Nothing touches the data until then.

  • attribution — every applied change and every comment records who made it (a real
    user, or the AI), with timestamps, in an auditable log.

State is in-memory, like the rest of the app's sessions. All the logic lives here as
plain functions so it is unit-testable without HTTP; main.py exposes it over
/workspace/* endpoints. The actual data transforms reuse the trusted executor.
"""
from __future__ import annotations

import time
import uuid

from . import store
from .executor import MultiStepError, OperationError, execute_multi

# Roles and what each may do. "ai" can propose (and comment) but never approves — a
# human must sign off on the AI's work. Viewers may only comment.
_CAN_PROPOSE = {"owner", "editor", "ai"}
_CAN_APPROVE = {"owner", "editor"}   # humans with edit rights; NOT the AI
_CAN_COMMENT = {"owner", "editor", "viewer", "ai"}
_VALID_ROLES = {"owner", "editor", "viewer", "ai"}

AI_USER_ID = "ai"

# workspace_id -> workspace dict (loaded from / snapshotted to the DB when persistence is on)
_WORKSPACES: dict[str, dict] = store.register("workspaces", store.load_dict("workspaces"))

# Bound memory like the rest of the app.
_MAX_WORKSPACES = 200
_MAX_LOG = 500
_MAX_COMMENTS = 500


class CollabError(Exception):
    """A user-facing collaboration error. `status` maps to the HTTP code main.py uses:
    400 bad request, 403 forbidden (permissions), 404 not found, 409 conflict (a stale
    edit that would overwrite newer work)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _get(ws_id: str) -> dict:
    ws = _WORKSPACES.get(ws_id)
    if ws is None:
        raise CollabError("That workspace doesn't exist (or has expired).", status=404)
    return ws


def _member(ws: dict, user_id: str) -> dict:
    m = ws["members"].get(user_id)
    if m is None:
        raise CollabError("You're not a member of this workspace.", status=403)
    return m


def _conflict(base_revision: int, current: int) -> CollabError:
    """The shared 'your edit is stale' error, so propose- and approve-time checks read
    identically to the user."""
    return CollabError(
        f"This change was based on version {base_revision}, but the data is now at "
        f"version {current} — someone changed it first. Refresh and re-apply so you "
        "don't overwrite their work.",
        status=409,
    )


def _summarize_ops(operations: list[dict]) -> str:
    """A short fallback summary of a change when the caller didn't supply one."""
    actions = [str(op.get("action") or "?").replace("_", " ") for op in operations]
    return ", then ".join(actions) if actions else "a change"


def _apply_ops(state: dict, operations: list[dict]) -> tuple[dict, list[str]]:
    """Run operations on a data state and return (new_state, notes). Mirrors how main.py
    builds the next state. Raises CollabError(422) if a step is invalid — and because the
    new state is only assigned on success, a failure leaves the workspace UNCHANGED."""
    tables, primary, exts = state["tables"], state["primary"], state["exts"]
    try:
        result, result_name, notes, _render = execute_multi(tables, primary, operations)
    except MultiStepError as exc:
        raise CollabError(f"Step {exc.failed_step} couldn't be applied: {exc.reason}", status=422)
    except OperationError as exc:
        raise CollabError(str(exc), status=422)

    if isinstance(result, dict):  # a workbook (combine_sheets)
        new_state = {
            "tables": {**tables, **result},
            "primary": next(iter(result)),
            "exts": {**exts, **{k: "xlsx" for k in result}},
        }
    else:
        new_state = {
            "tables": {**tables, result_name: result},
            "primary": result_name,
            "exts": {**exts, result_name: exts.get(result_name, "xlsx")},
        }
    return new_state, notes


def _apply_change(
    ws: dict, operations: list[dict], base_revision: int, author: str,
    summary: str | None, approver: str | None,
) -> list[str]:
    """The single choke point that mutates workspace data. Enforces optimistic
    concurrency (base_revision must equal the current revision) and records attribution.
    Raises CollabError(409) on a stale base so newer work is never silently overwritten."""
    if base_revision != ws["revision"]:
        raise _conflict(base_revision, ws["revision"])
    new_state, notes = _apply_ops(ws["state"], operations)
    ws["state"] = new_state
    ws["revision"] += 1
    ws["log"].append({
        "revision": ws["revision"],
        "author": author,
        "approved_by": approver,
        "summary": summary or _summarize_ops(operations),
        "at": _now(),
    })
    if len(ws["log"]) > _MAX_LOG:
        del ws["log"][: len(ws["log"]) - _MAX_LOG]
    return notes


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def create_workspace(
    name: str, owner_id: str, owner_name: str, state: dict,
    require_approval: bool = True, session_id: str | None = None,
) -> dict:
    """Create a shared workspace seeded with `state` ({tables, primary, exts}). The
    creator is the owner; an "ai" member is added so the assistant can participate.
    `session_id` links the workspace back to the creator's session so applied changes
    can be synced into it (keeping the chat's /parse view consistent)."""
    if not owner_id:
        raise CollabError("A workspace needs an owner.", status=400)
    ws_id = _new_id("ws")
    ws = {
        "id": ws_id,
        "name": (name or "Shared workspace").strip(),
        "created_at": _now(),
        "created_by": owner_id,
        "session_id": session_id,
        "require_approval": bool(require_approval),
        "revision": 0,
        "state": state,
        "members": {
            owner_id: {"name": owner_name or owner_id, "role": "owner", "joined_at": _now()},
            AI_USER_ID: {"name": "Sumio AI", "role": "ai", "joined_at": _now()},
        },
        "comments": [],
        "pending": [],
        "log": [],
    }
    _WORKSPACES[ws_id] = ws
    while len(_WORKSPACES) > _MAX_WORKSPACES:
        _WORKSPACES.pop(next(iter(_WORKSPACES)))
    return ws


def join_workspace(ws_id: str, user_id: str, user_name: str, role: str = "editor") -> dict:
    """Add (or update) a member. Re-joining keeps your existing role unless changed by an
    owner via set_role. The owner role can't be claimed by joining."""
    ws = _get(ws_id)
    if not user_id:
        raise CollabError("A member needs an id.", status=400)
    if role not in _VALID_ROLES or role in ("owner", "ai"):
        role = "editor"  # only the creator is owner; "ai" is reserved
    existing = ws["members"].get(user_id)
    if existing:
        return ws  # already a member — idempotent join
    ws["members"][user_id] = {"name": user_name or user_id, "role": role, "joined_at": _now()}
    return ws


def set_role(ws_id: str, actor_id: str, target_id: str, role: str) -> dict:
    """Owner-only: change a member's role (e.g. promote to editor, demote to viewer)."""
    ws = _get(ws_id)
    actor = _member(ws, actor_id)
    if actor["role"] != "owner":
        raise CollabError("Only the workspace owner can change roles.", status=403)
    if target_id not in ws["members"]:
        raise CollabError("That person isn't a member of this workspace.", status=404)
    if target_id in (actor_id, AI_USER_ID):
        raise CollabError("You can't change that member's role.", status=400)
    if role not in ("editor", "viewer"):
        raise CollabError("Role must be 'editor' or 'viewer'.", status=400)
    ws["members"][target_id]["role"] = role
    return ws


def propose_change(
    ws_id: str, author_id: str, operations: list[dict],
    base_revision: int, summary: str | None = None,
) -> dict:
    """Propose a data change. With approval OFF it applies immediately (still concurrency-
    checked + attributed). With approval ON it is queued as pending until a different
    member with approval rights accepts it. Returns a status dict."""
    ws = _get(ws_id)
    member = _member(ws, author_id)
    if member["role"] not in _CAN_PROPOSE:
        raise CollabError("Viewers can comment but can't change the data.", status=403)
    if not isinstance(operations, list) or not operations:
        raise CollabError("A change needs at least one operation.", status=400)
    if not isinstance(base_revision, int):
        raise CollabError("A change must say which version it's based on.", status=400)
    if base_revision > ws["revision"] or base_revision < 0:
        raise CollabError(
            f"Version {base_revision} doesn't exist (current is {ws['revision']}).", status=400
        )
    # Revisions only move forward, so a base behind the current one can never apply
    # cleanly — reject it now rather than queueing a doomed change.
    if base_revision != ws["revision"]:
        raise _conflict(base_revision, ws["revision"])

    if ws["require_approval"]:
        change = {
            "id": _new_id("chg"),
            "author": author_id,
            "operations": operations,
            "summary": (summary or _summarize_ops(operations)),
            "base_revision": base_revision,
            "status": "pending",
            "created_at": _now(),
            "decided_by": None,
            "decided_at": None,
            "reason": None,
        }
        ws["pending"].append(change)
        return {"status": "pending", "change_id": change["id"], "revision": ws["revision"]}

    notes = _apply_change(ws, operations, base_revision, author_id, summary, approver=None)
    return {"status": "applied", "revision": ws["revision"], "notes": notes}


def _find_pending(ws: dict, change_id: str) -> dict:
    for c in ws["pending"]:
        if c["id"] == change_id:
            return c
    raise CollabError("That change isn't in the approval queue.", status=404)


def approve_change(ws_id: str, approver_id: str, change_id: str) -> dict:
    """Approve a pending change and apply it. The approver must have approval rights and
    must NOT be the proposer (so a change can't approve itself). Re-checks concurrency at
    apply time: if the data advanced since the change was proposed, it conflicts (409)."""
    ws = _get(ws_id)
    approver = _member(ws, approver_id)
    if approver["role"] not in _CAN_APPROVE:
        raise CollabError("You don't have permission to approve changes.", status=403)
    change = _find_pending(ws, change_id)
    if change["status"] != "pending":
        raise CollabError(f"That change was already {change['status']}.", status=400)
    if change["author"] == approver_id:
        raise CollabError(
            "You can't approve your own change — someone else must review it.", status=403
        )

    # May raise 409 (stale) or 422 (invalid op); the change stays pending so the author
    # can rebase and re-propose, and the data is untouched.
    notes = _apply_change(
        ws, change["operations"], change["base_revision"],
        author=change["author"], summary=change["summary"], approver=approver_id,
    )
    change["status"] = "approved"
    change["decided_by"] = approver_id
    change["decided_at"] = _now()
    return {"status": "applied", "revision": ws["revision"], "notes": notes, "change_id": change_id}


def reject_change(ws_id: str, approver_id: str, change_id: str, reason: str = "") -> dict:
    """Reject a pending change (it never touches the data). Requires approval rights."""
    ws = _get(ws_id)
    approver = _member(ws, approver_id)
    if approver["role"] not in _CAN_APPROVE:
        raise CollabError("You don't have permission to reject changes.", status=403)
    change = _find_pending(ws, change_id)
    if change["status"] != "pending":
        raise CollabError(f"That change was already {change['status']}.", status=400)
    change["status"] = "rejected"
    change["decided_by"] = approver_id
    change["decided_at"] = _now()
    change["reason"] = (reason or "").strip() or None
    return {"status": "rejected", "change_id": change_id}


def withdraw_change(ws_id: str, author_id: str, change_id: str) -> dict:
    """The PROPOSER (or an owner) cancels their own pending change before it's decided — it
    leaves the queue without ever touching the data. This gives the approval workflow a
    'never mind' path that doesn't require an approver to reject your own no-longer-wanted
    change. Withdrawn changes stay in the auditable decision trail (who withdrew, when)."""
    ws = _get(ws_id)
    _member(ws, author_id)  # must be a member of the workspace
    change = _find_pending(ws, change_id)
    if change["status"] != "pending":
        raise CollabError(f"That change was already {change['status']}.", status=400)
    is_owner = ws["members"].get(author_id, {}).get("role") == "owner"
    if change["author"] != author_id and not is_owner:
        raise CollabError(
            "Only the person who proposed a change (or an owner) can withdraw it.", status=403
        )
    change["status"] = "withdrawn"
    change["decided_by"] = author_id
    change["decided_at"] = _now()
    return {"status": "withdrawn", "change_id": change_id}


def add_comment(ws_id: str, author_id: str, text: str, target=None) -> dict:
    """Attach a comment (optionally to a cell/column/row via free-form `target`)."""
    ws = _get(ws_id)
    member = _member(ws, author_id)
    if member["role"] not in _CAN_COMMENT:
        raise CollabError("You can't comment on this workspace.", status=403)
    text = (text or "").strip()
    if not text:
        raise CollabError("A comment can't be empty.", status=400)
    comment = {
        "id": _new_id("cmt"),
        "author": author_id,
        "text": text,
        "target": target,
        "created_at": _now(),
        "resolved": False,
        "resolved_by": None,
    }
    ws["comments"].append(comment)
    if len(ws["comments"]) > _MAX_COMMENTS:
        del ws["comments"][: len(ws["comments"]) - _MAX_COMMENTS]
    return comment


def resolve_comment(ws_id: str, user_id: str, comment_id: str) -> dict:
    ws = _get(ws_id)
    _member(ws, user_id)
    for c in ws["comments"]:
        if c["id"] == comment_id:
            c["resolved"] = True
            c["resolved_by"] = user_id
            return c
    raise CollabError("That comment doesn't exist.", status=404)


def linked_session(ws_id: str) -> str | None:
    """The session id this workspace was created from (for syncing applied changes back)."""
    return _get(ws_id).get("session_id")


def current_state(ws_id: str) -> dict:
    """The workspace's live data state ({tables, primary, exts}) at the current revision."""
    return _get(ws_id)["state"]


def state_summary(ws_id: str, table_summarizer=None) -> dict:
    """A JSON-friendly snapshot for the UI: members, revision, comments, the approval
    queue, the change log, and a light table preview. `table_summarizer(df)` -> dict is
    injected by main.py (reusing /inspect's summarizer) to avoid a circular import."""
    ws = _get(ws_id)
    tables = []
    if table_summarizer is not None:
        for tname, df in ws["state"]["tables"].items():
            s = table_summarizer(df)
            tables.append({"name": tname, **s})

    return {
        "id": ws["id"],
        "name": ws["name"],
        "revision": ws["revision"],
        "require_approval": ws["require_approval"],
        "primary": ws["state"]["primary"],
        "created_by": ws["created_by"],
        "members": [
            {"id": uid, "name": m["name"], "role": m["role"], "joined_at": m["joined_at"]}
            for uid, m in ws["members"].items()
        ],
        "comments": list(ws["comments"]),
        "pending": [
            {
                "id": c["id"], "author": c["author"], "summary": c["summary"],
                "operations": c["operations"], "base_revision": c["base_revision"],
                "status": c["status"], "created_at": c["created_at"],
            }
            for c in ws["pending"] if c["status"] == "pending"
        ],
        # Decision audit (Phase 5.1): every change that LEFT the queue — approved, rejected,
        # or withdrawn — with who decided it and why. The `log` records what touched the
        # DATA (applied changes only); this records the approval DECISIONS, so a rejection
        # ("Bob's change declined by Alice: out of scope") is attributable, not invisible.
        "decisions": [
            {
                "id": c["id"], "author": c["author"], "summary": c["summary"],
                "status": c["status"], "decided_by": c.get("decided_by"),
                "decided_at": c.get("decided_at"), "reason": c.get("reason"),
            }
            for c in ws["pending"] if c["status"] != "pending"
        ],
        "log": list(ws["log"]),
        "tables": tables,
    }
