"""Regression tests for curation lease identity."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from ciao import curation_run as cr


def _expired_and_replaced_lease(tmp_path):
    vault = tmp_path / "vault"
    now = datetime.now(UTC).replace(microsecond=0)
    expired = cr.begin_run(vault, holder="same-label", ttl_s=1, now=now)
    replacement = cr.begin_run(
        vault,
        holder="same-label",
        now=now + timedelta(seconds=2),
    )
    return vault, now, expired, replacement


def test_expired_holder_cannot_renew_replacement_lease(tmp_path) -> None:
    vault, now, expired, _replacement = _expired_and_replaced_lease(tmp_path)
    before = cr.state_path(vault).read_bytes()

    assert not cr.renew_run(
        vault, holder=expired.holder, now=now + timedelta(seconds=2)
    )
    assert cr.state_path(vault).read_bytes() == before


def test_expired_holder_cannot_record_progress_on_replacement_lease(tmp_path) -> None:
    vault, _now, expired, _replacement = _expired_and_replaced_lease(tmp_path)
    before = cr.state_path(vault).read_bytes()

    with pytest.raises(cr.CurationBusy):
        cr.record_done(vault, ["must-not-be-recorded"], holder=expired.holder)
    assert cr.state_path(vault).read_bytes() == before


def test_expired_holder_cannot_end_replacement_lease(tmp_path) -> None:
    vault, now, expired, _replacement = _expired_and_replaced_lease(tmp_path)
    before = cr.state_path(vault).read_bytes()

    with pytest.raises(cr.CurationBusy):
        cr.end_run(
            vault,
            holder=expired.holder,
            now=now + timedelta(seconds=2),
        )
    assert cr.state_path(vault).read_bytes() == before


def test_reacquired_identity_allows_progress_and_rejects_original_label(tmp_path) -> None:
    vault = tmp_path / "vault"
    now = datetime.now(UTC).replace(microsecond=0)
    first = cr.begin_run(vault, holder="repeat", now=now)
    cr.end_run(vault, holder=first.holder, now=now)
    second = cr.begin_run(vault, holder="repeat", now=now + timedelta(seconds=1))

    before = cr.state_path(vault).read_bytes()
    with pytest.raises(cr.CurationBusy):
        cr.record_done(vault, ["original-label-must-fail"], holder="repeat")
    with pytest.raises(cr.CurationBusy):
        cr.end_run(vault, holder="repeat", now=now + timedelta(seconds=1))
    assert cr.state_path(vault).read_bytes() == before

    assert cr.record_done(vault, ["new-identity-progress"], holder=second.holder) == 1
    assert cr.load_state(vault).done_keys["new-identity-progress"]
    cr.end_run(vault, holder=second.holder, now=now + timedelta(seconds=1))
    assert cr.active_lease(vault, now=now + timedelta(seconds=1)) is None


def test_default_acquisitions_have_distinct_holders_after_expiry(tmp_path) -> None:
    vault = tmp_path / "vault"
    now = datetime.now(UTC).replace(microsecond=0)

    first = cr.begin_run(vault, ttl_s=1, now=now)
    second = cr.begin_run(vault, now=now + timedelta(seconds=2))

    assert first.holder != second.holder


def test_legacy_live_holder_remains_usable(tmp_path) -> None:
    vault = tmp_path / "vault"
    now = datetime.now(UTC).replace(microsecond=0)
    path = cr.state_path(vault)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": cr.STATE_VERSION,
                "done_keys": {},
                "lease": {
                    "holder": " legacy label ",
                    "started_at": now.isoformat(),
                    "expires_at": (now + timedelta(minutes=5)).isoformat(),
                },
                "last_run": {},
            }
        ),
        encoding="utf-8",
    )

    assert cr.renew_run(vault, holder=" legacy label ", now=now)
    assert cr.record_done(vault, ["legacy-work"], holder=" legacy label ") == 1
    cr.end_run(vault, holder=" legacy label ", now=now)
    assert cr.active_lease(vault, now=now) is None

    acquired = cr.begin_run(vault, holder="legacy label", now=now)
    assert acquired.holder != "legacy label"


def test_released_and_reacquired_label_requires_returned_identity(tmp_path) -> None:
    vault = tmp_path / "vault"
    now = datetime.now(UTC).replace(microsecond=0)
    first = cr.begin_run(vault, holder="repeat", now=now)
    cr.end_run(vault, holder=first.holder, now=now)
    second = cr.begin_run(vault, holder="repeat", now=now + timedelta(seconds=1))

    assert first.holder != second.holder
    assert not cr.renew_run(vault, holder="repeat", now=now + timedelta(seconds=1))
    assert cr.renew_run(vault, holder=second.holder, now=now + timedelta(seconds=1))
    cr.end_run(vault, holder=second.holder, now=now + timedelta(seconds=1))


def test_authorize_holder_follows_only_the_live_lease(tmp_path) -> None:
    vault = tmp_path / "vault"
    now = datetime.now(UTC).replace(microsecond=0)
    run = cr.begin_run(vault, holder="live", ttl_s=60, now=now)
    before = cr.state_path(vault).read_bytes()

    assert cr.authorize_holder(
        vault, holder=run.holder, clock=lambda: now + timedelta(seconds=1)
    )
    assert not cr.authorize_holder(
        vault, holder="someone-else", clock=lambda: now + timedelta(seconds=1)
    )
    assert not cr.authorize_holder(
        vault, holder="", clock=lambda: now + timedelta(seconds=1)
    )
    assert not cr.authorize_holder(
        vault, holder=run.holder, clock=lambda: now + timedelta(seconds=120)
    )
    assert cr.state_path(vault).read_bytes() == before


def test_authorize_holder_refuses_a_replaced_lease(tmp_path) -> None:
    vault, now, expired, replacement = _expired_and_replaced_lease(tmp_path)
    later = lambda: now + timedelta(seconds=3)  # noqa: E731

    assert not cr.authorize_holder(vault, holder=expired.holder, clock=later)
    assert cr.authorize_holder(vault, holder=replacement.holder, clock=later)
