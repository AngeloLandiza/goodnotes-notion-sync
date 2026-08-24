"""Signed cookies, sessions and CSRF -- standard library only.

There is no session store. A session is a signed, expiring blob in a cookie,
which is the right shape for functions that share no memory between
invocations and which keeps the runtime dependency list where it is.

Everything here is security-critical, so it is small enough to read in one
sitting and every branch has a test.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie

__all__ = [
    "Session",
    "SESSION_COOKIE",
    "STATE_COOKIE",
    "clear_cookie",
    "new_session",
    "parse_cookies",
    "read_session",
    "set_cookie",
    "sign",
    "unsign",
]

SESSION_COOKIE = "gns_session"
STATE_COOKIE = "gns_oauth"
SESSION_TTL = 30 * 24 * 3600  # 30 days
STATE_TTL = 15 * 60


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict, secret: str, *, ttl: int) -> str:
    """`<payload>.<mac>`, where payload carries its own expiry."""
    if not secret:
        raise ValueError("refusing to sign with an empty secret")
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl
    raw = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(secret.encode("utf-8"), raw.encode("ascii"), hashlib.sha256)
    return f"{raw}.{_b64(mac.digest())}"


def unsign(token: str, secret: str) -> dict | None:
    """The payload, or None for anything wrong. Never raises.

    A tampered, expired, truncated or garbage cookie is not an exceptional
    condition -- it is Tuesday on a public URL -- and every one of them means
    the same thing: not signed in.
    """
    if not token or not secret:
        return None
    raw, _, provided = token.partition(".")
    if not raw or not provided:
        return None
    expected = hmac.new(
        secret.encode("utf-8"), raw.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        if not hmac.compare_digest(_unb64(provided), expected):
            return None
        payload = json.loads(_unb64(raw))
    except Exception:  # noqa: BLE001 - malformed input is just "no session"
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload


@dataclass
class Session:
    user_id: int
    email: str = ""
    csrf: str = ""
    raw: dict = field(default_factory=dict)


def new_session(user_id: int, email: str, secret: str) -> tuple[str, Session]:
    csrf = secrets.token_urlsafe(24)
    session = Session(user_id=user_id, email=email, csrf=csrf)
    token = sign(
        {"uid": int(user_id), "email": email, "csrf": csrf},
        secret,
        ttl=SESSION_TTL,
    )
    return token, session


def parse_cookies(header: str | None) -> dict[str, str]:
    if not header:
        return {}
    jar = SimpleCookie()
    try:
        jar.load(header)
    except Exception:  # noqa: BLE001
        return {}
    return {key: morsel.value for key, morsel in jar.items()}


def read_session(cookie_header: str | None, secret: str) -> Session | None:
    payload = unsign(
        parse_cookies(cookie_header).get(SESSION_COOKIE, ""), secret
    )
    if payload is None or "uid" not in payload:
        return None
    try:
        user_id = int(payload["uid"])
    except (TypeError, ValueError):
        return None
    return Session(
        user_id=user_id,
        email=str(payload.get("email", "")),
        csrf=str(payload.get("csrf", "")),
        raw=payload,
    )


def set_cookie(name: str, value: str, *, max_age: int, secure: bool = True) -> str:
    """A cookie header value.

    SameSite=Lax rather than Strict: the OAuth providers send the user back
    with a top-level GET, and Strict would withhold the cookie on exactly that
    navigation, so the callback could never find its own state.
    """
    parts = [
        f"{name}={value}",
        "Path=/",
        f"Max-Age={max_age}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie(name: str, *, secure: bool = True) -> str:
    return set_cookie(name, "", max_age=0, secure=secure)


def csrf_ok(session: Session | None, header_value: str | None) -> bool:
    """Whether a mutating request carries the session's own CSRF token.

    The session cookie is SameSite=Lax, which a cross-site *form* POST does not
    carry -- but "Lax" is not a guarantee across every browser and embedding,
    and the cost of the extra check is one header.
    """
    if session is None or not session.csrf:
        return False
    if not header_value:
        return False
    return hmac.compare_digest(session.csrf, header_value)
