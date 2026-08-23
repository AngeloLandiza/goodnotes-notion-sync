"""End-to-end sync behaviour against stub clients."""

import pytest

from goodnotes_notion_sync.matching import Candidate
from goodnotes_notion_sync.notion import Assignment
from goodnotes_notion_sync.sync import run_sync


class FakeNotion:
    def __init__(self, assignments):
        self._assignments = assignments
        self.writes = []

    def assignments(self, database_id, *, title_property="Title", url_property="Notes PDF"):
        return list(self._assignments)

    def set_url(self, page_id, property_name, url):
        self.writes.append((page_id, property_name, url))


class FakeDrive:
    def __init__(self, candidates):
        self._candidates = candidates

    def list_pdfs(self, folder_id):
        return list(self._candidates)


def assignment(page_id, title, notes=None):
    return Assignment(page_id=page_id, title=title, url="", notes_pdf=notes)


def pdf(ident, name):
    return Candidate(id=ident, name=name, url=f"https://drive.google.com/file/d/{ident}")


def sync(notion, drive, **kwargs):
    return run_sync(
        notion=notion,
        drive=drive,
        database_id="db",
        folder_id="folder",
        **kwargs,
    )


def test_happy_path_writes_the_link():
    notion = FakeNotion([assignment("p1", "CS 411 | Homework 3")])
    drive = FakeDrive([pdf("f1", "CS 411 Homework 3.pdf")])

    report = sync(notion, drive)

    assert notion.writes == [
        ("p1", "Notes PDF", "https://drive.google.com/file/d/f1")
    ]
    assert len(report.linked) == 1
    assert not report.orphan_files


def test_dry_run_writes_nothing():
    notion = FakeNotion([assignment("p1", "CS 411 | Homework 3")])
    drive = FakeDrive([pdf("f1", "CS 411 Homework 3.pdf")])

    report = sync(notion, drive, dry_run=True)

    assert notion.writes == []
    assert len(report.linked) == 1
    assert report.linked[0].action == "would-link"


def test_existing_links_are_left_alone():
    notion = FakeNotion([assignment("p1", "CS 411 | Homework 3", notes="https://old")])
    drive = FakeDrive([pdf("f1", "CS 411 Homework 3.pdf")])

    report = sync(notion, drive)

    assert notion.writes == []
    assert len(report.already) == 1


def test_force_relinks():
    notion = FakeNotion([assignment("p1", "CS 411 | Homework 3", notes="https://old")])
    drive = FakeDrive([pdf("f1", "CS 411 Homework 3.pdf")])

    sync(notion, drive, force=True)

    assert notion.writes[0][2] == "https://drive.google.com/file/d/f1"


def test_one_pdf_cannot_serve_two_assignments():
    notion = FakeNotion(
        [
            assignment("p1", "CS 411 | Homework 3"),
            assignment("p2", "Homework 3"),
        ]
    )
    drive = FakeDrive([pdf("f1", "CS 411 Homework 3.pdf")])

    report = sync(notion, drive)

    assert len(notion.writes) == 1
    assert len(report.unmatched) == 1
    assert "already claimed" in report.unmatched[0].reason


def test_stronger_title_wins_the_shared_pdf():
    """The exact match must win, regardless of assignment order."""
    notion = FakeNotion(
        [
            assignment("weak", "Homework 3"),
            assignment("exact", "CS 411 Homework 3"),
        ]
    )
    drive = FakeDrive([pdf("f1", "CS 411 Homework 3.pdf")])

    sync(notion, drive)

    assert [w[0] for w in notion.writes] == ["exact"]


def test_unmatched_assignment_is_reported_not_written():
    notion = FakeNotion([assignment("p1", "CS 411 | Homework 3")])
    drive = FakeDrive([pdf("f1", "Grocery list.pdf")])

    report = sync(notion, drive)

    assert notion.writes == []
    assert len(report.unmatched) == 1
    assert len(report.orphan_files) == 1


def test_orphan_files_are_listed_for_renaming():
    notion = FakeNotion([assignment("p1", "CS 411 | Homework 3")])
    drive = FakeDrive(
        [pdf("f1", "CS 411 Homework 3.pdf"), pdf("f2", "Random doodles.pdf")]
    )

    report = sync(notion, drive)

    assert [c.name for c in report.orphan_files] == ["Random doodles.pdf"]


def test_report_text_mentions_counts():
    notion = FakeNotion([assignment("p1", "CS 411 | Homework 3")])
    drive = FakeDrive([pdf("f1", "CS 411 Homework 3.pdf")])

    text = sync(notion, drive, dry_run=True).to_text(dry_run=True)

    assert "Would link" in text
    assert "CS 411 | Homework 3" in text


def test_empty_drive_folder_is_not_an_error():
    notion = FakeNotion([assignment("p1", "CS 411 | Homework 3")])
    report = sync(notion, FakeDrive([]))

    assert report.total_files == 0
    assert len(report.unmatched) == 1
