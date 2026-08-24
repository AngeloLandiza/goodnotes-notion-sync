"""Run every account's syncs, one after another.

The scheduler's job changed shape when accounts arrived: it used to be "sync
me", and it is now "sync everyone who wants it". The rule that matters is that
one broken account must not stop the others -- a classmate whose Canvas token
expired should not silently cost everybody else their nightly run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .canvas import CanvasClient
from .canvas_import import run_canvas_import
from .config import RunConfig, config_for_user
from .drive import DriveClient
from .notion import NotionClient
from .store import Store, User
from .sync import run_sync

log = logging.getLogger(__name__)

CANVAS_JOB = "canvas"
SYNC_JOB = "sync"
# Canvas first: rows it creates are then visible to the GoodNotes matcher in
# the same pass, so a new assignment and its notebook link up immediately.
JOBS = (CANVAS_JOB, SYNC_JOB)


@dataclass
class Outcome:
    email: str
    kind: str
    ok: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class FanoutReport:
    outcomes: list[Outcome] = field(default_factory=list)
    users: int = 0

    @property
    def failures(self) -> list[Outcome]:
        return [o for o in self.outcomes if not o.ok and not o.skipped]

    def to_text(self) -> str:
        lines = [f"{self.users} account(s)"]
        for outcome in self.outcomes:
            if outcome.skipped:
                mark = "-"
            elif outcome.ok:
                mark = "ok"
            else:
                mark = "FAIL"
            lines.append(f"  [{mark:>4}] {outcome.email} {outcome.kind}: {outcome.detail}")
        if self.failures:
            lines.append("")
            lines.append(f"{len(self.failures)} job(s) failed")
        return "\n".join(lines)


def run_job(kind: str, config: RunConfig, *, dry_run: bool) -> dict:
    """Run one job for one configuration and return its totals."""
    if kind == CANVAS_JOB:
        report = run_canvas_import(
            canvas=CanvasClient(config.canvas_token, config.canvas_base_url),
            notion=NotionClient(config.notion_token),
            assignments_db=config.assignments_db,
            courses_db=config.courses_db,
            tz_name=config.campus_timezone,
            dry_run=dry_run,
        )
        return {
            "created": len(report.created),
            "updated": len(report.updated),
            "adopted": len(report.adopted),
            "unmatchedCourses": len(report.unmatched_courses),
        }

    if kind == SYNC_JOB:
        drive = DriveClient(
            config.google_client_id,
            config.google_client_secret,
            config.google_refresh_token,
        )
        report = run_sync(
            notion=NotionClient(config.notion_token),
            drive=drive,
            database_id=config.assignments_db,
            folder_id=drive.resolve_folder(config.drive_folder_id),
            dry_run=dry_run,
        )
        return {
            "linked": len(report.linked),
            "unmatched": len(report.unmatched),
            "files": report.total_files,
        }

    raise ValueError(f"unknown job {kind!r}")


def _missing(kind: str, config: RunConfig) -> list[str]:
    return (
        config.missing_for_canvas() if kind == CANVAS_JOB else config.missing_for_sync()
    )


def run_for_user(
    store: Store,
    user: User,
    *,
    dry_run: bool = False,
    jobs: tuple[str, ...] = JOBS,
) -> list[Outcome]:
    config = config_for_user(store, user.id, label=user.email)
    outcomes: list[Outcome] = []

    for kind in jobs:
        absent = _missing(kind, config)
        if absent:
            # Not an error. Half-finished setup is the normal state of a new
            # account, and shouting about it nightly trains people to ignore
            # the report.
            outcomes.append(
                Outcome(
                    user.email,
                    kind,
                    ok=True,
                    skipped=True,
                    detail=f"not set up ({', '.join(absent)})",
                )
            )
            continue

        started = datetime.now(timezone.utc)
        try:
            totals = run_job(kind, config, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - one account must not stop the rest
            detail = f"{type(exc).__name__}: {exc}"
            log.warning("%s %s failed: %s", user.email, kind, detail)
            outcomes.append(Outcome(user.email, kind, ok=False, detail=detail))
            if not dry_run:
                store.record_run(
                    user.id, kind, ok=False, started_at=started, error=detail
                )
            continue

        detail = ", ".join(f"{k}={v}" for k, v in totals.items())
        outcomes.append(Outcome(user.email, kind, ok=True, detail=detail))
        if not dry_run:
            store.record_run(
                user.id, kind, ok=True, started_at=started, summary=totals
            )

    return outcomes


def run_for_all(
    store: Store,
    *,
    dry_run: bool = False,
    only_email: str = "",
    jobs: tuple[str, ...] = JOBS,
) -> FanoutReport:
    users = store.enabled_users()
    if only_email:
        wanted = only_email.strip().lower()
        users = [u for u in users if u.email == wanted]

    report = FanoutReport(users=len(users))
    for user in users:
        report.outcomes.extend(
            run_for_user(store, user, dry_run=dry_run, jobs=jobs)
        )
    return report
