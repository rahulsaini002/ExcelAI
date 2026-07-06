"""OIDC single sign-on — enterprise SSO that works with any OpenID Connect provider
(Okta, Azure AD / Entra, Auth0, Google Workspace, Keycloak, …) via standard discovery.

Why OIDC and not SAML: SAML's security rests on XML Digital Signatures (C14N canonicalization
+ enveloped signatures), which is unsafe to hand-roll and needs a vetted native library
(xmlsec). OIDC's security rests on a JWT signed by the IdP — verified with the same battle-
tested `pyjwt` + `cryptography` we already use. So OIDC is the responsible, dependency-free
path, and it covers essentially every modern IdP.

The flow (Authorization Code):
  /auth/oidc/start    → redirect the browser to the IdP with a signed `state` (CSRF) and a
                        `nonce` (binds the eventual ID token to THIS request).
  /auth/oidc/callback → validate state, swap the code for tokens at the IdP, then VERIFY the
                        ID token: signature (IdP's JWKS, RS256), issuer, audience==client_id,
                        nonce, expiry. Only then do we trust the email and sign the user in.

State is a short-lived JWT WE sign (stateless — no server session needed) carrying the nonce;
an attacker can't forge it, and it expires in minutes. What still needs YOU: register the app
at your IdP and set SUMIO_OIDC_ISSUER / _CLIENT_ID / _CLIENT_SECRET (and optionally
_REDIRECT_URI). Until then the endpoints report SSO is off rather than trusting anything.

The verification + state logic is pure and unit-tested (a self-signed key stands in for the
IdP); the network bits (discovery, token exchange, JWKS) are thin, injectable functions so
tests run fully offline.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import jwt

from . import config

SCOPE = "openid email profile"
STATE_MINUTES = 10


# ------------------------------------------------------------------------ config --------
def oidc_configured() -> bool:
    s = settings()
    return bool(s["issuer"] and s["client_id"] and s["client_secret"])


def settings() -> dict:
    return {
        "issuer": os.environ.get("SUMIO_OIDC_ISSUER", "").rstrip("/"),
        "client_id": os.environ.get("SUMIO_OIDC_CLIENT_ID", ""),
        "client_secret": os.environ.get("SUMIO_OIDC_CLIENT_SECRET", ""),
        "redirect_uri": os.environ.get("SUMIO_OIDC_REDIRECT_URI", "") or _default_redirect(),
    }


def _default_redirect() -> str:
    base = os.environ.get("SUMIO_BACKEND_URL", "http://localhost:8000").rstrip("/")
    return base + "/auth/oidc/callback"


# --------------------------------------------------------------------- discovery --------
_DISCO_CACHE: dict[str, dict] = {}


def discover(issuer: str) -> dict:
    """Fetch the IdP's OIDC metadata (authorization/token/jwks endpoints). Cached per issuer
    since it's stable. Injectable target for tests (they monkeypatch this)."""
    if issuer in _DISCO_CACHE:
        return _DISCO_CACHE[issuer]
    url = issuer + "/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 (configured issuer)
        doc = json.loads(r.read())
    _DISCO_CACHE[issuer] = doc
    return doc


# --------------------------------------------------------------- state (CSRF+nonce) -----
def create_state(nonce: str) -> str:
    """A signed, short-lived token carrying the nonce. Doubles as the CSRF `state`: because
    only we can sign it, a forged callback can't pass validation."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"kind": "oidc", "nonce": nonce, "iat": now, "exp": now + timedelta(minutes=STATE_MINUTES)},
        config.JWT_SECRET,
        algorithm=config.JWT_ALGORITHM,
    )


def read_state(state: str) -> str | None:
    """Return the nonce from a valid state, or None if it's forged/expired/wrong-kind."""
    try:
        payload = jwt.decode(state, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("kind") != "oidc":
        return None
    return payload.get("nonce")


# ------------------------------------------------------------------- authorize ----------
def authorization_url(
    authorization_endpoint: str, client_id: str, redirect_uri: str, state: str, nonce: str
) -> str:
    q = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "nonce": nonce,
    })
    return f"{authorization_endpoint}?{q}"


# --------------------------------------------------------------- token exchange ---------
def exchange_code(
    token_endpoint: str, code: str, client_id: str, client_secret: str, redirect_uri: str
) -> dict:
    """Swap the authorization code for tokens at the IdP. Thin + injectable for tests."""
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        token_endpoint, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 (discovered endpoint)
        return json.loads(r.read())


def signing_key_for_token(jwks_uri: str, id_token: str):
    """Fetch the IdP public key that signed this ID token (by its `kid`) from the JWKS.
    Injectable for tests (which supply the key directly)."""
    from jwt import PyJWKClient

    return PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token).key


# ------------------------------------------------------------- ID-token validation ------
def validate_id_token(
    id_token: str, signing_key, issuer: str, client_id: str, nonce: str | None = None
) -> dict:
    """Verify an ID token and return its claims, or raise. Checks the IdP signature (RS256),
    issuer, audience==client_id, and expiry (pyjwt), then the nonce binding (us). Raising on
    ANY problem is the point — we only trust the email after this passes."""
    claims = jwt.decode(
        id_token,
        signing_key,
        algorithms=["RS256"],
        audience=client_id,
        issuer=issuer,
        options={"require": ["exp", "iat", "aud", "iss"]},
    )
    if nonce is not None and claims.get("nonce") != nonce:
        raise jwt.InvalidTokenError("nonce mismatch")
    return claims


def email_from_claims(claims: dict) -> str | None:
    """The verified email from an ID token, or None if absent/unverified. We require the IdP
    to assert the email is verified — SSO links accounts by email, so an unverified one could
    let a user claim someone else's account."""
    email = claims.get("email")
    verified = claims.get("email_verified", True)  # some IdPs omit it; treat present-but-false as blocking
    if not email or str(verified).lower() in {"false", "0"}:
        return None
    return email
