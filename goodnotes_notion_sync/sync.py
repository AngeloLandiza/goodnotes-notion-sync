"""Tie Drive PDFs to Notion assignments."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .drive import DriveClient
from .matching import Candidate, best_match
from .notion import Assignment, NotionClient

log = logging.getLogger(__name__)


@dataclass
class Row:
    assignment: Assignment
    candidate: Candidate | None
    score: float
    reason: str
    action: str  # linked | would-link | skipped | unmatched | already-linked


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)
    orphan_files: list[Candidate] = field(default_factory=list)
    total_files: int = 0

    @property
    def linked(self) -> list[Row]:
        return [r for r in self.rows if r.action in ("linked", "would-link")]

    @property
    def unmatched(self) -> list[Row]:
        return [r for r in self.rows if r.action == "unmatched"]

    @property
    def already(self) -> list[Row]:
        return [r for r in self.rows if r.action == "already-linked"]

    def to_text(self, *, dry_run: bool) -> str:
        verb = "Would link" if dry_run else "Linked"
        lines: list[str] = []
        lines.append(
            f"{len(self.rows)} assignment(s), {self.total_files} PDF(s) in Drive"
        )
        lines.append("")

        if self.linked:
            lines.append(f"{verb} ({len(self.linked)}):")
            for row in self.linked:
                where = f"{row.candidate.path}/" if row.candidate.path else ""
                lines.append(
                    f"  {row.score:.2f}  {row.assignment.title}"
                    f"\n         -> {where}{row.candidate.name}"
                )
            lines.append("")

        if self.already:
            lines.append(f"Already linked ({len(self.already)}) - use --force to redo")
            lines.append("")

        if self.unmatched:
            lines.append(f"No match ({len(self.unmatched)}):")
            for row in self.unmatched:
                lines.append(f"  {row.assignment.title}\n         {row.reason}")
            lines.append("")

        if self.orphan_files:
            lines.append(
                f"PDFs with no assignment ({len(self.orphan_files)}) - "
                "rename the notebook to match if one of these should be linked:"
            )
            for candidate in self.orphan_files[:25]:
                where = f"{candidate.path}/" if candidate.path else ""
                lines.append(f"  {where}{candidate.name}")
            if len(self.orphan_files) > 25:
                lines.append(f"  ... and {len(self.orphan_files) - 25} more")

        return "\n".join(lines).rstrip()


def run_sync(
    *,
    notion: NotionClient,
    drive: DriveClient,
    database_id: str,
    folder_id: str,
    url_property: str = "Notes PDF",
    title_property: str = "Title",
    threshold: float = 0.78,
    margin: float = 0.06,
    dry_run: bool = False,
    force: bool = False,
) -> Report:
    assignments = notion.assignments(
        database_id, title_property=title_property, url_property=url_property
    )
    candidates = drive.list_pdfs(folder_id)

    report = Report(total_files=len(candidates))
    claimed: set[str] = set()

    # Highest-confidence matches win a file first, so a strong match is never
    # beaten to its PDF by a weaker one that happened to be processed earlier.
    ranked: list[tuple[float, Assignment, Candidate | None, str]] = []
    for assignment in assignments:
        if not assignment.title.strip():
            # Only reachable for a row the Canvas import created and someone
            # then emptied. Nothing can be matched on an empty title, and
            # listing it as "no match" is noise, not information.
            continue
        if assignment.has_notes and not force:
            report.rows.append(
                Row(assignment, None, 0.0, "already has a link", "already-linked")
            )
            continue
        result = best_match(
            assignment.title, candidates, threshold=threshold, margin=margin
        )
        ranked.append((result.score, assignment, result.candidate, result.reason))

    ranked.sort(key=lambda item: -item[0])

    for score, assignment, candidate, reason in ranked:
        if candidate is None:
            report.rows.append(Row(assignment, None, score, reason, "unmatched"))
            continue
        if candidate.id in claimed:
            report.rows.append(
                Row(
                    assignment,
                    None,
                    score,
                    f"{candidate.name!r} was already claimed by a closer title",
                    "unmatched",
                )
            )
            continue

        claimed.add(candidate.id)
        if dry_run:
            report.rows.append(
                Row(assignment, candidate, score, reason, "would-link")
            )
            continue

        notion.set_url(assignment.page_id, url_property, candidate.url)
        log.info("Linked %r -> %s", assignment.title, candidate.name)
        report.rows.append(Row(assignment, candidate, score, reason, "linked"))

    report.orphan_files = [c for c in candidates if c.id not in claimed]
    return report
