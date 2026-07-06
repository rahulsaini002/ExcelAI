"""Email + password authentication: hashing, login tokens, and the /auth endpoints.

The security idea, in plain terms:
  - We NEVER store the real password. We store a bcrypt *hash* — a one-way scramble.
    To check a login we re-scramble what they typed and compare hashes.
  - On success we hand back a signed JWT *token*. The browser sends it on later
    requests (header `Authorization: Bearer <token>`) to prove who it is, so the
    password is typed once, not every time.

This module is split in two:
  - pure helpers (hash_password, verify_password, create/decode token, user lookups)
    — no web framework, so they're easy to unit-test.
  - an APIRouter with POST /auth/signup, POST /auth/login, GET /auth/me, which main.py
    mounts onto the app.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config, totp
from .db import get_db
from .models import User

# bcrypt only hashes the first 72 BYTES of a password; longer inputs raise in bcrypt 4.x.
# We cap the policy well under that and also truncate defensively before hashing.
_BCRYPT_MAX_BYTES = 72
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128


# ----------------------------------------------------------------- password hashing ----
def hash_password(password: str) -> str:
    """Scramble a password into a storable bcrypt hash (includes its own random salt)."""
    raw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    """True if `password` matches the stored hash. False for Google-only users (no hash)
    or any malformed hash — never raises, so a bad value can't crash login."""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8")[:_BCRYPT_MAX_BYTES], password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------- login tokens ------
def create_access_token(user_id: str, token_version: int = 0) -> str:
    """Make a signed token that says 'this is user_id' and expires after JWT_EXPIRE_HOURS.
    `token_version` is stamped in so "sign out everywhere" can revoke older tokens."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,                                            # who
        "tv": token_version,                                       # session epoch (revocation)
        "iat": now,                                                # issued-at
        "exp": now + timedelta(hours=config.JWT_EXPIRE_HOURS),     # expires
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Verify a token's signature + expiry and return its payload. Raises jwt.PyJWTError
    (caught by the caller) if it's forged, tampered, or expired."""
    return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])


# --------------------------------------------------------------------- user helpers ----
def normalize_email(email: str) -> str:
    """Emails are case-insensitive; store + compare them lowercased and trimmed so
    'Alice@X.com ' and 'alice@x.com' are the same account."""
    return email.strip().lower()


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == normalize_email(email)))


def get_user_by_id(db: Session, user_id: str) -> User | None:
    return db.get(User, user_id)


def create_user(db: Session, email: str, password: str, name: str | None = None) -> User:
    """Create an email+password account. Caller must have validated the inputs and
    checked the email isn't taken."""
    user = User(
        email=normalize_email(email),
        password_hash=hash_password(password),
        name=(name.strip() if name else None),
    )
    db.add(user)
    db.flush()  # assigns/loads defaults (id) without ending the transaction
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    """Return the user if email+password are correct and the account is active, else None."""
    user = get_user_by_email(db, email)
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


# --------------------------------------------------------------------- password reset --
RESET_TOKEN_MINUTES = 30


def create_reset_token(user_id: str) -> str:
    """A short-lived, single-purpose token embedded in the emailed reset link. The
    'kind' claim stops a reset token being replayed as a login token (and vice versa —
    login tokens have no kind, so they can't reset passwords)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "kind": "pwreset",
        "iat": now,
        "exp": now + timedelta(minutes=RESET_TOKEN_MINUTES),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def send_reset_email(recipient: str, link: str) -> None:
    """Email the reset link (module-level so tests can stub it). Reuses the SMTP
    transport from distribution, which raises TransportError when SMTP isn't set up."""
    from . import distribution

    body = (
        "Someone asked to reset the password for your Sumio account.\n\n"
        f"Set a new password here (link expires in {RESET_TOKEN_MINUTES} minutes):\n"
        f"{link}\n\n"
        "If this wasn't you, you can ignore this email — nothing has changed."
    )
    distribution.email_transport(recipient, "Reset your Sumio password", body, b"", "")


def email_configured() -> bool:
    return bool(os.environ.get("SUMIO_SMTP_HOST"))


# ------------------------------------------------------------ two-factor auth (TOTP) ---
# The 2FA login "challenge": after the password step passes, a user with 2FA on gets this
# short-lived token instead of a login token. They then post it back WITH a code to finish.
# It carries kind='2fa', so current_user_optional refuses it as a bearer token.
CHALLENGE_MINUTES = 5


def create_2fa_challenge(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "kind": "2fa",
        "iat": now,
        "exp": now + timedelta(minutes=CHALLENGE_MINUTES),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def _hash_recovery_code(code: str) -> str:
    """Recovery codes are hashed with the same bcrypt we use for passwords — normalized
    first so formatting (dashes, case) never affects the match."""
    return hash_password(totp.normalize_recovery_code(code))


def set_recovery_codes(user: User, plaintext_codes: list[str]) -> None:
    """Store the HASHES of a fresh batch of recovery codes on the user (never plaintext)."""
    user.totp_recovery_codes = json.dumps([_hash_recovery_code(c) for c in plaintext_codes])


def consume_recovery_code(user: User, code: str) -> bool:
    """If `code` matches one of the user's unused recovery codes, spend it (remove it) and
    return True. Each code works exactly once."""
    try:
        stored: list[str] = json.loads(user.totp_recovery_codes or "[]")
    except (ValueError, TypeError):
        return False
    norm = totp.normalize_recovery_code(code)
    for h in stored:
        if verify_password(norm, h):
            stored.remove(h)
            user.totp_recovery_codes = json.dumps(stored)
            return True
    return False


def recovery_codes_remaining(user: User) -> int:
    try:
        return len(json.loads(user.totp_recovery_codes or "[]"))
    except (ValueError, TypeError):
        return 0


def verify_second_factor(user: User, code: str, db: Session) -> bool:
    """True if `code` is a valid current TOTP code OR an unused recovery code for this user.
    Recovery-code use mutates the user (consumes the code); the caller commits."""
    if user.totp_secret and totp.verify(user.totp_secret, code):
        return True
    return consume_recovery_code(user, code)


# --------------------------------------------------------------------- Google sign-in --
def verify_google_id_token(token: str) -> dict:
    """Verify a Google ID token came from Google and was issued for OUR app, then return
    its claims (sub, email, email_verified, name). Raises ValueError if it's invalid,
    expired, or for the wrong audience. Isolated here so tests can stub it without network.

    Imports the Google libs lazily so the rest of auth works even if they're absent."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    return google_id_token.verify_oauth2_token(
        token, google_requests.Request(), config.GOOGLE_CLIENT_ID
    )


def get_or_create_sso_user(db: Session, email: str, name: str | None = None) -> User:
    """Find or create the account for an SSO (OIDC) identity, linked by the IdP-verified
    email — so an existing email/Google account and an SSO login for the same address are ONE
    account. New SSO accounts are passwordless (like Google-only), authenticated by the IdP."""
    user = get_user_by_email(db, email)
    if user is not None:
        if not user.name and name:
            user.name = name.strip()
            db.flush()
        return user
    user = User(email=normalize_email(email), name=(name.strip() if name else None))
    db.add(user)
    db.flush()
    return user


def get_user_by_google_sub(db: Session, sub: str) -> User | None:
    return db.scalar(select(User).where(User.google_sub == sub))


def upsert_google_user(db: Session, sub: str, email: str, name: str | None) -> User:
    """Find or create the account for a verified Google identity, linking on email so a
    password account and a Google sign-in for the same address are ONE account:
      1. already linked to this Google id  -> log in.
      2. an email account with that address -> link Google to it (sets google_sub).
      3. nobody yet                          -> create a Google-only account (no password).
    """
    user = get_user_by_google_sub(db, sub)
    if user is not None:
        return user

    user = get_user_by_email(db, email)
    if user is not None:
        user.google_sub = sub                      # link Google to the existing account
        if not user.name and name:
            user.name = name.strip()
        db.flush()
        return user

    user = User(email=normalize_email(email), google_sub=sub, name=(name.strip() if name else None))
    db.add(user)
    db.flush()
    return user


# ----------------------------------------------------- FastAPI request/response models -
class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleRequest(BaseModel):
    # The ID token the Google sign-in button produces in the browser.
    id_token: str


class ForgotRequest(BaseModel):
    email: EmailStr


class ResetRequest(BaseModel):
    token: str
    password: str


class PublicUser(BaseModel):
    """What we expose about a user — never the hash."""
    id: str
    email: str
    name: str | None = None
    totp_enabled: bool = False
    # Which sign-in methods are actually linked (so Settings shows the real ones, not
    # decorative provider chips). has_password = email+password; has_google = Google linked.
    has_password: bool = False
    has_google: bool = False


class AuthResponse(BaseModel):
    token: str
    user: PublicUser


class TotpLoginRequest(BaseModel):
    # The challenge from the password step, plus a 6-digit app code or a recovery code.
    challenge: str
    code: str


class TotpEnableRequest(BaseModel):
    code: str


class TotpDisableRequest(BaseModel):
    # Re-auth to turn 2FA off: a password (email accounts) OR a current code (any account).
    password: str | None = None
    code: str | None = None


def _public(user: User) -> PublicUser:
    return PublicUser(
        id=user.id,
        email=user.email,
        name=user.name,
        totp_enabled=bool(user.totp_enabled),
        has_password=user.password_hash is not None,
        has_google=user.google_sub is not None,
    )


# --------------------------------------------------------------------- dependencies ----
def _token_from_header(authorization: str | None) -> str | None:
    """Pull the token out of an 'Authorization: Bearer <token>' header, if present."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def current_user_optional(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User | None:
    """The logged-in user, or None if no/invalid token. Use for endpoints that work
    for both signed-in and anonymous callers (so existing features keep working)."""
    token = _token_from_header(authorization)
    if not token:
        return None
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        return None
    # Login tokens carry NO 'kind'. Special-purpose tokens (password reset, the 2FA
    # login challenge) set one — reject them here so they can never be replayed as a
    # bearer token to access the app. (The reverse — a login token used to reset a
    # password — is already blocked by the kind check in reset_password.)
    if payload.get("kind"):
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    user = get_user_by_id(db, user_id)
    if not user or not user.is_active:
        return None
    # Reject tokens minted before the last "sign out everywhere" (stale session epoch).
    if int(payload.get("tv", 0)) != int(user.token_version or 0):
        return None
    return user


def current_user(
    user: User | None = Depends(current_user_optional),
) -> User:
    """Require a logged-in user; 401 otherwise. Use to PROTECT an endpoint."""
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in to continue.")
    return user


# -------------------------------------------------------------------------- endpoints --
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=AuthResponse)
def signup(body: SignupRequest, db: Session = Depends(get_db)) -> AuthResponse:
    password = body.password or ""
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LEN} characters.",
        )
    if len(password) > MAX_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at most {MAX_PASSWORD_LEN} characters.",
        )
    if get_user_by_email(db, body.email) is not None:
        # Don't reveal more than needed, but be helpful: this email is taken.
        raise HTTPException(status_code=409, detail="An account with that email already exists.")

    user = create_user(db, body.email, password, body.name)
    from . import org  # local import keeps auth's module-load free of org

    org.apply_pending_invite(db, user)  # if this email was invited to a team, join it
    try:
        db.commit()
    except IntegrityError:
        # Two simultaneous signups can both pass the pre-check; the unique constraint
        # catches the loser — same friendly answer as the pre-check, not a 500.
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with that email already exists.")
    return AuthResponse(token=create_access_token(user.id, user.token_version), user=_public(user))


@router.post("/login")
def login(body: LoginRequest, db: Session = Depends(get_db)) -> dict:
    """Password step. Returns a full login token immediately UNLESS the account has 2FA on,
    in which case it returns {status:'totp_required', challenge} and the caller must finish
    at /auth/login/totp. (No response_model: the shape is one of two, by design.)"""
    user = authenticate(db, body.email, body.password)
    if user is None:
        # One generic message for both "no such email" and "wrong password" so an
        # attacker can't probe which emails exist.
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    if user.totp_enabled:
        # Password was right, but we don't hand out a login token until the second factor.
        return {"status": "totp_required", "challenge": create_2fa_challenge(user.id)}
    return AuthResponse(token=create_access_token(user.id, user.token_version), user=_public(user)).model_dump()


@router.post("/login/totp", response_model=AuthResponse)
def login_totp(body: TotpLoginRequest, db: Session = Depends(get_db)) -> AuthResponse:
    """Finish a 2FA login: the challenge from /auth/login proves the password step passed;
    `code` is a 6-digit authenticator code or a one-time recovery code."""
    try:
        payload = decode_token(body.challenge)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Your login session expired — please sign in again.")
    if payload.get("kind") != "2fa" or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Your login session expired — please sign in again.")

    user = get_user_by_id(db, payload["sub"])
    if user is None or not user.is_active or not user.totp_enabled:
        raise HTTPException(status_code=401, detail="Your login session expired — please sign in again.")

    if not verify_second_factor(user, body.code, db):
        raise HTTPException(status_code=401, detail="That code isn't right. Try again, or use a recovery code.")

    db.commit()  # persist a recovery-code consumption, if that's how they verified
    return AuthResponse(token=create_access_token(user.id, user.token_version), user=_public(user))


@router.post("/google")
def google_signin(body: GoogleRequest, db: Session = Depends(get_db)) -> dict:
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google sign-in isn't enabled on this server.")

    try:
        claims = verify_google_id_token(body.id_token)
    except Exception:
        # Bad/expired/forged token, or wrong audience — one generic message.
        raise HTTPException(status_code=401, detail="Google sign-in failed. Please try again.")

    sub = claims.get("sub")
    email = claims.get("email")
    if not sub or not email:
        raise HTTPException(status_code=401, detail="Google didn't share the needed account details.")
    # Only trust a Google-verified email (Google sends this as True/"true").
    if str(claims.get("email_verified", "")).lower() not in {"true", "1"}:
        raise HTTPException(status_code=401, detail="Your Google email isn't verified.")

    try:
        user = upsert_google_user(db, sub, email, claims.get("name"))
        db.commit()
    except IntegrityError:
        # Simultaneous first sign-ins with the same Google account: the loser's insert
        # collides — re-run the upsert, which now finds the winner's row and links to it.
        db.rollback()
        user = upsert_google_user(db, sub, email, claims.get("name"))
    from . import org  # apply any pending team invite for this (possibly new) account

    org.apply_pending_invite(db, user)
    db.commit()
    if user.totp_enabled:
        # 2FA means 2FA regardless of first factor — a verified Google identity still
        # completes at /auth/login/totp.
        return {"status": "totp_required", "challenge": create_2fa_challenge(user.id)}
    return AuthResponse(token=create_access_token(user.id, user.token_version), user=_public(user)).model_dump()


@router.post("/forgot")
def forgot_password(body: ForgotRequest, db: Session = Depends(get_db)) -> dict:
    """Email a password-reset link. The answer is the SAME whether or not the account
    exists (no email probing); only a server missing SMTP config answers differently,
    honestly, so users aren't left waiting for an email that can never arrive."""
    if not email_configured():
        raise HTTPException(
            status_code=503,
            detail="Password reset isn't available on this server yet "
                   "(email delivery isn't configured). Please contact support.",
        )
    user = get_user_by_email(db, body.email)
    if user is not None and user.is_active:
        link = f"{config.FRONTEND_URL}/reset-password?token={create_reset_token(user.id)}"
        try:
            send_reset_email(user.email, link)
        except Exception:
            # Don't leak whether the account exists via an SMTP hiccup; log-side only.
            import traceback

            traceback.print_exc()
    return {"status": "ok", "message": "If an account exists for that email, a reset link is on its way."}


@router.post("/reset")
def reset_password(body: ResetRequest, db: Session = Depends(get_db)) -> dict:
    """Set a new password from an emailed reset token."""
    try:
        payload = decode_token(body.token)
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=400,
            detail="This reset link is invalid or has expired — request a new one.",
        )
    if payload.get("kind") != "pwreset" or not payload.get("sub"):
        raise HTTPException(
            status_code=400,
            detail="This reset link is invalid or has expired — request a new one.",
        )
    if len(body.password) < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LEN} characters.",
        )
    if len(body.password) > MAX_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at most {MAX_PASSWORD_LEN} characters.",
        )
    user = get_user_by_id(db, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=400,
            detail="This reset link is invalid or has expired — request a new one.",
        )
    user.password_hash = hash_password(body.password)
    db.commit()
    return {"status": "ok", "message": "Password updated — you can log in now."}


@router.get("/me", response_model=PublicUser)
def me(user: User = Depends(current_user)) -> PublicUser:
    """Who am I? Lets the frontend confirm a saved token is still valid."""
    return _public(user)


# ------------------------------------------------------------- two-factor endpoints ----
@router.get("/2fa/status")
def totp_status(user: User = Depends(current_user)) -> dict:
    """Whether 2FA is on for the signed-in user, and how many recovery codes are left."""
    return {
        "enabled": bool(user.totp_enabled),
        "recovery_codes_remaining": recovery_codes_remaining(user),
    }


@router.post("/2fa/setup")
def totp_setup(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Begin enrollment: mint a secret and return it plus an otpauth:// URI for the QR.
    2FA is NOT active yet — the user must confirm a code at /2fa/enable. Calling this again
    before enabling issues a fresh secret (e.g. they lost the QR)."""
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already on.")
    secret = totp.generate_secret()
    user.totp_secret = secret
    db.commit()
    return {
        "secret": secret,
        "otpauth_uri": totp.provisioning_uri(secret, user.email),
    }


@router.post("/2fa/enable")
def totp_enable(
    body: TotpEnableRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Confirm enrollment: verify a code against the pending secret, then turn 2FA on and
    hand back one-time recovery codes (shown ONCE — we only store their hashes)."""
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already on.")
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="Start setup first, then enter a code.")
    if not totp.verify(user.totp_secret, body.code):
        raise HTTPException(
            status_code=400,
            detail="That code didn't match. Check your authenticator app (and that your device clock is correct).",
        )
    recovery = totp.generate_recovery_codes()
    set_recovery_codes(user, recovery)
    user.totp_enabled = True
    db.commit()
    return {"status": "ok", "recovery_codes": recovery}


@router.post("/2fa/disable")
def totp_disable(
    body: TotpDisableRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Turn 2FA off. Requires re-auth — a password (email accounts) or a current code —
    so a stolen login token alone can't strip the second factor."""
    if not user.totp_enabled:
        return {"status": "ok"}  # already off; idempotent
    reauthed = (
        (body.password is not None and verify_password(body.password, user.password_hash))
        or (body.code is not None and verify_second_factor(user, body.code, db))
    )
    if not reauthed:
        raise HTTPException(
            status_code=400,
            detail="Confirm it's you: enter your password or a current authenticator code.",
        )
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_recovery_codes = None
    db.commit()
    return {"status": "ok"}


@router.delete("/account")
def delete_account(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Permanently delete the signed-in user's account and all server-side data tied to it:
    team memberships, activity records, the weekly-digest log, and any pending invites to
    their email. Teams they OWN are dissolved (the org + its memberships/invites go too).
    This is real and irreversible — the frontend confirms first."""
    from .models import DigestLog, OrgInvite, OrgMembership, Organization, RunEvent

    uid, email = user.id, user.email

    # Teams this user founded are dissolved along with their memberships + pending invites.
    owned = db.scalars(select(Organization).where(Organization.created_by == uid)).all()
    for organization in owned:
        db.query(OrgMembership).filter(OrgMembership.org_id == organization.id).delete()
        db.query(OrgInvite).filter(OrgInvite.org_id == organization.id).delete()
        db.delete(organization)

    db.query(OrgMembership).filter(OrgMembership.user_id == uid).delete()
    db.query(RunEvent).filter(RunEvent.user_id == uid).delete()
    db.query(DigestLog).filter(DigestLog.user_id == uid).delete()
    db.query(OrgInvite).filter(OrgInvite.email == email).delete()
    db.delete(user)
    db.commit()
    return {"status": "ok"}


@router.post("/logout-all", response_model=AuthResponse)
def logout_all(user: User = Depends(current_user), db: Session = Depends(get_db)) -> AuthResponse:
    """Sign out of every device: bump the session epoch so all previously-issued tokens are
    rejected, then hand back a fresh token for THIS device so the caller stays signed in."""
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    return AuthResponse(token=create_access_token(user.id, user.token_version), user=_public(user))
