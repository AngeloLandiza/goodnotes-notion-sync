"""Shared fixtures.

The storage tests need a real Postgres. They are skipped, not failed, when
`TEST_DATABASE_URL` is unset, so `pytest` stays a one-command, no-setup thing
for anyone who just wants to work on the matching logic -- while CI and anyone
touching `store.py` still gets real coverage.

    docker run -e POSTGRES_PASSWORD=x -p 5432:5432 -d postgres:16
    export TEST_DATABASE_URL=postgresql://postgres:x@localhost:5432/postgres
"""

import os

import pytest

from goodnotes_notion_sync.crypto import SecretBox, generate_key

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
def store():
    if not TEST_DSN:
        pytest.skip("set TEST_DATABASE_URL to run the storage tests")
    from goodnotes_notion_sync.store import Store

    box = SecretBox([generate_key()])
    instance = Store(TEST_DSN, box)
    _wipe(instance)
    instance.migrate()
    yield instance
    _wipe(instance)


def _wipe(instance) -> None:
    with instance._cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS runs, connections, settings, invites, users,"
            " schema_migrations CASCADE"
        )
