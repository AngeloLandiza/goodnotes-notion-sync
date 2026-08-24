"""Shared plumbing for the Vercel functions.

These handlers sit on a public URL, so *everything* here is behind a token.
Without one, a stranger who guesses the deployment name could read your
assignment titles and Drive filenames, and trigger writes into your Notion.
"""

from __future__ import annotations

import hmac
import os
import sys
import time

# The package lives at the repo root, one level above api/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from goodnotes_notion_sync.drive import DriveClient  # noqa: E402
from goodnotes_notion_sync.notion import NotionClient  # noqa: E402
from goodnotes_notion_sync.sync import Report, run_sync  # noqa: E402

REQUIRED = (
    "NOTION_TOKEN",
    "NOTION_ASSIGNMENTS_DB",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "GDRIVE_FOLDER_ID",
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

    return any(hmac.compare_digest(provided, value) for value in accepted)


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


def execute(*, dry_run: bool, force: bool = False) -> dict:
    started = time.monotonic()
    notion = NotionClient(env("NOTION_TOKEN"))
    drive = DriveClient(
        env("GOOGLE_CLIENT_ID"),
        env("GOOGLE_CLIENT_SECRET"),
        env("GOOGLE_REFRESH_TOKEN"),
    )
    report = run_sync(
        notion=notion,
        drive=drive,
        database_id=env("NOTION_ASSIGNMENTS_DB"),
        folder_id=drive.resolve_folder(env("GDRIVE_FOLDER_ID")),
        dry_run=dry_run,
        force=force,
    )
    return serialise(report, dry_run=dry_run, elapsed=time.monotonic() - started)
