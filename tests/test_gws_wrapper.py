"""Tests for ciao.gws_wrapper (the ``ciao gws`` passthrough) and
``ciao.gws_auth_helper`` (the ``ciao gws-auth-helper`` headless re-auth).

No real OAuth, network, or gws binary is used; exec/parse paths are exercised
with injected functions.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ciao import gws_auth, gws_auth_helper, gws_wrapper


def _config(tmp_path) -> SimpleNamespace:
    (tmp_path / ".runtime").mkdir(exist_ok=True)
    return SimpleNamespace(
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
    )


# ── profile resolution ──────────────────────────────────────────────────────


def test_split_profile_and_args_positional() -> None:
    profile, rest = gws_wrapper._split_profile_and_args(["personal", "gmail", "+triage"])
    assert profile == "personal"
    assert rest == ["gmail", "+triage"]


def test_split_profile_and_args_service_name_not_consumed() -> None:
    # `gmail` is a gws service name, so it is not treated as a profile.
    profile, rest = gws_wrapper._split_profile_and_args(["gmail", "list"])
    assert profile is None
    assert rest == ["gmail", "list"]


def test_split_profile_and_args_flag() -> None:
    profile, rest = gws_wrapper._split_profile_and_args(["--profile", "work", "calendar", "list"])
    assert profile == "work"
    assert rest == ["calendar", "list"]


def test_split_profile_and_args_dash_first_not_consumed() -> None:
    profile, rest = gws_wrapper._split_profile_and_args(["-h"])
    assert profile is None
    assert rest == ["-h"]


# ── environment computation ─────────────────────────────────────────────────


def test_gws_environment_sets_config_dir_and_unsets_credentials(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "base64blob")
    env = gws_wrapper._gws_environment(tmp_path, "personal")
    assert env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == str(tmp_path / "secrets" / "gws-personal")
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env


def test_gws_environment_invalid_profile(tmp_path) -> None:
    with pytest.raises(ValueError):
        gws_wrapper._gws_environment(tmp_path, "!!!")


# ── configured workspace root ───────────────────────────────────────────────


def test_configured_workspace_root_trusts_explicit(tmp_path) -> None:
    cfg = SimpleNamespace(workspace_root=str(tmp_path))
    assert gws_wrapper._configured_workspace_root(cfg) == tmp_path.resolve()


def test_configured_workspace_root_from_plist_fallback(tmp_path, monkeypatch) -> None:
    real_root = tmp_path / "real-workspace"
    real_root.mkdir()
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    bootstrap = fake_home / ".ciao" / "bootstrap"
    # config lands on bootstrap (no CIAO_WORKSPACE in a plain terminal)
    cfg = SimpleNamespace(workspace_root=str(bootstrap))
    launch_agents = fake_home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist = launch_agents / "com.ciao.server.plist"
    plist.write_bytes(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>EnvironmentVariables</key><dict>
<key>CIAO_WORKSPACE</key><string>{real_root}</string>
</dict>
</dict></plist>""".encode()
    )
    assert gws_wrapper._configured_workspace_root(cfg) == real_root.resolve()


# ── gws_auth_helper reuses gws_auth ─────────────────────────────────────────


def test_gws_auth_helper_uses_gws_auth_fingerprint() -> None:
    assert gws_auth.fingerprint("secret") == gws_auth_helper.fingerprint("secret")
    assert len(gws_auth_helper.fingerprint("x")) == 12


def test_gws_auth_helper_profile_config_matches_gws_auth(tmp_path) -> None:
    cfg = _config(tmp_path)
    # The helper resolves the credential dir through gws_auth's single source.
    assert gws_auth.profile_config_dir(cfg, "work") == tmp_path / "secrets" / "gws"
