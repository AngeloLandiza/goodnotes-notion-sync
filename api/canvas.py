"""POST /api/canvas  -> import Canvas assignments into Notion

`?dry=1` reports what would change without writing, which is what the
dashboard loads before you press anything.

Canvas is optional. When it is not configured this answers 200 with
`configured: false` rather than an error, so a deployment that only wants the
GoodNotes sync is not broken by this file existing.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Same reason as api/sync.py: Vercel imports this module without its own
# directory on sys.path, and a bare `from _shared import ...` would fail at
# cold start as an opaque FUNCTION_INVOCATION_FAILED.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IMPORT_ERROR: str | None = None
try:
    from _shared import (
        ConfigError,
        authorised,
        canvas_missing_env,
        execute_canvas,
    )
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


class handler(BaseHTTPRequestHandler):
    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _run(self) -> None:
        if _IMPORT_ERROR is not None:
            self._respond(
                500,
                {
                    "ok": False,
                    "error": "The function failed to import its dependencies",
                    "raw": _IMPORT_ERROR,
                },
            )
            return

        if not authorised(self.headers):
            self._respond(401, {"ok": False, "error": "Unauthorised"})
            return

        absent = canvas_missing_env()
        if absent:
            # Not an error: Canvas is an optional half of this project.
            self._respond(
                200,
                {
                    "ok": True,
                    "configured": False,
                    "missing": absent,
                    "error": "Canvas is not configured for this deployment",
                },
            )
            return

        query = parse_qs(urlparse(self.path).query)
        dry_run = query.get("dry", ["0"])[0] in ("1", "true", "yes")

        try:
            payload = execute_canvas(dry_run=dry_run)
            payload["configured"] = True
            self._respond(200, payload)
        except ConfigError as exc:
            self._respond(500, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface the failure to the UI
            self._respond(502, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:
        self._run()

    def do_POST(self) -> None:
        self._run()

    def log_message(self, *args) -> None:
        """Silence the default stderr access log; Vercel captures it already."""
