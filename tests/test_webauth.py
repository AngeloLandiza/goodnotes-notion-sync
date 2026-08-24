"""Sessions and CSRF. Every test here is about something an attacker tries."""

import time

import pytest

from goodnotes_notion_sync import webauth

SECRET = "a-long-random-session-secret"


def test_a_session_round_trips():
    token, session = webauth.new_session(42, "a@b.c", SECRET)
    read = webauth.read_session(f"{webauth.SESSION_COOKIE}={token}", SECRET)

    assert read.user_id == 42
    assert read.email == "a@b.c"
    assert read.csrf == session.csrf


def test_a_tampered_payload_is_rejected():
    """The whole security model is this one assertion.

    Flip a byte of the user id and the signature no longer matches, so the
    cookie stops being a session rather than becoming someone else's.
    """
    token, _ = webauth.new_session(1, "a@b.c", SECRET)
    payload, _, mac = token.partition(".")
    forged = payload[:-1] + ("A" if payload[-1] != "A" else "B") + "." + mac

    assert webauth.read_session(f"{webauth.SESSION_COOKIE}={forged}", SECRET) is None


def test_a_cookie_signed_with_another_secret_is_rejected():
    token, _ = webauth.new_session(1, "a@b.c", "their-secret")
    assert webauth.read_session(f"{webauth.SESSION_COOKIE}={token}", SECRET) is None


def test_an_expired_session_is_rejected(monkeypatch):
    token = webauth.sign({"uid": 1}, SECRET, ttl=10)
    later = time.time() + 3600
    monkeypatch.setattr(webauth.time, "time", lambda: later)
    assert webauth.unsign(token, SECRET) is None


def test_expiry_is_inside_the_signature(monkeypatch):
    """Moving the clock forward must not be defeatable by editing the cookie.

    `exp` lives in the signed payload, so pushing it out invalidates the MAC.
    """
    token = webauth.sign({"uid": 1}, SECRET, ttl=10)
    payload, _, mac = token.partition(".")
    forged = payload.replace("A", "B") + "." + mac
    assert webauth.unsign(forged, SECRET) is None


@pytest.mark.parametrize(
    "cookie",
    ["", "garbage", "no-dot-here", "a.b", ".", "x." + "y" * 40, "%%%.%%%"],
)
def test_malformed_cookies_are_not_signed_in_rather_than_a_crash(cookie):
    """A public URL sees every kind of junk. All of it means 'not signed in'."""
    assert webauth.read_session(f"{webauth.SESSION_COOKIE}={cookie}", SECRET) is None


def test_no_secret_means_no_session():
    token, _ = webauth.new_session(1, "a@b.c", SECRET)
    assert webauth.read_session(f"{webauth.SESSION_COOKIE}={token}", "") is None


def test_signing_with_an_empty_secret_is_refused():
    """Otherwise a missing SESSION_SECRET would mint forgeable sessions."""
    with pytest.raises(ValueError):
        webauth.sign({"uid": 1}, "", ttl=60)


def test_a_session_for_a_different_cookie_name_is_ignored():
    token, _ = webauth.new_session(1, "a@b.c", SECRET)
    assert webauth.read_session(f"other={token}", SECRET) is None


def test_csrf_requires_an_exact_match():
    _, session = webauth.new_session(1, "a@b.c", SECRET)

    assert webauth.csrf_ok(session, session.csrf)
    assert not webauth.csrf_ok(session, session.csrf + "x")
    assert not webauth.csrf_ok(session, "")
    assert not webauth.csrf_ok(session, None)
    assert not webauth.csrf_ok(None, session.csrf)


def test_a_session_with_no_csrf_never_passes_the_check():
    """A hand-made cookie must not be able to opt out of CSRF by omission."""
    token = webauth.sign({"uid": 1}, SECRET, ttl=60)
    session = webauth.read_session(f"{webauth.SESSION_COOKIE}={token}", SECRET)
    assert session.csrf == ""
    assert not webauth.csrf_ok(session, "")


def test_cookies_are_httponly_and_samesite_lax():
    header = webauth.set_cookie("x", "y", max_age=60)
    assert "HttpOnly" in header
    assert "Secure" in header
    # Strict would withhold the cookie on the OAuth provider's redirect back,
    # so the callback could never find its own state.
    assert "SameSite=Lax" in header


def test_clearing_a_cookie_expires_it_immediately():
    assert "Max-Age=0" in webauth.clear_cookie("x")
