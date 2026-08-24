"""Postgres storage: users, invites, connections, settings, run history.

Vercel Postgres no longer exists -- it moved to the Marketplace in December
2024 and existing databases were migrated to Neon. Any Postgres works here;
Neon is simply the one Vercel provisions for you and injects as `DATABASE_URL`.

Two rules run through this module:

* **Secrets are never stored in the clear.** Every credential goes through
  `SecretBox` on the way in and out. There is no code path that writes a token
  without a key, because there is no encryption key fallback.
* **The database is optional.** With no `DATABASE_URL` the whole app falls back
  to the original environment-variable configuration, so a working single-user
  deployment is never broken by this file existing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from .crypto import SecretBox

log = logging.getLogger(__name__)

__all__ = [
    "Connection",
    "Run",
    "Settings",
    "Store",
    "StoreError",
    "User",
    "database_configured",
]

GOOGLE = "google"
NOTION = "notion"
CANVAS = "canvas"
PROVIDERS = (GOOGLE, NOTION, CANVAS)


class StoreError(RuntimeError):
    pass


def database_configured(environ: dict | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return bool(
        (env.get("DATABASE_URL") or env.get("POSTGRES_URL") or "").strip()
    )


@dataclass
class User:
    id: int
    email: str
    google_sub: str = ""
    name: str = ""
    picture: str = ""
    is_owner: bool = False
    created_at: datetime | None = None
    last_login_at: datetime | None = None


@dataclass
class Connection:
    provider: str
    secret: str
    metadata: dict = field(default_factory=dict)
    updated_at: datetime | None = None


@dataclass
class Settings:
    user_id: int = 0
    assignments_db: str = ""
    courses_db: str = ""
    drive_folder_id: str = ""
    campus_timezone: str = "America/Chicago"
    canvas_base_url: str = "https://canvas.uic.edu"
    auto_sync: bool = True

    @property
    def ready_for_goodnotes(self) -> bool:
        return bool(self.assignments_db and self.drive_folder_id)

    @property
    def ready_for_canvas(self) -> bool:
        return bool(self.assignments_db and self.courses_db)


@dataclass
class Run:
    id: int
    user_id: int
    kind: str
    ok: bool
    summary: dict
    error: str
    started_at: datetime
    finished_at: datetime


# Ordered, append-only. Each entry runs once; never edit one that has shipped.
MIGRATIONS: list[tuple[str, str]] = [
    (
        "0001_core",
        """
        CREATE TABLE IF NOT EXISTS users (
            id            BIGSERIAL PRIMARY KEY,
            email         TEXT NOT NULL UNIQUE,
            google_sub    TEXT UNIQUE,
            name          TEXT NOT NULL DEFAULT '',
            picture       TEXT NOT NULL DEFAULT '',
            is_owner      BOOLEAN NOT NULL DEFAULT FALSE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_login_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS invites (
            email      TEXT PRIMARY KEY,
            invited_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS connections (
            user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider   TEXT NOT NULL,
            secret     BYTEA NOT NULL,
            metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, provider)
        );

        CREATE TABLE IF NOT EXISTS settings (
            user_id         BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            assignments_db  TEXT NOT NULL DEFAULT '',
            courses_db      TEXT NOT NULL DEFAULT '',
            drive_folder_id TEXT NOT NULL DEFAULT '',
            campus_timezone TEXT NOT NULL DEFAULT 'America/Chicago',
            canvas_base_url TEXT NOT NULL DEFAULT 'https://canvas.uic.edu',
            auto_sync       BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS runs (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind        TEXT NOT NULL,
            ok          BOOLEAN NOT NULL,
            summary     JSONB NOT NULL DEFAULT '{}'::jsonb,
            error       TEXT NOT NULL DEFAULT '',
            started_at  TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS runs_user_finished
            ON runs (user_id, finished_at DESC);
        """,
    ),
]


def _psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise StoreError(
            "psycopg is required for database-backed mode. "
            "pip install -r requirements.txt"
        ) from exc
    return psycopg, dict_row, Jsonb


class Store:
    def __init__(self, dsn: str, box: SecretBox) -> None:
        if not dsn:
            raise StoreError("No database URL")
        self._dsn = dsn
        self._box = box

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "Store":
        env = environ if environ is not None else os.environ
        dsn = (env.get("DATABASE_URL") or env.get("POSTGRES_URL") or "").strip()
        if not dsn:
            raise StoreError(
                "No DATABASE_URL. Install a Postgres integration from the "
                "Vercel Marketplace (Neon is the default) and it will be "
                "injected for you."
            )
        return cls(dsn, SecretBox.from_env(env))

    @contextlib.contextmanager
    def _cursor(self) -> Iterator[Any]:
        psycopg, dict_row, _ = _psycopg()
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                yield cur

    # -- schema -------------------------------------------------------------

    def migrate(self) -> list[str]:
        """Apply pending migrations. Safe to call on every cold start."""
        applied: list[str] = []
        with self._cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " name TEXT PRIMARY KEY,"
                " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur.execute("SELECT name FROM schema_migrations")
            done = {row["name"] for row in cur.fetchall()}
            for name, sql in MIGRATIONS:
                if name in done:
                    continue
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (name) VALUES (%s)"
                    " ON CONFLICT DO NOTHING",
                    (name,),
                )
                applied.append(name)
        if applied:
            log.info("Applied migrations: %s", ", ".join(applied))
        return applied

    # -- users and invitations ---------------------------------------------

    def ensure_owner(self, email: str) -> None:
        """Seed the invite list so the first sign-in is possible.

        Without this the app is a locked room with the key inside: nobody can
        sign in to invite anybody, including the person who deployed it.
        """
        email = normalise_email(email)
        if not email:
            return
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO invites (email) VALUES (%s) ON CONFLICT DO NOTHING",
                (email,),
            )
            cur.execute(
                "UPDATE users SET is_owner = TRUE WHERE email = %s", (email,)
            )

    def is_invited(self, email: str) -> bool:
        email = normalise_email(email)
        if not email:
            return False
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM invites WHERE email = %s", (email,))
            return cur.fetchone() is not None

    def list_invites(self) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT i.email, i.created_at, u.email AS signed_in_as"
                " FROM invites i"
                " LEFT JOIN users u ON u.email = i.email"
                " ORDER BY i.created_at"
            )
            return list(cur.fetchall())

    def add_invite(self, email: str, invited_by: int | None = None) -> str:
        email = normalise_email(email)
        if "@" not in email:
            raise StoreError(f"{email!r} is not an email address")
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO invites (email, invited_by) VALUES (%s, %s)"
                " ON CONFLICT (email) DO NOTHING",
                (email, invited_by),
            )
        return email

    def remove_invite(self, email: str) -> None:
        email = normalise_email(email)
        with self._cursor() as cur:
            # An owner must not be able to lock themselves out.
            cur.execute(
                "DELETE FROM invites WHERE email = %s AND email NOT IN ("
                " SELECT email FROM users WHERE is_owner)",
                (email,),
            )

    def upsert_google_user(
        self, *, sub: str, email: str, name: str = "", picture: str = ""
    ) -> User:
        email = normalise_email(email)
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (email, google_sub, name, picture, last_login_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (email) DO UPDATE SET
                    google_sub = EXCLUDED.google_sub,
                    name = EXCLUDED.name,
                    picture = EXCLUDED.picture,
                    last_login_at = now()
                RETURNING *
                """,
                (email, sub, name, picture),
            )
            row = cur.fetchone()
            cur.execute(
                "INSERT INTO settings (user_id) VALUES (%s)"
                " ON CONFLICT DO NOTHING",
                (row["id"],),
            )
        return _user(row)

    def get_user(self, user_id: int) -> User | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        return _user(row) if row else None

    def user_by_email(self, email: str) -> User | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE email = %s", (normalise_email(email),)
            )
            row = cur.fetchone()
        return _user(row) if row else None

    def enabled_users(self) -> list[User]:
        """Users the scheduler should sync: invited, and not opted out."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT u.* FROM users u"
                " JOIN invites i ON i.email = u.email"
                " LEFT JOIN settings s ON s.user_id = u.id"
                " WHERE COALESCE(s.auto_sync, TRUE)"
                " ORDER BY u.id"
            )
            return [_user(row) for row in cur.fetchall()]

    # -- credentials --------------------------------------------------------

    def set_connection(
        self,
        user_id: int,
        provider: str,
        secret: str,
        metadata: dict | None = None,
    ) -> None:
        if provider not in PROVIDERS:
            raise StoreError(f"unknown provider {provider!r}")
        if not secret:
            raise StoreError("refusing to store an empty credential")
        _, _, Jsonb = _psycopg()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO connections (user_id, provider, secret, metadata)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, provider) DO UPDATE SET
                    secret = EXCLUDED.secret,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """,
                (user_id, provider, self._box.encrypt(secret), Jsonb(metadata or {})),
            )

    def get_connection(self, user_id: int, provider: str) -> Connection | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT provider, secret, metadata, updated_at FROM connections"
                " WHERE user_id = %s AND provider = %s",
                (user_id, provider),
            )
            row = cur.fetchone()
        if not row:
            return None
        return Connection(
            provider=row["provider"],
            secret=self._box.decrypt(row["secret"]),
            metadata=_as_dict(row["metadata"]),
            updated_at=row["updated_at"],
        )

    def connection_status(self, user_id: int) -> dict[str, dict]:
        """What is connected, with no secrets in the answer.

        This is what the dashboard renders, so it must be impossible for a
        token to reach it -- hence a separate query rather than a filtered
        `get_connection`.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT provider, metadata, updated_at FROM connections"
                " WHERE user_id = %s",
                (user_id,),
            )
            rows = cur.fetchall()
        return {
            row["provider"]: {
                "connected": True,
                "metadata": _as_dict(row["metadata"]),
                "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else "",
            }
            for row in rows
        }

    def delete_connection(self, user_id: int, provider: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM connections WHERE user_id = %s AND provider = %s",
                (user_id, provider),
            )

    # -- settings -----------------------------------------------------------

    def get_settings(self, user_id: int) -> Settings:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM settings WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        if not row:
            return Settings(user_id=user_id)
        return Settings(
            user_id=row["user_id"],
            assignments_db=row["assignments_db"],
            courses_db=row["courses_db"],
            drive_folder_id=row["drive_folder_id"],
            campus_timezone=row["campus_timezone"],
            canvas_base_url=row["canvas_base_url"],
            auto_sync=row["auto_sync"],
        )

    def save_settings(self, user_id: int, **fields: Any) -> Settings:
        allowed = {
            "assignments_db",
            "courses_db",
            "drive_folder_id",
            "campus_timezone",
            "canvas_base_url",
            "auto_sync",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_settings(user_id)
        columns = ", ".join(f"{k} = %s" for k in updates)
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO settings (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (user_id,),
            )
            cur.execute(
                f"UPDATE settings SET {columns}, updated_at = now()"
                " WHERE user_id = %s",
                (*updates.values(), user_id),
            )
        return self.get_settings(user_id)

    # -- run history --------------------------------------------------------

    def record_run(
        self,
        user_id: int,
        kind: str,
        *,
        ok: bool,
        started_at: datetime,
        summary: dict | None = None,
        error: str = "",
    ) -> None:
        _, _, Jsonb = _psycopg()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO runs (user_id, kind, ok, summary, error, started_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, kind, ok, Jsonb(summary or {}), error[:2000], started_at),
            )

    def recent_runs(self, user_id: int, limit: int = 10) -> list[Run]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM runs WHERE user_id = %s"
                " ORDER BY finished_at DESC LIMIT %s",
                (user_id, limit),
            )
            return [
                Run(
                    id=row["id"],
                    user_id=row["user_id"],
                    kind=row["kind"],
                    ok=row["ok"],
                    summary=_as_dict(row["summary"]),
                    error=row["error"],
                    started_at=row["started_at"],
                    finished_at=row["finished_at"],
                )
                for row in cur.fetchall()
            ]


def normalise_email(email: str) -> str:
    """Lowercased and trimmed.

    Google returns whatever case the user typed at sign-up, so an invite for
    `Angelo@example.com` must match a login as `angelo@example.com` -- and the
    unique index has to see them as one row.
    """
    return (email or "").strip().lower()


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _user(row: dict) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        google_sub=row.get("google_sub") or "",
        name=row.get("name") or "",
        picture=row.get("picture") or "",
        is_owner=bool(row.get("is_owner")),
        created_at=row.get("created_at"),
        last_login_at=row.get("last_login_at"),
    )
