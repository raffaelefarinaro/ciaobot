"""Dev-mode rebuild and reinstall of the macOS Tauri desktop shell.

Settings -> Restart rebuilds the engine and the PWA from source, but the
desktop app is a separate bundle: changes under ``desktop/`` (the Rust tray,
the startup shell) only reach the window Raffa is looking at after the bundle
is rebuilt and copied over the installed app. This module adds that as a
deploy step for instances running with ``CIAO_DEV_MODE``, so released installs
never try to run cargo.

Three constraints shape the implementation:

* A release Rust build costs minutes, so the step is skipped unless a watched
  source under ``desktop/`` is newer than the built bundle.
* ``tauri-plugin-single-instance`` makes ``open`` focus the running instance
  instead of launching the new binary, so a relaunch has to quit first.
* Deleting a bundle while its process runs leaves the app reading pages from a
  removed inode. So the build stages the new bundle next to the installed one
  and the swap happens between the quit and the relaunch, once the deploy
  response is already on the wire.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

APP_BUNDLE_NAME = "Ciaobot.app"
# The Mach-O inside the bundle is named after the Cargo package
# (desktop/src-tauri/Cargo.toml `name`), NOT after tauri.conf.json
# `productName`, which only names the .app wrapper. Getting this wrong makes
# every freshness check fall back to the directory mtime and every build fail
# its "did tauri actually produce a binary" check.
APP_EXECUTABLE_NAME = "ciaobot-desktop"
BUNDLE_IDENTIFIER = "local.ciaobot.app"
INSTALL_DIR = Path("/Applications")
STAGING_NAME = ".Ciaobot.app.deploy"

# The native-arch release bundle. Dev builds skip the universal target that
# releases use: it builds both architectures, and the extra one is dead weight
# on the machine doing the build.
BUNDLE_REL_PATH = Path("src-tauri/target/release/bundle/macos") / APP_BUNDLE_NAME

# Everything that gets compiled or bundled into the .app, repo-relative.
# Deliberately not the whole desktop/ tree: target/, node_modules/, and
# src-tauri/gen/ are rewritten by every build, so watching them would make the
# freshness check permanently true and defeat the skip. The two icons live
# outside desktop/ because tauri.conf.json points at the engine's stock assets.
WATCHED_SOURCES = (
    "desktop/src",
    "desktop/startup.html",
    "desktop/package.json",
    "desktop/package-lock.json",
    "desktop/vite.config.ts",
    "desktop/src-tauri/src",
    "desktop/src-tauri/build.rs",
    "desktop/src-tauri/capabilities",
    "desktop/src-tauri/permissions",
    "desktop/src-tauri/Cargo.toml",
    "desktop/src-tauri/Cargo.lock",
    "desktop/src-tauri/tauri.conf.json",
    # The native voice sidecar. `npm run tauri build` rebuilds it via the
    # pretauri hook, but the freshness check has to see the Swift source or a
    # voice-only change would be skipped as "sources unchanged".
    "desktop/native",
    "ciao/stock/deploy/face_template.png",
    "ciao/stock/deploy/Ciaobot.icns",
)

# A cold cargo release build of the shell runs well past ten minutes on a
# laptop, and a timeout here means a half-built bundle plus a failed deploy.
BUILD_TIMEOUT_S = 2400
NPM_TIMEOUT_S = 600
# How long the app gets to honor an AppleScript quit before the relaunch is
# reported as failed and left to the operator.
QUIT_TIMEOUT_S = 20.0

# A step runner with the same signature as routes_api._run_step: it turns a
# missing binary or a timeout into a failed CompletedProcess instead of an
# exception, so a deploy reports a structured step rather than a 500.
Runner = Callable[..., subprocess.CompletedProcess]


def run_step(args: list[str], *, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Default ``Runner`` for callers outside the web deploy handler.

    Same contract as ``routes_api._run_step``: a missing binary or a timeout
    becomes a failed CompletedProcess rather than an exception, so a caller
    reports a structured step instead of a traceback. Exists here so the CLI
    install path can reuse the swap logic below without importing the web layer.
    """
    try:
        return subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            args=args, returncode=127, stdout="",
            stderr=f"{args[0]} not found on PATH: {exc}",
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=args, returncode=124, stdout="",
            stderr=f"{args[0]} timed out after {timeout}s",
        )


def desktop_dir(repo: Path) -> Path:
    return Path(repo) / "desktop"


def built_bundle(repo: Path) -> Path:
    return desktop_dir(repo) / BUNDLE_REL_PATH


def installed_bundle(install_dir: Path = INSTALL_DIR) -> Path:
    return Path(install_dir) / APP_BUNDLE_NAME


def bundle_executable(bundle: Path) -> Path | None:
    """The Mach-O inside ``bundle``, or None when there is none.

    Prefers the known name but falls back to whatever single file sits in
    ``Contents/MacOS``, so renaming the Cargo package degrades the freshness
    check instead of breaking the build outright.
    """
    macos_dir = bundle / "Contents" / "MacOS"
    expected = macos_dir / APP_EXECUTABLE_NAME
    if expected.is_file():
        return expected
    try:
        found = [path for path in sorted(macos_dir.iterdir()) if path.is_file()]
    except OSError:
        return None
    return found[0] if found else None


def _bundle_stamp(bundle: Path) -> float | None:
    """Modification time of a bundle, or None when it is not there.

    Prefers the Mach-O executable over the bundle directory: copying resources
    into an existing .app touches the directory without producing a new binary,
    and the binary is what a rebuild is actually for.
    """
    executable = bundle_executable(bundle)
    for candidate in (executable, bundle):
        if candidate is None:
            continue
        try:
            return candidate.stat().st_mtime
        except OSError:
            continue
    return None


def _newest_source(repo: Path) -> tuple[float, str]:
    """Newest watched source in ``repo`` and its repo-relative path."""
    newest = 0.0
    newest_path = ""
    for relative in WATCHED_SOURCES:
        target = repo / relative
        if not target.exists():
            continue
        candidates = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]
        for path in candidates:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
                newest_path = str(path.relative_to(repo))
    return newest, newest_path


def needs_rebuild(repo: Path, *, install_dir: Path = INSTALL_DIR) -> tuple[bool, str]:
    """Whether the desktop bundle is stale, and the human-readable reason."""
    desktop = desktop_dir(repo)
    if not (desktop / "src-tauri" / "tauri.conf.json").exists():
        return False, f"no Tauri project under {desktop}"

    built = _bundle_stamp(built_bundle(repo))
    if built is None:
        return True, "no local bundle has been built yet"

    newest, path = _newest_source(Path(repo))
    if newest > built:
        return True, f"{path} is newer than the built bundle"

    installed = _bundle_stamp(installed_bundle(install_dir))
    if installed is None:
        return True, f"{installed_bundle(install_dir)} is not installed"
    if installed < built:
        return True, "the installed app is older than the built bundle"

    return False, "desktop sources unchanged since the last build"


# Where a Rust toolchain lives when it is installed but not on PATH. rustup
# installed via Homebrew keeps its shims under `opt/rustup/bin`, which is NOT
# `/opt/homebrew/bin`, so `cargo` is invisible to a GUI- or launchd-started
# engine even on a machine that has Rust. `tauri build` then dies inside cargo
# and the failure reaches the operator as an unexplained non-zero exit.
_CARGO_SEARCH_DIRS = (
    Path.home() / ".cargo" / "bin",
    Path("/opt/homebrew/opt/rustup/bin"),
    Path("/usr/local/opt/rustup/bin"),
)


def ensure_cargo_on_path() -> str | None:
    """Make `cargo` reachable for child builds; return its directory if added.

    No-op when cargo is already on PATH. Prepending here keeps the knowledge of
    where toolchains hide in the module that needs cargo, rather than requiring
    every launch context (shell, launchd plist, Finder) to be configured.
    """
    if shutil.which("cargo"):
        return None
    for directory in _CARGO_SEARCH_DIRS:
        if (directory / "cargo").is_file():
            os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
            return str(directory)
    return None


def _missing_rust_toolchain() -> str | None:
    """A ready-to-show message when the desktop build cannot possibly succeed."""
    ensure_cargo_on_path()
    if shutil.which("cargo"):
        return None
    searched = ", ".join(str(d) for d in _CARGO_SEARCH_DIRS)
    return (
        "cargo was not found on PATH, so `tauri build` cannot compile the app. "
        "Install a Rust toolchain (`brew install rustup && rustup default stable`, "
        "or https://rustup.rs) and restart the engine so it inherits the new "
        f"PATH. Also searched: {searched}."
    )


def build_and_stage(
    repo: Path,
    *,
    runner: Runner,
    install_dir: Path = INSTALL_DIR,
) -> tuple[list[dict], bool]:
    """Build the desktop bundle and stage it beside the installed app.

    Returns the deploy steps and whether a bundle is staged and waiting for
    ``install_staged_and_relaunch``. Stops at the first failing step.
    """
    desktop = desktop_dir(repo)
    steps: list[dict] = []

    def record(name: str, result: subprocess.CompletedProcess) -> bool:
        ok = result.returncode == 0
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        # On failure keep BOTH streams, stderr last. npm echoes the command it
        # ran to stdout while the actual cause (cargo, tsc) goes to stderr, so
        # preferring stdout reported only the echo and hid every real error --
        # and the trailing 500-char window must not cut the cause away either.
        detail = (out or err) if ok else "\n".join(part for part in (out, err) if part)
        steps.append({"step": name, "ok": ok, "output": detail[-500:]})
        return ok

    missing = _missing_rust_toolchain()
    if missing:
        steps.append({"step": "desktop toolchain", "ok": False, "output": missing})
        return steps, False

    if not (desktop / "node_modules").is_dir():
        result = runner(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=str(desktop),
            timeout=NPM_TIMEOUT_S,
        )
        if not record("desktop npm install", result):
            return steps, False

    result = runner(["npm", "run", "build"], cwd=str(desktop), timeout=NPM_TIMEOUT_S)
    if not record("desktop npm build", result):
        return steps, False

    # --bundles app skips the dmg, and disabling updater artifacts drops the
    # signing requirement: a dev build has no TAURI_SIGNING_PRIVATE_KEY and
    # nothing consumes its latest.json.
    result = runner(
        [
            "npm",
            "run",
            "tauri",
            "build",
            "--",
            "--bundles",
            "app",
            "--config",
            '{"bundle":{"createUpdaterArtifacts":false}}',
        ],
        cwd=str(desktop),
        timeout=BUILD_TIMEOUT_S,
    )
    if not record("desktop tauri build", result):
        return steps, False

    bundle = built_bundle(repo)
    if bundle_executable(bundle) is None:
        steps.append({
            "step": "desktop stage",
            "ok": False,
            "output": f"tauri build reported success but {bundle} has no executable",
        })
        return steps, False

    staging = Path(install_dir) / STAGING_NAME
    try:
        if staging.exists():
            shutil.rmtree(staging)
    except OSError as exc:
        steps.append({"step": "desktop stage", "ok": False, "output": f"could not clear {staging}: {exc}"})
        return steps, False

    # ditto rather than shutil.copytree: it preserves the ad-hoc code
    # signature and the extended attributes that Gatekeeper reads.
    result = runner(["ditto", str(bundle), str(staging)], cwd=str(desktop), timeout=NPM_TIMEOUT_S)
    if not record("desktop stage", result):
        return steps, False

    steps[-1]["output"] = f"staged at {staging}"
    return steps, True


def install_staged_and_relaunch(
    *,
    runner: Runner,
    install_dir: Path = INSTALL_DIR,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Quit the running app, swap the staged bundle in, and launch it again.

    Runs after the deploy response has been sent, because the window it closes
    is the one showing that response.
    """
    staging = Path(install_dir) / STAGING_NAME
    destination = installed_bundle(install_dir)
    steps: list[dict] = []

    if not staging.exists():
        steps.append({"step": "desktop install", "ok": False, "output": f"nothing staged at {staging}"})
        return steps

    running = _app_is_running(runner=runner)
    if running is None:
        steps.append({
            "step": "desktop install",
            "ok": False,
            "output": (
                "could not tell whether the desktop app is running (pgrep failed), "
                f"so the swap was skipped rather than risk deleting a live bundle; "
                f"the new bundle stays staged at {staging}"
            ),
        })
        return steps

    if running:
        # `if ... is running` matters: a bare `tell application id ... to quit`
        # launches the app first when it is not running.
        runner(
            [
                "osascript",
                "-e",
                f'if application id "{BUNDLE_IDENTIFIER}" is running then '
                f'tell application id "{BUNDLE_IDENTIFIER}" to quit',
            ],
            cwd=str(install_dir),
            timeout=30,
        )
        deadline = monotonic() + QUIT_TIMEOUT_S
        # Only a confirmed False leaves this loop. An unreadable probe (None)
        # keeps waiting rather than falling through to the swap, so the bundle
        # is never deleted while the app might still hold it open.
        while _app_is_running(runner=runner) is not False:
            if monotonic() >= deadline:
                steps.append({
                    "step": "desktop install",
                    "ok": False,
                    "output": (
                        f"the running app did not quit within {QUIT_TIMEOUT_S:.0f}s; "
                        f"the new bundle is staged at {staging} and will be swapped in on "
                        "the next restart"
                    ),
                })
                return steps
            sleep(0.5)

    # Move the old bundle aside, put the new one in place, then delete the old.
    # Never rmtree the installed app first: a delete that fails halfway (a
    # permission on /Applications, a quarantine xattr) would leave a gutted
    # Ciaobot.app and the only good copy hidden in the staging dir. Renames
    # within one directory are atomic, so every failure below is recoverable.
    previous = Path(install_dir) / f"{STAGING_NAME}.previous"
    try:
        if previous.exists():
            shutil.rmtree(previous)
    except OSError as exc:
        steps.append({
            "step": "desktop install",
            "ok": False,
            "output": f"could not clear {previous}: {exc}",
        })
        return steps

    moved_aside = False
    try:
        if destination.exists():
            destination.rename(previous)
            moved_aside = True
        staging.rename(destination)
    except OSError as exc:
        if moved_aside and not destination.exists():
            # Put the working app back before giving up.
            try:
                previous.rename(destination)
            except OSError:
                logger.exception("desktop install: could not restore %s", destination)
        steps.append({"step": "desktop install", "ok": False, "output": f"could not install {destination}: {exc}"})
        return steps
    if moved_aside:
        shutil.rmtree(previous, ignore_errors=True)

    result = runner(["open", "-a", str(destination)], cwd=str(install_dir), timeout=60)
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    steps.append({
        "step": "desktop install",
        "ok": result.returncode == 0,
        "output": (output or f"installed and relaunched {destination}")[-500:],
    })
    return steps


def _app_is_running(*, runner: Runner) -> bool | None:
    """Whether a Ciaobot desktop process is alive, by bundle executable path.

    Matches the path inside the bundle rather than the bare process name so a
    locally built bundle counts too, and so unrelated processes with "Ciaobot"
    in their command line do not.

    Returns None when the probe itself failed rather than answering. ``pgrep``
    exits 1 for "no match", so only that is a real negative; ``_run_step`` turns
    a missing binary into 127 and a timeout into 124, and reading either as "not
    running" would delete the bundle out from under a live app, which is the one
    thing the staging design exists to prevent.
    """
    result = runner(
        ["pgrep", "-f", f"{APP_BUNDLE_NAME}/Contents/MacOS/{APP_EXECUTABLE_NAME}"],
        cwd="/",
        timeout=15,
    )
    if result.returncode == 0:
        return bool((result.stdout or "").strip())
    if result.returncode == 1:
        return False
    return None
