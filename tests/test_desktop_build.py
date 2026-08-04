"""Dev-mode rebuild of the Tauri desktop shell during Settings → Restart."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from ciao import desktop_build
from ciao.web.routes_api import _checkout_problem, _resolve_codebase_root

# Captured before the autouse stub below replaces it, so the probe's own tests
# can restore the real implementation.
_REAL_MISSING_TOOLCHAIN = desktop_build._missing_rust_toolchain


@pytest.fixture(autouse=True)
def _assume_toolchain_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the build tests independent of whether this host has cargo.

    CI runs pytest before it sets up Rust, so a real toolchain probe here would
    make these pass or fail on runner-image trivia. The probe has its own tests
    below, which drive it explicitly.
    """
    monkeypatch.setattr(desktop_build, "_missing_rust_toolchain", lambda: None)


class FakeRunner:
    """Stands in for routes_api._run_step, recording argv per call."""

    def __init__(self, results: dict[str, subprocess.CompletedProcess] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, args, *, cwd, timeout):
        self.calls.append(list(args))
        key = " ".join(args[:2])
        if key in self.results:
            return self.results[key]
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")


def _make_repo(tmp_path: Path, *, bundle: bool = True) -> Path:
    """A checkout whose desktop sources all predate the built bundle."""
    repo = tmp_path / "ciaobot"
    desktop = repo / "desktop"
    (desktop / "src-tauri" / "src").mkdir(parents=True)
    (desktop / "src-tauri" / "tauri.conf.json").write_text("{}", encoding="utf-8")
    (desktop / "src-tauri" / "src" / "lib.rs").write_text("fn main() {}", encoding="utf-8")
    (desktop / "src").mkdir()
    (desktop / "src" / "desktop.css").write_text("body{}", encoding="utf-8")
    for path in desktop.rglob("*"):
        if path.is_file():
            _touch(path, 500.0)
    if bundle:
        _write_bundle(desktop_build.built_bundle(repo), mtime=1_000.0)
    return repo


def _write_bundle(bundle: Path, *, mtime: float) -> Path:
    binary = bundle / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("macho", encoding="utf-8")
    import os

    os.utime(binary, (mtime, mtime))
    return bundle


def _touch(path: Path, mtime: float) -> None:
    import os

    os.utime(path, (mtime, mtime))


def test_the_executable_name_matches_the_cargo_package() -> None:
    """The Mach-O is named after the Cargo package, not tauri's productName.

    `productName: "Ciaobot"` names the .app wrapper; the binary inside it comes
    from `[package] name` in Cargo.toml. Assuming "Ciaobot" for both made
    build_and_stage reject every real build as "no executable", which returned
    500 from the deploy and stopped Settings -> Restart before it restarted.
    Every fixture in this file fabricates the bundle, so only reading the real
    manifest catches a drift here.
    """
    cargo_toml = Path(__file__).resolve().parents[1] / "desktop" / "src-tauri" / "Cargo.toml"
    package_name = ""
    in_package = False
    for line in cargo_toml.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_package = stripped == "[package]"
            continue
        if in_package and stripped.startswith("name"):
            package_name = stripped.split("=", 1)[1].strip().strip('"')
            break
    assert package_name, "could not read [package] name from Cargo.toml"
    assert desktop_build.APP_EXECUTABLE_NAME == package_name


def test_bundle_executable_falls_back_to_whatever_is_in_macos(tmp_path: Path) -> None:
    """A renamed binary should degrade the freshness check, not break the build."""
    bundle = tmp_path / "Ciaobot.app"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "something-else").write_text("macho", encoding="utf-8")

    assert desktop_build.bundle_executable(bundle) == macos / "something-else"
    # And an empty bundle is still reported as having no executable.
    empty = tmp_path / "Empty.app"
    (empty / "Contents" / "MacOS").mkdir(parents=True)
    assert desktop_build.bundle_executable(empty) is None


def test_rebuild_is_skipped_when_desktop_sources_are_older(tmp_path: Path) -> None:
    # The whole point of the freshness check: a Python-only change must not pay
    # for a multi-minute Rust build on every restart.
    repo = _make_repo(tmp_path)
    _touch(repo / "desktop" / "src-tauri" / "src" / "lib.rs", 500.0)
    install_dir = tmp_path / "Applications"
    _write_bundle(desktop_build.installed_bundle(install_dir), mtime=1_000.0)

    needed, reason = desktop_build.needs_rebuild(repo, install_dir=install_dir)
    assert needed is False
    assert "unchanged" in reason


def test_rebuild_is_needed_when_a_rust_source_is_newer(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    install_dir = tmp_path / "Applications"
    _write_bundle(desktop_build.installed_bundle(install_dir), mtime=1_000.0)
    _touch(repo / "desktop" / "src-tauri" / "src" / "lib.rs", 2_000.0)

    needed, reason = desktop_build.needs_rebuild(repo, install_dir=install_dir)
    assert needed is True
    assert "lib.rs" in reason


def test_rebuild_ignores_target_and_node_modules_churn(tmp_path: Path) -> None:
    # target/ is rewritten by the build itself, so watching it would make the
    # check permanently true and defeat the skip.
    repo = _make_repo(tmp_path)
    _touch(repo / "desktop" / "src-tauri" / "src" / "lib.rs", 500.0)
    install_dir = tmp_path / "Applications"
    _write_bundle(desktop_build.installed_bundle(install_dir), mtime=1_000.0)

    churn = repo / "desktop" / "node_modules" / "left-pad" / "index.js"
    churn.parent.mkdir(parents=True)
    churn.write_text("x", encoding="utf-8")
    _touch(churn, 9_000.0)

    needed, _ = desktop_build.needs_rebuild(repo, install_dir=install_dir)
    assert needed is False


def test_rebuild_tracks_the_bundled_icons_outside_desktop(tmp_path: Path) -> None:
    # tauri.conf.json points its icon paths at ciao/stock/deploy, so an icon
    # change is a bundle change even though nothing under desktop/ moved.
    repo = _make_repo(tmp_path)
    install_dir = tmp_path / "Applications"
    _write_bundle(desktop_build.installed_bundle(install_dir), mtime=1_000.0)
    icon = repo / "ciao" / "stock" / "deploy" / "Ciaobot.icns"
    icon.parent.mkdir(parents=True)
    icon.write_text("icns", encoding="utf-8")
    _touch(icon, 2_000.0)

    needed, reason = desktop_build.needs_rebuild(repo, install_dir=install_dir)
    assert needed is True
    assert reason.startswith("ciao/stock/deploy/Ciaobot.icns")


def test_rebuild_is_needed_when_the_build_never_reached_applications(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _touch(repo / "desktop" / "src-tauri" / "src" / "lib.rs", 500.0)

    needed, reason = desktop_build.needs_rebuild(repo, install_dir=tmp_path / "Applications")
    assert needed is True
    assert "not installed" in reason


def test_rebuild_is_skipped_without_a_tauri_project(tmp_path: Path) -> None:
    # A vault-only checkout (or any repo without desktop/) must not fail the
    # deploy, just skip the step.
    needed, reason = desktop_build.needs_rebuild(tmp_path, install_dir=tmp_path / "Applications")
    assert needed is False
    assert "no Tauri project" in reason


def test_build_stages_beside_the_installed_app_without_touching_it(tmp_path: Path) -> None:
    # Staging instead of overwriting is what keeps the running app from reading
    # pages out of a deleted bundle while the build finishes.
    repo = _make_repo(tmp_path)
    install_dir = tmp_path / "Applications"
    install_dir.mkdir()
    live = _write_bundle(desktop_build.installed_bundle(install_dir), mtime=1_000.0)
    (repo / "desktop" / "node_modules").mkdir()

    runner = FakeRunner()

    def dispatch(args, *, cwd, timeout):
        result = runner(args, cwd=cwd, timeout=timeout)
        if args[0] == "ditto":
            _write_bundle(Path(args[2]), mtime=3_000.0)
        return result

    steps, staged = desktop_build.build_and_stage(repo, runner=dispatch, install_dir=install_dir)

    assert staged is True
    assert [s["step"] for s in steps] == ["desktop npm build", "desktop tauri build", "desktop stage"]
    assert all(s["ok"] for s in steps)
    assert (install_dir / desktop_build.STAGING_NAME).exists()
    # The live app is untouched until install_staged_and_relaunch runs.
    assert (live / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME).read_text(encoding="utf-8") == "macho"


def test_build_installs_npm_deps_only_when_node_modules_is_missing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    install_dir = tmp_path / "Applications"
    install_dir.mkdir()
    runner = FakeRunner({
        "npm run": subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="vite failed"),
    })

    steps, staged = desktop_build.build_and_stage(repo, runner=runner, install_dir=install_dir)

    assert staged is False
    assert steps[0]["step"] == "desktop npm install"
    assert steps[-1] == {"step": "desktop npm build", "ok": False, "output": "vite failed"}


def test_build_does_not_stage_when_tauri_reports_success_without_a_binary(tmp_path: Path) -> None:
    # A green tauri exit code with no Mach-O is the failure mode that would
    # otherwise ship an empty bundle straight into /Applications.
    repo = _make_repo(tmp_path, bundle=False)
    (repo / "desktop" / "node_modules").mkdir()
    install_dir = tmp_path / "Applications"
    install_dir.mkdir()

    steps, staged = desktop_build.build_and_stage(repo, runner=FakeRunner(), install_dir=install_dir)

    assert staged is False
    assert steps[-1]["ok"] is False
    assert "no executable" in steps[-1]["output"]


def test_install_quits_before_swapping_then_reopens(tmp_path: Path) -> None:
    install_dir = tmp_path / "Applications"
    install_dir.mkdir()
    _write_bundle(desktop_build.installed_bundle(install_dir), mtime=1_000.0)
    staging = _write_bundle(install_dir / desktop_build.STAGING_NAME, mtime=3_000.0)
    (staging / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME).write_text("new", encoding="utf-8")

    order: list[str] = []
    alive = {"value": True}

    def runner(args, *, cwd, timeout):
        order.append(args[0])
        if args[0] == "pgrep":
            if alive["value"]:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="4242", stderr="")
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
        if args[0] == "osascript":
            alive["value"] = False
        if args[0] == "open":
            # The swap must already have happened by the time the app relaunches.
            assert (desktop_build.installed_bundle(install_dir) / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME).read_text(
                encoding="utf-8"
            ) == "new"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    steps = desktop_build.install_staged_and_relaunch(runner=runner, install_dir=install_dir)

    assert steps == [{
        "step": "desktop install",
        "ok": True,
        "output": f"installed and relaunched {desktop_build.installed_bundle(install_dir)}",
    }]
    assert order.index("osascript") < order.index("open")
    assert not (install_dir / desktop_build.STAGING_NAME).exists()


def test_install_leaves_the_bundle_staged_when_the_app_refuses_to_quit(tmp_path: Path) -> None:
    # Swapping under a live process is the thing we are avoiding, so a stuck
    # app must abort the install rather than force it.
    install_dir = tmp_path / "Applications"
    install_dir.mkdir()
    _write_bundle(desktop_build.installed_bundle(install_dir), mtime=1_000.0)
    _write_bundle(install_dir / desktop_build.STAGING_NAME, mtime=3_000.0)

    opened: list[list[str]] = []
    clock = {"now": 0.0}

    def runner(args, *, cwd, timeout):
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="4242", stderr="")
        opened.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    steps = desktop_build.install_staged_and_relaunch(
        runner=runner,
        install_dir=install_dir,
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        monotonic=lambda: clock["now"],
    )

    assert steps[0]["ok"] is False
    assert "did not quit" in steps[0]["output"]
    assert [a[0] for a in opened] == ["osascript"]
    assert (install_dir / desktop_build.STAGING_NAME).exists()


def test_install_launches_the_app_when_none_is_running(tmp_path: Path) -> None:
    # A bare `tell application id ... to quit` launches a stopped app, so the
    # quit must be skipped entirely rather than relying on AppleScript.
    install_dir = tmp_path / "Applications"
    install_dir.mkdir()
    _write_bundle(install_dir / desktop_build.STAGING_NAME, mtime=3_000.0)

    seen: list[str] = []

    def runner(args, *, cwd, timeout):
        seen.append(args[0])
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    steps = desktop_build.install_staged_and_relaunch(runner=runner, install_dir=install_dir)

    assert steps[0]["ok"] is True
    assert "osascript" not in seen
    assert desktop_build.installed_bundle(install_dir).exists()


def test_install_skips_the_swap_when_pgrep_cannot_answer(tmp_path: Path) -> None:
    # `_run_step` reports a missing binary as 127 and a timeout as 124. Reading
    # either as "not running" would delete the bundle under a live app, so an
    # unreadable probe has to abort and leave the staged copy alone.
    install_dir = tmp_path / "Applications"
    install_dir.mkdir()
    _write_bundle(desktop_build.installed_bundle(install_dir), mtime=1_000.0)
    _write_bundle(install_dir / desktop_build.STAGING_NAME, mtime=3_000.0)

    seen: list[str] = []

    def runner(args, *, cwd, timeout):
        seen.append(args[0])
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(
                args=args, returncode=127, stdout="", stderr="pgrep not found on PATH"
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    steps = desktop_build.install_staged_and_relaunch(runner=runner, install_dir=install_dir)

    assert steps[0]["ok"] is False
    assert "could not tell whether" in steps[0]["output"]
    # Nothing was quit, nothing was swapped, nothing was opened.
    assert seen == ["pgrep"]
    assert (install_dir / desktop_build.STAGING_NAME).exists()
    assert desktop_build.installed_bundle(install_dir).exists()


def test_install_restores_the_old_app_when_the_swap_fails(tmp_path: Path) -> None:
    # The installed bundle is moved aside rather than deleted, so a failed swap
    # leaves a working app in /Applications instead of a gutted one.
    install_dir = tmp_path / "Applications"
    install_dir.mkdir()
    destination = desktop_build.installed_bundle(install_dir)
    _write_bundle(destination, mtime=1_000.0)
    (destination / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME).write_text("old", encoding="utf-8")
    staging = install_dir / desktop_build.STAGING_NAME
    _write_bundle(staging, mtime=3_000.0)

    real_rename = Path.rename

    def flaky_rename(self, target):
        # Fail only the staged -> installed move, after the old app is aside.
        if Path(self) == staging:
            raise OSError("Operation not permitted")
        return real_rename(self, target)

    def runner(args, *, cwd, timeout):
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    with mock.patch.object(Path, "rename", flaky_rename):
        steps = desktop_build.install_staged_and_relaunch(runner=runner, install_dir=install_dir)

    assert steps[0]["ok"] is False
    assert "could not install" in steps[0]["output"]
    # The working app is back where it belongs, not left as a hole.
    assert (destination / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME).read_text(encoding="utf-8") == "old"


def test_tauri_build_skips_the_dmg_and_the_signed_updater_artifacts(tmp_path: Path) -> None:
    # Updater artifacts need TAURI_SIGNING_PRIVATE_KEY, which a dev machine
    # does not have, and nothing consumes a local latest.json.
    repo = _make_repo(tmp_path)
    (repo / "desktop" / "node_modules").mkdir()
    install_dir = tmp_path / "Applications"
    install_dir.mkdir()
    runner = FakeRunner()

    desktop_build.build_and_stage(repo, runner=runner, install_dir=install_dir)

    tauri = next(call for call in runner.calls if call[:4] == ["npm", "run", "tauri", "build"])
    assert "--bundles" in tauri and tauri[tauri.index("--bundles") + 1] == "app"
    assert '{"bundle":{"createUpdaterArtifacts":false}}' in tauri
    assert "--target" not in tauri


class _Config:
    def __init__(self, app_repo=None):
        self.app_repo = app_repo


def test_codebase_root_prefers_the_configured_checkout() -> None:
    assert _resolve_codebase_root(_Config(Path("/repos/ciaobot"))) == Path("/repos/ciaobot")


def test_codebase_root_falls_back_to_the_module_path() -> None:
    root = _resolve_codebase_root(_Config())
    assert (root / "ciao" / "web" / "routes_api.py").exists()


def test_installed_engine_without_app_repo_is_reported_as_the_real_problem(tmp_path: Path) -> None:
    # An engine installed from Homebrew resolves to site-packages: no .git, no
    # web/. Deploy used to surface this as "not a git repository".
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    assert "not a git checkout" in _checkout_problem(site_packages)


def test_checkout_with_git_file_and_web_package_passes(tmp_path: Path) -> None:
    # A git worktree has .git as a file, not a directory.
    repo = tmp_path / "wt"
    (repo / "web").mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /elsewhere", encoding="utf-8")
    (repo / "web" / "package.json").write_text("{}", encoding="utf-8")
    assert _checkout_problem(repo) == ""


# ── Toolchain probe and failure reporting ─────────────────────────────────


def test_missing_cargo_is_reported_with_the_fix_instead_of_a_doomed_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No cargo anywhere: stop before `tauri build` dies inside cargo."""
    monkeypatch.setattr(desktop_build, "_missing_rust_toolchain", _REAL_MISSING_TOOLCHAIN)
    monkeypatch.setattr(desktop_build.shutil, "which", lambda _name: None)
    monkeypatch.setattr(desktop_build, "_CARGO_SEARCH_DIRS", (tmp_path / "nowhere",))
    repo = _make_repo(tmp_path)
    (repo / "desktop" / "node_modules").mkdir()
    install_dir = tmp_path / "Applications"
    install_dir.mkdir()
    runner = FakeRunner()

    steps, staged = desktop_build.build_and_stage(
        repo, runner=runner, install_dir=install_dir
    )

    assert staged is False
    assert steps == [{
        "step": "desktop toolchain",
        "ok": False,
        "output": steps[0]["output"],
    }]
    assert "cargo was not found" in steps[0]["output"]
    assert "rustup" in steps[0]["output"]
    assert runner.calls == [], "must not start a build that cannot succeed"


def test_cargo_outside_path_is_found_and_prepended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Homebrew's rustup keeps cargo in opt/rustup/bin, not /opt/homebrew/bin."""
    hidden = tmp_path / "opt" / "rustup" / "bin"
    hidden.mkdir(parents=True)
    (hidden / "cargo").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(desktop_build, "_CARGO_SEARCH_DIRS", (hidden,))
    monkeypatch.setattr(desktop_build.shutil, "which", lambda _name: None)
    monkeypatch.setenv("PATH", "/usr/bin")

    added = desktop_build.ensure_cargo_on_path()

    assert added == str(hidden)
    import os
    assert os.environ["PATH"].startswith(f"{hidden}:")


def test_ensure_cargo_on_path_is_a_noop_when_cargo_already_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop_build.shutil, "which", lambda _name: "/usr/bin/cargo")
    monkeypatch.setenv("PATH", "/usr/bin")

    assert desktop_build.ensure_cargo_on_path() is None
    import os
    assert os.environ["PATH"] == "/usr/bin"


def test_failure_output_keeps_stderr_when_stdout_has_the_npm_echo(
    tmp_path: Path,
) -> None:
    """The real cause is on stderr; npm's command echo is on stdout.

    Reporting only stdout is what turned every desktop build failure into an
    unexplained `> tauri build --bundles app ...` with no reason attached.
    """
    repo = _make_repo(tmp_path)
    (repo / "desktop" / "node_modules").mkdir()
    install_dir = tmp_path / "Applications"
    install_dir.mkdir()
    runner = FakeRunner({
        "npm run": subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="> ciaobot-desktop@0.7.1 tauri\n> tauri build --bundles app",
            stderr="error: linker `cc` not found",
        ),
    })

    steps, staged = desktop_build.build_and_stage(
        repo, runner=runner, install_dir=install_dir
    )

    assert staged is False
    assert "linker `cc` not found" in steps[-1]["output"]
