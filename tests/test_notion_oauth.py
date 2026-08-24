"""Notion's public-integration flow."""

import base64

import pytest

from goodnotes_notion_sync.notion_oauth import (
    NotionOAuthError,
    build_auth_url,
    exchange_code,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        return self._response


def test_the_authorize_url_carries_what_notion_requires():
    url = build_auth_url(
        client_id="abc", redirect_uri="https://app.test/cb", state="s1"
    )
    assert url.startswith("https://api.notion.com/v1/oauth/authorize?")
    for fragment in ("client_id=abc", "response_type=code", "owner=user", "state=s1"):
        assert fragment in url
    assert "redirect_uri=https%3A%2F%2Fapp.test%2Fcb" in url


def test_the_token_exchange_uses_http_basic_auth():
    """Notion authenticates this call with Basic auth, not a client_secret
    body field like most other providers. Getting it wrong is an easy hour."""
    session = FakeSession(FakeResponse({"access_token": "ntn_x", "workspace_name": "W"}))

    exchange_code(
        client_id="id",
        client_secret="secret",
        code="code-1",
        redirect_uri="https://app.test/cb",
        session=session,
    )

    _, body, headers = session.calls[0]
    scheme, _, value = headers["Authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(value).decode() == "id:secret"
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "code-1"
    assert "client_secret" not in body


def test_the_grant_keeps_workspace_details_but_not_in_metadata_secrets():
    session = FakeSession(
        FakeResponse(
            {
                "access_token": "ntn_x",
                "workspace_id": "w1",
                "workspace_name": "Angelo's Notion",
                "bot_id": "b1",
            }
        )
    )
    grant = exchange_code(
        client_id="id", client_secret="s", code="c",
        redirect_uri="https://app.test/cb", session=session,
    )

    assert grant.access_token == "ntn_x"
    assert grant.metadata["workspace_name"] == "Angelo's Notion"
    assert "ntn_x" not in str(grant.metadata), "metadata is rendered in the UI"


def test_an_error_response_is_reported_not_swallowed():
    session = FakeSession(
        FakeResponse({"error": "invalid_grant", "error_description": "expired"}, 400)
    )
    with pytest.raises(NotionOAuthError) as exc:
        exchange_code(
            client_id="id", client_secret="s", code="c",
            redirect_uri="https://app.test/cb", session=session,
        )
    assert "invalid_grant" in str(exc.value)


def test_a_missing_access_token_is_an_error():
    session = FakeSession(FakeResponse({"workspace_id": "w"}))
    with pytest.raises(NotionOAuthError):
        exchange_code(
            client_id="id", client_secret="s", code="c",
            redirect_uri="https://app.test/cb", session=session,
        )
