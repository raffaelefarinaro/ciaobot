"""Removal of a legacy Ciaobot.app bundle left by an older install."""
from __future__ import annotations

import errno
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ciao import desktop_install


def _native_bundle(root: Path) -> Path:
    executable = root / desktop_install.APP_BUNDLE_NAME / "Contents" / "MacOS"
    executable.mkdir(parents=True)
    (executable / desktop_install.APP_EXECUTABLE_NAME).write_bytes(b"native app")
    return executable.parent.parent


def _tree_bytes(root: Path) -> dict[str, tuple[str, bytes | str]]:
    result: dict[str, tuple[str, bytes | str]] = {}
    for path in root.rglob("*"):
        name = str(path.relative_to(root))
        if path.is_symlink():
            result[name] = ("link", os.readlink(path))
        elif path.is_file():
            result[name] = ("file", path.read_bytes())
        else:
            result[name] = ("directory", "")
    return result


@pytest.fixture
def retired_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    bundle = _native_bundle(home / "Applications")
    sentinels = {
        home / "Library/LaunchAgents/com.ciao.server.plist": b"server plist",
        home / "Library/LaunchAgents/Ciaobot.plist": b"desktop plist",
        home / ".local/bin/ciao": b"shim",
        home / "workspace/.runtime/state.json": b"workspace",
        tmp_path / "outside/important": b"outside Applications",
    }
    for path, content in sentinels.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(home / "memory"))
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(home / "Library/LaunchAgents"))
    return home, bundle


@pytest.mark.parametrize("case", [
    "relative", "dotdot", "wrong_root", "root", "wrong_name", "server_host",
    "parent_link", "bundle_link", "dangling_bundle", "contents_link", "macos_link",
    "missing_executable", "nonregular_executable", "executable_link", "replaced_executable", "replaced_bundle",
    "missing_identity", "short_identity", "long_identity", "list_identity", "negative_identity", "wrong_identity_type",
])
def test_retired_cleanup_refusals_preserve_every_byte(
    tmp_path: Path, retired_tree: tuple[Path, Path], case: str,
) -> None:
    home, bundle = retired_tree
    identity = desktop_install.inspect_retired_bundle(bundle, home=home)
    expected: object = identity
    candidate = bundle
    executable = bundle / "Contents/MacOS/ciaobot-desktop"
    if case == "relative":
        candidate = Path("Ciaobot.app")
    elif case == "dotdot":
        candidate = bundle.parent / ".." / "Applications" / bundle.name
    elif case == "wrong_root":
        candidate = _native_bundle(tmp_path / "foreign")
    elif case == "root":
        candidate = bundle.parent
    elif case in ("wrong_name", "server_host"):
        candidate = bundle.with_name("Other.app" if case == "wrong_name" else "Ciaobot Server.app")
        candidate.mkdir()
    elif case == "parent_link":
        link = bundle.parent / "link"
        link.symlink_to(bundle.parent, target_is_directory=True)
        candidate = link / bundle.name
    elif case in ("bundle_link", "dangling_bundle"):
        saved = bundle.with_name("saved.app")
        bundle.rename(saved)
        bundle.symlink_to(saved if case == "bundle_link" else tmp_path / "missing", target_is_directory=True)
    elif case in ("contents_link", "macos_link"):
        original = bundle / ("Contents" if case == "contents_link" else "Contents/MacOS")
        saved = tmp_path / "saved"
        original.rename(saved)
        original.symlink_to(saved, target_is_directory=True)
    elif case == "missing_executable":
        executable.unlink()
    elif case == "nonregular_executable":
        executable.unlink()
        executable.mkdir()
    elif case == "executable_link":
        executable.rename(executable.with_name("saved"))
        executable.symlink_to(executable.with_name("saved"))
    elif case == "replaced_executable":
        executable.rename(executable.with_name("saved"))
        executable.write_bytes(b"replacement")
    elif case == "replaced_bundle":
        bundle.rename(bundle.with_name("saved.app"))
        _native_bundle(bundle.parent)
    elif case == "missing_identity":
        expected = None
    elif case == "short_identity":
        expected = identity[:3]
    elif case == "long_identity":
        expected = (*identity, 1)
    elif case == "list_identity":
        expected = list(identity)
    elif case == "negative_identity":
        expected = (-1, *identity[1:])
    elif case == "wrong_identity_type":
        expected = (str(identity[0]), *identity[1:])
    before = _tree_bytes(tmp_path)
    assert desktop_install.remove_retired_bundle(candidate, expected_identity=expected, home=home) == "refused"
    assert _tree_bytes(tmp_path) == before


def test_retired_cleanup_invalid_identity_refused_when_bundle_absent(retired_tree: tuple[Path, Path]) -> None:
    home, bundle = retired_tree
    bundle.rename(bundle.with_name("saved.app"))
    assert desktop_install.remove_retired_bundle(bundle, expected_identity=None, home=home) == "refused"


def test_retired_cleanup_absent_requires_existing_safe_parent(
    tmp_path: Path, retired_tree: tuple[Path, Path],
) -> None:
    home, bundle = retired_tree
    identity = desktop_install.inspect_retired_bundle(bundle, home=home)
    bundle.rename(bundle.with_name("saved.app"))
    before = _tree_bytes(tmp_path)
    assert desktop_install.remove_retired_bundle(bundle, expected_identity=identity, home=home) == "absent"
    missing_parent = bundle.parent / "missing" / bundle.name
    assert desktop_install.remove_retired_bundle(missing_parent, expected_identity=identity, home=home) == "refused"
    assert _tree_bytes(tmp_path) == before


def test_retired_cleanup_success_preserves_service_shim_workspace_and_outside(
    tmp_path: Path, retired_tree: tuple[Path, Path],
) -> None:
    home, bundle = retired_tree
    identity = desktop_install.inspect_retired_bundle(bundle, home=home)
    before = _tree_bytes(tmp_path)
    bundle_key = str(bundle.relative_to(tmp_path))
    expected = {name: value for name, value in before.items()
                if name != bundle_key and not name.startswith(bundle_key + os.sep)}
    assert desktop_install.remove_retired_bundle(bundle, expected_identity=identity, home=home) == "removed"
    assert _tree_bytes(tmp_path) == expected


def test_retired_cleanup_cli_identity_and_closed_status(
    tmp_path: Path, retired_tree: tuple[Path, Path],
) -> None:
    home, bundle = retired_tree
    command = [sys.executable, "-m", "ciao.desktop_install"]
    before = _tree_bytes(tmp_path)
    inspected = subprocess.run(
        [*command, "inspect-retired-bundle", "--path", str(bundle)],
        capture_output=True, text=True, check=False,
    )
    assert inspected.returncode == 0 and inspected.stderr == ""
    assert re.fullmatch(r"[0-9]+ [0-9]+ [0-9]+ [0-9]+\n", inspected.stdout)
    fields = inspected.stdout.split()
    assert tuple(map(int, fields)) == desktop_install.inspect_retired_bundle(bundle, home=home)
    base = [*command, "remove-retired-bundle", "--path", str(bundle), "--identity"]
    negative = subprocess.run([*base, "-1", *fields[1:]], capture_output=True, text=True, check=False)
    assert negative.returncode != 0 and negative.stdout == "refused\n"
    for invalid in (fields[:3], [*fields, "1"], ["not-a-number", *fields[1:]]):
        result = subprocess.run([*base, *invalid], capture_output=True, text=True, check=False)
        assert result.returncode != 0 and result.stdout == ""
    assert _tree_bytes(tmp_path) == before
    removed = subprocess.run([*base, *fields], capture_output=True, text=True, check=False)
    assert removed.returncode == 0 and removed.stdout == "removed\n"
    absent = subprocess.run([*base, *fields], capture_output=True, text=True, check=False)
    assert absent.returncode == 0 and absent.stdout == "absent\n"


def test_retired_cleanup_validates_identity_and_only_removes_bundle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    bundle = _native_bundle(home / "Applications")
    outside = home / "workspace"
    outside.write_bytes(b"keep")
    identity = desktop_install.inspect_retired_bundle(bundle, home=home)
    assert desktop_install.remove_retired_bundle(bundle, expected_identity=(0, 0, 0, 0), home=home) == "refused"
    assert bundle.exists()
    assert desktop_install.remove_retired_bundle(bundle, expected_identity=identity, home=home) == "removed"
    assert outside.read_bytes() == b"keep"
    assert desktop_install.remove_retired_bundle(bundle, expected_identity=identity, home=home) == "absent"


def test_retired_cleanup_rejects_untrusted_bundle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    bundle = _native_bundle(home / "Applications")
    identity = desktop_install.inspect_retired_bundle(bundle, home=home)
    foreign = _native_bundle(tmp_path / "foreign")
    linked = home / "Applications" / "link"
    linked.symlink_to(foreign.parent, target_is_directory=True)
    for path in (Path("Ciaobot.app"), bundle.parent / ".." / "Applications" / bundle.name,
                 foreign, bundle.parent, bundle.parent / "Ciaobot Server.app", linked / bundle.name):
        assert desktop_install.remove_retired_bundle(path, expected_identity=identity, home=home) == "refused"
    executable = bundle / "Contents/MacOS/ciaobot-desktop"
    executable.rename(executable.with_name("old"))
    executable.write_bytes(b"replacement")
    assert desktop_install.remove_retired_bundle(bundle, expected_identity=identity, home=home) == "refused"
    assert foreign.exists() and bundle.exists()
    identity = desktop_install.inspect_retired_bundle(bundle, home=home)
    bundle.rename(bundle.with_name("old.app"))
    replacement = _native_bundle(home / "Applications")
    assert desktop_install.remove_retired_bundle(replacement, expected_identity=identity, home=home) == "refused"
    assert replacement.exists()
    replacement.rename(replacement.with_name("replacement.app"))
    replacement.symlink_to(foreign, target_is_directory=True)
    assert desktop_install.remove_retired_bundle(replacement, expected_identity=identity, home=home) == "refused"
    replacement.unlink()
    replacement.symlink_to(tmp_path / "missing", target_is_directory=True)
    assert desktop_install.remove_retired_bundle(replacement, expected_identity=identity, home=home) == "refused"
    assert foreign.exists()


def test_retired_cleanup_does_not_follow_descendant_symlinks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    bundle = _native_bundle(home / "Applications")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "important").write_bytes(b"keep")
    (bundle / "link").symlink_to(outside, target_is_directory=True)
    identity = desktop_install.inspect_retired_bundle(bundle, home=home)
    assert desktop_install.remove_retired_bundle(bundle, expected_identity=identity, home=home) == "removed"
    assert (outside / "important").read_bytes() == b"keep"


def test_retired_cleanup_removed_absent_permission_denied_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired_tree: tuple[Path, Path],
) -> None:
    home, bundle = retired_tree
    (bundle / "extra").write_bytes(b"extra")
    identity = desktop_install.inspect_retired_bundle(bundle, home=home)
    before = _tree_bytes(tmp_path)
    real_unlink = os.unlink
    calls = 0

    def deny(path: object, *args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise PermissionError(errno.EPERM, "denied")

    with monkeypatch.context() as patch:
        patch.setattr(desktop_install.os, "unlink", deny)
        assert desktop_install.remove_retired_bundle(bundle, expected_identity=identity, home=home) == "permission_denied"
    assert calls == 1 and bundle.exists()
    assert _tree_bytes(tmp_path) == before

    calls = 0
    def deny_second(path: object, *args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError(errno.EPERM, "denied")
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(desktop_install.os, "unlink", deny_second)
        assert desktop_install.remove_retired_bundle(bundle, expected_identity=identity, home=home) == "partial"
    assert calls == 2 and bundle.exists()
    after = _tree_bytes(tmp_path)
    assert after.keys() < before.keys()
    assert all(before[name] == value for name, value in after.items())
    for name in ("home/Library/LaunchAgents/com.ciao.server.plist", "home/.local/bin/ciao",
                 "home/workspace/.runtime/state.json", "outside/important"):
        assert after[str(Path(name))] == before[str(Path(name))]


@pytest.mark.skipif(not hasattr(os, "geteuid") or getattr(os, "geteuid", lambda: 0)() == 0,
                    reason="real chmod denial requires a non-root POSIX account")
def test_retired_cleanup_real_unprivileged_permission_denial(tmp_path: Path) -> None:
    home = tmp_path / "home"
    bundle = _native_bundle(home / "Applications")
    identity = desktop_install.inspect_retired_bundle(bundle, home=home)
    directory = bundle / "Contents/MacOS"
    directory.chmod(0o555)
    try:
        assert desktop_install.remove_retired_bundle(bundle, expected_identity=identity, home=home) == "permission_denied"
        assert (directory / "ciaobot-desktop").is_file()
    finally:
        directory.chmod(0o755)


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
        plistlib.dump({"ProgramArguments": [str(bundle / "Contents" / "MacOS" / desktop_install.APP_EXECUTABLE_NAME)]}, stream)
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
                str(bundle / "Contents" / "MacOS" / desktop_install.APP_EXECUTABLE_NAME)
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
                    / desktop_install.APP_EXECUTABLE_NAME
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
    """A shim body in the shape the retired app installer wrote."""
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
