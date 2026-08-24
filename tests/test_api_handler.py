"""Drive the Vercel handler over real HTTP.

`test_api_auth.py` unit-tests the token check. This file boots the actual
`handler` class on a socket and makes requests, which is the only way to catch
the failure mode that matters in production: the function raising before it can
reply, so the client gets an HTML error page instead of JSON.
"""

import json
import pathlib
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT))

import sync as sync_module  # noqa: E402  (api/sync.py)


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), sync_module.handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def request(url, *, token=None, method="POST"):
    req = urllib.request.Request(url, method=method)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            pytest.fail(
                f"HTTP {exc.code} returned non-JSON, which is what makes the "
                f"dashboard show a bare 'HTTP {exc.code}': {body[:200]!r}"
            )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("APP_TOKEN", "CRON_SECRET"):
        monkeypatch.delenv(name, raising=False)


def test_unauthorised_request_gets_json_not_a_crash(server):
    status, body = request(f"{server}/api/sync", method="POST")
    assert status == 401
    assert body == {"ok": False, "error": "Unauthorised"}


def test_wrong_token_is_rejected(server, monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "right")
    status, body = request(f"{server}/api/sync", token="wrong")
    assert status == 401


def test_cron_get_is_also_gated(server):
    status, _ = request(f"{server}/api/sync", method="GET")
    assert status == 401


def test_missing_config_reports_which_variables(server, monkeypatch):
    """The 500 a real deployment hits first — and it must be actionable."""
    monkeypatch.setenv("APP_TOKEN", "tok")
    status, body = request(f"{server}/api/sync?dry=1", token="tok")

    assert status == 500
    assert body["ok"] is False
    assert "missing" in body, "the response must name the absent variables"
    assert "NOTION_TOKEN" in body["missing"]
    assert "GOOGLE_REFRESH_TOKEN" in body["missing"]


def test_error_responses_are_json_content_type(server):
    req = urllib.request.Request(f"{server}/api/sync", method="POST")
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        assert exc.headers["Content-Type"].startswith("application/json")


def test_handler_imports_standalone():
    """Vercel imports api/sync.py without the repo root on sys.path.

    If `from _shared import ...` only resolves because pytest happened to add
    the directory, the deployed function raises ImportError at cold start and
    the browser sees an HTML 500 with no JSON body at all.
    """
    script = (
        "import sys; sys.path.insert(0, %r); "
        "import sync; print(sync.handler.__name__)" % str(ROOT / "api")
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(ROOT.parent),  # deliberately not the repo root
    )
    assert result.returncode == 0, (
        f"api/sync.py fails to import in isolation:\n{result.stderr}"
    )
    assert "handler" in result.stdout
