"""Minimal Notion REST client -- just the two calls this tool needs."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionError(RuntimeError):
    pass


@dataclass
class Assignment:
    page_id: str
    title: str
    url: str
    notes_pdf: str | None
    course: str = ""
    canvas_id: str = ""
    due: str = ""

    @property
    def has_notes(self) -> bool:
        return bool(self.notes_pdf)


@dataclass
class Course:
    """A row in the Courses database, keyed by its ``Code`` for joining."""

    page_id: str
    name: str
    code: str


def retry_after(header: str | None, fallback: float) -> float:
    """Seconds to wait, from a ``Retry-After`` header.

    RFC 9110 allows an HTTP-date as well as a delay in seconds, and proxies in
    front of these APIs do send one. ``float()`` on it raises ValueError, which
    escapes every caller's except clause and dumps a traceback in place of a
    handled retry.
    """
    try:
        return max(0.0, float(header))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(header)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return fallback
    if when is None:
        return fallback
    from datetime import datetime, timezone as _tz

    now = datetime.now(_tz.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_tz.utc)
    return max(0.0, min((when - now).total_seconds(), 120.0))


def _plain_text(rich: list[dict[str, Any]] | None) -> str:
    if not rich:
        return ""
    return "".join(part.get("plain_text", "") for part in rich).strip()


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        timeout: int = 30,
    ) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        url = f"{API}{path}"
        for attempt in range(5):
            response = self._session.request(
                method, url, headers=self._headers, timeout=self._timeout, **kwargs
            )
            if response.status_code == 429:
                wait = retry_after(response.headers.get("Retry-After"), 2 ** attempt)
                log.warning("Notion rate limit, sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if response.status_code >= 400:
                raise NotionError(
                    f"{method} {path} failed ({response.status_code}): "
                    f"{response.text[:400]}"
                )
            return response.json()
        raise NotionError(f"{method} {path} kept failing after 5 attempts")

    # -- reads --------------------------------------------------------------

    def iter_pages(self, database_id: str) -> Iterator[dict]:
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            payload = self._request(
                "POST", f"/databases/{database_id}/query", json=body
            )
            yield from payload.get("results", [])
            if not payload.get("has_more"):
                return
            cursor = payload.get("next_cursor")

    def assignments(
        self,
        database_id: str,
        *,
        title_property: str = "Title",
        url_property: str = "Notes PDF",
        course_property: str = "Course",
        canvas_id_property: str = "Canvas ID",
        due_property: str = "Due Date",
    ) -> list[Assignment]:
        out: list[Assignment] = []
        for page in self.iter_pages(database_id):
            properties = page.get("properties", {})

            # Fall back to whichever property *is* the title. Checking the
            # type, not just the key, matters: a rich_text column called
            # "Title" alongside a real title called "Name" would otherwise
            # make the whole database read as empty.
            title_prop = properties.get(title_property)
            if not title_prop or title_prop.get("type") != "title":
                title_prop = next(
                    (p for p in properties.values() if p.get("type") == "title"), None
                )
            title = _plain_text((title_prop or {}).get("title"))

            url_prop = properties.get(url_property) or {}
            notes_pdf = url_prop.get("url")

            course = ""
            course_prop = properties.get(course_property) or {}
            if course_prop.get("type") == "relation":
                course = ", ".join(
                    rel.get("id", "") for rel in course_prop.get("relation", [])
                )

            canvas_id = _plain_text(
                (properties.get(canvas_id_property) or {}).get("rich_text")
            )
            if not title and not canvas_id:
                continue
            due = ((properties.get(due_property) or {}).get("date") or {}).get(
                "start"
            ) or ""

            out.append(
                Assignment(
                    page_id=page["id"],
                    title=title,
                    url=page.get("url", ""),
                    notes_pdf=notes_pdf,
                    course=course,
                    canvas_id=canvas_id,
                    due=due,
                )
            )
        log.info("Loaded %d assignment(s) from Notion", len(out))
        return out

    def courses(
        self,
        database_id: str,
        *,
        code_property: str = "Code",
    ) -> list[Course]:
        """Rows from the Courses database, for joining Canvas courses by code."""
        out: list[Course] = []
        for page in self.iter_pages(database_id):
            properties = page.get("properties", {})

            title_prop = next(
                (p for p in properties.values() if p.get("type") == "title"), None
            )
            name = _plain_text((title_prop or {}).get("title"))

            code_prop = properties.get(code_property) or {}
            code = _plain_text(code_prop.get("rich_text"))
            if not code and code_prop.get("type") == "select":
                code = (code_prop.get("select") or {}).get("name", "")

            out.append(Course(page_id=page["id"], name=name, code=code.strip()))
        log.info("Loaded %d course(s) from Notion", len(out))
        return out

    # -- writes -------------------------------------------------------------

    def set_url(self, page_id: str, property_name: str, url: str) -> None:
        self.update_properties(page_id, {property_name: {"url": url}})

    def update_properties(self, page_id: str, properties: dict[str, Any]) -> None:
        self._request("PATCH", f"/pages/{page_id}", json={"properties": properties})

    def create_page(self, database_id: str, properties: dict[str, Any]) -> dict:
        return self._request(
            "POST",
            "/pages",
            json={"parent": {"database_id": database_id}, "properties": properties},
        )
