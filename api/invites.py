"""POST /api/invites -- owner-only management of who may sign in.

This is the app's entire access-control surface, so the owner check is the
first thing that happens after authentication and there is no path around it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IMPORT_ERROR: str | None = None
try:
    from _app import BaseHandler
    from goodnotes_notion_sync.store import StoreError
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


class handler(BaseHandler if not _IMPORT_ERROR else object):  # type: ignore[misc]
    def do_POST(self) -> None:
        if _IMPORT_ERROR is not None:
            self.respond(
                500,
                {"ok": False, "error": "The function failed to import", "raw": _IMPORT_ERROR},
            )
            return

        resolved = self.require_user()
        if not isinstance(resolved, tuple):
            self.fail(resolved)
            return
        store, user_id, session = resolved

        denied = self.require_csrf(session)
        if denied:
            self.fail(denied)
            return

        user = store.get_user(user_id)
        if not user or not user.is_owner:
            # 404, not 403: a non-owner does not need to learn that an invite
            # system exists, let alone who is on it.
            self.respond(404, {"ok": False, "error": "Not found"})
            return

        body = self.body_json()
        action = str(body.get("action", "")).strip()
        email = str(body.get("email", "")).strip()

        try:
            if action == "add":
                store.add_invite(email, invited_by=user_id)
            elif action == "remove":
                store.remove_invite(email)
            else:
                self.respond(400, {"ok": False, "error": "action must be add or remove"})
                return
        except StoreError as exc:
            self.respond(400, {"ok": False, "error": str(exc)})
            return

        self.respond(
            200,
            {
                "ok": True,
                "invites": [
                    {
                        "email": row["email"],
                        "createdAt": row["created_at"].isoformat() if row["created_at"] else "",
                        "signedIn": bool(row["signed_in_as"]),
                    }
                    for row in store.list_invites()
                ],
            },
        )
