"""POST /api/settings -- update one user's own configuration.

Every write here is scoped to the session's user id. Nothing in the request
body chooses whose row is written, which is the single most important property
of this file.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IMPORT_ERROR: str | None = None
try:
    from _app import BaseHandler
    from goodnotes_notion_sync.canvas import api_root
    from goodnotes_notion_sync.store import CANVAS, GOOGLE, NOTION
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

TEXT_FIELDS = {
    "assignmentsDb": "assignments_db",
    "coursesDb": "courses_db",
    "driveFolderId": "drive_folder_id",
    "campusTimezone": "campus_timezone",
    "canvasBaseUrl": "canvas_base_url",
}
DISCONNECTABLE = {GOOGLE, NOTION, CANVAS}


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

        body = self.body_json()

        disconnect = str(body.get("disconnect", "")).strip()
        if disconnect:
            if disconnect not in DISCONNECTABLE:
                self.respond(400, {"ok": False, "error": "Unknown connection"})
                return
            store.delete_connection(user_id, disconnect)
            self.respond(200, {"ok": True, "disconnected": disconnect})
            return

        canvas_token = str(body.get("canvasToken", "")).strip()
        if canvas_token:
            # Canvas has no OAuth for students -- a developer key has to be
            # issued by the school's Canvas admin -- so a personal access token
            # is the only route. It is encrypted like everything else.
            store.set_connection(user_id, CANVAS, canvas_token, {"kind": "personal_access_token"})

        updates: dict = {}
        for incoming, column in TEXT_FIELDS.items():
            if incoming in body:
                updates[column] = str(body[incoming] or "").strip()
        if "autoSync" in body:
            updates["auto_sync"] = bool(body["autoSync"])

        if "canvas_base_url" in updates and updates["canvas_base_url"]:
            # Normalise now rather than at request time, so what the user sees
            # saved is what will actually be called.
            updates["canvas_base_url"] = api_root(
                updates["canvas_base_url"]
            ).removesuffix("/api/v1")

        settings = store.save_settings(user_id, **updates)
        self.respond(
            200,
            {
                "ok": True,
                "settings": {
                    "assignmentsDb": settings.assignments_db,
                    "coursesDb": settings.courses_db,
                    "driveFolderId": settings.drive_folder_id,
                    "campusTimezone": settings.campus_timezone,
                    "canvasBaseUrl": settings.canvas_base_url,
                    "autoSync": settings.auto_sync,
                },
            },
        )
