"""Read-only Google Drive access for the GoodNotes backup folder.

GoodNotes' auto-backup writes one PDF per notebook into a folder tree that
mirrors your GoodNotes folders, so the walk here is recursive.

Auth is a user OAuth refresh token rather than a service account: a service
account has its own empty Drive and cannot see your personal files unless you
share into it, which is more setup than it is worth for one person.
"""

from __future__ import annotations

import logging
from typing import Iterator

import requests

from .matching import Candidate

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
FILES_URL = "https://www.googleapis.com/drive/v3/files"

PDF_MIME = "application/pdf"
FOLDER_MIME = "application/vnd.google-apps.folder"

# Read-only is all this tool ever needs.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


class DriveError(RuntimeError):
    pass


class DriveClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        session: requests.Session | None = None,
        timeout: int = 30,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._session = session or requests.Session()
        self._timeout = timeout
        self._access_token: str | None = None

    # -- auth ---------------------------------------------------------------

    def _refresh(self) -> str:
        response = self._session.post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=self._timeout,
        )
        if response.status_code != 200:
            raise DriveError(
                "Google refused the refresh token "
                f"({response.status_code}): {response.text[:300]}"
            )
        token = response.json().get("access_token")
        if not token:
            raise DriveError("Google returned no access_token")
        self._access_token = token
        return token

    def _headers(self) -> dict[str, str]:
        if self._access_token is None:
            self._refresh()
        return {"Authorization": f"Bearer {self._access_token}"}

    # -- files --------------------------------------------------------------

    def _list(self, query: str) -> Iterator[dict]:
        page_token: str | None = None
        while True:
            params = {
                "q": query,
                "fields": "nextPageToken, files(id, name, mimeType, webViewLink, modifiedTime)",
                "pageSize": 200,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "orderBy": "name_natural",
            }
            if page_token:
                params["pageToken"] = page_token

            response = self._session.get(
                FILES_URL, headers=self._headers(), params=params, timeout=self._timeout
            )
            if response.status_code == 401:
                self._refresh()
                response = self._session.get(
                    FILES_URL,
                    headers=self._headers(),
                    params=params,
                    timeout=self._timeout,
                )
            if response.status_code != 200:
                raise DriveError(
                    f"Drive list failed ({response.status_code}): {response.text[:300]}"
                )

            payload = response.json()
            yield from payload.get("files", [])
            page_token = payload.get("nextPageToken")
            if not page_token:
                return

    def list_pdfs(self, folder_id: str, *, max_depth: int = 8) -> list[Candidate]:
        """Every PDF under ``folder_id``, recursively.

        ``path`` on each candidate is the folder trail below the root, which is
        what makes the dry-run report readable when two courses both have a
        "Homework 3".
        """
        found: list[Candidate] = []
        seen_folders: set[str] = set()
        queue: list[tuple[str, str, int]] = [(folder_id, "", 0)]

        while queue:
            current_id, prefix, depth = queue.pop(0)
            if current_id in seen_folders:
                continue
            seen_folders.add(current_id)

            query = f"'{current_id}' in parents and trashed = false"
            for entry in self._list(query):
                mime = entry.get("mimeType", "")
                name = entry.get("name", "")
                if mime == FOLDER_MIME:
                    if depth < max_depth:
                        child_prefix = f"{prefix}/{name}" if prefix else name
                        queue.append((entry["id"], child_prefix, depth + 1))
                    else:
                        log.warning("Stopping at depth %s inside %r", max_depth, name)
                    continue
                # PDFs only, deliberately. When the backup format is set to
                # "both", every notebook also lands as a `.goodnotes` archive
                # with the same stem. Indexing those would give each notebook
                # two candidates scoring identically, and the ambiguity guard
                # in best_match() would then refuse to link anything at all.
                # The PDF is also the only one of the pair Drive and Notion can
                # preview, so it is the copy worth linking to.
                if mime != PDF_MIME and not name.lower().endswith(".pdf"):
                    continue
                found.append(
                    Candidate(
                        id=entry["id"],
                        name=name,
                        url=entry.get("webViewLink")
                        or f"https://drive.google.com/file/d/{entry['id']}/view",
                        path=prefix,
                        modified_time=entry.get("modifiedTime", ""),
                    )
                )

        log.info("Found %d PDF(s) under folder %s", len(found), folder_id)
        return found

    def resolve_folder(self, name_or_id: str) -> str:
        """Accept a folder id, a Drive folder URL, or a folder name."""
        candidate = name_or_id.strip()

        if "drive.google.com" in candidate:
            tail = candidate.rstrip("/").split("/")[-1]
            return tail.split("?")[0]

        # Ids have no spaces and are long; treat anything else as a name.
        if " " not in candidate and len(candidate) > 20:
            return candidate

        escaped = candidate.replace("'", "\\'")
        query = (
            f"mimeType = '{FOLDER_MIME}' and trashed = false and name = '{escaped}'"
        )
        matches = list(self._list(query))
        if not matches:
            raise DriveError(f"No Drive folder named {candidate!r}")
        if len(matches) > 1:
            ids = ", ".join(m["id"] for m in matches)
            raise DriveError(
                f"{len(matches)} folders named {candidate!r}; use an id instead: {ids}"
            )
        return matches[0]["id"]
