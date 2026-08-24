"""POST /api/sync   -> link GoodNotes PDFs to Notion assignments
GET  /api/sync   -> same, for the scheduler

Who is asking decides whose data is touched: a signed-in browser runs against
that account's stored credentials, a caller holding the bearer token runs
against the environment's. `?dry=1` reports without writing.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

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
    from _app import BaseHandler, Denied, resolve_caller
    from _shared import ConfigError, execute
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if _IMPORT_ERROR:  # pragma: no cover - only when the bundle is broken
    from http.server import BaseHTTPRequestHandler as BaseHandler  # type: ignore


class handler(BaseHandler):  # type: ignore[misc]
    KIND = "sync"
    # Canvas is the optional half of the project: a deployment that only wants
    # the GoodNotes sync must not be told it is broken for not having it.
    OPTIONAL = False

    def _respond(self, status: int, payload: dict) -> None:
        """Kept for the import-failure path, which cannot rely on _app."""
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _work(self, config, dry_run: bool, force: bool) -> dict:
        return execute(dry_run=dry_run, force=force, config=config)

    def _missing(self, config) -> list[str]:
        return config.missing_for_sync()

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

        query = self.query()
        dry_run = query.get("dry", "0") in ("1", "true", "yes")
        force = query.get("force", "0") in ("1", "true", "yes")

        # A dry run reads but never writes, so it does not need a CSRF token --
        # requiring one would stop the dashboard painting on first load.
        resolved = resolve_caller(self, mutating=not dry_run)
        if isinstance(resolved, Denied):
            self.fail(resolved)
            return
        config, store, user_id = resolved

        absent = self._missing(config)
        if absent:
            # In accounts mode "not set up yet" is the normal state of a new
            # account, not a failure, so the dashboard gets a 200 it can render
            # as a checklist. In single-user mode a missing variable really is
            # a broken deployment -- except for Canvas, which is opt-in.
            friendly = store is not None or self.OPTIONAL
            self.respond(
                200 if friendly else 500,
                {
                    "ok": friendly,
                    "configured": False,
                    "error": "Not configured yet"
                    if friendly
                    else "Deployment is missing environment variables",
                    "missing": absent,
                    "who": config.label,
                },
            )
            return

        started = datetime.now(timezone.utc)
        try:
            payload = self._work(config, dry_run, force)
        except ConfigError as exc:
            self.respond(500, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - surface the failure to the UI
            message = f"{type(exc).__name__}: {exc}"
            if store is not None and user_id and not dry_run:
                store.record_run(
                    user_id, self.KIND, ok=False, started_at=started, error=message
                )
            self.respond(502, {"ok": False, "error": message})
            return

        if store is not None and user_id and not dry_run:
            store.record_run(
                user_id,
                self.KIND,
                ok=True,
                started_at=started,
                summary=payload.get("totals", {}),
            )

        payload["who"] = config.label
        self.respond(200, payload)

    def do_GET(self) -> None:  # scheduler
        self._run()

    def do_POST(self) -> None:  # dashboard button
        self._run()

    def log_message(self, *args) -> None:
        """Silence the default stderr access log; Vercel captures it already."""
