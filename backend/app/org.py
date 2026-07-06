"""Organization/team operations, backed by the database. Enforces the RBAC rules from
rbac.py so no endpoint has to re-implement them.

Model choice (kept deliberately simple): a user belongs to at most ONE org. Creating a team
makes you its owner; you invite EXISTING Sumio accounts by email. Emailing signup invites to
people without accounts yet is a natural follow-up, not built here.

Every mutating function takes the ACTOR's membership and checks permission first, so calling
these directly (tests, other modules) is as safe as going through the HTTP endpoints.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import rbac
from .models import Organization, OrgInvite, OrgMembership, User

ASSIGNABLE_ROLES = ("viewer", "member", "admin")  # 'owner' is set only at creation


class OrgError(Exception):
    """Carries an HTTP status so the endpoint can translate it (403 forbidden, 404 not
    found, 409 conflict, 400 bad request)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ------------------------------------------------------------------- lookups ------------
def get_membership(db: Session, org_id: str, user_id: str) -> OrgMembership | None:
    return db.scalar(
        select(OrgMembership).where(
            OrgMembership.org_id == org_id, OrgMembership.user_id == user_id
        )
    )


def get_user_membership(db: Session, user_id: str) -> OrgMembership | None:
    """The user's single membership (this model allows at most one)."""
    return db.scalar(select(OrgMembership).where(OrgMembership.user_id == user_id))


def list_members(db: Session, org_id: str) -> list[dict]:
    """All members with their account details, owner first then by role power."""
    rows = db.scalars(select(OrgMembership).where(OrgMembership.org_id == org_id)).all()
    out = []
    for m in rows:
        u = db.get(User, m.user_id)
        out.append({
            "user_id": m.user_id,
            "email": u.email if u else None,
            "name": u.name if u else None,
            "role": m.role,
        })
    out.sort(key=lambda r: rbac.rank(r["role"]), reverse=True)
    return out


# ------------------------------------------------------------------- mutations ----------
def create_org(db: Session, name: str, owner_id: str) -> Organization:
    """Create a team; the caller becomes its owner. Fails if they're already on a team."""
    name = (name or "").strip()
    if not name:
        raise OrgError("A team needs a name.", status=400)
    if get_user_membership(db, owner_id) is not None:
        raise OrgError("You're already on a team.", status=409)
    org = Organization(name=name, created_by=owner_id)
    db.add(org)
    db.flush()  # assign org.id
    db.add(OrgMembership(org_id=org.id, user_id=owner_id, role="owner"))
    db.flush()
    return org


def _require_manage(actor: OrgMembership) -> None:
    if not rbac.can(actor.role, "org:manage_members"):
        raise OrgError("You don't have permission to manage this team.", status=403)


def _check_grant(actor: OrgMembership, role: str) -> None:
    """Shared guard for handing out a role (used by add_member AND create_invite): you must
    be able to manage members, and can only grant a role strictly below your own."""
    _require_manage(actor)
    if role not in ASSIGNABLE_ROLES:
        raise OrgError("Role must be admin, member, or viewer.", status=400)
    if rbac.rank(role) >= rbac.rank(actor.role):
        raise OrgError("You can't grant a role at or above your own.", status=403)


def add_member(db: Session, actor: OrgMembership, email: str, role: str) -> dict:
    """Add an EXISTING account onto the team. The actor must be able to manage members,
    and can only grant a role BELOW their own (so an admin can't mint another admin)."""
    _check_grant(actor, role)
    target = db.scalar(select(User).where(User.email == (email or "").strip().lower()))
    if target is None:
        raise OrgError("No Sumio account has that email address.", status=404)
    if get_user_membership(db, target.id) is not None:
        raise OrgError("That person is already on a team.", status=409)

    m = OrgMembership(org_id=actor.org_id, user_id=target.id, role=role)
    db.add(m)
    db.flush()
    return {"user_id": target.id, "email": target.email, "name": target.name, "role": role}


def set_member_role(db: Session, actor: OrgMembership, target_user_id: str, role: str) -> None:
    """Change a teammate's role. You can only modify someone strictly below you, and only to
    a role below your own — so the owner is untouchable and admins can't create admins."""
    _require_manage(actor)
    if target_user_id == actor.user_id:
        raise OrgError("You can't change your own role.", status=400)
    if role not in ASSIGNABLE_ROLES:
        raise OrgError("Role must be admin, member, or viewer.", status=400)
    target = get_membership(db, actor.org_id, target_user_id)
    if target is None:
        raise OrgError("That person isn't on this team.", status=404)
    if not rbac.outranks(actor.role, target.role):
        raise OrgError("You can't change that member's role.", status=403)
    if rbac.rank(role) >= rbac.rank(actor.role):
        raise OrgError("You can't grant a role at or above your own.", status=403)
    target.role = role
    db.flush()


def remove_member(db: Session, actor: OrgMembership, target_user_id: str) -> None:
    """Remove a teammate. You can only remove someone strictly below you (never the owner,
    never a peer), and not yourself."""
    _require_manage(actor)
    if target_user_id == actor.user_id:
        raise OrgError("You can't remove yourself from the team.", status=400)
    target = get_membership(db, actor.org_id, target_user_id)
    if target is None:
        raise OrgError("That person isn't on this team.", status=404)
    if not rbac.outranks(actor.role, target.role):
        raise OrgError("You can't remove that member.", status=403)
    db.delete(target)
    db.flush()


# ------------------------------------------------------------- pending invitations ------
def create_invite(db: Session, actor: OrgMembership, email: str, role: str) -> OrgInvite:
    """Invite an email that has NO account yet. Same permission rules as add_member. The
    invite is applied automatically when that email signs up (see apply_pending_invite)."""
    _check_grant(actor, role)
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email:
        raise OrgError("Enter a valid email address.", status=400)
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise OrgError("That person already has a Sumio account — add them directly.", status=409)
    # De-dupe: a re-invite (or role change) replaces any earlier pending invite for the email.
    for old in db.scalars(select(OrgInvite).where(OrgInvite.email == email, OrgInvite.accepted.is_(False))).all():
        db.delete(old)
    invite = OrgInvite(org_id=actor.org_id, email=email, role=role, invited_by=actor.user_id)
    db.add(invite)
    db.flush()
    return invite


def invite_or_add(db: Session, actor: OrgMembership, email: str, role: str) -> dict:
    """Add an EXISTING account immediately, or create a PENDING invite for a new email.
    One entry point for the UI's single 'invite by email' box. Returns
    {"kind": "member", "member": {...}} or {"kind": "invite", "invite": {...}}."""
    email_n = (email or "").strip().lower()
    if db.scalar(select(User).where(User.email == email_n)) is not None:
        return {"kind": "member", "member": add_member(db, actor, email_n, role)}
    invite = create_invite(db, actor, email_n, role)
    return {"kind": "invite", "invite": {"id": invite.id, "email": invite.email, "role": invite.role}}


def list_invites(db: Session, org_id: str) -> list[dict]:
    """Still-pending invitations for a team."""
    rows = db.scalars(
        select(OrgInvite).where(OrgInvite.org_id == org_id, OrgInvite.accepted.is_(False))
    ).all()
    return [{"id": i.id, "email": i.email, "role": i.role} for i in rows]


def revoke_invite(db: Session, actor: OrgMembership, invite_id: str) -> None:
    """Cancel a pending invitation (managers only, and only for their own team)."""
    _require_manage(actor)
    inv = db.get(OrgInvite, invite_id)
    if inv is None or inv.org_id != actor.org_id or inv.accepted:
        raise OrgError("That invitation doesn't exist.", status=404)
    db.delete(inv)
    db.flush()


def apply_pending_invite(db: Session, user: User) -> bool:
    """Called right after signup: if this email was invited to a team, join them to it (with
    the invited role) and mark the invite accepted. Returns True if an invite was applied.
    A no-op if they're somehow already on a team or there's no invite."""
    if get_user_membership(db, user.id) is not None:
        return False
    email = (user.email or "").strip().lower()
    inv = db.scalar(
        select(OrgInvite)
        .where(OrgInvite.email == email, OrgInvite.accepted.is_(False))
        .order_by(OrgInvite.created_at.desc())
    )
    if inv is None:
        return False
    db.add(OrgMembership(org_id=inv.org_id, user_id=user.id, role=inv.role))
    inv.accepted = True
    db.flush()
    return True
