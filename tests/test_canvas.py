"""Canvas -> Notion import, entirely offline.

Same rule as the rest of the suite: no network. Canvas is a fake session that
replays recorded-looking payloads, and Notion is a recorder, so the assertions
are about *what would be written*, which is the part that can quietly go wrong.
"""

import pytest

from goodnotes_notion_sync.canvas import (
    CanvasAssignment,
    CanvasClient,
    CanvasCourse,
    CanvasError,
    api_root,
    next_link,
)
from goodnotes_notion_sync.canvas_import import (
    PropertyNames,
    adoption_key,
    assignment_type,
    build_title,
    link_courses,
    local_due,
    run_canvas_import,
    same_moment,
)
from goodnotes_notion_sync.notion import Assignment, Course


# --------------------------------------------------------------------------
# URL and header plumbing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [
        "canvas.uic.edu",
        "https://canvas.uic.edu",
        "https://canvas.uic.edu/",
        "https://canvas.uic.edu/api/v1",
        "  https://canvas.uic.edu/api/v1/  ",
        "",
    ],
)
def test_api_root_normalises_every_shape_people_paste(given):
    assert api_root(given) == "https://canvas.uic.edu/api/v1"


def test_next_link_finds_the_next_page():
    header = (
        '<https://c/api/v1/courses?page=1>; rel="current",'
        '<https://c/api/v1/courses?page=2>; rel="next",'
        '<https://c/api/v1/courses?page=9>; rel="last"'
    )
    assert next_link(header) == "https://c/api/v1/courses?page=2"


def test_next_link_is_none_on_the_last_page():
    assert next_link('<https://c/api/v1/courses?page=9>; rel="last"') is None
    assert next_link(None) is None
    assert next_link("") is None


def test_next_link_tolerates_an_unquoted_rel():
    assert next_link("<https://c/x?page=2>; rel=next") == "https://c/x?page=2"


# --------------------------------------------------------------------------
# Course codes
# --------------------------------------------------------------------------


def test_course_code_survives_uics_underscored_format():
    """The whole join depends on this one string parsing.

    `course_code()` anchors on word boundaries and `_` is a word character, so
    without stripping them `CS_411` reads as having no code at all -- and every
    course would land in the unmatched pile.
    """
    course = CanvasCourse(1, "Software Architecture", "2026_Fall_CS_411_39421")
    assert course.code == "CS 411"


def test_course_code_falls_back_to_the_name():
    course = CanvasCourse(2, "CS 412 Introduction to Machine Learning", "")
    assert course.code == "CS 412"


def test_course_code_is_none_when_nothing_parses():
    assert CanvasCourse(3, "Independent Study", "IND_STUDY").code is None


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload, *, status=200, link=None, text=""):
        self._payload = payload
        self.status_code = status
        self.headers = {"Link": link} if link else {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, params, dict(headers or {})))
        return self._responses.pop(0)


def test_pagination_follows_link_and_does_not_resend_params():
    """Re-sending `page=1` params on the next URL loops forever.

    Canvas puts the cursor in the `Link` URL. Passing the original params
    alongside it overrides the cursor, page 1 comes back again, and the client
    spins until the list stops growing -- or never.
    """
    session = FakeSession(
        [
            FakeResponse(
                [{"id": 1, "name": "One", "course_code": "CS_411"}],
                link='<https://canvas.uic.edu/api/v1/courses?page=2>; rel="next"',
            ),
            FakeResponse([{"id": 2, "name": "Two", "course_code": "CS_412"}]),
        ]
    )
    client = CanvasClient("tok", "canvas.uic.edu", session=session)

    courses = client.courses()

    assert [c.id for c in courses] == [1, 2]
    first_url, first_params, _ = session.calls[0]
    second_url, second_params, _ = session.calls[1]
    assert first_params["per_page"] == 100
    assert second_url == "https://canvas.uic.edu/api/v1/courses?page=2"
    assert second_params is None, "params must not override the pagination cursor"


def test_the_token_goes_in_the_authorization_header():
    session = FakeSession([FakeResponse([])])
    CanvasClient("secret-token", "canvas.uic.edu", session=session).courses()
    assert session.calls[0][2]["Authorization"] == "Bearer secret-token"


def test_a_bad_token_says_how_to_fix_it():
    session = FakeSession([FakeResponse({}, status=401, text="unauthorized")])
    client = CanvasClient("stale", "canvas.uic.edu", session=session)

    with pytest.raises(CanvasError) as exc:
        client.courses()

    message = str(exc.value)
    assert "401" in message
    assert "profile/settings" in message, "point the user at the fix, not the code"


def test_a_rate_limit_is_retried_not_treated_as_a_refusal(monkeypatch):
    """Canvas throttles with `403 Forbidden (Rate Limit Exceeded)`, not 429.

    Read as a permanent refusal, a throttled run aborts halfway through -- with
    rows already written -- and blames the user's enrolment.
    """
    monkeypatch.setattr("goodnotes_notion_sync.canvas.time.sleep", lambda _: None)
    session = FakeSession(
        [
            FakeResponse({}, status=403, text="403 Forbidden (Rate Limit Exceeded)"),
            FakeResponse([{"id": 1, "name": "One", "course_code": "CS_411"}]),
        ]
    )

    courses = CanvasClient("tok", "canvas.uic.edu", session=session).courses()

    assert [c.id for c in courses] == [1]


def test_a_real_403_is_still_a_refusal():
    session = FakeSession([FakeResponse({}, status=403, text="user not authorized")])
    with pytest.raises(CanvasError) as exc:
        CanvasClient("tok", "canvas.uic.edu", session=session).courses()
    assert "403" in str(exc.value)


def test_assignments_are_parsed_into_dataclasses():
    session = FakeSession(
        [
            FakeResponse(
                [
                    {
                        "id": 987,
                        "name": "Homework 3",
                        "due_at": "2026-09-14T04:59:59Z",
                        "html_url": "https://canvas.uic.edu/courses/1/assignments/987",
                        "points_possible": 20,
                        "submission_types": ["online_upload"],
                    }
                ]
            )
        ]
    )
    client = CanvasClient("tok", "canvas.uic.edu", session=session)

    (item,) = client.assignments(1)

    assert item.id == 987
    assert item.course_id == 1
    assert item.name == "Homework 3"
    assert item.due_at == "2026-09-14T04:59:59Z"
    assert item.points_possible == 20
    assert item.submission_types == ("online_upload",)
    assert item.is_quiz is False


# --------------------------------------------------------------------------
# Due dates
# --------------------------------------------------------------------------


def test_due_dates_come_back_in_campus_time_not_utc():
    """An 11:59pm Central deadline is stored by Canvas as 04:59 UTC *the next
    day*. Copied over unconverted, every deadline lands a day late in Notion's
    calendar -- the failure mode that actually makes someone miss a hand-in.
    """
    assert local_due("2026-09-14T04:59:59Z") == "2026-09-13T23:59:59-05:00"


def test_due_dates_respect_daylight_saving():
    # September is CDT (-05:00); January is CST (-06:00).
    assert local_due("2027-01-15T05:59:00Z").endswith("-06:00")
    assert local_due("2026-09-14T04:59:59Z").endswith("-05:00")


def test_no_due_date_stays_absent():
    assert local_due(None) is None
    assert local_due("") is None
    assert local_due("not a date") is None


def test_same_moment_ignores_notions_reformatting():
    """Notion echoes `.000` back. String equality would rewrite every row,
    every run, forever."""
    assert same_moment("2026-09-13T23:59:59-05:00", "2026-09-13T23:59:59.000-05:00")
    assert not same_moment("2026-09-13T23:59:59-05:00", "2026-09-14T23:59:59-05:00")
    assert same_moment(None, None)
    assert not same_moment(None, "2026-09-13T23:59:59-05:00")


def test_same_moment_compares_dates_when_one_side_has_no_time():
    assert same_moment("2026-09-13", "2026-09-13T23:59:59-05:00")


# --------------------------------------------------------------------------
# Titles and types
# --------------------------------------------------------------------------


def test_title_is_prefixed_with_the_course_code():
    assert build_title("CS 411", "Homework 3") == "CS 411 | Homework 3"


def test_title_does_not_stutter_when_canvas_already_prefixed_it():
    assert build_title("CS 411", "CS 411 Homework 3") == "CS 411 Homework 3"


def test_title_without_a_code_is_just_the_name():
    assert build_title(None, "Homework 3") == "Homework 3"


@pytest.mark.parametrize(
    "name, quiz, expected",
    [
        ("Homework 3", False, "Assignment"),
        ("Quiz 2", False, "Quiz"),
        ("Lab 4 Writeup", False, "Lab"),
        ("Final Project Proposal", False, "Project"),
        ("Final Paper", False, "Assignment"),
        ("Final", False, "Exam"),
        ("Final Exam", False, "Exam"),
        ("Midterm Exam", False, "Exam"),
        ("Weekly Check-in", True, "Quiz"),
        # A midterm built as a Canvas quiz object is still an exam.
        ("Midterm 1", True, "Exam"),
    ],
)
def test_type_is_inferred_from_the_assignment(name, quiz, expected):
    item = CanvasAssignment(id=1, course_id=1, name=name, is_quiz=quiz)
    assert assignment_type(item) == expected


# --------------------------------------------------------------------------
# Course linking
# --------------------------------------------------------------------------


def test_courses_link_by_code_ignoring_spacing():
    links = link_courses(
        [CanvasCourse(1, "Software Architecture", "2026_Fall_CS_411_39421")],
        [Course(page_id="c1", name="Software Architecture", code="CS411")],
    )
    assert links[0].notion.page_id == "c1"


def test_an_unlinkable_course_says_why():
    links = link_courses(
        [CanvasCourse(1, "Software Architecture", "2026_Fall_CS_411_39421")],
        [Course(page_id="c1", name="Machine Learning", code="CS 412")],
    )
    assert links[0].notion is None
    assert "CS 411" in links[0].reason


# --------------------------------------------------------------------------
# The import loop
# --------------------------------------------------------------------------


class FakeCanvas:
    def __init__(self, courses, assignments):
        self._courses = courses
        self._assignments = assignments

    def courses(self, *, enrollment_state="active"):
        return list(self._courses)

    def assignments(self, course_id):
        return list(self._assignments.get(course_id, []))


class FakeNotion:
    def __init__(self, assignments=(), courses=()):
        self._assignments = list(assignments)
        self._courses = list(courses)
        self.created = []
        self.updated = []

    def assignments(self, database_id, **kwargs):
        return list(self._assignments)

    def courses(self, database_id, **kwargs):
        return list(self._courses)

    def create_page(self, database_id, properties):
        self.created.append(properties)
        return {"id": f"new-{len(self.created)}"}

    def update_properties(self, page_id, properties):
        self.updated.append((page_id, properties))


CS411 = CanvasCourse(1, "Software Architecture", "2026_Fall_CS_411_39421")
NOTION_CS411 = Course(page_id="course-1", name="Software Architecture", code="CS 411")


def hw(ident=987, name="Homework 3", due="2026-09-14T04:59:59Z"):
    return CanvasAssignment(
        id=ident,
        course_id=1,
        name=name,
        due_at=due,
        html_url=f"https://canvas.uic.edu/courses/1/assignments/{ident}",
    )


def run(notion, canvas, **kwargs):
    return run_canvas_import(
        canvas=canvas,
        notion=notion,
        assignments_db="assign-db",
        courses_db="course-db",
        **kwargs,
    )


def title_of(properties):
    return properties["Title"]["title"][0]["text"]["content"]


def test_a_new_assignment_becomes_a_notion_row():
    notion = FakeNotion(courses=[NOTION_CS411])
    report = run(notion, FakeCanvas([CS411], {1: [hw()]}))

    (props,) = notion.created
    assert title_of(props) == "CS 411 | Homework 3"
    assert props["Due Date"]["date"]["start"] == "2026-09-13T23:59:59-05:00"
    assert props["Course"]["relation"] == [{"id": "course-1"}]
    assert props["Canvas ID"]["rich_text"][0]["text"]["content"] == "987"
    assert props["Canvas URL"]["url"].endswith("/assignments/987")
    assert len(report.created) == 1


def test_dry_run_writes_nothing():
    notion = FakeNotion(courses=[NOTION_CS411])
    report = run(notion, FakeCanvas([CS411], {1: [hw()]}), dry_run=True)

    assert notion.created == []
    assert notion.updated == []
    assert len(report.created) == 1, "still reports what it would have done"


def test_running_twice_does_not_duplicate():
    existing = Assignment(
        page_id="p1",
        title="CS 411 | Homework 3",
        url="",
        notes_pdf=None,
        canvas_id="987",
        due="2026-09-13T23:59:59.000-05:00",
    )
    notion = FakeNotion(assignments=[existing], courses=[NOTION_CS411])

    report = run(notion, FakeCanvas([CS411], {1: [hw()]}))

    assert notion.created == []
    assert notion.updated == []
    assert len(report.unchanged) == 1


def test_a_moved_due_date_is_updated():
    existing = Assignment(
        page_id="p1",
        title="CS 411 | Homework 3",
        url="",
        notes_pdf=None,
        canvas_id="987",
        due="2026-09-13T23:59:59-05:00",
    )
    notion = FakeNotion(assignments=[existing], courses=[NOTION_CS411])

    report = run(
        notion, FakeCanvas([CS411], {1: [hw(due="2026-09-21T04:59:59Z")]})
    )

    (page_id, props) = notion.updated[0]
    assert page_id == "p1"
    assert props["Due Date"]["date"]["start"] == "2026-09-20T23:59:59-05:00"
    assert len(report.updated) == 1


def test_an_update_never_rewrites_the_title():
    """Renaming a row to match a GoodNotes notebook is a deliberate act.

    The GoodNotes matcher links on title, so if the import restored Canvas'
    wording on every run it would silently break the link the user set up --
    and the sync report would blame the notebook.
    """
    existing = Assignment(
        page_id="p1",
        title="CS 411 | HW3 handwritten",  # renamed by hand
        url="",
        notes_pdf=None,
        canvas_id="987",
        due="2026-09-13T23:59:59-05:00",
    )
    notion = FakeNotion(assignments=[existing], courses=[NOTION_CS411])

    run(notion, FakeCanvas([CS411], {1: [hw(due="2026-09-21T04:59:59Z")]}))

    (_, props) = notion.updated[0]
    assert "Title" not in props


def test_a_hand_made_row_is_adopted_rather_than_duplicated():
    existing = Assignment(
        page_id="p1", title="CS 411 Homework 3", url="", notes_pdf=None
    )
    notion = FakeNotion(assignments=[existing], courses=[NOTION_CS411])

    report = run(notion, FakeCanvas([CS411], {1: [hw()]}))

    assert notion.created == [], "must not create a second row for the same work"
    (page_id, props) = notion.updated[0]
    assert page_id == "p1"
    assert props["Canvas ID"]["rich_text"][0]["text"]["content"] == "987"
    assert "Title" not in props
    assert len(report.adopted) == 1


def test_adoption_refuses_when_two_rows_share_a_title():
    """Which of the two is the real one is unknowable, and picking either
    writes a Canvas id onto a row that looks right and is not."""
    duplicates = [
        Assignment(page_id="p1", title="CS 411 | Homework 3", url="", notes_pdf=None),
        Assignment(page_id="p2", title="CS 411 Homework 3", url="", notes_pdf=None),
    ]
    notion = FakeNotion(assignments=duplicates, courses=[NOTION_CS411])

    report = run(notion, FakeCanvas([CS411], {1: [hw()]}))

    assert notion.updated == []
    assert len(report.created) == 1


def test_an_unmatched_course_is_reported_and_skipped():
    notion = FakeNotion(courses=[Course(page_id="c2", name="ML", code="CS 412")])
    report = run(notion, FakeCanvas([CS411], {1: [hw()]}))

    assert notion.created == [], "no Course relation means an invisible row"
    assert len(report.unmatched_courses) == 1
    assert "CS 411" in report.to_text()


def test_unmatched_courses_can_be_imported_on_request():
    notion = FakeNotion(courses=[Course(page_id="c2", name="ML", code="CS 412")])
    run(notion, FakeCanvas([CS411], {1: [hw()]}), allow_unmatched_courses=True)

    (props,) = notion.created
    assert "Course" not in props
    assert title_of(props) == "CS 411 | Homework 3"


def test_undated_assignments_can_be_skipped():
    notion = FakeNotion(courses=[NOTION_CS411])
    report = run(
        notion,
        FakeCanvas([CS411], {1: [hw(due=None)]}),
        include_undated=False,
    )

    assert notion.created == []
    assert len(report.skipped) == 1


def test_undated_assignments_are_imported_by_default():
    notion = FakeNotion(courses=[NOTION_CS411])
    run(notion, FakeCanvas([CS411], {1: [hw(due=None)]}))

    (props,) = notion.created
    assert "Due Date" not in props
    assert title_of(props) == "CS 411 | Homework 3"


def test_the_report_names_the_codes_notion_knows_about():
    notion = FakeNotion(
        courses=[Course(page_id="c2", name="ML", code="CS 412")]
    )
    report = run(notion, FakeCanvas([CS411], {1: [hw()]}))

    text = report.to_text()
    assert "Notion knows: CS 412" in text


def test_canvas_dropping_a_due_date_does_not_churn_the_row():
    """Canvas having no date is absence of information, not a clear instruction.

    Without this, an undated Canvas assignment whose Notion row has a
    hand-typed date is reported as "updated" on every single run, forever.
    """
    existing = Assignment(
        page_id="p1",
        title="CS 411 | Homework 3",
        url="",
        notes_pdf=None,
        canvas_id="987",
        due="2026-09-13T23:59:59-05:00",
    )
    notion = FakeNotion(assignments=[existing], courses=[NOTION_CS411])

    report = run(notion, FakeCanvas([CS411], {1: [hw(due=None)]}))

    assert notion.updated == []
    assert len(report.unchanged) == 1
    assert report.unchanged[0].due == "2026-09-13T23:59:59-05:00"


# --------------------------------------------------------------------------
# Adoption: the cases a review found were wrong
# --------------------------------------------------------------------------


def test_adoption_key_does_not_drop_words_the_way_the_filename_matcher_does():
    """`matching.normalize()` is built for filenames and deletes "notes" and
    "for". Under it, a page called "CS 411 Notes for Homework 3" is
    indistinguishable from the assignment "CS 411 | Homework 3" -- so the
    import would stamp a Canvas id onto the notes page and never create the
    assignment row at all.
    """
    assert adoption_key("CS 411 | Homework 3") == adoption_key("CS 411 Homework 3")
    assert adoption_key("CS 411 | Homework 3") != adoption_key(
        "CS 411 Notes for Homework 3"
    )
    assert adoption_key("Exam 1") != adoption_key("Exam I")
    assert adoption_key("Quiz 2") != adoption_key("Quiz No 2")


def test_a_notes_page_is_not_adopted_as_the_assignment():
    notion = FakeNotion(
        assignments=[
            Assignment(
                page_id="notes", title="CS 411 Notes for Homework 3", url="", notes_pdf=None
            )
        ],
        courses=[NOTION_CS411],
    )

    report = run(notion, FakeCanvas([CS411], {1: [hw()]}))

    assert notion.updated == [], "the notes page must be left alone"
    assert len(report.created) == 1


def test_two_canvas_assignments_cannot_adopt_the_same_row():
    """`existing` is a snapshot read once.

    Without claiming the row inside this run, both assignments adopt it: two
    PATCHes to one page, an arbitrary winner, *no* row created for the loser --
    and a duplicate-looking row for it on the next run instead.
    """
    notion = FakeNotion(
        assignments=[
            Assignment(page_id="p1", title="CS 411 | Discussion", url="", notes_pdf=None)
        ],
        courses=[NOTION_CS411],
    )

    report = run(
        notion,
        FakeCanvas(
            [CS411],
            {1: [hw(100, "Discussion"), hw(200, "Discussion")]},
        ),
    )

    assert len(report.adopted) == 1
    assert len(report.created) == 1, "the second assignment still needs a row"
    assert len(notion.updated) == 1, "one page, written once"


def test_a_row_already_claimed_by_another_canvas_id_is_not_re_adopted():
    notion = FakeNotion(
        assignments=[
            Assignment(
                page_id="p1",
                title="CS 411 | Homework 3",
                url="",
                notes_pdf=None,
                canvas_id="555",  # belongs to a different Canvas assignment
            )
        ],
        courses=[NOTION_CS411],
    )

    report = run(notion, FakeCanvas([CS411], {1: [hw(987)]}))

    assert notion.updated == []
    assert len(report.created) == 1


def test_adoption_fills_in_a_missing_course_relation():
    """A row with no Course is invisible in every course-filtered view.

    Leaving an adopted row that way is exactly the failure this project
    refuses to accept when it skips unmatched courses.
    """
    notion = FakeNotion(
        assignments=[
            Assignment(page_id="p1", title="CS 411 | Homework 3", url="", notes_pdf=None)
        ],
        courses=[NOTION_CS411],
    )

    run(notion, FakeCanvas([CS411], {1: [hw()]}))

    (_, props) = notion.updated[0]
    assert props["Course"]["relation"] == [{"id": "course-1"}]


def test_adoption_leaves_an_existing_course_relation_alone():
    notion = FakeNotion(
        assignments=[
            Assignment(
                page_id="p1",
                title="CS 411 | Homework 3",
                url="",
                notes_pdf=None,
                course="some-other-course",
            )
        ],
        courses=[NOTION_CS411],
    )

    run(notion, FakeCanvas([CS411], {1: [hw()]}))

    (_, props) = notion.updated[0]
    assert "Course" not in props


# --------------------------------------------------------------------------
# Property payloads and names
# --------------------------------------------------------------------------


def test_type_and_status_are_sent_in_the_shapes_notion_accepts():
    notion = FakeNotion(courses=[NOTION_CS411])
    run(notion, FakeCanvas([CS411], {1: [hw(name="Midterm Exam")]}))

    (props,) = notion.created
    assert props["Type"] == {"select": {"name": "Exam"}}
    assert props["Status"] == {"status": {"name": "Not started"}}


def test_a_database_whose_title_is_called_name_still_works():
    """Notion's own default title property is "Name", not "Title".

    Hardcoding "Title" made every create fail with
    `400 Title is not a property that exists`.
    """
    notion = FakeNotion(courses=[NOTION_CS411])
    run(
        notion,
        FakeCanvas([CS411], {1: [hw()]}),
        names=PropertyNames(title="Name"),
    )

    (props,) = notion.created
    assert props["Name"]["title"][0]["text"]["content"] == "CS 411 | Homework 3"
    assert "Title" not in props


def test_properties_the_database_does_not_have_can_be_switched_off():
    notion = FakeNotion(courses=[NOTION_CS411])
    run(
        notion,
        FakeCanvas([CS411], {1: [hw()]}),
        names=PropertyNames(type="", status=""),
    )

    (props,) = notion.created
    assert "Type" not in props and "Status" not in props


# --------------------------------------------------------------------------
# One bad course must not take the run with it
# --------------------------------------------------------------------------


class FlakyCanvas(FakeCanvas):
    def __init__(self, courses, assignments, failing):
        super().__init__(courses, assignments)
        self._failing = failing

    def assignments(self, course_id):
        if course_id == self._failing:
            raise CanvasError("500 from Canvas")
        return super().assignments(course_id)


def test_one_unreadable_course_does_not_discard_the_whole_run():
    """Rows for the other courses are already written by then.

    Letting the error escape throws away the report that says what those writes
    were, and the CLI prints only `error: ...`.
    """
    cs412 = CanvasCourse(2, "Machine Learning", "2026_Fall_CS_412_10101")
    notion = FakeNotion(
        courses=[
            NOTION_CS411,
            Course(page_id="course-2", name="Machine Learning", code="CS 412"),
        ]
    )

    report = run(
        notion,
        FlakyCanvas([CS411, cs412], {1: [hw()], 2: [hw(200, "Project 1")]}, failing=1),
    )

    assert len(report.created) == 1, "the readable course still imported"
    assert report.created[0].title == "CS 412 | Project 1"
    assert len(report.errors) == 1
    assert "500 from Canvas" in report.to_text()


# --------------------------------------------------------------------------
# same_moment negatives (the earlier test could not tell "equal" from "always")
# --------------------------------------------------------------------------


def test_same_moment_says_no_when_the_dates_differ():
    assert not same_moment("2026-09-14", "2026-09-13T23:59:59-05:00")


def test_a_same_day_deadline_move_is_not_hidden():
    """A naive datetime is not a bare date.

    Falling back to comparing dates whenever one side has no offset made a
    move from 9am to midnight on the same day read as unchanged -- reported
    fine, never written.
    """
    assert not same_moment("2026-09-13T23:59:59", "2026-09-13T09:00:00-05:00")
    assert same_moment("2026-09-13T09:00:00", "2026-09-13T09:00:00-05:00")
