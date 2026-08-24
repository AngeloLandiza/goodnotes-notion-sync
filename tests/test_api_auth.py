"""The Vercel handlers sit on a public URL. These are the security tests."""

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT))

import _shared  # noqa: E402


class Headers(dict):
    """Stand-in for http.client.HTTPMessage, which is dict-like via .get()."""

    def get(self, key, default=None):
        return dict.get(self, key, default)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("APP_TOKEN", raising=False)
    monkeypatch.delenv("CRON_SECRET", raising=False)


def test_fails_closed_when_no_token_configured():
    """An unset token must never mean 'open to everyone'."""
    assert _shared.authorised(Headers({"Authorization": "Bearer anything"})) is False


def test_accepts_app_token(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "s3cret")
    assert _shared.authorised(Headers({"Authorization": "Bearer s3cret"})) is True


def test_accepts_vercel_cron_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-abc")
    assert _shared.authorised(Headers({"Authorization": "Bearer cron-abc"})) is True


def test_both_tokens_work_side_by_side(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "s3cret")
    monkeypatch.setenv("CRON_SECRET", "cron-abc")
    assert _shared.authorised(Headers({"Authorization": "Bearer s3cret"})) is True
    assert _shared.authorised(Headers({"Authorization": "Bearer cron-abc"})) is True


def test_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "s3cret")
    assert _shared.authorised(Headers({"Authorization": "Bearer wrong"})) is False


def test_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "s3cret")
    assert _shared.authorised(Headers({})) is False


def test_rejects_bare_token_without_bearer(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "s3cret")
    assert _shared.authorised(Headers({"Authorization": "s3cret"})) is False


def test_header_name_and_scheme_are_case_insensitive(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "s3cret")
    assert _shared.authorised(Headers({"authorization": "bearer s3cret"})) is True


def test_empty_string_token_is_not_accepted(monkeypatch):
    """A blank APP_TOKEN must not let a blank bearer through."""
    monkeypatch.setenv("APP_TOKEN", "")
    assert _shared.authorised(Headers({"Authorization": "Bearer "})) is False


def test_missing_env_lists_everything_when_unconfigured():
    assert set(_shared.missing_env()) == set(_shared.REQUIRED)


def test_missing_env_is_empty_when_fully_configured(monkeypatch):
    for name in _shared.REQUIRED:
        monkeypatch.setenv(name, "x")
    assert _shared.missing_env() == []


def test_a_non_ascii_token_is_rejected_not_a_crash(monkeypatch):
    """`hmac.compare_digest` raises TypeError on a non-ASCII str.

    The auth check runs *outside* the handler's try block, so this reached the
    platform as FUNCTION_INVOCATION_FAILED with no JSON body -- a crash any
    passer-by could trigger with one curl.
    """
    monkeypatch.setenv("APP_TOKEN", "correct-token")
    assert _shared.authorised(Headers({"Authorization": "Bearer pässwort"})) is False


def test_a_non_ascii_token_can_also_be_the_right_one(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "pässwort")
    assert _shared.authorised(Headers({"Authorization": "Bearer pässwort"})) is True
