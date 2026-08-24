"""Sign-in and account connections.

One function serves five routes. `vercel.json` rewrites the real paths onto it
with an `?action=` parameter, because Google requires an OAuth redirect URI to
be registered ahead of time and matched exactly, and it does not accept a query
string in a registered URI. So the browser sees `/api/auth/google/callback`,
which is what Google has on file, while the code lives in one place.

  /api/auth/google/start     begin sign-in (and Drive authorisation)
  /api/auth/google/callback  Google returns here
  /api/auth/notion/start     connect a Notion workspace
  /api/auth/notion/callback  Notion returns here
  /api/auth/logout           drop the session cookie
"""

from __future__ import annotations

import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IMPORT_ERROR: str | None = None
try:
    from _app import (
        AppError,
        BaseHandler,
        base_url,
        env,
        get_store,
        multi_user,
        readiness,
        session_secret,
    )
    from goodnotes_notion_sync import notion_oauth, oauth, webauth
    from goodnotes_notion_sync.drive import SCOPES as DRIVE_SCOPES
    from goodnotes_notion_sync.store import GOOGLE, NOTION, normalise_email
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

# openid/email/profile identify the person; drive.readonly is what the sync
# needs. Asking for both in one consent is the whole point -- it removes the
# separate `auth` CLI step that used to be the worst part of setup.
LOGIN_SCOPES = ["openid", "email", "profile"] + list(
    DRIVE_SCOPES if not _IMPORT_ERROR else []
)


def _callback_url(provider: str) -> str:
    return f"{base_url()}/api/auth/{provider}/callback"


class handler(BaseHandler if not _IMPORT_ERROR else object):  # type: ignore[misc]
    def _guard(self) -> bool:
        if _IMPORT_ERROR is not None:
            self.respond(
                500,
                {
                    "ok": False,
                    "error": "The auth function failed to import",
                    "raw": _IMPORT_ERROR,
                },
            )
            return False
        if not multi_user():
            self.respond(
                400,
                {
                    "ok": False,
                    "error": (
                        "Accounts need a database. Add a Postgres integration "
                        "from the Vercel Marketplace and redeploy."
                    ),
                },
            )
            return False
        absent = readiness()
        if absent:
            self.respond(
                500,
                {
                    "ok": False,
                    "error": "Multi-user mode is missing configuration",
                    "missing": absent,
                },
            )
            return False
        return True

    # -- state cookie ------------------------------------------------------

    def _issue_state(self, provider: str, **extra) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        cookie_value = webauth.sign(
            {"provider": provider, "state": state, **extra},
            session_secret(),
            ttl=webauth.STATE_TTL,
        )
        return state, webauth.set_cookie(
            webauth.STATE_COOKIE, cookie_value, max_age=webauth.STATE_TTL
        )

    def _consume_state(self, provider: str) -> dict | None:
        """Verify the callback belongs to a flow this browser actually started.

        Without this an attacker can hand someone a callback URL carrying their
        own authorisation code and silently attach their account to the
        victim's session.
        """
        jar = webauth.parse_cookies(self.cookie_header())
        payload = webauth.unsign(jar.get(webauth.STATE_COOKIE, ""), session_secret())
        if not payload or payload.get("provider") != provider:
            return None
        given = self.query().get("state", "")
        stored = str(payload.get("state", ""))
        if not stored or not secrets.compare_digest(stored, given):
            return None
        return payload

    # -- google ------------------------------------------------------------

    def _google_start(self) -> None:
        verifier, challenge = oauth.pkce_pair()
        state, cookie = self._issue_state("google", verifier=verifier)
        self.redirect(
            oauth.build_auth_url(
                client_id=env("GOOGLE_CLIENT_ID"),
                redirect_uri=_callback_url("google"),
                challenge=challenge,
                state=state,
                scopes=LOGIN_SCOPES,
            ),
            cookies=[cookie],
        )

    def _google_callback(self) -> None:
        params = self.query()
        if params.get("error"):
            self.bounce(f"Google sign-in was cancelled ({params['error']}).")
            return

        flow = self._consume_state("google")
        if flow is None:
            self.bounce("That sign-in link expired. Please try again.")
            return

        try:
            payload = oauth.exchange_code_full(
                client_id=env("GOOGLE_CLIENT_ID"),
                client_secret=env("GOOGLE_CLIENT_SECRET"),
                code=params.get("code", ""),
                verifier=str(flow.get("verifier", "")),
                redirect_uri=_callback_url("google"),
            )
            identity = oauth.decode_id_token(payload.get("id_token", ""))
        except Exception as exc:  # noqa: BLE001
            self.bounce(f"Google sign-in failed: {exc}")
            return

        if not identity.email_verified:
            self.bounce("That Google account has no verified email address.")
            return

        store = get_store()
        owner = normalise_email(env("OWNER_EMAIL"))
        if owner:
            store.ensure_owner(owner)

        if not store.is_invited(identity.email):
            # Deliberately not "no such user": whether an address is on the
            # list is not something an uninvited visitor gets to learn.
            self.bounce(
                f"{identity.email} has not been invited to this app."
            )
            return

        user = store.upsert_google_user(
            sub=identity.sub,
            email=identity.email,
            name=identity.name,
            picture=identity.picture,
        )
        if owner:
            # Again, now the row exists: the first run had nothing to mark.
            store.ensure_owner(owner)

        refresh = payload.get("refresh_token")
        if refresh:
            store.set_connection(
                user.id,
                GOOGLE,
                refresh,
                {"scopes": payload.get("scope", ""), "email": identity.email},
            )
        # No refresh_token means Google decided this client was already
        # authorised. Any token already stored is still good, so leave it --
        # overwriting with nothing would silently disconnect Drive.

        token, _ = webauth.new_session(user.id, user.email, session_secret())
        self.redirect(
            "/",
            cookies=[
                webauth.set_cookie(
                    webauth.SESSION_COOKIE, token, max_age=webauth.SESSION_TTL
                ),
                webauth.clear_cookie(webauth.STATE_COOKIE),
            ],
        )

    # -- notion ------------------------------------------------------------

    def _notion_start(self) -> None:
        resolved = self.require_user()
        if not isinstance(resolved, tuple):
            self.fail(resolved)
            return
        _, user_id, _ = resolved

        client_id = env("NOTION_OAUTH_CLIENT_ID")
        if not client_id or not env("NOTION_OAUTH_CLIENT_SECRET"):
            self.respond(
                500,
                {
                    "ok": False,
                    "error": (
                        "Notion OAuth is not configured. Create a public "
                        "integration at notion.so/my-integrations and set "
                        "NOTION_OAUTH_CLIENT_ID and NOTION_OAUTH_CLIENT_SECRET."
                    ),
                },
            )
            return

        state, cookie = self._issue_state("notion", uid=user_id)
        self.redirect(
            notion_oauth.build_auth_url(
                client_id=client_id,
                redirect_uri=_callback_url("notion"),
                state=state,
            ),
            cookies=[cookie],
        )

    def _notion_callback(self) -> None:
        params = self.query()
        if params.get("error"):
            self.bounce(f"Notion did not connect ({params['error']}).")
            return

        flow = self._consume_state("notion")
        if flow is None:
            self.bounce("That Notion link expired. Please try again.")
            return

        resolved = self.require_user()
        if not isinstance(resolved, tuple):
            self.bounce("Please sign in again, then connect Notion.")
            return
        store, user_id, _ = resolved

        # The flow was started by whoever was signed in then. If that is not
        # who is signed in now, the workspace would be attached to the wrong
        # account.
        if int(flow.get("uid", 0)) != user_id:
            self.bounce("That Notion link belongs to a different account.")
            return

        try:
            grant = notion_oauth.exchange_code(
                client_id=env("NOTION_OAUTH_CLIENT_ID"),
                client_secret=env("NOTION_OAUTH_CLIENT_SECRET"),
                code=params.get("code", ""),
                redirect_uri=_callback_url("notion"),
            )
        except Exception as exc:  # noqa: BLE001
            self.bounce(f"Notion connection failed: {exc}")
            return

        store.set_connection(user_id, NOTION, grant.access_token, grant.metadata)
        self.redirect(
            "/?notice=Notion+connected",
            cookies=[webauth.clear_cookie(webauth.STATE_COOKIE)],
        )

    # -- routing -----------------------------------------------------------

    ACTIONS = {
        "google-start": "_google_start",
        "google-callback": "_google_callback",
        "notion-start": "_notion_start",
        "notion-callback": "_notion_callback",
    }

    def _route(self) -> None:
        if _IMPORT_ERROR is not None or not self._guard():
            return
        action = self.query().get("action", "")
        method = self.ACTIONS.get(action)
        if method is None:
            self.respond(404, {"ok": False, "error": f"Unknown action {action!r}"})
            return
        getattr(self, method)()

    def do_GET(self) -> None:
        if self.query().get("action") == "logout":
            self._logout()
            return
        self._route()

    def do_POST(self) -> None:
        if self.query().get("action") == "logout":
            self._logout()
            return
        self._route()

    def _logout(self) -> None:
        if _IMPORT_ERROR is not None:
            self.respond(500, {"ok": False, "error": "import failed"})
            return
        # Signing out must work even when the deployment is misconfigured --
        # otherwise a broken setting can trap someone in a session.
        self.redirect(
            "/?notice=Signed+out",
            cookies=[
                webauth.clear_cookie(webauth.SESSION_COOKIE),
                webauth.clear_cookie(webauth.STATE_COOKIE),
            ],
        )
