"""The Notion read/write layer, against a fake session.

This file exists because of a gap a review found: every other test hand-builds
`Assignment` and `Course` objects, so the code that *extracts* them from a
Notion API payload was completely unverified. Idempotency depends entirely on
`canvas_id` and `due` being read back correctly -- get either wrong and every
run duplicates every row, with a green test suite.
"""

import json

import pytest

from goodnotes_notion_sync.notion import NotionClient, NotionError, retry_after


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.requests.append((method, url, kwargs.get("json")))
        return self._responses.pop(0)


def page(properties, page_id="p1"):
    return {"id": page_id, "url": f"https://notion.so/{page_id}", "properties": properties}


def query(pages):
    return FakeResponse({"results": pages, "has_more": False, "next_cursor": None})


def title(text):
    return {"type": "title", "title": [{"plain_text": text}]}


def rich(text):
    return {"type": "rich_text", "rich_text": [{"plain_text": text}]}


def client(responses):
    session = FakeSession(responses)
    return NotionClient("token", session=session), session


# -- reads -----------------------------------------------------------------


def test_canvas_id_and_due_date_are_read_back():
    """The two fields the whole import's idempotency rests on."""
    notion, _ = client(
        [
            query(
                [
                    page(
                        {
                            "Title": title("CS 411 | Homework 3"),
                            "Canvas ID": rich("987"),
                            "Due Date": {
                                "type": "date",
                                "date": {"start": "2026-09-13T23:59:59.000-05:00"},
                            },
                            "Notes PDF": {"type": "url", "url": None},
                        }
                    )
                ]
            )
        ]
    )

    (item,) = notion.assignments("db")

    assert item.canvas_id == "987"
    assert item.due == "2026-09-13T23:59:59.000-05:00"
    assert item.title == "CS 411 | Homework 3"


def test_a_row_with_no_canvas_id_reads_as_empty_not_missing():
    notion, _ = client([query([page({"Title": title("Hand written"), })])])
    (item,) = notion.assignments("db")
    assert item.canvas_id == ""
    assert item.due == ""


def test_an_empty_date_does_not_crash():
    notion, _ = client(
        [query([page({"Title": title("X"), "Due Date": {"type": "date", "date": None}})])]
    )
    (item,) = notion.assignments("db")
    assert item.due == ""


def test_the_title_falls_back_by_type_not_just_by_key():
    """A rich_text column called "Title" next to a real title called "Name".

    Checking only for a missing key made the whole database read as empty --
    zero assignments, so every Canvas assignment looked new, every run.
    """
    notion, _ = client(
        [
            query(
                [
                    page(
                        {
                            "Title": rich("not the title property"),
                            "Name": title("CS 411 | Homework 3"),
                        }
                    )
                ]
            )
        ]
    )

    (item,) = notion.assignments("db")
    assert item.title == "CS 411 | Homework 3"


def test_a_titleless_row_is_kept_when_it_carries_a_canvas_id():
    """Dropping it would hide it from the dedupe index and duplicate it."""
    notion, _ = client(
        [query([page({"Title": {"type": "title", "title": []}, "Canvas ID": rich("42")})])]
    )
    (item,) = notion.assignments("db")
    assert item.canvas_id == "42"
    assert item.title == ""


def test_a_row_with_neither_title_nor_canvas_id_is_dropped():
    notion, _ = client([query([page({"Title": {"type": "title", "title": []}})])])
    assert notion.assignments("db") == []


def test_the_course_relation_is_read_so_adoption_knows_to_fill_it():
    notion, _ = client(
        [
            query(
                [
                    page(
                        {
                            "Title": title("X"),
                            "Course": {
                                "type": "relation",
                                "relation": [{"id": "course-1"}],
                            },
                        }
                    )
                ]
            )
        ]
    )
    (item,) = notion.assignments("db")
    assert item.course == "course-1"


def test_courses_are_read_with_their_code():
    notion, _ = client(
        [
            query(
                [
                    page(
                        {"Course Name": title("Software Architecture"), "Code": rich("CS 411")},
                        page_id="c1",
                    )
                ]
            )
        ]
    )

    (course,) = notion.courses("db")

    assert course.page_id == "c1"
    assert course.code == "CS 411"
    assert course.name == "Software Architecture"


def test_a_code_stored_as_a_select_is_read_too():
    notion, _ = client(
        [
            query(
                [
                    page(
                        {
                            "Course Name": title("ML"),
                            "Code": {"type": "select", "select": {"name": "CS 412"}},
                        }
                    )
                ]
            )
        ]
    )
    (course,) = notion.courses("db")
    assert course.code == "CS 412"


def test_pagination_follows_the_cursor():
    notion, session = client(
        [
            FakeResponse(
                {
                    "results": [page({"Title": title("One")}, "p1")],
                    "has_more": True,
                    "next_cursor": "cursor-2",
                }
            ),
            query([page({"Title": title("Two")}, "p2")]),
        ]
    )

    items = notion.assignments("db")

    assert [i.title for i in items] == ["One", "Two"]
    assert session.requests[1][2]["start_cursor"] == "cursor-2"


# -- writes ----------------------------------------------------------------


def test_create_page_posts_to_the_right_parent():
    notion, session = client([FakeResponse({"id": "new"})])

    notion.create_page("db-id", {"Title": {"title": []}})

    method, url, body = session.requests[0]
    assert (method, url) == ("POST", "https://api.notion.com/v1/pages")
    assert body["parent"] == {"database_id": "db-id"}


def test_update_properties_patches_the_page():
    notion, session = client([FakeResponse({"id": "p1"})])

    notion.update_properties("p1", {"Due Date": {"date": {"start": "2026-09-13"}}})

    method, url, body = session.requests[0]
    assert method == "PATCH"
    assert url.endswith("/pages/p1")
    assert body == {"properties": {"Due Date": {"date": {"start": "2026-09-13"}}}}


def test_a_400_is_raised_with_the_message_notion_gave():
    notion, _ = client([FakeResponse({"message": "Title is not a property"}, status=400)])
    with pytest.raises(NotionError) as exc:
        notion.create_page("db", {})
    assert "Title is not a property" in str(exc.value)


# -- Retry-After -----------------------------------------------------------


def test_retry_after_accepts_seconds():
    assert retry_after("12", 99) == 12.0


def test_retry_after_survives_an_http_date():
    """RFC 9110 allows a date here, and proxies send one.

    `float()` on it raises ValueError, which is not in any caller's except
    clause -- so a rate limit produced a traceback instead of a retry.
    """
    assert retry_after("Mon, 24 Aug 2026 12:00:00 GMT", 7) >= 0.0


def test_retry_after_falls_back_when_absent_or_junk():
    assert retry_after(None, 7) == 7
    assert retry_after("soon", 7) == 7
