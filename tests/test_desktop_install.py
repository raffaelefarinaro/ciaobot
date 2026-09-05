"""Safe cleanup for bundles installed by the one-line installer."""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from ciao import desktop_build, desktop_install


def _native_bundle(root: Path) -> Path:
    executable = root / desktop_install.APP_BUNDLE_NAME / "Contents" / "MacOS"
    executable.mkdir(parents=True)
    (executable / desktop_build.APP_EXECUTABLE_NAME).write_bytes(b"native app")
    return executable.parent.parent


def test_uninstall_removes_only_the_native_app(tmp_path: Path) -> None:
    bundle = _native_bundle(tmp_path)

    result = desktop_install.uninstall_desktop_app(app_dir=tmp_path)

    assert result == {
        "removed": True,
        "path": str(tmp_path / desktop_install.APP_BUNDLE_NAME),
        "removed_agents": [],
    }
    assert not bundle.exists()


def test_uninstall_leaves_a_browser_pwa_alone(tmp_path: Path) -> None:
    app = tmp_path / desktop_install.APP_BUNDLE_NAME / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "app_mode_loader").write_text("browser pwa", encoding="utf-8")

    with pytest.raises(desktop_install.InstallError, match="not the Ciaobot desktop app"):
        desktop_install.uninstall_desktop_app(app_dir=tmp_path)

    assert app.exists()


def test_uninstall_boots_out_and_removes_installer_agents(tmp_path: Path) -> None:
    bundle = _native_bundle(tmp_path)
    engine = bundle / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    engine.parent.mkdir(parents=True)
    engine.write_bytes(b"engine")
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    desktop_plist = agents / "Ciaobot.plist"
    server_plist = agents / "com.ciao.server.plist"
    with desktop_plist.open("wb") as stream:
        plistlib.dump({"ProgramArguments": [str(bundle / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME)]}, stream)
    with server_plist.open("wb") as stream:
        plistlib.dump({"ProgramArguments": [str(engine), "run"]}, stream)
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs):
        calls.append(args)
        return desktop_install.subprocess.CompletedProcess(args, 0, "", "")

    result = desktop_install.uninstall_desktop_app(
        app_dir=tmp_path,
        launch_agents_dir=agents,
        uid=501,
        runner=runner,
    )

    assert result["removed_agents"] == [str(desktop_plist), str(server_plist)]
    assert calls == [
        ["launchctl", "bootout", "gui/501/Ciaobot"],
        ["launchctl", "bootout", "gui/501/com.ciao.server"],
    ]
    assert not desktop_plist.exists()
    assert not server_plist.exists()

def test_uninstall_reports_an_empty_directory(tmp_path: Path) -> None:
    assert desktop_install.uninstall_desktop_app(app_dir=tmp_path) == {
        "removed": False,
        "path": str(tmp_path / desktop_install.APP_BUNDLE_NAME),
        "removed_agents": [],
    }


def test_uninstall_removes_the_installers_ciao_shim(tmp_path: Path) -> None:
    """The installer puts a `ciao` shim in ~/.local/bin so terminals have the
    command; leaving it behind would point at a bundle that no longer exists."""
    bundle = _native_bundle(tmp_path)
    shim = tmp_path / "bin" / "ciao"
    shim.parent.mkdir()
    shim.write_text(
        f'#!/bin/sh\n{desktop_install.SHIM_MARKER}\nexec "{bundle}/Contents/Resources/ciao-runtime/bin/ciao" "$@"\n',
        encoding="utf-8",
    )

    result = desktop_install.uninstall_desktop_app(app_dir=tmp_path, shim_path=shim)

    assert result["removed_shim"] == str(shim)
    assert not shim.exists()


def test_uninstall_leaves_a_ciao_it_did_not_install_alone(tmp_path: Path) -> None:
    """`ciao` collides with other projects (Ciao Prolog ships one), and a shim
    naming a different bundle belongs to that install, not this uninstall."""
    _native_bundle(tmp_path)
    foreign = tmp_path / "bin" / "ciao"
    foreign.parent.mkdir()
    foreign.write_text("#!/bin/sh\necho other project\n", encoding="utf-8")
    other_bundle = tmp_path / "elsewhere" / desktop_install.APP_BUNDLE_NAME
    other_shim = tmp_path / "bin" / "ciao-other"
    other_shim.write_text(
        f'#!/bin/sh\n{desktop_install.SHIM_MARKER}\nexec "{other_bundle}/x" "$@"\n',
        encoding="utf-8",
    )

    assert "removed_shim" not in desktop_install.uninstall_desktop_app(
        app_dir=tmp_path, shim_path=foreign
    )
    assert foreign.exists()

    _native_bundle(tmp_path)
    assert "removed_shim" not in desktop_install.uninstall_desktop_app(
        app_dir=tmp_path, shim_path=other_shim
    )
    assert other_shim.exists()


def test_uninstall_keeps_the_shim_when_the_bundle_could_not_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rmtree leaves the app installed, so removing the shim first
    would take away the `ciao` command while what it points at still runs."""
    bundle = _native_bundle(tmp_path)
    shim = tmp_path / "bin" / "ciao"
    shim.parent.mkdir()
    shim.write_text(
        f'#!/bin/sh\n{desktop_install.SHIM_MARKER}\nexec "{bundle}/Contents/Resources/ciao-runtime/bin/ciao" "$@"\n',
        encoding="utf-8",
    )

    def boom(path):
        raise OSError("Operation not permitted")

    monkeypatch.setattr(desktop_install.shutil, "rmtree", boom)

    with pytest.raises(desktop_install.InstallError, match="could not remove"):
        desktop_install.uninstall_desktop_app(app_dir=tmp_path, shim_path=shim)

    assert shim.exists()


def test_uninstall_leaves_a_shim_for_a_prefix_named_bundle_alone(tmp_path: Path) -> None:
    """`str(destination) in content` would also match a longer path that
    merely starts with this bundle's path."""
    _native_bundle(tmp_path)
    other = tmp_path / f"{desktop_install.APP_BUNDLE_NAME}.previous"
    shim = tmp_path / "bin" / "ciao"
    shim.parent.mkdir()
    shim.write_text(
        f'#!/bin/sh\n{desktop_install.SHIM_MARKER}\nexec "{other}/Contents/Resources/ciao-runtime/bin/ciao" "$@"\n',
        encoding="utf-8",
    )

    result = desktop_install.uninstall_desktop_app(app_dir=tmp_path, shim_path=shim)

    assert "removed_shim" not in result
    assert shim.exists()


def test_uninstall_removes_an_orphaned_shim_when_the_bundle_is_already_gone(
    tmp_path: Path,
) -> None:
    """Dragging Ciaobot.app to the Trash leaves the shim behind.

    The uninstall used to return early on a missing bundle without touching
    ~/.local/bin, so `ciao` stayed on PATH exec'ing into a deleted bundle and
    every invocation died with "no such file" — with nothing left to clean it
    up, since the uninstall now has no bundle to key off.
    """
    bundle = tmp_path / desktop_install.APP_BUNDLE_NAME  # never created
    shim = tmp_path / "bin" / "ciao"
    shim.parent.mkdir()
    shim.write_text(
        f'#!/bin/sh\n{desktop_install.SHIM_MARKER}\n'
        f'exec "{bundle}/Contents/Resources/ciao-runtime/bin/ciao" "$@"\n',
        encoding="utf-8",
    )

    result = desktop_install.uninstall_desktop_app(app_dir=tmp_path, shim_path=shim)

    assert result["removed"] is False
    assert result["removed_shim"] == str(shim)
    assert not shim.exists()


def test_uninstall_leaves_a_foreign_shim_alone_when_the_bundle_is_gone(
    tmp_path: Path,
) -> None:
    """The identity check still applies on the bundle-missing path."""
    foreign = tmp_path / "bin" / "ciao"
    foreign.parent.mkdir()
    foreign.write_text("#!/bin/sh\necho other project\n", encoding="utf-8")

    result = desktop_install.uninstall_desktop_app(app_dir=tmp_path, shim_path=foreign)

    assert result["removed"] is False
    assert "removed_shim" not in result
    assert foreign.exists()


def test_uninstall_removes_orphaned_launch_agents_when_the_bundle_is_gone(
    tmp_path: Path,
) -> None:
    """Trashing the bundle by hand orphans the launch agents too.

    Without this, `~/Library/LaunchAgents/Ciaobot.plist` and
    `com.ciao.server.plist` survive the uninstall, launchd keeps respawning a
    binary that no longer exists, and a later reinstall inherits the stale
    plists. The identity check needs no bundle on disk: both sides name the
    same path.
    """
    bundle = tmp_path / desktop_install.APP_BUNDLE_NAME  # never created
    engine = bundle / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    desktop_plist = agents / "Ciaobot.plist"
    server_plist = agents / "com.ciao.server.plist"
    foreign_plist = agents / "com.other.app.plist"
    with desktop_plist.open("wb") as stream:
        plistlib.dump(
            {"ProgramArguments": [
                str(bundle / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME)
            ]},
            stream,
        )
    with server_plist.open("wb") as stream:
        plistlib.dump({"ProgramArguments": [str(engine), "run"]}, stream)
    with foreign_plist.open("wb") as stream:
        plistlib.dump({"ProgramArguments": ["/usr/bin/true"]}, stream)

    def runner(args: list[str], **kwargs):
        return desktop_install.subprocess.CompletedProcess(args, 0, "", "")

    result = desktop_install.uninstall_desktop_app(
        app_dir=tmp_path,
        launch_agents_dir=agents,
        uid=501,
        runner=runner,
    )

    assert result["removed"] is False
    assert result["removed_agents"] == [str(desktop_plist), str(server_plist)]
    assert not desktop_plist.exists()
    assert not server_plist.exists()
    # A plist belonging to something else is never touched.
    assert foreign_plist.exists()


def test_uninstall_leaves_another_installs_agents_alone_when_the_bundle_is_gone(
    tmp_path: Path,
) -> None:
    """The orphan path is exactly where a mis-scoped comparison would bite.

    A filename-only check would pass every test above — the loop only ever
    reads `Ciaobot.plist` and `com.ciao.server.plist`, so a plist under any
    *other* name is skipped for free and proves nothing. What has to hold is
    that agents carrying those two names but pointing at a DIFFERENT bundle
    survive: with `~/Applications/Ciaobot.app` dragged to the Trash while an
    older `/Applications` install is still running, dropping the path
    comparison would boot out and delete the live install's agents.
    """
    other_bundle = tmp_path / "elsewhere" / desktop_install.APP_BUNDLE_NAME
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    desktop_plist = agents / "Ciaobot.plist"
    server_plist = agents / "com.ciao.server.plist"
    with desktop_plist.open("wb") as stream:
        plistlib.dump(
            {"ProgramArguments": [
                str(
                    other_bundle
                    / "Contents"
                    / "MacOS"
                    / desktop_build.APP_EXECUTABLE_NAME
                )
            ]},
            stream,
        )
    with server_plist.open("wb") as stream:
        plistlib.dump(
            {"ProgramArguments": [
                str(
                    other_bundle
                    / "Contents"
                    / "Resources"
                    / "ciao-runtime"
                    / "bin"
                    / "ciao"
                ),
                "run",
            ]},
            stream,
        )
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs):
        calls.append(args)
        return desktop_install.subprocess.CompletedProcess(args, 0, "", "")

    result = desktop_install.uninstall_desktop_app(
        app_dir=tmp_path,  # tmp_path/Ciaobot.app was never created
        launch_agents_dir=agents,
        uid=501,
        runner=runner,
    )

    assert result["removed"] is False
    assert result["removed_agents"] == []
    # Neither booted out nor deleted: they belong to the other install.
    assert calls == []
    assert desktop_plist.exists()
    assert server_plist.exists()


def _shim(engine: Path, *, marker: str | None = None) -> str:
    """A shim body in exactly the shape `scripts/install.sh` writes."""
    return (
        "#!/bin/sh\n"
        f"{desktop_install.SHIM_MARKER if marker is None else marker}\n"
        f'exec "{engine}" "$@"\n'
    )


def test_shim_target_survives_quoting_and_path_variance(tmp_path: Path) -> None:
    """Identity is a resolved path comparison, not a substring match.

    A doubled slash (`CIAO_APP_DIR=~/Applications/`) used to orphan the shim,
    which is why a slash-collapsing helper existed. Parsing the exec target and
    resolving it makes that class of variance — doubled slashes, `.` segments,
    a symlinked Applications dir — stop mattering at once.
    """
    bundle = _native_bundle(tmp_path)
    engine = bundle / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    shim = tmp_path / "bin" / "ciao"
    shim.parent.mkdir()
    doubled = str(engine).replace("/Ciaobot.app", "//Ciaobot.app", 1)
    shim.write_text(_shim(Path(doubled)), encoding="utf-8")

    result = desktop_install.uninstall_desktop_app(app_dir=tmp_path, shim_path=shim)

    assert result["removed_shim"] == str(shim)
    assert not shim.exists()


def test_shim_target_is_not_matched_by_a_prefix_bundle(tmp_path: Path) -> None:
    """`Ciaobot.app` is a path prefix of nothing else, but the check must not
    rely on that: a resolved comparison is per-path-segment by construction."""
    _native_bundle(tmp_path)
    other = tmp_path / "elsewhere" / desktop_install.APP_BUNDLE_NAME
    shim = tmp_path / "bin" / "ciao"
    shim.parent.mkdir()
    shim.write_text(
        _shim(other / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"),
        encoding="utf-8",
    )

    assert "removed_shim" not in desktop_install.uninstall_desktop_app(
        app_dir=tmp_path, shim_path=shim
    )
    assert shim.exists()


def test_shim_exec_target_requires_the_marker(tmp_path: Path) -> None:
    """A `ciao` with our exec line but no marker is not ours to delete."""
    engine = tmp_path / "x" / "ciao"
    assert desktop_install.shim_exec_target(_shim(engine)) == engine
    assert desktop_install.shim_exec_target(_shim(engine, marker="# someone else")) is None
    assert desktop_install.shim_exec_target("#!/bin/sh\necho hi\n") is None
