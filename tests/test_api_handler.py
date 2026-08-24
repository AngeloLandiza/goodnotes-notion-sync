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

import canvas as canvas_module  # noqa: E402  (api/canvas.py)
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


@pytest.fixture
def canvas_server():
    httpd = HTTPServer(("127.0.0.1", 0), canvas_module.handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "APP_TOKEN",
        "CRON_SECRET",
        "CANVAS_TOKEN",
        "NOTION_COURSES_DB",
        "NOTION_TOKEN",
        "NOTION_ASSIGNMENTS_DB",
    ):
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


def test_handler_imports_with_nothing_on_sys_path():
    """Load api/sync.py the way Vercel does: by file, with no path help.

    The first version of this test inserted `api/` into sys.path itself, so it
    passed while production raised ModuleNotFoundError on `_shared` at cold
    start. Loading by spec, from an unrelated working directory, reproduces
    what the platform actually does.
    """
    script = (
        "import importlib.util;"
        "spec = importlib.util.spec_from_file_location('sync', %r);"
        "m = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(m);"
        "print(m.handler.__name__)" % str(ROOT / "api" / "sync.py")
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(ROOT.parent),  # deliberately not the repo root
    )
    assert result.returncode == 0, (
        f"api/sync.py cannot load the way Vercel loads it:\n{result.stderr}"
    )
    assert "handler" in result.stdout


def test_import_failure_still_answers_with_json(server, monkeypatch):
    """Even a broken import must not produce an HTML error page."""
    monkeypatch.setattr(sync_module, "_IMPORT_ERROR", "ImportError: boom")
    status, body = request(f"{server}/api/sync", method="POST")

    assert status == 500
    assert body["ok"] is False
    assert body["raw"] == "ImportError: boom"


# -- the Canvas endpoint ---------------------------------------------------


def test_canvas_endpoint_is_gated_too(canvas_server):
    status, body = request(f"{canvas_server}/api/canvas", method="POST")
    assert status == 401
    assert body == {"ok": False, "error": "Unauthorised"}


def test_canvas_without_configuration_is_not_an_error(canvas_server, monkeypatch):
    """A deployment that only wants the GoodNotes sync must not see a 500.

    Canvas is the optional half of this project. Reporting its absence as a
    failure would make an unrelated, working deployment look broken.
    """
    monkeypatch.setenv("APP_TOKEN", "tok")
    status, body = request(f"{canvas_server}/api/canvas?dry=1", token="tok")

    assert status == 200
    assert body["ok"] is True
    assert body["configured"] is False
    assert "CANVAS_TOKEN" in body["missing"]
    assert "NOTION_COURSES_DB" in body["missing"]


def test_canvas_handler_imports_with_nothing_on_sys_path():
    """Load api/canvas.py the way Vercel does: by file, from elsewhere."""
    script = (
        "import importlib.util;"
        "spec = importlib.util.spec_from_file_location('canvas_fn', %r);"
        "m = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(m);"
        "assert m._IMPORT_ERROR is None, m._IMPORT_ERROR;"
        "print(m.handler.__name__)" % str(ROOT / "api" / "canvas.py")
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(ROOT.parent),  # deliberately not the repo root
    )
    assert result.returncode == 0, (
        f"api/canvas.py cannot load the way Vercel loads it:\n{result.stderr}"
    )
    assert "handler" in result.stdout
