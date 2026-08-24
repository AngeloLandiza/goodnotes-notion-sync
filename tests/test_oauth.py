"""Loopback OAuth flow. Nothing here touches the network."""

import base64
import hashlib
import urllib.parse

import pytest

from goodnotes_notion_sync.oauth import (
    OAuthError,
    build_auth_url,
    exchange_code,
    pkce_pair,
)


# -- PKCE -------------------------------------------------------------------


def test_pkce_verifier_length_is_within_rfc7636_bounds():
    verifier, _ = pkce_pair()
    assert 43 <= len(verifier) <= 128


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert challenge == expected


def test_pkce_challenge_has_no_padding():
    """Base64url padding is not allowed in a code_challenge."""
    _, challenge = pkce_pair()
    assert "=" not in challenge


def test_pkce_pairs_are_unique():
    assert pkce_pair()[0] != pkce_pair()[0]


# -- auth URL ---------------------------------------------------------------


def params_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def sample_url(**overrides) -> str:
    kwargs = dict(
        client_id="cid",
        redirect_uri="http://127.0.0.1:5000",
        challenge="chal",
        state="st",
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    kwargs.update(overrides)
    return build_auth_url(**kwargs)


def test_auth_url_requests_offline_access_and_consent():
    """Both are required or Google returns no refresh token."""
    p = params_of(sample_url())
    assert p["access_type"] == "offline"
    assert p["prompt"] == "consent"


def test_auth_url_uses_s256_pkce():
    p = params_of(sample_url())
    assert p["code_challenge"] == "chal"
    assert p["code_challenge_method"] == "S256"


def test_auth_url_carries_state_and_redirect():
    p = params_of(sample_url())
    assert p["state"] == "st"
    assert p["redirect_uri"] == "http://127.0.0.1:5000"
    assert p["response_type"] == "code"


def test_auth_url_requests_readonly_drive_scope():
    """The scope the device flow could not grant, which is why we are here."""
    p = params_of(sample_url())
    assert p["scope"] == "https://www.googleapis.com/auth/drive.readonly"


def test_auth_url_space_joins_multiple_scopes():
    p = params_of(sample_url(scopes=["a", "b"]))
    assert p["scope"] == "a b"


def test_auth_url_points_at_google():
    assert sample_url().startswith("https://accounts.google.com/o/oauth2/v2/auth?")


# -- code exchange ----------------------------------------------------------


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.sent = None

    def post(self, url, data=None, timeout=None):
        self.sent = data
        return self._response


def exchange(session):
    return exchange_code(
        client_id="cid",
        client_secret="secret",
        code="the-code",
        verifier="the-verifier",
        redirect_uri="http://127.0.0.1:5000",
        session=session,
    )


def test_exchange_returns_the_refresh_token():
    session = FakeSession(FakeResponse(200, {"refresh_token": "1//token"}))
    assert exchange(session) == "1//token"


def test_exchange_sends_the_pkce_verifier():
    session = FakeSession(FakeResponse(200, {"refresh_token": "x"}))
    exchange(session)
    assert session.sent["code_verifier"] == "the-verifier"
    assert session.sent["grant_type"] == "authorization_code"


def test_exchange_raises_on_http_error():
    session = FakeSession(
        FakeResponse(400, {"error": "invalid_grant", "error_description": "bad code"})
    )
    with pytest.raises(OAuthError, match="invalid_grant"):
        exchange(session)


def test_missing_refresh_token_explains_the_reauthorisation_fix():
    """A 200 with only an access token means the client was already authorised."""
    session = FakeSession(FakeResponse(200, {"access_token": "ya29.only"}))
    with pytest.raises(OAuthError, match="revoke"):
        exchange(session)


# -- the id_token that says who signed in -----------------------------------

import base64 as _b64  # noqa: E402
import json as _json  # noqa: E402

from goodnotes_notion_sync.oauth import decode_id_token  # noqa: E402


def _id_token(claims: dict) -> str:
    def part(obj):
        raw = _json.dumps(obj).encode()
        return _b64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature-not-checked"


def test_the_identity_comes_out_of_the_claims():
    token = _id_token(
        {
            "sub": "1234",
            "email": "Angelo@Example.com",
            "email_verified": True,
            "name": "Angelo",
            "picture": "https://pic",
        }
    )
    identity = decode_id_token(token)

    assert identity.sub == "1234"
    assert identity.email == "angelo@example.com", "lowercased to match invites"
    assert identity.email_verified is True
    assert identity.name == "Angelo"


def test_email_verified_is_accepted_as_a_string_too():
    """Google has sent this as the string "true" as well as a bool."""
    assert decode_id_token(_id_token({"email": "a@b.c", "email_verified": "true"})).email_verified
    assert not decode_id_token(_id_token({"email": "a@b.c", "email_verified": "false"})).email_verified


def test_an_unverified_email_is_reported_as_unverified():
    identity = decode_id_token(_id_token({"email": "a@b.c", "email_verified": False}))
    assert identity.email_verified is False


def test_a_token_with_no_email_is_rejected():
    """The email is the join to the invite list; without it there is no
    identity to check, so this must fail rather than default to something."""
    with pytest.raises(OAuthError):
        decode_id_token(_id_token({"sub": "1234"}))


@pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.b", "a.b.c.d"])
def test_a_malformed_id_token_is_rejected(bad):
    with pytest.raises(OAuthError):
        decode_id_token(bad)


def test_undecodable_claims_are_rejected():
    with pytest.raises(OAuthError):
        decode_id_token("header.!!!not-base64!!!.sig")
