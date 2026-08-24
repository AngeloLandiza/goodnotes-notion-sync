"""POST /api/sync   -> run a real sync (dashboard button)
GET  /api/sync   -> run a real sync (Vercel cron)

Both require a bearer token. `?dry=1` reports without writing, which is what
the dashboard loads on first paint.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Vercel loads this file as a module without putting its own directory on
# sys.path, so a bare `from _shared import ...` raises ModuleNotFoundError at
# cold start -- which surfaces as FUNCTION_INVOCATION_FAILED with no usable
# message. Add our directory first.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# An import failure here must still produce JSON. Otherwise the platform
# returns an HTML error page and the dashboard can only show a bare status
# code, which is exactly the debugging dead end this indirection avoids.
_IMPORT_ERROR: str | None = None
try:
    from _shared import ConfigError, authorised, execute, missing_env
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
            # Deliberately terse: do not tell an unauthenticated caller whether
            # the deployment is configured, or which token was wrong.
            self._respond(401, {"ok": False, "error": "Unauthorised"})
            return

        absent = missing_env()
        if absent:
            self._respond(
                500,
                {
                    "ok": False,
                    "error": "Deployment is missing environment variables",
                    "missing": absent,
                },
            )
            return

        query = parse_qs(urlparse(self.path).query)
        dry_run = query.get("dry", ["0"])[0] in ("1", "true", "yes")
        force = query.get("force", ["0"])[0] in ("1", "true", "yes")

        try:
            self._respond(200, execute(dry_run=dry_run, force=force))
        except ConfigError as exc:
            self._respond(500, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface the failure to the UI
            self._respond(
                502,
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            )

    def do_GET(self) -> None:  # Vercel cron
        self._run()

    def do_POST(self) -> None:  # dashboard button
        self._run()

    def log_message(self, *args) -> None:
        """Silence the default stderr access log; Vercel captures it already."""
