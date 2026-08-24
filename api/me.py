"""GET /api/me -- everything the dashboard needs to draw itself.

Deliberately the only read endpoint, and deliberately incapable of returning a
credential: connection state comes from `Store.connection_status`, which never
decrypts anything.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IMPORT_ERROR: str | None = None
try:
    from _app import (
        BaseHandler,
        base_url,
        config_from_env,
        env,
        multi_user,
        readiness,
        session_secret,
    )
    from goodnotes_notion_sync.notion import NotionClient
    from goodnotes_notion_sync.store import CANVAS, GOOGLE, NOTION
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


class handler(BaseHandler if not _IMPORT_ERROR else object):  # type: ignore[misc]
    def do_GET(self) -> None:
        if _IMPORT_ERROR is not None:
            self.respond(
                500,
                {"ok": False, "error": "The function failed to import", "raw": _IMPORT_ERROR},
            )
            return

        if not multi_user():
            # Single-user mode: the old bearer-token dashboard still applies.
            self.respond(
                200,
                {
                    "ok": True,
                    "mode": "single",
                    "signedIn": False,
                    "missing": config_from_env().missing_for_sync(),
                },
            )
            return

        absent = readiness()
        if absent:
            self.respond(
                200,
                {
                    "ok": True,
                    "mode": "accounts",
                    "signedIn": False,
                    "setupNeeded": absent,
                },
            )
            return

        resolved = self.require_user()
        if not isinstance(resolved, tuple):
            self.respond(
                200,
                {
                    "ok": True,
                    "mode": "accounts",
                    "signedIn": False,
                    "loginUrl": "/api/auth/google/start",
                },
            )
            return

        store, user_id, session = resolved
        user = store.get_user(user_id)
        settings = store.get_settings(user_id)
        connections = store.connection_status(user_id)

        payload = {
            "ok": True,
            "mode": "accounts",
            "signedIn": True,
            "csrf": session.csrf,
            "user": {
                "email": user.email,
                "name": user.name,
                "picture": user.picture,
                "isOwner": user.is_owner,
            },
            "connections": {
                name: connections.get(name, {"connected": False})
                for name in (GOOGLE, NOTION, CANVAS)
            },
            "notionOAuthReady": bool(env("NOTION_OAUTH_CLIENT_ID")),
            "settings": {
                "assignmentsDb": settings.assignments_db,
                "coursesDb": settings.courses_db,
                "driveFolderId": settings.drive_folder_id,
                "campusTimezone": settings.campus_timezone,
                "canvasBaseUrl": settings.canvas_base_url,
                "autoSync": settings.auto_sync,
            },
            "ready": {
                "goodnotes": settings.ready_for_goodnotes
                and connections.get(GOOGLE, {}).get("connected", False)
                and connections.get(NOTION, {}).get("connected", False),
                "canvas": settings.ready_for_canvas
                and connections.get(CANVAS, {}).get("connected", False)
                and connections.get(NOTION, {}).get("connected", False),
            },
            "runs": [
                {
                    "kind": run.kind,
                    "ok": run.ok,
                    "summary": run.summary,
                    "error": run.error,
                    "finishedAt": run.finished_at.isoformat(),
                }
                for run in store.recent_runs(user_id, limit=8)
            ],
        }

        if user.is_owner:
            payload["invites"] = [
                {
                    "email": row["email"],
                    "createdAt": row["created_at"].isoformat() if row["created_at"] else "",
                    "signedIn": bool(row["signed_in_as"]),
                }
                for row in store.list_invites()
            ]

        # Listing databases costs a Notion API call, so it is opt-in: the
        # dashboard asks only when someone opens the settings panel.
        if self.query().get("databases") and connections.get(NOTION, {}).get("connected"):
            try:
                token = store.get_connection(user_id, NOTION).secret
                payload["notionDatabases"] = NotionClient(token).databases()
            except Exception as exc:  # noqa: BLE001
                payload["notionDatabasesError"] = f"{type(exc).__name__}: {exc}"

        self.respond(200, payload)
