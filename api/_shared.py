"""Shared plumbing for the Vercel functions.

These handlers sit on a public URL, so *everything* here is behind a token.
Without one, a stranger who guesses the deployment name could read your
assignment titles and Drive filenames, and trigger writes into your Notion.
"""

from __future__ import annotations

import os
import sys
import time

# The package lives at the repo root, one level above api/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from goodnotes_notion_sync.canvas import DEFAULT_BASE_URL, CanvasClient  # noqa: E402
from goodnotes_notion_sync.canvas_import import (  # noqa: E402
    DEFAULT_TIMEZONE,
    ImportReport,
    run_canvas_import,
)
from goodnotes_notion_sync.drive import DriveClient  # noqa: E402
from goodnotes_notion_sync.notion import NotionClient  # noqa: E402
from goodnotes_notion_sync.sync import Report, run_sync  # noqa: E402

from _app import authorised, config_from_env  # noqa: E402,F401

REQUIRED = (
    "NOTION_TOKEN",
    "NOTION_ASSIGNMENTS_DB",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "GDRIVE_FOLDER_ID",
)

# Canvas is opt-in and deliberately *not* part of REQUIRED: adding it there
# would 500 every deployment that only wants the GoodNotes sync.
CANVAS_REQUIRED = (
    "NOTION_TOKEN",
    "NOTION_ASSIGNMENTS_DB",
    "NOTION_COURSES_DB",
    "CANVAS_TOKEN",
)


class ConfigError(RuntimeError):
    pass


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing environment variable {name}")
    return value


def missing_env() -> list[str]:
    return [name for name in REQUIRED if not os.environ.get(name, "").strip()]


def canvas_missing_env() -> list[str]:
    return [name for name in CANVAS_REQUIRED if not os.environ.get(name, "").strip()]


def canvas_configured() -> bool:
    return not canvas_missing_env()


# `authorised` lives in _app so that _app can gate requests without
# importing this module back. Re-exported here for existing callers.

def serialise(report: Report, *, dry_run: bool, elapsed: float) -> dict:
    def row(entry):
        out = {
            "title": entry.assignment.title,
            "notionUrl": entry.assignment.url,
            "score": round(entry.score, 3),
            "action": entry.action,
            "reason": entry.reason,
        }
        if entry.candidate is not None:
            out["file"] = entry.candidate.name
            out["path"] = entry.candidate.path
            out["driveUrl"] = entry.candidate.url
        return out

    return {
        "ok": True,
        "dryRun": dry_run,
        "elapsedSeconds": round(elapsed, 2),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "totals": {
            "assignments": len(report.rows),
            "files": report.total_files,
            "linked": len(report.linked),
            "unmatched": len(report.unmatched),
            "alreadyLinked": len(report.already),
            "orphanFiles": len(report.orphan_files),
        },
        "linked": [row(r) for r in report.linked],
        "unmatched": [row(r) for r in report.unmatched],
        "orphanFiles": [
            {"name": c.name, "path": c.path, "driveUrl": c.url}
            for c in report.orphan_files
        ],
    }


def serialise_canvas(report: ImportReport, *, dry_run: bool, elapsed: float) -> dict:
    def row(entry):
        return {
            "title": entry.title,
            "due": entry.due,
            "code": entry.code,
            "action": entry.action,
            "detail": entry.detail,
        }

    return {
        "ok": True,
        "dryRun": dry_run,
        "elapsedSeconds": round(elapsed, 2),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "totals": {
            "courses": len(report.links),
            "matchedCourses": len(report.links) - len(report.unmatched_courses),
            "assignments": len(report.rows),
            "created": len(report.created),
            "updated": len(report.updated),
            "adopted": len(report.adopted),
            "unchanged": len(report.unchanged),
        },
        "created": [row(r) for r in report.created],
        "updated": [row(r) for r in report.updated],
        "adopted": [row(r) for r in report.adopted],
        "skipped": [row(r) for r in report.skipped],
        "unmatchedCourses": [
            {
                "name": link.course.name or link.course.course_code,
                "code": link.code or "",
                "reason": link.reason,
            }
            for link in report.unmatched_courses
        ],
        "knownCodes": report.known_codes,
        "errors": [
            {"course": course.name or course.course_code, "message": message}
            for course, message in report.errors
        ],
    }


def execute(*, dry_run: bool, force: bool = False, config=None) -> dict:
    """Run the GoodNotes sync for one configuration.

    `config` is a RunConfig -- one user's stored credentials in accounts mode,
    or the process environment in single-user mode. Passing it in rather than
    reading os.environ here is what makes the same code path serve both.
    """
    cfg = config or config_from_env()
    absent = cfg.missing_for_sync()
    if absent:
        raise ConfigError("Missing configuration: " + ", ".join(absent))

    started = time.monotonic()
    notion = NotionClient(cfg.notion_token)
    drive = DriveClient(
        cfg.google_client_id, cfg.google_client_secret, cfg.google_refresh_token
    )
    report = run_sync(
        notion=notion,
        drive=drive,
        database_id=cfg.assignments_db,
        folder_id=drive.resolve_folder(cfg.drive_folder_id),
        dry_run=dry_run,
        force=force,
    )
    return serialise(report, dry_run=dry_run, elapsed=time.monotonic() - started)


def execute_canvas(*, dry_run: bool, config=None) -> dict:
    cfg = config or config_from_env()
    absent = cfg.missing_for_canvas()
    if absent:
        raise ConfigError("Missing configuration: " + ", ".join(absent))

    started = time.monotonic()
    notion = NotionClient(cfg.notion_token)
    canvas = CanvasClient(cfg.canvas_token, cfg.canvas_base_url or DEFAULT_BASE_URL)
    report = run_canvas_import(
        canvas=canvas,
        notion=notion,
        assignments_db=cfg.assignments_db,
        courses_db=cfg.courses_db,
        tz_name=cfg.campus_timezone or DEFAULT_TIMEZONE,
        dry_run=dry_run,
    )
    return serialise_canvas(report, dry_run=dry_run, elapsed=time.monotonic() - started)
