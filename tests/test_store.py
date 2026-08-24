"""Storage, against a real Postgres.

Fakes would not have caught any of the things this file actually pins: that
secrets reach the database encrypted, that the unique index and the email
normalisation agree, or that `connection_status` cannot leak a token.
"""

import pytest

from goodnotes_notion_sync.store import CANVAS, GOOGLE, NOTION, StoreError, normalise_email


def user(store, email="a@b.c", sub="sub-1"):
    store.add_invite(email)
    return store.upsert_google_user(sub=sub, email=email, name="A")


# -- users and invitations -------------------------------------------------


def test_migrations_are_idempotent(store):
    assert store.migrate() == []


def test_signing_in_twice_does_not_create_a_second_account(store):
    first = user(store)
    second = store.upsert_google_user(sub="sub-1", email="a@b.c", name="A Renamed")

    assert first.id == second.id
    assert second.name == "A Renamed"


def test_email_case_does_not_split_an_account(store):
    """Google echoes back whatever case the address was typed in.

    Without normalisation, an invite for Angelo@x.com and a login as
    angelo@x.com are two different people, and the second one is locked out.
    """
    store.add_invite("Angelo@Example.COM ")
    assert store.is_invited("angelo@example.com")

    first = store.upsert_google_user(sub="s", email="ANGELO@example.com")
    second = store.upsert_google_user(sub="s", email="angelo@Example.com")
    assert first.id == second.id


def test_an_uninvited_address_is_not_invited(store):
    assert store.is_invited("stranger@example.com") is False
    assert store.is_invited("") is False


def test_ensure_owner_seeds_the_invite_list(store):
    """Without this the app is a locked room with the key inside."""
    store.ensure_owner("owner@example.com")
    assert store.is_invited("owner@example.com")


def test_ensure_owner_marks_an_existing_row(store):
    store.ensure_owner("owner@example.com")
    store.upsert_google_user(sub="s", email="owner@example.com")
    store.ensure_owner("owner@example.com")

    assert store.user_by_email("owner@example.com").is_owner


def test_an_owner_cannot_be_uninvited(store):
    """Otherwise one misclick locks everyone out of the app permanently."""
    store.ensure_owner("owner@example.com")
    store.upsert_google_user(sub="s", email="owner@example.com")
    store.ensure_owner("owner@example.com")

    store.remove_invite("owner@example.com")

    assert store.is_invited("owner@example.com")


def test_a_member_can_be_uninvited(store):
    store.add_invite("member@example.com")
    store.remove_invite("member@example.com")
    assert not store.is_invited("member@example.com")


def test_rubbish_is_not_an_invitation(store):
    with pytest.raises(StoreError):
        store.add_invite("not-an-email")


def test_inviting_twice_is_harmless(store):
    store.add_invite("x@y.z")
    store.add_invite("x@y.z")
    assert [row["email"] for row in store.list_invites()] == ["x@y.z"]


# -- credentials -----------------------------------------------------------


def test_a_credential_round_trips(store):
    person = user(store)
    store.set_connection(person.id, NOTION, "ntn_live", {"workspace_name": "W"})

    conn = store.get_connection(person.id, NOTION)
    assert conn.secret == "ntn_live"
    assert conn.metadata["workspace_name"] == "W"


def test_the_token_is_not_readable_in_the_table(store):
    """The point of the whole crypto module, asserted against real bytes."""
    person = user(store)
    store.set_connection(person.id, CANVAS, "canvas-token-plaintext")

    with store._cursor() as cur:
        cur.execute("SELECT secret FROM connections")
        stored = bytes(cur.fetchone()["secret"])

    assert b"canvas-token-plaintext" not in stored


def test_connection_status_cannot_leak_a_secret(store):
    """This is what the dashboard renders, so it must never decrypt."""
    person = user(store)
    store.set_connection(person.id, NOTION, "ntn_live", {"workspace_name": "W"})

    status = store.connection_status(person.id)

    assert status[NOTION]["connected"] is True
    assert "ntn_live" not in repr(status)


def test_reconnecting_replaces_rather_than_duplicates(store):
    person = user(store)
    store.set_connection(person.id, GOOGLE, "first")
    store.set_connection(person.id, GOOGLE, "second")

    assert store.get_connection(person.id, GOOGLE).secret == "second"


def test_an_empty_credential_is_refused(store):
    """A blank token would read as 'connected' and fail at run time instead."""
    person = user(store)
    with pytest.raises(StoreError):
        store.set_connection(person.id, GOOGLE, "")


def test_an_unknown_provider_is_refused(store):
    person = user(store)
    with pytest.raises(StoreError):
        store.set_connection(person.id, "dropbox", "x")


def test_disconnecting_removes_the_row(store):
    person = user(store)
    store.set_connection(person.id, CANVAS, "tok")
    store.delete_connection(person.id, CANVAS)

    assert store.get_connection(person.id, CANVAS) is None


def test_one_account_cannot_see_another_ones_credentials(store):
    a = user(store, "a@x.com", "sa")
    b = user(store, "b@x.com", "sb")
    store.set_connection(a.id, NOTION, "a-token")

    assert store.get_connection(b.id, NOTION) is None
    assert store.connection_status(b.id) == {}


def test_deleting_a_user_takes_their_credentials_with_them(store):
    person = user(store)
    store.set_connection(person.id, NOTION, "tok")

    with store._cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (person.id,))
        cur.execute("SELECT count(*) AS n FROM connections")
        assert cur.fetchone()["n"] == 0


# -- settings and runs -----------------------------------------------------


def test_settings_default_and_save(store):
    person = user(store)

    assert store.get_settings(person.id).campus_timezone == "America/Chicago"

    saved = store.save_settings(person.id, assignments_db="abc", auto_sync=False)
    assert saved.assignments_db == "abc"
    assert saved.auto_sync is False


def test_settings_ignore_fields_that_are_not_settings(store):
    person = user(store)
    store.save_settings(person.id, assignments_db="abc", is_owner=True)

    assert store.get_settings(person.id).assignments_db == "abc"
    assert store.get_user(person.id).is_owner is False


def test_a_field_name_cannot_smuggle_sql(store):
    """`save_settings` interpolates the *keys* into the UPDATE statement.

    Values are parameterised, but column names cannot be, so the allow-list is
    the only thing standing between a caller-supplied key and arbitrary SQL.
    This test is the reason that list exists.
    """
    person = user(store)
    store.save_settings(person.id, assignments_db="keep-me")

    store.save_settings(
        person.id,
        **{"assignments_db = 'pwned', auto_sync = FALSE, campus_timezone": "x"},
    )

    settings = store.get_settings(person.id)
    assert settings.assignments_db == "keep-me"
    assert settings.auto_sync is True


def test_the_scheduler_skips_accounts_that_opted_out(store):
    person = user(store)
    assert [u.email for u in store.enabled_users()] == ["a@b.c"]

    store.save_settings(person.id, auto_sync=False)
    assert store.enabled_users() == []


def test_the_scheduler_skips_accounts_that_were_uninvited(store):
    person = user(store)
    store.remove_invite(person.email)
    assert store.enabled_users() == []


def test_runs_are_recorded_newest_first(store):
    from datetime import datetime, timezone

    person = user(store)
    now = datetime.now(timezone.utc)
    store.record_run(person.id, "canvas", ok=True, started_at=now, summary={"created": 2})
    store.record_run(person.id, "sync", ok=False, started_at=now, error="boom")

    runs = store.recent_runs(person.id)
    assert [r.kind for r in runs] == ["sync", "canvas"]
    assert runs[0].error == "boom"
    assert runs[1].summary == {"created": 2}


def test_normalise_email_is_total():
    assert normalise_email("  A@B.C ") == "a@b.c"
    assert normalise_email("") == ""
    assert normalise_email(None) == ""
