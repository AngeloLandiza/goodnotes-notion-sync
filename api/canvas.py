"""POST /api/canvas -> import Canvas assignments into Notion

Shares every decision with /api/sync -- caller resolution, dry runs, run
history -- and differs only in which job it runs, so it subclasses rather than
repeating the logic.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IMPORT_ERROR: str | None = None
try:
    import sync as sync_function
    from _shared import execute_canvas
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


if _IMPORT_ERROR is None:

    class handler(sync_function.handler):  # type: ignore[misc]
        KIND = "canvas"
        OPTIONAL = True

        def _work(self, config, dry_run: bool, force: bool) -> dict:
            return execute_canvas(dry_run=dry_run, config=config)

        def _missing(self, config) -> list[str]:
            return config.missing_for_canvas()

else:  # pragma: no cover - only when the bundle is broken
    import json
    from http.server import BaseHTTPRequestHandler

    class handler(BaseHTTPRequestHandler):  # type: ignore[misc]
        def _fail(self) -> None:
            body = json.dumps(
                {
                    "ok": False,
                    "error": "The function failed to import its dependencies",
                    "raw": _IMPORT_ERROR,
                }
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _fail

        def log_message(self, *args) -> None:
            pass
