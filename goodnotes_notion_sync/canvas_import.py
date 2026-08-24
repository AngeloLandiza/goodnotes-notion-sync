"""Pull Canvas assignments into the Notion assignments database.

The join between the two systems is the **course code**. Canvas knows a course
as ``2026_Fall_CS_411_39421``; Notion knows it as a row whose ``Code`` reads
``CS 411``. Everything else follows from lining those up.

Rows are titled ``"CS 411 | Homework 3"`` -- the same convention the GoodNotes
matcher already expects, so an imported assignment is immediately linkable to a
notebook of that name.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone as _utc

from .canvas import CanvasAssignment, CanvasClient, CanvasCourse, CanvasError
from .notion import Course, NotionClient

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Chicago"

# Canvas has no "kind of thing" field, so the Notion Type select is inferred
# from the assignment itself.
_EXAM_RE = re.compile(r"\b(exam|midterm)\b", re.I)
_QUIZ_RE = re.compile(r"\b(quiz|clicker)\b", re.I)
_LAB_RE = re.compile(r"\blabs?\b", re.I)
_PROJECT_RE = re.compile(r"\b(project|capstone)\b", re.I)

# "Final" on its own means the final exam. Attached to a deliverable it does
# not: a "Final Project Proposal" is a project, and typing that as an exam puts
# a test that does not exist on the course's countdown.
_FINAL_RE = re.compile(r"\bfinals?\b", re.I)
_FINAL_OF_SOMETHING_RE = re.compile(
    r"\b(project|paper|report|draft|presentation|essay|portfolio|proposal|"
    r"submission|deliverable|writeup)\b",
    re.I,
)


def _is_exam(name: str) -> bool:
    if _EXAM_RE.search(name):
        return True
    return bool(_FINAL_RE.search(name)) and not _FINAL_OF_SOMETHING_RE.search(name)


def assignment_type(item: CanvasAssignment) -> str:
    """Best guess at the Notion ``Type`` select for a Canvas assignment."""
    name = item.name
    if item.is_quiz or "online_quiz" in item.submission_types:
        # Midterms are very often built as Canvas quiz objects, so the name
        # still gets the last word.
        return "Exam" if _is_exam(name) else "Quiz"
    if _EXAM_RE.search(name):
        return "Exam"
    if _QUIZ_RE.search(name):
        return "Quiz"
    if _LAB_RE.search(name):
        return "Lab"
    if _PROJECT_RE.search(name):
        return "Project"
    if _is_exam(name):
        return "Exam"
    return "Assignment"


def build_title(code: str | None, name: str) -> str:
    """``"CS 411 | Homework 3"``, or just the name when there is no code.

    Titling this way is not decoration. The GoodNotes matcher treats a course
    code as a veto, so a prefixed title can never be linked to another course's
    notebook by accident.
    """
    name = name.strip()
    if not code:
        return name
    if name.upper().startswith(code.upper()):
        # Already prefixed by the professor; don't stutter.
        return name
    return f"{code} | {name}"


def adoption_key(title: str) -> str:
    """A conservative key for recognising a row someone typed by hand.

    Deliberately *not* ``matching.normalize()``. That one is built for
    filenames: it drops the words "notes", "copy", "for", maps ``hw`` to
    ``homework`` and roman numerals to digits. Under it, an existing page
    called "CS 411 Notes for Homework 3" is indistinguishable from the
    assignment "CS 411 | Homework 3" -- so the import would write a Canvas id
    onto the notes page and never create the assignment row.

    Here, only case, accents and punctuation are flattened. Nothing is dropped.
    """
    folded = unicodedata.normalize("NFKD", title.casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", folded).split())


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - missing tzdata on a slim runtime
        log.warning("No timezone data for %r; leaving due dates in UTC", name)
        return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def local_due(due_at: str | None, tz_name: str = DEFAULT_TIMEZONE) -> str | None:
    """Canvas' UTC ``due_at`` as a local ISO string with an offset.

    Canvas stores an 11:59pm Central deadline as ``2026-09-14T04:59:59Z`` --
    the next calendar day. Handing that to Notion unconverted moves every
    deadline a day later in the calendar view, which is the kind of wrong that
    makes someone miss a hand-in. The offset is kept in the output so Notion
    never has to guess.
    """
    parsed = _parse_iso(due_at)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_utc.utc)
    zone = _zone(tz_name)
    if zone is None:
        return parsed.isoformat()
    return parsed.astimezone(zone).isoformat()


def _is_date_only(value: str) -> bool:
    return "t" not in value.lower()


def same_moment(left: str | None, right: str | None) -> bool:
    """Whether two ISO strings mean the same time, ignoring formatting.

    Notion echoes back ``2026-09-13T23:59:59.000-05:00`` for what was sent as
    ``2026-09-13T23:59:59-05:00``. String equality would call every row changed
    and rewrite the whole database on every run.
    """
    if not left or not right:
        return not left and not right
    a, b = _parse_iso(left), _parse_iso(right)
    if a is None or b is None:
        return left == right
    if _is_date_only(left) or _is_date_only(right):
        # A bare "2026-09-13" asserts nothing about the time; compare only
        # what both sides actually claim.
        return a.date() == b.date()
    if (a.tzinfo is None) != (b.tzinfo is None):
        # A datetime with no offset is not a bare date. Falling back to a
        # date comparison here would hide a deadline moved from 9am to
        # midnight on the same day -- reported "unchanged", never written.
        if a.tzinfo is None:
            a = a.replace(tzinfo=b.tzinfo)
        else:
            b = b.replace(tzinfo=a.tzinfo)
    return a == b


@dataclass
class ImportRow:
    canvas_id: str
    title: str
    due: str | None
    code: str
    action: str  # created | updated | adopted | unchanged | skipped
    detail: str = ""

    @property
    def written(self) -> bool:
        return self.action in ("created", "updated", "adopted")


@dataclass
class CourseLink:
    course: CanvasCourse
    code: str | None
    notion: Course | None
    reason: str = ""


@dataclass
class ImportReport:
    links: list[CourseLink] = field(default_factory=list)
    rows: list[ImportRow] = field(default_factory=list)
    known_codes: list[str] = field(default_factory=list)
    errors: list[tuple[CanvasCourse, str]] = field(default_factory=list)
    dry_run: bool = False

    def _by(self, action: str) -> list[ImportRow]:
        return [r for r in self.rows if r.action == action]

    @property
    def created(self) -> list[ImportRow]:
        return self._by("created")

    @property
    def updated(self) -> list[ImportRow]:
        return self._by("updated")

    @property
    def adopted(self) -> list[ImportRow]:
        return self._by("adopted")

    @property
    def unchanged(self) -> list[ImportRow]:
        return self._by("unchanged")

    @property
    def skipped(self) -> list[ImportRow]:
        return self._by("skipped")

    @property
    def unmatched_courses(self) -> list[CourseLink]:
        return [link for link in self.links if link.notion is None]

    def to_text(self) -> str:
        verb = "Would " if self.dry_run else ""
        lines: list[str] = []

        matched = [link for link in self.links if link.notion is not None]
        lines.append(
            f"{len(self.links)} Canvas course(s), {len(matched)} matched to Notion, "
            f"{len(self.rows)} assignment(s) seen"
        )
        lines.append("")

        for label, rows in (
            (f"{verb}create" if self.dry_run else "Created", self.created),
            (f"{verb}update" if self.dry_run else "Updated", self.updated),
            (f"{verb}adopt" if self.dry_run else "Adopted", self.adopted),
        ):
            if not rows:
                continue
            lines.append(f"{label.capitalize()} ({len(rows)}):")
            for row in rows:
                when = row.due[:16].replace("T", " ") if row.due else "no due date"
                extra = f"  [{row.detail}]" if row.detail else ""
                lines.append(f"  {row.title}\n         {when}{extra}")
            lines.append("")

        if self.unchanged:
            lines.append(f"Already up to date ({len(self.unchanged)})")
            lines.append("")

        if self.skipped:
            lines.append(f"Skipped ({len(self.skipped)}):")
            for row in self.skipped[:25]:
                lines.append(f"  {row.title}\n         {row.detail}")
            if len(self.skipped) > 25:
                lines.append(f"  ... and {len(self.skipped) - 25} more")
            lines.append("")

        if self.errors:
            lines.append(f"Courses Canvas would not return ({len(self.errors)}):")
            for course, message in self.errors:
                lines.append(f"  {course.name or course.course_code}\n         {message}")
            lines.append("")

        if self.unmatched_courses:
            lines.append(
                f"Canvas courses with no Notion match ({len(self.unmatched_courses)}) - "
                "set the Code property on the Notion course row to fix:"
            )
            for link in self.unmatched_courses:
                name = link.course.name or link.course.course_code
                lines.append(f"  {name}\n         {link.reason}")
            if self.known_codes:
                lines.append(f"  Notion knows: {', '.join(self.known_codes)}")

        return "\n".join(lines).rstrip()


def _norm_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def link_courses(
    canvas_courses: list[CanvasCourse], notion_courses: list[Course]
) -> list[CourseLink]:
    """Pair each Canvas course with its Notion row, by course code."""
    index: dict[str, Course] = {}
    for course in notion_courses:
        key = _norm_code(course.code)
        if key:
            index.setdefault(key, course)

    links: list[CourseLink] = []
    for course in canvas_courses:
        code = course.code
        if not code:
            links.append(
                CourseLink(
                    course,
                    None,
                    None,
                    reason="no course code could be parsed from Canvas",
                )
            )
            continue
        match = index.get(_norm_code(code))
        links.append(
            CourseLink(
                course,
                code,
                match,
                reason="" if match else f"no Notion course has Code {code!r}",
            )
        )
    return links


@dataclass(frozen=True)
class PropertyNames:
    """Which Notion properties this import writes.

    Every one is overridable because none of them is guaranteed to exist:
    Notion's default title property is called "Name", and a database that has
    no "Type" or "Status" column would get a 400 on the very first create.
    Set a name to "" to stop writing that property at all.
    """

    title: str = "Title"
    canvas_id: str = "Canvas ID"
    canvas_url: str = "Canvas URL"
    due: str = "Due Date"
    course: str = "Course"
    type: str = "Type"
    status: str = "Status"
    new_status: str = "Not started"


def _properties(
    names: PropertyNames,
    *,
    title: str | None = None,
    canvas_id: str | None = None,
    url: str = "",
    due: str | None = None,
    course_page_id: str | None = None,
    type_name: str | None = None,
    status: bool = False,
) -> dict:
    props: dict = {}
    if title is not None and names.title:
        props[names.title] = {"title": [{"text": {"content": title[:2000]}}]}
    if canvas_id is not None and names.canvas_id:
        props[names.canvas_id] = {"rich_text": [{"text": {"content": canvas_id}}]}
    if url and names.canvas_url:
        props[names.canvas_url] = {"url": url}
    if due is not None and names.due:
        props[names.due] = {"date": {"start": due}}
    if course_page_id and names.course:
        props[names.course] = {"relation": [{"id": course_page_id}]}
    if type_name and names.type:
        props[names.type] = {"select": {"name": type_name}}
    if status and names.status and names.new_status:
        props[names.status] = {"status": {"name": names.new_status}}
    return props


def run_canvas_import(
    *,
    canvas: CanvasClient,
    notion: NotionClient,
    assignments_db: str,
    courses_db: str,
    tz_name: str = DEFAULT_TIMEZONE,
    dry_run: bool = False,
    include_undated: bool = True,
    allow_unmatched_courses: bool = False,
    enrollment_state: str = "active",
    names: PropertyNames | None = None,
) -> ImportReport:
    names = names or PropertyNames()

    notion_courses = notion.courses(courses_db)
    canvas_courses = canvas.courses(enrollment_state=enrollment_state)
    links = link_courses(canvas_courses, notion_courses)

    existing = notion.assignments(
        assignments_db,
        title_property=names.title,
        canvas_id_property=names.canvas_id,
        due_property=names.due,
        course_property=names.course,
    )
    by_canvas_id = {a.canvas_id: a for a in existing if a.canvas_id}

    # Adoption index: rows someone typed by hand before the import existed.
    # Keyed strictly (see adoption_key) and only ever consulted when exactly
    # one unclaimed row holds the key.
    by_title: dict[str, list] = {}
    for item in existing:
        if item.canvas_id:
            continue
        by_title.setdefault(adoption_key(item.title), []).append(item)

    report = ImportReport(
        links=links,
        dry_run=dry_run,
        known_codes=sorted({c.code for c in notion_courses if c.code}),
    )

    for link in links:
        if link.notion is None and not allow_unmatched_courses:
            continue

        # One unreadable course must not discard the work already done for the
        # others, nor the report that says what that work was.
        try:
            items = canvas.assignments(link.course.id)
        except CanvasError as exc:
            link.reason = f"could not read assignments: {exc}"
            report.errors.append((link.course, str(exc)))
            continue

        for item in items:
            code = link.code or ""
            title = build_title(link.code, item.name)
            due = local_due(item.due_at, tz_name)
            canvas_id = str(item.id)

            if due is None and not include_undated:
                report.rows.append(
                    ImportRow(
                        canvas_id, title, None, code, "skipped", "no due date in Canvas"
                    )
                )
                continue

            known = by_canvas_id.get(canvas_id)
            if known is None:
                key = adoption_key(title)
                candidates = by_title.get(key, [])
                # Never adopt when two rows share the key: which one is right
                # is unknowable, and picking either writes a Canvas id onto a
                # row that then looks correct and is not.
                if len(candidates) == 1:
                    adoptee = candidates[0]
                    course_id = (
                        link.notion.page_id
                        if link.notion and not adoptee.course
                        else None
                    )
                    if not dry_run:
                        notion.update_properties(
                            adoptee.page_id,
                            _properties(
                                names,
                                canvas_id=canvas_id,
                                url=item.html_url,
                                due=due,
                                course_page_id=course_id,
                            ),
                        )
                    # Claim it, in this run's own bookkeeping as well as
                    # Notion's. `existing` is a snapshot: without both of these
                    # a second Canvas assignment with the same title adopts the
                    # same row, the first assignment silently gets no row at
                    # all, and next run creates a duplicate for it.
                    adoptee.canvas_id = canvas_id
                    by_canvas_id[canvas_id] = adoptee
                    by_title.pop(key, None)
                    report.rows.append(
                        ImportRow(
                            canvas_id,
                            adoptee.title,
                            due,
                            code,
                            "adopted",
                            "matched an existing row by title",
                        )
                    )
                    continue

                if not dry_run:
                    notion.create_page(
                        assignments_db,
                        _properties(
                            names,
                            title=title,
                            canvas_id=canvas_id,
                            url=item.html_url,
                            due=due,
                            course_page_id=link.notion.page_id if link.notion else None,
                            type_name=assignment_type(item),
                            status=True,
                        ),
                    )
                report.rows.append(ImportRow(canvas_id, title, due, code, "created"))
                continue

            # `due is None` means Canvas has no date for this assignment, not
            # that the date was cleared. Treating absence as a change would
            # rewrite the row on every run and report it as updated forever,
            # and would throw away a date the user typed in by hand.
            if due is None or same_moment(known.due, due):
                report.rows.append(
                    ImportRow(
                        canvas_id, known.title, known.due or None, code, "unchanged"
                    )
                )
                continue

            # The due date moved. Only the date and the Canvas link are
            # rewritten: the title is left exactly as it is, because renaming a
            # row to match a GoodNotes notebook is a thing the user does on
            # purpose and an import must never undo it.
            if not dry_run:
                notion.update_properties(
                    known.page_id,
                    _properties(names, url=item.html_url, due=due),
                )
            was = (known.due or "none")[:16].replace("T", " ")
            known.due = due
            report.rows.append(
                ImportRow(canvas_id, known.title, due, code, "updated", f"was {was}")
            )

    return report
