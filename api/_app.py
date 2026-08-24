"""Web-app plumbing shared by every handler: config, session, request helpers.

The design rule here is that **the database is optional**. With no
`DATABASE_URL` this app behaves exactly as it did before accounts existed: one
user, configured by environment variables, gated by `APP_TOKEN`. Adding
multi-user support must not be able to break a deployment that never asked for
it.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from goodnotes_notion_sync import webauth  # noqa: E402
from goodnotes_notion_sync.canvas import DEFAULT_BASE_URL as CANVAS_DEFAULT  # noqa: E402
from goodnotes_notion_sync.canvas_import import DEFAULT_TIMEZONE  # noqa: E402
from goodnotes_notion_sync.config import (  # noqa: E402
    RunConfig,
    config_for_user,
    config_from_env,
)
from goodnotes_notion_sync.crypto import CryptoError  # noqa: E402
from goodnotes_notion_sync.store import (  # noqa: E402
    CANVAS,
    GOOGLE,
    NOTION,
    Store,
    StoreError,
    database_configured,
)

MAX_BODY = 256 * 1024


class AppError(RuntimeError):
    pass


# -- environment -----------------------------------------------------------


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def base_url() -> str:
    """The origin OAuth providers must redirect back to.

    `VERCEL_URL` is *not* usable here: it changes on every deployment, and an
    OAuth redirect URI has to be registered ahead of time and match exactly.
    A preview deployment would send users to a URL Google has never heard of.
    """
    explicit = env("APP_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    production = env("VERCEL_PROJECT_PRODUCTION_URL")
    if production:
        return f"https://{production}".rstrip("/")
    return ""


def session_secret() -> str:
    return env("SESSION_SECRET")


def multi_user() -> bool:
    return database_configured()


def get_store() -> Store | None:
    """A Store, or None when this deployment is in single-user mode."""
    if not multi_user():
        return None
    return Store.from_env()


def readiness() -> list[str]:
    """Configuration that multi-user mode cannot start without."""
    missing = []
    if not env("SESSION_SECRET"):
        missing.append("SESSION_SECRET")
    if not env("APP_ENCRYPTION_KEY"):
        missing.append("APP_ENCRYPTION_KEY")
    if not base_url():
        missing.append("APP_BASE_URL")
    if not env("GOOGLE_CLIENT_ID"):
        missing.append("GOOGLE_CLIENT_ID")
    if not env("GOOGLE_CLIENT_SECRET"):
        missing.append("GOOGLE_CLIENT_SECRET")
    return missing


# -- per-user configuration ------------------------------------------------


# -- request handling ------------------------------------------------------


@dataclass
class Denied:
    """A refusal, carried rather than raised so handlers stay linear."""

    status: int
    error: str
    extra: dict = field(default_factory=dict)


class BaseHandler(BaseHTTPRequestHandler):
    """Shared response helpers. Vercel needs the subclass to be named `handler`."""

    # -- responses ---------------------------------------------------------

    def respond(self, status: int, payload: dict, *, cookies: list[str] = ()) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str, *, cookies: list[str] = ()) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def fail(self, denied: Denied) -> None:
        self.respond(denied.status, {"ok": False, "error": denied.error, **denied.extra})

    def bounce(self, message: str) -> None:
        """Send a browser back to the dashboard with a message to display."""
        from urllib.parse import quote

        self.redirect(f"/?notice={quote(message)[:300]}")

    # -- requests ----------------------------------------------------------

    def query(self) -> dict[str, str]:
        raw = parse_qs(urlparse(self.path).query)
        return {key: values[0] for key, values in raw.items() if values}

    def body_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    def cookie_header(self) -> str:
        return self.headers.get("Cookie") or ""

    def session(self) -> webauth.Session | None:
        secret = session_secret()
        if not secret:
            return None
        return webauth.read_session(self.cookie_header(), secret)

    def require_user(self) -> tuple[Store, int, webauth.Session] | Denied:
        """Resolve the signed-in user, or explain what is missing."""
        if not multi_user():
            return Denied(400, "This deployment is not in multi-user mode")
        session = self.session()
        if session is None:
            return Denied(401, "Not signed in")
        try:
            store = Store.from_env()
        except (StoreError, CryptoError) as exc:
            return Denied(500, str(exc))
        if store.get_user(session.user_id) is None:
            # The row is gone but the cookie is still valid; treat as signed out.
            return Denied(401, "Not signed in")
        return store, session.user_id, session

    def require_csrf(self, session: webauth.Session) -> Denied | None:
        if not webauth.csrf_ok(session, self.headers.get("X-CSRF-Token")):
            return Denied(403, "Missing or invalid CSRF token")
        return None

    def log_message(self, *args) -> None:
        """Silence the default access log; Vercel captures requests already."""


def authorised(headers) -> bool:
    """True when the caller presents the dashboard token or Vercel's cron secret.

    Vercel sends `Authorization: Bearer $CRON_SECRET` on scheduled invocations.
    The dashboard sends `APP_TOKEN` the same way. Compared with
    `hmac.compare_digest` so the check is not timing-sensitive.
    """
    header = headers.get("Authorization") or headers.get("authorization") or ""
    provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not provided:
        return False

    accepted = {
        os.environ.get("APP_TOKEN", "").strip(),
        os.environ.get("CRON_SECRET", "").strip(),
    }
    accepted.discard("")
    if not accepted:
        # Fail closed. An unset token must never mean "open to everyone".
        return False

    # compare_digest raises TypeError on a non-ASCII str, and this runs
    # *outside* the handler's try block -- so a request with an accented
    # character in the token would crash the function instead of getting a 401.
    supplied = provided.encode("utf-8")
    return any(
        hmac.compare_digest(supplied, value.encode("utf-8")) for value in accepted
    )


def resolve_caller(request: BaseHandler, *, mutating: bool):
    """Work out who is asking and which configuration to run for them.

    Three callers exist and they are not interchangeable:

    * a signed-in browser  -> that user's stored credentials, CSRF enforced
    * a scheduler with the bearer token -> the environment's credentials
    * anyone else -> 401

    The session is checked first so that in accounts mode a stale `APP_TOKEN`
    can never quietly run against the wrong person's data.
    """
    if multi_user():
        session = request.session()
        if session is not None:
            resolved = request.require_user()
            if not isinstance(resolved, tuple):
                return resolved
            store, user_id, session = resolved
            if mutating:
                denied = request.require_csrf(session)
                if denied:
                    return denied
            # Label it with the email so reports and run history say who,
            # not an opaque row id.
            return config_for_user(store, user_id, label=session.email), store, user_id

    if authorised(request.headers):
        return config_from_env(), None, None

    if multi_user():
        return Denied(401, "Not signed in", {"loginUrl": "/api/auth/google/start"})
    return Denied(401, "Unauthorised")
