"""Notion's public-integration OAuth flow.

The single-user version asked people to create an *internal* integration, copy
its token, then open each database and share it with that integration -- five
fiddly steps that are hard to explain and easy to half-finish, and the failure
mode is a confusing "could not find database" much later.

A public integration turns that into one button. The user picks which pages to
share inside Notion's own picker, and the pages they choose are exactly what
the token can see.
"""

from __future__ import annotations

import base64
import urllib.parse
from dataclasses import dataclass

import requests

AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
TOKEN_URL = "https://api.notion.com/v1/oauth/token"


class NotionOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class NotionGrant:
    access_token: str
    workspace_id: str = ""
    workspace_name: str = ""
    workspace_icon: str = ""
    bot_id: str = ""

    @property
    def metadata(self) -> dict:
        """The parts that are safe to show in a UI. No token in here."""
        return {
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "workspace_icon": self.workspace_icon,
            "bot_id": self.bot_id,
        }


def build_auth_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "owner": "user",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    session: requests.Session | None = None,
) -> NotionGrant:
    """Swap the code for a workspace token.

    Notion authenticates this call with HTTP Basic using the client id and
    secret -- not a `client_secret` body field, which is what most other
    providers want and what makes this an easy hour to lose.
    """
    http = session or requests
    basic = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("ascii")
    response = http.post(
        TOKEN_URL,
        json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise NotionOAuthError(
            f"Notion returned a non-JSON response ({response.status_code})"
        ) from exc

    if response.status_code != 200:
        raise NotionOAuthError(
            f"Notion token exchange failed ({response.status_code}): "
            f"{payload.get('error')} - {payload.get('error_description', '')}"
        )

    token = payload.get("access_token")
    if not token:
        raise NotionOAuthError("Notion returned no access_token")

    return NotionGrant(
        access_token=token,
        workspace_id=str(payload.get("workspace_id") or ""),
        workspace_name=str(payload.get("workspace_name") or ""),
        workspace_icon=str(payload.get("workspace_icon") or ""),
        bot_id=str(payload.get("bot_id") or ""),
    )
