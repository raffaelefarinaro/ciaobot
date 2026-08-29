"""PKCE verifier handling in ``ciao gws-auth-helper``.

``--redirect-url`` is a *second* process: the authorization code it carries was
issued against the challenge the *first*, interactive invocation printed. A
verifier generated fresh in the second process therefore cannot match, and
Google rejects the exchange with ``invalid_grant`` — so the interactive run
parks its verifier and the non-interactive one picks it up.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import gws_auth_helper


# --- the parked-verifier store ----------------------------------------------


def test_verifier_round_trips_and_is_owner_only(tmp_path: Path) -> None:
    gws_auth_helper._store_pending_verifier(tmp_path, "verifier-abc")

    assert gws_auth_helper._load_pending_verifier(tmp_path) == "verifier-abc"
    mode = gws_auth_helper._pending_verifier_path(tmp_path).stat().st_mode & 0o777
    assert mode == 0o600


def test_missing_verifier_reads_as_empty(tmp_path: Path) -> None:
    assert gws_auth_helper._load_pending_verifier(tmp_path) == ""


def test_stale_verifier_is_refused_and_removed(tmp_path: Path) -> None:
    """An authorization code expires long before this window.

    A verifier older than it can only belong to an abandoned attempt, so
    reusing it would produce a confusing `invalid_grant` instead of a clear
    "start again".
    """
    gws_auth_helper._store_pending_verifier(tmp_path, "verifier-old")
    path = gws_auth_helper._pending_verifier_path(tmp_path)
    stale = time.time() - gws_auth_helper._PENDING_VERIFIER_MAX_AGE_S - 60
    os.utime(path, (stale, stale))

    assert gws_auth_helper._load_pending_verifier(tmp_path) == ""
    assert not path.exists()


def test_clearing_a_missing_verifier_is_not_an_error(tmp_path: Path) -> None:
    gws_auth_helper._clear_pending_verifier(tmp_path)


# --- the --redirect-url path ------------------------------------------------


@pytest.fixture
def helper_env(tmp_path: Path, monkeypatch):
    """Stub everything outside the helper: config, client secret, and Google."""
    config_dir = tmp_path / "profile"
    config_dir.mkdir(parents=True)

    exchanges: list[dict] = []

    monkeypatch.setattr(
        gws_auth_helper, "_read_client_secret",
        lambda _dir: {
            "client_id": "cid",
            "client_secret": "csecret",
            "redirect_uris": ["http://localhost"],
        },
    )
    monkeypatch.setattr(
        gws_auth_helper.gws_auth, "profile_config_dir", lambda _cfg, _p: config_dir
    )
    monkeypatch.setattr(
        gws_auth_helper.gws_wrapper, "_configured_workspace_root", lambda _c: tmp_path
    )
    monkeypatch.setattr(
        gws_auth_helper.gws_auth, "extract_code_from_input", lambda url: "the-code"
    )

    def _exchange_code(**kwargs):
        exchanges.append(kwargs)
        return {"refresh_token": "rt", "id_token": "", "scope": "s"}

    monkeypatch.setattr(gws_auth_helper.gws_auth, "exchange_code", _exchange_code)
    monkeypatch.setattr(
        gws_auth_helper.gws_auth, "store_credentials", lambda *a, **k: None
    )
    monkeypatch.setattr(
        gws_auth_helper.gws_auth, "extract_email_from_id_token", lambda _t: "a@b.c"
    )

    import ciao.config

    monkeypatch.setattr(
        ciao.config.CiaoConfig,
        "from_env",
        classmethod(lambda cls: SimpleNamespace(workspace_root=str(tmp_path))),
    )

    return SimpleNamespace(config_dir=config_dir, exchanges=exchanges)


def test_redirect_url_exchanges_with_the_parked_verifier(helper_env) -> None:
    """The whole point: the code is paired with the verifier that made it."""
    gws_auth_helper._store_pending_verifier(helper_env.config_dir, "parked-verifier")

    rc = gws_auth_helper.main_entry(
        ["personal", "--redirect-url", "http://localhost/?code=x"]
    )

    assert rc == 0
    assert len(helper_env.exchanges) == 1
    assert helper_env.exchanges[0]["code_verifier"] == "parked-verifier"


def test_redirect_url_without_a_verifier_refuses_to_exchange(helper_env) -> None:
    """Better a clear error than an `invalid_grant` from Google."""
    rc = gws_auth_helper.main_entry(
        ["personal", "--redirect-url", "http://localhost/?code=x"]
    )

    assert rc == 1
    assert helper_env.exchanges == []


def test_explicit_code_verifier_wins_over_the_parked_one(helper_env) -> None:
    gws_auth_helper._store_pending_verifier(helper_env.config_dir, "parked-verifier")

    rc = gws_auth_helper.main_entry([
        "personal",
        "--redirect-url", "http://localhost/?code=x",
        "--code-verifier", "explicit-verifier",
    ])

    assert rc == 0
    assert helper_env.exchanges[0]["code_verifier"] == "explicit-verifier"


def test_the_parked_verifier_is_spent_after_the_exchange(helper_env) -> None:
    """The code is single-use; a leftover verifier could only mislead a later run."""
    gws_auth_helper._store_pending_verifier(helper_env.config_dir, "parked-verifier")

    gws_auth_helper.main_entry(
        ["personal", "--redirect-url", "http://localhost/?code=x"]
    )

    assert not gws_auth_helper._pending_verifier_path(helper_env.config_dir).exists()


def test_a_failed_exchange_keeps_the_verifier_for_a_retry(helper_env, monkeypatch) -> None:
    """A transient failure need not cost the operator another consent round.

    The exchange can fail without Google consuming the code (dropped
    connection, 5xx), and that code is still usable — but only with the
    verifier that produced it.
    """
    gws_auth_helper._store_pending_verifier(helper_env.config_dir, "parked-verifier")

    def _boom(**kwargs):
        raise ValueError("connection reset")

    monkeypatch.setattr(gws_auth_helper.gws_auth, "exchange_code", _boom)

    rc = gws_auth_helper.main_entry(
        ["personal", "--redirect-url", "http://localhost/?code=x"]
    )

    assert rc == 1
    assert (
        gws_auth_helper._load_pending_verifier(helper_env.config_dir)
        == "parked-verifier"
    )


def test_the_retry_after_a_failure_succeeds_with_the_same_verifier(
    helper_env, monkeypatch
) -> None:
    gws_auth_helper._store_pending_verifier(helper_env.config_dir, "parked-verifier")

    # Fail once, then let the transport recover. Swapping the stub rather than
    # calling monkeypatch.undo(), which would also revert the fixture's own
    # patches and send the helper at the real profile directory.
    attempts = {"n": 0}

    def _flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ValueError("5xx")
        helper_env.exchanges.append(kwargs)
        return {"refresh_token": "rt", "id_token": "", "scope": "s"}

    monkeypatch.setattr(gws_auth_helper.gws_auth, "exchange_code", _flaky)

    assert gws_auth_helper.main_entry(
        ["personal", "--redirect-url", "http://localhost/?code=x"]
    ) == 1

    rc = gws_auth_helper.main_entry(
        ["personal", "--redirect-url", "http://localhost/?code=x"]
    )

    assert rc == 0
    assert helper_env.exchanges[-1]["code_verifier"] == "parked-verifier"
    assert not gws_auth_helper._pending_verifier_path(helper_env.config_dir).exists()
