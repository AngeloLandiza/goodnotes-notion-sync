"""Read-only Canvas LMS access.

Canvas is the other half of the assignment picture: Notion is where the work
gets planned, Canvas is where the professor publishes it. This module reads
courses and assignments; it never writes back to Canvas.

Auth is a personal access token (Canvas -> Account -> Settings -> New Access
Token). There is no OAuth dance to run: Canvas hands the token straight to the
user, and it is scoped to that user's own enrolments.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests

from .matching import course_code
from .notion import retry_after

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://canvas.uic.edu"

# Link: <https://...&page=2>; rel="next", <https://...>; rel="last"
_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?([a-z]+)"?', re.I)


class CanvasError(RuntimeError):
    pass


def api_root(base_url: str) -> str:
    """Accept whatever shape of URL a person pastes and return the API root.

    ``canvas.uic.edu``, ``https://canvas.uic.edu/``, and
    ``https://canvas.uic.edu/api/v1`` all mean the same thing. Getting this
    wrong produces a 404 on every call with no hint as to why, so it is
    normalised once, here.
    """
    url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if not url:
        url = DEFAULT_BASE_URL
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    if url.endswith("/api/v1"):
        return url
    return f"{url}/api/v1"


def next_link(link_header: str | None) -> str | None:
    """The ``rel="next"`` URL from a Canvas ``Link`` header, if there is one.

    Canvas paginates everything and does *not* report a total, so the only way
    to know you have every assignment is to follow this until it disappears.
    """
    if not link_header:
        return None
    for url, rel in _LINK_RE.findall(link_header):
        if rel.lower() == "next":
            return url
    return None


def _is_throttled(response) -> bool:
    """Canvas reports rate limiting as ``403 Forbidden (Rate Limit Exceeded)``."""
    try:
        return "rate limit" in (response.text or "").lower()
    except Exception:  # noqa: BLE001
        return False


def _despacer(text: str) -> str:
    """Underscores to spaces, so ``course_code()`` can see word boundaries."""
    return text.replace("_", " ")


@dataclass(frozen=True)
class CanvasCourse:
    id: int
    name: str
    course_code: str

    @property
    def code(self) -> str | None:
        """Canonical ``"CS 411"`` code, parsed from whichever field has one.

        UIC's Canvas course_code looks like ``2026_Fall_CS_411_39421`` and the
        name like ``CS 411 Software Architecture``. Either can carry the code,
        and which one does varies by course.

        The underscores have to go first. ``course_code()`` anchors on word
        boundaries, and ``_`` is a word character, so ``CS_411`` has no
        boundary in front of the ``CS`` and the pattern never fires -- the code
        would silently read as absent for every UIC course.
        """
        return course_code(_despacer(self.course_code)) or course_code(
            _despacer(self.name)
        )


@dataclass(frozen=True)
class CanvasAssignment:
    id: int
    course_id: int
    name: str
    due_at: str | None = None
    html_url: str = ""
    points_possible: float | None = None
    submission_types: tuple[str, ...] = ()
    is_quiz: bool = False


def _course(payload: dict[str, Any]) -> CanvasCourse:
    return CanvasCourse(
        id=int(payload["id"]),
        name=str(payload.get("name") or "").strip(),
        course_code=str(payload.get("course_code") or "").strip(),
    )


def _assignment(payload: dict[str, Any], course_id: int) -> CanvasAssignment:
    points = payload.get("points_possible")
    return CanvasAssignment(
        id=int(payload["id"]),
        course_id=course_id,
        name=str(payload.get("name") or "").strip(),
        due_at=payload.get("due_at") or None,
        html_url=str(payload.get("html_url") or ""),
        points_possible=float(points) if isinstance(points, (int, float)) else None,
        submission_types=tuple(payload.get("submission_types") or ()),
        is_quiz=payload.get("quiz_id") is not None,
    )


class CanvasClient:
    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        session: requests.Session | None = None,
        timeout: int = 30,
    ) -> None:
        self._root = api_root(base_url)
        self._session = session or requests.Session()
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    @property
    def root(self) -> str:
        return self._root

    def _get(self, url: str, params: dict[str, Any] | None = None):
        for attempt in range(5):
            response = self._session.get(
                url, headers=self._headers, params=params, timeout=self._timeout
            )
            if response.status_code == 401:
                raise CanvasError(
                    "Canvas rejected the token (401). Generate a new one at "
                    f"{self._root.removesuffix('/api/v1')}/profile/settings "
                    "-> New Access Token, and check CANVAS_BASE_URL points at "
                    "your school's Canvas."
                )
            if response.status_code == 403 and not _is_throttled(response):
                raise CanvasError(
                    "Canvas refused the request (403). The token is valid but "
                    "not allowed to read this - most often a course you are no "
                    "longer enrolled in."
                )
            if (
                response.status_code in (403, 429)
                or response.status_code >= 500
            ):
                # Canvas signals throttling as 403 with "Rate Limit Exceeded"
                # in the body, not 429. Read as a permanent refusal it aborts a
                # large-enrolment run mid-write, with the wrong explanation.
                wait = retry_after(response.headers.get("Retry-After"), 2**attempt)
                log.warning("Canvas %s, sleeping %.1fs", response.status_code, wait)
                time.sleep(wait)
                continue
            if response.status_code >= 400:
                raise CanvasError(
                    f"GET {url} failed ({response.status_code}): "
                    f"{response.text[:400]}"
                )
            return response
        raise CanvasError(f"GET {url} kept failing after 5 attempts")

    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[dict]:
        url: str | None = f"{self._root}{path}"
        query: dict[str, Any] | None = {"per_page": 100, **params}
        while url:
            response = self._get(url, query)
            payload = response.json()
            if isinstance(payload, dict):
                # Canvas returns an object, not a list, for some error shapes.
                raise CanvasError(f"Unexpected response from {url}: {payload}")
            yield from payload
            # The next URL already carries the query string; re-sending params
            # would reset the cursor and loop forever on page 1.
            url = next_link(response.headers.get("Link"))
            query = None

    # -- reads --------------------------------------------------------------

    def courses(self, *, enrollment_state: str = "active") -> list[CanvasCourse]:
        """Courses the user is enrolled in.

        ``enrollment_state=active`` is the difference between this term's six
        courses and every course since freshman year.
        """
        out = [
            _course(item)
            for item in self._paginate(
                "/courses",
                {"enrollment_state": enrollment_state, "include[]": "term"},
            )
            if item.get("id") is not None and not item.get("access_restricted_by_date")
        ]
        log.info("Canvas: %d %s course(s)", len(out), enrollment_state)
        return out

    def assignments(self, course_id: int) -> list[CanvasAssignment]:
        out = [
            _assignment(item, course_id)
            for item in self._paginate(f"/courses/{course_id}/assignments", {})
            if item.get("id") is not None
        ]
        log.info("Canvas: %d assignment(s) in course %s", len(out), course_id)
        return out
