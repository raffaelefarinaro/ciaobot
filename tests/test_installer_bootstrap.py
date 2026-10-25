from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import cli, main
from ciao.config import CiaoConfig
from ciao.web.app import create_app


def test_bootstrap_scratch_is_never_an_upgrade_migration_target(tmp_path):
    from ciao.workspace_reroot import migrate_if_needed

    scratch = tmp_path / "scratch"
    config = SimpleNamespace(
        bootstrap_mode=True,
        workspace_root=scratch,
        state_path=scratch / "runtime" / "state.json",
    )
    assert migrate_if_needed(config) == {"status": "skipped"}
    assert not scratch.exists()


def _parser_run(argv: list[str]):
    parser = cli.build_parser()
    return parser.parse_args(argv)


def test_run_parser_forwards_installer_mode_and_run_id(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run_server(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_run_server", fake_run_server)
    run_id = str(uuid.uuid4())
    args = _parser_run(["run", "--installer-bootstrap", "--installer-run-id", run_id])
    assert args.func(args) == 0
    assert seen == {
        "supervised": False,
        "installer_bootstrap": True,
        "installer_run_id": run_id,
        "installer_suggest_workspace": None,
    }


def test_run_parser_forwards_installer_suggested_workspace(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(cli, "_run_server", lambda **kwargs: seen.update(kwargs) or 0)
    suggestion = tmp_path / "new-workspace"
    args = _parser_run(["run", "--installer-bootstrap", "--installer-suggest-workspace", str(suggestion)])
    assert args.func(args) == 0
    assert seen["installer_suggest_workspace"] == str(suggestion)


def test_installer_suggestion_rejects_relative_path_and_orphan_flag(capsys, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _parser_run(["run", "--installer-bootstrap", "--installer-suggest-workspace", "relative"])
    assert exc.value.code == 2
    assert "absolute" in capsys.readouterr().err
    args = _parser_run(["run", "--installer-suggest-workspace", str(tmp_path / "work")])
    assert args.func(args) == 2
    assert "requires --installer-bootstrap" in capsys.readouterr().err


def test_ordinary_config_never_reads_a_suggested_workspace(monkeypatch) -> None:
    monkeypatch.setenv("CIAO_SUGGESTED_WORKSPACE", "/must/not/be/read")
    assert CiaoConfig.from_env().suggested_workspace is None


def test_installer_config_records_the_suggestion_without_creating_it(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = tmp_path / "not-created"
    config = CiaoConfig.from_env(installer_bootstrap=True, installer_suggest_workspace=str(target))
    assert config.suggested_workspace == target
    assert not target.exists()


def test_installer_arguments_reject_supervised_and_orphan_run_id(capsys) -> None:
    args = _parser_run(["run", "--installer-run-id", str(uuid.uuid4())])
    assert args.func(args) == 2
    assert "requires --installer-bootstrap" in capsys.readouterr().err

    args = _parser_run(["run", "--installer-bootstrap", "--supervised"])
    assert args.func(args) == 2
    assert "cannot be combined with --supervised" in capsys.readouterr().err


def test_freshness_refusal_precedes_config_secrets_and_directory_creation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao.install_discovery import InstallerEntryRefused

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "ciao.install_discovery.require_fresh_installer_entry",
        lambda **_kwargs: (_ for _ in ()).throw(InstallerEntryRefused("not fresh")),
    )

    def explode(**_kwargs):
        raise AssertionError("config must not be built after a refusal")

    monkeypatch.setattr(main.CiaoConfig, "from_env", explode)

    import asyncio

    assert asyncio.run(main._async_main(installer_bootstrap=True)) == 64
    assert "not fresh" in capsys.readouterr().err
    assert not (home / ".ciao").exists()


def test_installer_config_ignores_dotenv_cwd_discovery_and_redirect_environment(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path / "ignored"))
    monkeypatch.setenv("CIAO_VAULT_ROOT", str(tmp_path / "ignored-vault"))
    (tmp_path / ".env").write_text("CIAO_WORKSPACE=/ignored\nPWA_PORT=9999\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    config = CiaoConfig.from_env(installer_bootstrap=True)

    root = home / ".ciao" / "installer-bootstrap"
    assert config.workspace_root == root
    assert config.state_path == root / "runtime" / "state.json"
    assert config.bootstrap_mode is True
    assert config.pwa_host == "127.0.0.1"
    assert config.pwa_port == 8443
    assert config.vault_root == root / "memory-vault"


def test_installer_bootstrap_rejects_symlinked_scratch_paths(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    root = home / ".ciao" / "installer-bootstrap"
    (home / ".ciao").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root.symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="symlink"):
        CiaoConfig.from_env(installer_bootstrap=True)


def test_installer_bootstrap_clears_direct_environment_redirects(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CIAO_WORKSPACE", "/somewhere")
    monkeypatch.setenv("CIAO_VAULT_ROOT", "/elsewhere")
    monkeypatch.setenv("PWA_PORT", "9999")
    monkeypatch.setenv("CIAO_NO_BROWSER", "1")
    monkeypatch.setenv("GWS_PROFILE", "work")

    previous_cwd = Path.cwd()
    try:
        main._enter_installer_bootstrap_environment(config_root=tmp_path / "scratch")

        assert "CIAO_WORKSPACE" not in os.environ
        assert "CIAO_VAULT_ROOT" not in os.environ
        assert "PWA_PORT" not in os.environ
        assert "GWS_PROFILE" not in os.environ
        assert os.environ["CIAO_NO_BROWSER"] == "1"
        assert Path.cwd() == (tmp_path / "scratch" / "launcher").resolve()
    finally:
        # This call changes the process cwd; leaving it changed leaks into
        # whatever test this worker runs next.
        os.chdir(previous_cwd)


def test_app_factory_initializes_explicit_installer_state(tmp_path: Path) -> None:
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        bootstrap_mode=True,
    )
    run_id = str(uuid.uuid4())
    app = create_app(config, installer_bootstrap=True, installer_run_id=run_id)
    assert app.state.installer_bootstrap is True
    assert app.state.installer_run_id == run_id


def test_app_factory_rejects_inconsistent_installer_state(tmp_path: Path) -> None:
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    with pytest.raises(ValueError):
        create_app(config, installer_bootstrap=True, installer_run_id=str(uuid.uuid4()))
    with pytest.raises(ValueError):
        create_app(config, installer_run_id=str(uuid.uuid4()))


def test_installer_exit_zero_and_restart_are_returned_without_reexec(
    monkeypatch, capsys
) -> None:
    from ciao.config import RESTART_EXIT_CODE

    codes = iter([0, RESTART_EXIT_CODE])

    def fake_main(**_kwargs):
        raise SystemExit(next(codes))

    monkeypatch.setattr("ciao.main.main", fake_main)

    assert cli._run_server(installer_bootstrap=True) == 0
    assert cli._run_server(installer_bootstrap=True) == RESTART_EXIT_CODE
    # No "relaunching" message: the parent owns the child.
    assert "relaunching" not in capsys.readouterr().err


def test_ordinary_run_retains_existing_discovery_and_reexec_behavior(monkeypatch) -> None:
    from ciao.config import RESTART_EXIT_CODE

    calls: list[dict] = []

    def fake_main(**kwargs):
        calls.append(kwargs)
        raise SystemExit(RESTART_EXIT_CODE)

    monkeypatch.setattr("ciao.main.main", fake_main)
    monkeypatch.setattr("os.execv", lambda *_args: calls.append({"exec": True}))

    assert cli._run_server() == RESTART_EXIT_CODE
    # The ordinary path still re-execs, with its original single argument.
    assert calls[0] == {"supervised": False}
    assert calls[-1] == {"exec": True}
