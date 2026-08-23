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

    @property
    def has_notes(self) -> bool:
        return bool(self.notes_pdf)


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
                wait = float(response.headers.get("Retry-After", 2 ** attempt))
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
    ) -> list[Assignment]:
        out: list[Assignment] = []
        for page in self.iter_pages(database_id):
            properties = page.get("properties", {})

            title_prop = properties.get(title_property)
            if title_prop is None:
                # Fall back to whichever property is the title.
                title_prop = next(
                    (p for p in properties.values() if p.get("type") == "title"), None
                )
            title = _plain_text((title_prop or {}).get("title"))
            if not title:
                continue

            url_prop = properties.get(url_property) or {}
            notes_pdf = url_prop.get("url")

            course = ""
            course_prop = properties.get(course_property) or {}
            if course_prop.get("type") == "relation":
                course = ", ".join(
                    rel.get("id", "") for rel in course_prop.get("relation", [])
                )

            out.append(
                Assignment(
                    page_id=page["id"],
                    title=title,
                    url=page.get("url", ""),
                    notes_pdf=notes_pdf,
                    course=course,
                )
            )
        log.info("Loaded %d assignment(s) from Notion", len(out))
        return out

    # -- writes -------------------------------------------------------------

    def set_url(self, page_id: str, property_name: str, url: str) -> None:
        self._request(
            "PATCH",
            f"/pages/{page_id}",
            json={"properties": {property_name: {"url": url}}},
        )
