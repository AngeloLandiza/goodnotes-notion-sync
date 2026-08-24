"""Scheduled runs across every account."""

import pytest

from goodnotes_notion_sync import fanout
from goodnotes_notion_sync.config import RunConfig
from goodnotes_notion_sync.store import User


class FakeStore:
    def __init__(self, users):
        self._users = users
        self.recorded = []

    def enabled_users(self):
        return list(self._users)

    def record_run(self, user_id, kind, *, ok, started_at, summary=None, error=""):
        self.recorded.append((user_id, kind, ok, error))


def users(*emails):
    return [User(id=i + 1, email=email) for i, email in enumerate(emails)]


def complete_config(label):
    return RunConfig(
        notion_token="n", google_client_id="c", google_client_secret="s",
        google_refresh_token="r", canvas_token="t", assignments_db="a",
        courses_db="b", drive_folder_id="f", label=label,
    )


@pytest.fixture
def wired(monkeypatch):
    """Every account fully configured, with the jobs stubbed out."""
    monkeypatch.setattr(
        fanout, "config_for_user", lambda store, uid, label="": complete_config(label)
    )
    ran = []
    monkeypatch.setattr(
        fanout,
        "run_job",
        lambda kind, config, dry_run: ran.append((config.label, kind)) or {"n": 1},
    )
    return ran


def test_every_account_is_synced(wired):
    store = FakeStore(users("a@x.com", "b@x.com"))
    report = fanout.run_for_all(store)

    assert report.users == 2
    assert wired == [
        ("a@x.com", "canvas"), ("a@x.com", "sync"),
        ("b@x.com", "canvas"), ("b@x.com", "sync"),
    ]
    assert not report.failures


def test_canvas_runs_before_the_goodnotes_sync(wired):
    """Rows Canvas creates are then visible to the matcher in the same pass."""
    fanout.run_for_all(FakeStore(users("a@x.com")))
    assert [kind for _, kind in wired] == ["canvas", "sync"]


def test_one_broken_account_does_not_stop_the_others(monkeypatch):
    """A classmate's expired Canvas token must not cost everyone else their
    nightly run."""
    monkeypatch.setattr(
        fanout, "config_for_user", lambda store, uid, label="": complete_config(label)
    )

    def flaky(kind, config, dry_run):
        if config.label == "broken@x.com":
            raise RuntimeError("token expired")
        return {"n": 1}

    monkeypatch.setattr(fanout, "run_job", flaky)
    store = FakeStore(users("ok1@x.com", "broken@x.com", "ok2@x.com"))

    report = fanout.run_for_all(store)

    assert len(report.failures) == 2  # both of the broken account's jobs
    succeeded = {o.email for o in report.outcomes if o.ok and not o.skipped}
    assert succeeded == {"ok1@x.com", "ok2@x.com"}
    assert "token expired" in report.to_text()


def test_a_failure_is_recorded_against_that_account(monkeypatch):
    monkeypatch.setattr(
        fanout, "config_for_user", lambda store, uid, label="": complete_config(label)
    )
    monkeypatch.setattr(
        fanout, "run_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    store = FakeStore(users("a@x.com"))

    fanout.run_for_all(store)

    assert all(not ok for _, _, ok, _ in store.recorded)
    assert "nope" in store.recorded[0][3]


def test_an_unconfigured_account_is_skipped_quietly(monkeypatch):
    """Shouting nightly about half-finished setup trains people to ignore the
    report, and then a real failure goes unread."""
    monkeypatch.setattr(
        fanout, "config_for_user", lambda store, uid, label="": RunConfig(label=label)
    )
    ran = []
    monkeypatch.setattr(fanout, "run_job", lambda *a, **k: ran.append(a) or {})

    report = fanout.run_for_all(FakeStore(users("new@x.com")))

    assert ran == []
    assert not report.failures
    assert all(o.skipped for o in report.outcomes)
    assert "not set up" in report.to_text()


def test_a_dry_run_records_nothing(wired):
    store = FakeStore(users("a@x.com"))
    fanout.run_for_all(store, dry_run=True)
    assert store.recorded == []


def test_only_can_target_one_account(wired):
    fanout.run_for_all(FakeStore(users("a@x.com", "b@x.com")), only_email="B@X.com")
    assert {label for label, _ in wired} == {"b@x.com"}
