"""One user's runnable configuration, from wherever this deployment keeps it.

There are two sources and the rest of the code must not care which is in play:
the process environment (single-user, the original design) or a row per user in
Postgres (accounts mode). Everything downstream takes a `RunConfig`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .canvas import DEFAULT_BASE_URL as CANVAS_DEFAULT
from .canvas_import import DEFAULT_TIMEZONE
from .store import CANVAS, GOOGLE, NOTION, Store

__all__ = ["RunConfig", "config_for_user", "config_from_env"]


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass
class RunConfig:
    """Everything a sync needs, from wherever this deployment keeps it."""

    notion_token: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    canvas_token: str = ""
    canvas_base_url: str = CANVAS_DEFAULT
    assignments_db: str = ""
    courses_db: str = ""
    drive_folder_id: str = ""
    campus_timezone: str = DEFAULT_TIMEZONE
    label: str = ""

    def missing_for_sync(self) -> list[str]:
        pairs = {
            "NOTION_TOKEN": self.notion_token,
            "NOTION_ASSIGNMENTS_DB": self.assignments_db,
            "GOOGLE_CLIENT_ID": self.google_client_id,
            "GOOGLE_CLIENT_SECRET": self.google_client_secret,
            "GOOGLE_REFRESH_TOKEN": self.google_refresh_token,
            "GDRIVE_FOLDER_ID": self.drive_folder_id,
        }
        return [name for name, value in pairs.items() if not value]

    def missing_for_canvas(self) -> list[str]:
        pairs = {
            "NOTION_TOKEN": self.notion_token,
            "NOTION_ASSIGNMENTS_DB": self.assignments_db,
            "NOTION_COURSES_DB": self.courses_db,
            "CANVAS_TOKEN": self.canvas_token,
        }
        return [name for name, value in pairs.items() if not value]


def config_from_env() -> RunConfig:
    return RunConfig(
        notion_token=_env("NOTION_TOKEN"),
        google_client_id=_env("GOOGLE_CLIENT_ID"),
        google_client_secret=_env("GOOGLE_CLIENT_SECRET"),
        google_refresh_token=_env("GOOGLE_REFRESH_TOKEN"),
        canvas_token=_env("CANVAS_TOKEN"),
        canvas_base_url=_env("CANVAS_BASE_URL", CANVAS_DEFAULT),
        assignments_db=_env("NOTION_ASSIGNMENTS_DB"),
        courses_db=_env("NOTION_COURSES_DB"),
        drive_folder_id=_env("GDRIVE_FOLDER_ID"),
        campus_timezone=_env("CAMPUS_TIMEZONE", DEFAULT_TIMEZONE),
        label="environment",
    )


def config_for_user(store: Store, user_id: int, *, label: str = "") -> RunConfig:
    """One user's configuration, assembled from their stored connections.

    The Google *client* stays an environment variable -- it identifies this
    application to Google and is the same for everybody. Only the refresh
    token, which identifies the person, comes from the database.
    """
    settings = store.get_settings(user_id)
    notion = store.get_connection(user_id, NOTION)
    google = store.get_connection(user_id, GOOGLE)
    canvas = store.get_connection(user_id, CANVAS)
    return RunConfig(
        notion_token=notion.secret if notion else "",
        google_client_id=_env("GOOGLE_CLIENT_ID"),
        google_client_secret=_env("GOOGLE_CLIENT_SECRET"),
        google_refresh_token=google.secret if google else "",
        canvas_token=canvas.secret if canvas else "",
        canvas_base_url=settings.canvas_base_url or CANVAS_DEFAULT,
        assignments_db=settings.assignments_db,
        courses_db=settings.courses_db,
        drive_folder_id=settings.drive_folder_id,
        campus_timezone=settings.campus_timezone or DEFAULT_TIMEZONE,
        label=label or f"user {user_id}",
    )
