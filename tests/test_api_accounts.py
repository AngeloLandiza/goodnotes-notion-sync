"""The accounts HTTP surface, driven over real sockets against real Postgres.

These are the tests that would have caught an authorisation mistake. Each one
is phrased as the attack it prevents.
"""

import importlib.util
import json
import os
import pathlib
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from goodnotes_notion_sync import webauth
from goodnotes_notion_sync.crypto import generate_key
from goodnotes_notion_sync.store import GOOGLE, NOTION, Store

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEY = generate_key()
SECRET = "session-secret-for-tests"


def load(name: str):
    """Import an api/ handler the way Vercel does: by file."""
    sys.path.insert(0, str(ROOT / "api"))
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(name, ROOT / "api" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app_env(monkeypatch):
    if not TEST_DSN:
        pytest.skip("set TEST_DATABASE_URL to run the accounts tests")
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("APP_ENCRYPTION_KEY", KEY)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("APP_BASE_URL", "https://example.test")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_ID", "notion-client")
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_SECRET", "notion-secret")
    monkeypatch.delenv("APP_TOKEN", raising=False)
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.delenv("OWNER_EMAIL", raising=False)

    instance = Store.from_env()
    with instance._cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS runs, connections, settings, invites, users,"
            " schema_migrations CASCADE"
        )
    instance.migrate()
    yield instance
    with instance._cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS runs, connections, settings, invites, users,"
            " schema_migrations CASCADE"
        )


def serve(module):
    httpd = HTTPServer(("127.0.0.1", 0), module.handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def call(url, *, method="GET", cookie=None, csrf=None, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    if cookie:
        req.add_header("Cookie", cookie)
    if csrf:
        req.add_header("X-CSRF-Token", csrf)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req) as response:
            raw = response.read()
            return response.status, _maybe_json(raw), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, _maybe_json(exc.read()), dict(exc.headers)


def _maybe_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw.decode("utf-8", "replace")[:400]}


def signed_in(store, email="member@example.com", owner=False):
    store.add_invite(email)
    user = store.upsert_google_user(sub=f"sub-{email}", email=email)
    if owner:
        store.ensure_owner(email)
        user = store.get_user(user.id)
    token, session = webauth.new_session(user.id, user.email, SECRET)
    return user, f"{webauth.SESSION_COOKIE}={token}", session.csrf


# -- /api/me ---------------------------------------------------------------


def test_me_signed_out_offers_a_login_url(app_env):
    httpd, base = serve(load("me"))
    try:
        status, body, _ = call(f"{base}/api/me")
    finally:
        httpd.shutdown()

    assert status == 200
    assert body["signedIn"] is False
    assert body["loginUrl"] == "/api/auth/google/start"


def test_me_never_returns_a_credential(app_env):
    """The dashboard payload is the widest thing this app renders."""
    user, cookie, _ = signed_in(app_env)
    app_env.set_connection(user.id, NOTION, "ntn_super_secret", {"workspace_name": "W"})

    httpd, base = serve(load("me"))
    try:
        status, body, _ = call(f"{base}/api/me", cookie=cookie)
    finally:
        httpd.shutdown()

    assert status == 200
    assert body["connections"]["notion"]["connected"] is True
    assert "ntn_super_secret" not in json.dumps(body)


def test_me_does_not_show_the_invite_list_to_a_member(app_env):
    _, cookie, _ = signed_in(app_env)
    app_env.add_invite("someone.else@example.com")

    httpd, base = serve(load("me"))
    try:
        _, body, _ = call(f"{base}/api/me", cookie=cookie)
    finally:
        httpd.shutdown()

    assert "invites" not in body
    assert "someone.else@example.com" not in json.dumps(body)


def test_a_forged_session_cookie_is_not_signed_in(app_env):
    user, _, _ = signed_in(app_env)
    forged = webauth.sign({"uid": user.id, "email": user.email}, "wrong-secret", ttl=600)

    httpd, base = serve(load("me"))
    try:
        _, body, _ = call(
            f"{base}/api/me", cookie=f"{webauth.SESSION_COOKIE}={forged}"
        )
    finally:
        httpd.shutdown()

    assert body["signedIn"] is False


def test_a_session_for_a_deleted_account_is_not_signed_in(app_env):
    user, cookie, _ = signed_in(app_env)
    with app_env._cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (user.id,))

    httpd, base = serve(load("me"))
    try:
        _, body, _ = call(f"{base}/api/me", cookie=cookie)
    finally:
        httpd.shutdown()

    assert body["signedIn"] is False


# -- /api/settings ---------------------------------------------------------


def test_settings_requires_a_csrf_token(app_env):
    """The session cookie alone must not be enough to change anything."""
    _, cookie, _ = signed_in(app_env)

    httpd, base = serve(load("settings"))
    try:
        status, body, _ = call(
            f"{base}/api/settings",
            method="POST",
            cookie=cookie,
            body={"assignmentsDb": "hijacked"},
        )
    finally:
        httpd.shutdown()

    assert status == 403
    assert "CSRF" in body["error"]


def test_settings_saves_with_a_csrf_token(app_env):
    user, cookie, csrf = signed_in(app_env)

    httpd, base = serve(load("settings"))
    try:
        status, body, _ = call(
            f"{base}/api/settings",
            method="POST",
            cookie=cookie,
            csrf=csrf,
            body={"assignmentsDb": "abc", "canvasToken": "canvas-tok"},
        )
    finally:
        httpd.shutdown()

    assert status == 200
    assert body["settings"]["assignmentsDb"] == "abc"
    assert app_env.get_settings(user.id).assignments_db == "abc"


def test_a_canvas_token_posted_here_is_stored_encrypted(app_env):
    user, cookie, csrf = signed_in(app_env)

    httpd, base = serve(load("settings"))
    try:
        call(
            f"{base}/api/settings",
            method="POST",
            cookie=cookie,
            csrf=csrf,
            body={"canvasToken": "canvas-plaintext"},
        )
    finally:
        httpd.shutdown()

    with app_env._cursor() as cur:
        cur.execute("SELECT secret FROM connections WHERE provider = 'canvas'")
        assert b"canvas-plaintext" not in bytes(cur.fetchone()["secret"])


def test_settings_writes_only_the_session_owners_row(app_env):
    """Nothing in the body chooses whose settings are written."""
    victim, _, _ = signed_in(app_env, "victim@example.com")
    _, cookie, csrf = signed_in(app_env, "attacker@example.com")

    httpd, base = serve(load("settings"))
    try:
        call(
            f"{base}/api/settings",
            method="POST",
            cookie=cookie,
            csrf=csrf,
            body={"assignmentsDb": "attacker-value", "user_id": victim.id,
                  "userId": victim.id, "email": "victim@example.com"},
        )
    finally:
        httpd.shutdown()

    assert app_env.get_settings(victim.id).assignments_db == ""


def test_settings_rejects_an_unknown_connection_name(app_env):
    _, cookie, csrf = signed_in(app_env)

    httpd, base = serve(load("settings"))
    try:
        status, _, _ = call(
            f"{base}/api/settings",
            method="POST",
            cookie=cookie,
            csrf=csrf,
            body={"disconnect": "users"},
        )
    finally:
        httpd.shutdown()

    assert status == 400


# -- /api/invites ----------------------------------------------------------


def test_a_member_cannot_invite_anyone(app_env):
    _, cookie, csrf = signed_in(app_env, "member@example.com")

    httpd, base = serve(load("invites"))
    try:
        status, _, _ = call(
            f"{base}/api/invites",
            method="POST",
            cookie=cookie,
            csrf=csrf,
            body={"action": "add", "email": "friend@example.com"},
        )
    finally:
        httpd.shutdown()

    assert status == 404, "a non-owner should not learn that invites exist"
    assert not app_env.is_invited("friend@example.com")


def test_the_owner_can_invite(app_env):
    _, cookie, csrf = signed_in(app_env, "owner@example.com", owner=True)

    httpd, base = serve(load("invites"))
    try:
        status, body, _ = call(
            f"{base}/api/invites",
            method="POST",
            cookie=cookie,
            csrf=csrf,
            body={"action": "add", "email": "Friend@Example.com"},
        )
    finally:
        httpd.shutdown()

    assert status == 200
    assert app_env.is_invited("friend@example.com")
    assert "friend@example.com" in [i["email"] for i in body["invites"]]


def test_inviting_needs_a_csrf_token_too(app_env):
    _, cookie, _ = signed_in(app_env, "owner@example.com", owner=True)

    httpd, base = serve(load("invites"))
    try:
        status, _, _ = call(
            f"{base}/api/invites",
            method="POST",
            cookie=cookie,
            body={"action": "add", "email": "friend@example.com"},
        )
    finally:
        httpd.shutdown()

    assert status == 403
    assert not app_env.is_invited("friend@example.com")


# -- sign-in ---------------------------------------------------------------


class FakeIdentity:
    def __init__(self, email, verified=True):
        self.sub = "google-sub"
        self.email = email
        self.email_verified = verified
        self.name = "Someone"
        self.picture = ""


def start_google(base):
    """Begin a sign-in and return (state, state cookie)."""
    status, _, headers = call(f"{base}/api/auth?action=google-start")
    assert status == 302
    cookie = headers["Set-Cookie"].split(";")[0]
    location = headers["Location"]
    state = location.split("state=")[1].split("&")[0]
    return state, cookie


def test_sign_in_creates_an_account_for_an_invited_address(app_env, monkeypatch):
    app_env.add_invite("invited@example.com")
    module = load("auth")
    monkeypatch.setattr(
        module.oauth,
        "exchange_code_full",
        lambda **kw: {"refresh_token": "google-refresh", "id_token": "x", "scope": "s"},
    )
    monkeypatch.setattr(
        module.oauth, "decode_id_token", lambda _: FakeIdentity("invited@example.com")
    )

    httpd, base = serve(module)
    try:
        state, cookie = start_google(base)
        status, _, headers = call(
            f"{base}/api/auth?action=google-callback&code=abc&state={state}",
            cookie=cookie,
        )
    finally:
        httpd.shutdown()

    assert status == 302 and headers["Location"] == "/"
    user = app_env.user_by_email("invited@example.com")
    assert user is not None
    assert app_env.get_connection(user.id, GOOGLE).secret == "google-refresh"


def test_an_uninvited_address_cannot_sign_in(app_env, monkeypatch):
    """The invite list is the entire access-control model."""
    module = load("auth")
    monkeypatch.setattr(
        module.oauth,
        "exchange_code_full",
        lambda **kw: {"refresh_token": "r", "id_token": "x"},
    )
    monkeypatch.setattr(
        module.oauth, "decode_id_token", lambda _: FakeIdentity("stranger@example.com")
    )

    httpd, base = serve(module)
    try:
        state, cookie = start_google(base)
        status, _, headers = call(
            f"{base}/api/auth?action=google-callback&code=abc&state={state}",
            cookie=cookie,
        )
    finally:
        httpd.shutdown()

    assert status == 302
    assert "not+been+invited" in headers["Location"].replace("%20", "+")
    assert app_env.user_by_email("stranger@example.com") is None
    assert webauth.SESSION_COOKIE not in headers.get("Set-Cookie", "")


def test_an_unverified_google_email_cannot_sign_in(app_env, monkeypatch):
    app_env.add_invite("invited@example.com")
    module = load("auth")
    monkeypatch.setattr(
        module.oauth, "exchange_code_full", lambda **kw: {"id_token": "x"}
    )
    monkeypatch.setattr(
        module.oauth,
        "decode_id_token",
        lambda _: FakeIdentity("invited@example.com", verified=False),
    )

    httpd, base = serve(module)
    try:
        state, cookie = start_google(base)
        status, _, headers = call(
            f"{base}/api/auth?action=google-callback&code=abc&state={state}",
            cookie=cookie,
        )
    finally:
        httpd.shutdown()

    assert status == 302
    assert app_env.user_by_email("invited@example.com") is None


def test_a_callback_without_the_state_cookie_is_refused(app_env, monkeypatch):
    """Otherwise an attacker can hand someone a callback URL carrying their own
    authorisation code and attach their account to the victim's session."""
    app_env.add_invite("invited@example.com")
    module = load("auth")
    called = []
    monkeypatch.setattr(
        module.oauth,
        "exchange_code_full",
        lambda **kw: called.append(kw) or {"id_token": "x"},
    )

    httpd, base = serve(module)
    try:
        state, _ = start_google(base)
        status, _, headers = call(
            f"{base}/api/auth?action=google-callback&code=abc&state={state}"
        )
    finally:
        httpd.shutdown()

    assert status == 302
    assert "expired" in headers["Location"]
    assert called == [], "the code must never be exchanged without matching state"


def test_a_mismatched_state_is_refused(app_env, monkeypatch):
    app_env.add_invite("invited@example.com")
    module = load("auth")
    called = []
    monkeypatch.setattr(
        module.oauth,
        "exchange_code_full",
        lambda **kw: called.append(kw) or {"id_token": "x"},
    )

    httpd, base = serve(module)
    try:
        _, cookie = start_google(base)
        status, _, _ = call(
            f"{base}/api/auth?action=google-callback&code=abc&state=not-the-one",
            cookie=cookie,
        )
    finally:
        httpd.shutdown()

    assert status == 302
    assert called == []


def test_re_authorising_without_a_refresh_token_keeps_the_existing_one(
    app_env, monkeypatch
):
    """Google omits refresh_token when it thinks the client is already
    authorised. Writing that absence through would silently disconnect Drive.
    """
    app_env.add_invite("invited@example.com")
    user = app_env.upsert_google_user(sub="s", email="invited@example.com")
    app_env.set_connection(user.id, GOOGLE, "original-refresh")

    module = load("auth")
    monkeypatch.setattr(
        module.oauth, "exchange_code_full", lambda **kw: {"id_token": "x"}
    )
    monkeypatch.setattr(
        module.oauth, "decode_id_token", lambda _: FakeIdentity("invited@example.com")
    )

    httpd, base = serve(module)
    try:
        state, cookie = start_google(base)
        call(
            f"{base}/api/auth?action=google-callback&code=abc&state={state}",
            cookie=cookie,
        )
    finally:
        httpd.shutdown()

    assert app_env.get_connection(user.id, GOOGLE).secret == "original-refresh"


def test_sign_out_clears_the_cookie(app_env):
    _, cookie, _ = signed_in(app_env)
    httpd, base = serve(load("auth"))
    try:
        status, _, headers = call(f"{base}/api/auth?action=logout", cookie=cookie)
    finally:
        httpd.shutdown()

    assert status == 302
    assert "Max-Age=0" in headers["Set-Cookie"]


# -- the sync endpoints in accounts mode -----------------------------------


def test_sync_without_a_session_is_unauthorised(app_env):
    httpd, base = serve(load("sync"))
    try:
        status, body, _ = call(f"{base}/api/sync?dry=1", method="POST")
    finally:
        httpd.shutdown()

    assert status == 401
    assert body["loginUrl"] == "/api/auth/google/start"


def test_a_new_account_is_told_what_to_connect_rather_than_erroring(app_env):
    """Half-finished setup is the normal state of a new account."""
    _, cookie, csrf = signed_in(app_env)

    httpd, base = serve(load("sync"))
    try:
        status, body, _ = call(
            f"{base}/api/sync?dry=1", method="POST", cookie=cookie, csrf=csrf
        )
    finally:
        httpd.shutdown()

    assert status == 200
    assert body["configured"] is False
    assert "NOTION_TOKEN" in body["missing"]


def test_a_real_run_needs_a_csrf_token(app_env):
    _, cookie, _ = signed_in(app_env)

    httpd, base = serve(load("sync"))
    try:
        status, _, _ = call(f"{base}/api/sync", method="POST", cookie=cookie)
    finally:
        httpd.shutdown()

    assert status == 403


def test_a_dry_run_does_not_need_one(app_env):
    """It reads and never writes, and requiring a token would stop the
    dashboard painting on first load."""
    _, cookie, _ = signed_in(app_env)

    httpd, base = serve(load("sync"))
    try:
        status, _, _ = call(f"{base}/api/sync?dry=1", method="POST", cookie=cookie)
    finally:
        httpd.shutdown()

    assert status == 200


def test_the_bearer_token_still_works_for_the_scheduler(app_env, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-value")

    httpd, base = serve(load("sync"))
    try:
        status, body, _ = call(f"{base}/api/sync?dry=1", token="cron-value")
    finally:
        httpd.shutdown()

    # No environment credentials in the test process, so it reports what is
    # missing -- the point is that it got past authentication.
    assert status in (200, 500)
    assert body.get("who") == "environment" or "missing" in body


def test_a_stale_app_token_cannot_act_as_a_signed_in_user(app_env, monkeypatch):
    """In accounts mode the session is checked first, so a leftover APP_TOKEN
    can never quietly run against somebody's stored credentials."""
    monkeypatch.setenv("APP_TOKEN", "leftover")
    user, cookie, csrf = signed_in(app_env)
    app_env.save_settings(user.id, assignments_db="mine")

    httpd, base = serve(load("sync"))
    try:
        _, body, _ = call(f"{base}/api/sync?dry=1", token="leftover")
    finally:
        httpd.shutdown()

    assert body.get("who") != user.email


def test_a_signed_in_request_is_never_downgraded_to_the_shared_token(
    app_env, monkeypatch
):
    """A browser can carry both a session cookie and an Authorization header.

    If the bearer branch were checked first, that request would run against the
    deployment's environment credentials instead of the signed-in person's --
    writing one account's Canvas assignments into whatever Notion database the
    env vars happen to name.
    """
    monkeypatch.setenv("APP_TOKEN", "leftover")
    monkeypatch.setenv("NOTION_TOKEN", "env-notion")
    monkeypatch.setenv("NOTION_ASSIGNMENTS_DB", "env-db")
    user, cookie, csrf = signed_in(app_env)

    httpd, base = serve(load("sync"))
    try:
        _, body, _ = call(
            f"{base}/api/sync?dry=1",
            method="POST",
            cookie=cookie,
            csrf=csrf,
            token="leftover",
        )
    finally:
        httpd.shutdown()

    assert body.get("who") == user.email, "the session must win over the token"


def test_a_notion_link_cannot_be_redeemed_by_a_different_account(app_env, monkeypatch):
    """The flow is started by whoever is signed in at the time.

    If the callback did not check that it is still the same account, a Notion
    workspace could be attached to the wrong person -- and then that person's
    syncs would read and write someone else's Notion.
    """
    starter, starter_cookie, _ = signed_in(app_env, "starter@example.com")
    other, other_cookie, _ = signed_in(app_env, "other@example.com")

    module = load("auth")
    exchanged = []
    monkeypatch.setattr(
        module.notion_oauth,
        "exchange_code",
        lambda **kw: exchanged.append(kw)
        or module.notion_oauth.NotionGrant(access_token="ntn_x", workspace_name="W"),
    )

    httpd, base = serve(module)
    try:
        status, _, headers = call(
            f"{base}/api/auth?action=notion-start", cookie=starter_cookie
        )
        assert status == 302
        state_cookie = headers["Set-Cookie"].split(";")[0]
        state = headers["Location"].split("state=")[1].split("&")[0]

        # The callback arrives while a *different* account holds the session.
        status, _, _ = call(
            f"{base}/api/auth?action=notion-callback&code=abc&state={state}",
            cookie=f"{other_cookie}; {state_cookie}",
        )
    finally:
        httpd.shutdown()

    assert status == 302
    assert app_env.get_connection(other.id, NOTION) is None
    assert app_env.get_connection(starter.id, NOTION) is None


def test_the_account_that_started_the_notion_flow_gets_the_workspace(
    app_env, monkeypatch
):
    user, cookie, _ = signed_in(app_env, "starter@example.com")

    module = load("auth")
    monkeypatch.setattr(
        module.notion_oauth,
        "exchange_code",
        lambda **kw: module.notion_oauth.NotionGrant(
            access_token="ntn_x", workspace_name="Angelo"
        ),
    )

    httpd, base = serve(module)
    try:
        _, _, headers = call(f"{base}/api/auth?action=notion-start", cookie=cookie)
        state_cookie = headers["Set-Cookie"].split(";")[0]
        state = headers["Location"].split("state=")[1].split("&")[0]
        call(
            f"{base}/api/auth?action=notion-callback&code=abc&state={state}",
            cookie=f"{cookie}; {state_cookie}",
        )
    finally:
        httpd.shutdown()

    conn = app_env.get_connection(user.id, NOTION)
    assert conn.secret == "ntn_x"
    assert conn.metadata["workspace_name"] == "Angelo"
