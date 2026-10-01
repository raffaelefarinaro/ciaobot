"""Command-line entrypoint for the packaged Ciaobot app."""

from __future__ import annotations

import argparse
import datetime
import html
import http.cookiejar
import json
import os
import plistlib
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
import urllib.error
import urllib.request

from ciao import dev, gws_wrapper, package_smoke, public_release, release, service_backend
from ciao.setup_status import detect_nested_workspaces
from ciao.macos_service import default_launch_agents_dir
from ciao.jsonio import write_private_text
from ciao.os_support.console import use_utf8_stdio
from ciao.os_support.shell_hints import path_hint, path_hint_note
from ciao.sync_skills import SETUP_MEMORY_FAILED_RC

if TYPE_CHECKING:  # only ever a type here; the queue model is imported locally.
    from ciao import skill_proposals
    from ciao.config import CiaoConfig

_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _workspace_name_arg(value: str) -> str:
    name = value.strip()
    if not _WORKSPACE_NAME_RE.fullmatch(name):
        raise argparse.ArgumentTypeError(
            "workspace name must use letters, numbers, dashes, or underscores"
        )
    return name


def _relaunch_argv() -> list[str]:
    """argv for re-execing the CLI: a fresh interpreter picks up new code
    after a package update."""
    return [sys.executable, "-m", "ciao.cli", *sys.argv[1:]]


def _run_server(*, supervised: bool = False) -> int:
    from ciao.config import RESTART_EXIT_CODE
    from ciao.main import main as server_main

    try:
        server_main(supervised=supervised)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    else:
        code = 0
    if code == RESTART_EXIT_CODE:
        # The setup wizard and package updates request a restart by exiting
        # with this code. Under launchd KeepAlive relaunches us anyway, but a
        # foreground `ciao run` would just die and leave the site unreachable.
        # Re-exec (rather than loop) so the relaunch picks up new code. Under
        # `ciao supervise` the supervisor owns the relaunch instead and only
        # needs the code back.
        print("Restart requested — relaunching Ciaobot…", file=sys.stderr)
        sys.stderr.flush()
        if supervised:
            return code
        # The exec inherits os.environ, and load_dotenv never overrides a key
        # that is already set, so without this a value edited in the workspace
        # .env would be shadowed by the stale copy the old process exported.
        # Only keys the .env added are dropped; the fresh process reloads them.
        from ciao.config import reset_exported_dotenv

        reset_exported_dotenv()
        os.execv(sys.executable, _relaunch_argv())
    return code


def _supervise_command(args: argparse.Namespace) -> int:
    from ciao.supervise import supervise

    extra = list(args.child_args)
    if extra[:1] == ["--"]:
        extra = extra[1:]
    return supervise(extra)


def _copy_tree(src, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_dir():
            _copy_tree(item, dest / item.name)
        else:
            target = dest / item.name
            # sync-skills mirrors canonical commands/ and subagents/ into
            # .claude/. write_bytes() follows symlinks, so without this a
            # setup re-run would silently overwrite the user's custom file
            # through the link instead of the stock copy.
            if target.is_symlink():
                target.unlink()
            target.write_bytes(item.read_bytes())


def _copy_tree_if_missing(src, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            written.extend(_copy_tree_if_missing(item, target))
        elif not target.exists():
            target.write_bytes(item.read_bytes())
            written.append(target)
    return written


def _import_legacy_workspaces_for_setup(root: Path, existing_env: dict[str, str]) -> None:
    """Run the one-time ``CIAO_WORKSPACES`` import against ``root``'s ``.env``.

    Built from the install's own ``.env`` (not the ambient environment) so the
    import targets the same runtime root the server will use.
    """
    from ciao.config import CiaoConfig

    runtime = Path(existing_env.get("CIAO_RUNTIME_ROOT", "").strip() or ".runtime").expanduser()
    if not runtime.is_absolute():
        runtime = root / runtime
    source = {
        **existing_env,
        "CIAO_WORKSPACE": str(root),
        "CIAO_RUNTIME_ROOT": str(runtime.resolve()),
        "PWA_AUTH_TOKEN": existing_env.get("PWA_AUTH_TOKEN") or "setup",
    }
    CiaoConfig.from_env(source).import_legacy_workspaces_env()


def _write_if_missing(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding="utf-8", newline="")


def _launchd_program_arguments(executable: str) -> str:
    """Render the arguments needed to start either a ciao launcher or Python."""

    name = Path(executable).name.lower()
    arguments = (
        ["-m", "ciao.cli", "run"]
        if name == "python" or name.startswith("python3")
        else ["run"]
    )
    return "\n".join(
        f"        <string>{html.escape(argument, quote=False)}</string>"
        for argument in arguments
    )


def _render_launchd_plist(
    *,
    workspace: Path,
    python_path: str | None = None,
    engine_path: str | None = None,
    runtime_root: Path | None = None,
    port: int,
    path: str = "",
    template_name: str = "com.ciao.server.plist.tmpl",
) -> str:
    executable = engine_path or python_path or sys.executable
    template = resources.files("ciao.stock").joinpath(
        "deploy", template_name
    ).read_text(encoding="utf-8")
    # Under launchd the default PATH is minimal. Bake the user's development
    # PATH from setup time into the plist for optional deploy tooling.
    resolved_path = path or os.environ.get("PATH", "")
    replacements = {
        "{{CIAO_WORKSPACE}}": html.escape(str(workspace), quote=False),
        "{{CIAO_RUNTIME_ROOT}}": html.escape(
            str((runtime_root or (workspace / ".runtime")).resolve()), quote=False
        ),
        "{{CIAO_EXECUTABLE}}": html.escape(executable, quote=False),
        "{{LAUNCHD_PROGRAM_ARGUMENTS}}": _launchd_program_arguments(executable),
        "{{CIAO_PORT}}": html.escape(str(port), quote=False),
        "{{CIAO_PATH}}": html.escape(resolved_path, quote=False),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def _write_launchd_plist(
    *,
    workspace: Path,
    launch_agents_dir: Path,
    python_path: str | None = None,
    engine_path: str | None = None,
    runtime_root: Path | None = None,
    port: int,
    path: str = "",
    plist_name: str = "com.ciao.server.plist",
    confirm_repoint: bool = False,
) -> Path:
    if not confirm_repoint:
        allow_env = os.environ.get("CIAO_ALLOW_LAUNCH_AGENT_REPOINT", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if not allow_env:
            # Only guard the real per-user dir. Shadow dirs (tests, explicit
            # launch_agents_dir overrides) are not live — the resolved-path
            # comparison already distinguishes them, so an env override alone
            # must not bypass protection when the target is still the real dir.
            if service_backend.current_backend().is_live_agents_dir(launch_agents_dir):
                existing = _plist_workspace(launch_agents_dir)
                try:
                    requested = Path(workspace).expanduser().resolve()
                except OSError:
                    requested = Path(workspace).expanduser()
                if existing is not None and existing != requested:
                    raise RuntimeError(
                        f"Refusing to repoint live LaunchAgent from {existing} to {requested}: "
                        f"the real {launch_agents_dir / plist_name} already points elsewhere. "
                        "Pass confirm_repoint=True (or --yes via CLI) or set "
                        "CIAO_ALLOW_LAUNCH_AGENT_REPOINT=1 to allow, or pass "
                        "launch_agents_dir to isolate."
                    )
    plist = launch_agents_dir.expanduser() / plist_name
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(
        _render_launchd_plist(
            workspace=workspace,
            python_path=python_path,
            engine_path=engine_path,
            runtime_root=runtime_root,
            port=port,
            path=path,
            template_name=f"{plist_name}.tmpl",
            ),
        encoding="utf-8", newline="",
    )
    return plist


def _setup_token_path(workspace: Path) -> Path:
    return workspace / ".runtime" / "setup-token"


def _ensure_setup_token(workspace: Path) -> str:
    path = _setup_token_path(workspace)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    return _rotate_setup_token(workspace)


def _rotate_setup_token(workspace: Path) -> str:
    """Write a fresh one-time setup token, replacing any existing one.

    Unlike ``_ensure_setup_token`` this always mints a new value: use it when
    the caller wants a guaranteed-valid login URL (the token is redeemed and
    deleted on first login, so a stale file otherwise yields "invalid setup
    token"). The app launcher and menu bar read the token live from disk, so
    they pick up the rotated value on the next open.
    """

    path = _setup_token_path(workspace)
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8", newline="")
    return token


def _pwa_port_from_env(workspace: Path, fallback: int) -> int:
    """Port the server actually listens on, read from the workspace ``.env``.

    Falls back to ``fallback`` when ``.env`` is absent or has no ``PWA_PORT``,
    so ``setup-url`` reports a URL that matches a configured install rather
    than a hard-coded default.
    """

    env_path = workspace / ".env"
    if not env_path.exists():
        return fallback
    try:
        from dotenv import dotenv_values

        raw = (dotenv_values(env_path).get("PWA_PORT") or "").strip()
        return int(raw) if raw else fallback
    except (OSError, ValueError):
        return fallback


def _path_export_hint() -> str | None:
    """A shell line that puts the interpreter's bin dir on PATH, or
    ``None`` when it is already on PATH.

    Ciaobot installs into a standalone venv (``~/.ciaobot-venv``) that is not
    added to PATH, so ``ciao`` is normally invoked by absolute path. Shell
    users who want to type ``ciao`` need this hint.
    """

    # Not .resolve(): a venv's bin/python is a symlink to the base interpreter,
    # and resolving it would report the base interpreter's bin dir instead of
    # the venv's own bin/ where the `ciao` entry point actually lives.
    bin_dir = Path(sys.executable).parent
    entries = {
        str(Path(p).expanduser())
        for p in os.environ.get("PATH", "").split(os.pathsep)
        if p
    }
    if str(bin_dir) in entries:
        return None
    return path_hint(str(bin_dir), persist=False)


def _print_setup_summary(workspace: Path, port: int) -> None:
    """Print the resolved workspace, the login URL, and a PATH hint.

    Surfaces the two things setup previously left implicit: which workspace was
    configured (a mismatch with the running server produces "invalid setup
    token"), and the URL to open when the generated app is unavailable.
    """

    token = _setup_token_path(workspace).read_text(encoding="utf-8").strip()
    base = f"http://localhost:{port}/"
    url = f"{base}?setup={token}" if token else base
    print()
    print(f"Workspace: {workspace}")
    print(f"Open Ciaobot: {url}")
    hint = _path_export_hint()
    if hint is not None:
        print("To run `ciao` from a shell, add its venv to PATH:")
        print(f"  {hint}")
        # Only alongside the line it qualifies: with the bin dir already on PATH
        # there is nothing to change and nothing to wait for.
        note = path_hint_note()
        if note:
            print(f"  {note}")


def _default_app_dir() -> Path:
    """Return the per-user app directory used by the release installer."""

    return Path.home() / "Applications"


_OUR_BUNDLE_IDS = ("local.ciao.app", "local.ciaobot.app")
# Launcher bundles previous versions wrote. Nothing creates these any more, but
# installs upgrading from an older version still have one on disk, so setup
# removes them. "Ciaobot.app" is in the list because both the pre-rename
# launcher and the retired app used that name; _is_our_app_bundle keeps the app
# bundle of the same name safe by checking the executable inside.
_LEGACY_APP_BUNDLE_NAMES = (
    "Ciao.app",
    "Ciaobot.app",
    "Ciaobot Menu Bar.app",
    "Ciaobot Server.app",
)
# Executable inside the retired Ciaobot.app bundle.
_DESKTOP_EXECUTABLE_NAME = "ciaobot-desktop"


def _is_our_app_bundle(app_root: Path) -> bool:
    """Whether ``app_root`` is a launcher bundle created by Ciaobot.

    The retired app shipped as ``Ciaobot.app`` under the same
    ``local.ciaobot.app`` identifier our pre-rename launcher used, so the
    bundle id cannot tell them apart. Misidentifying it is destructive rather
    than merely wasteful: the launcher we write is named ``Ciaobot Server.app``,
    so ``_remove_legacy_app_shortcuts`` must never delete the app bundle and
    anything back in its place, leaving a running process on a bundle that no
    longer exists on disk. The executable name is the discriminator.
    """

    if (app_root / "Contents" / "MacOS" / _DESKTOP_EXECUTABLE_NAME).is_file():
        return False
    plist = app_root / "Contents" / "Info.plist"
    try:
        text = plist.read_text(encoding="utf-8")
    except OSError:
        return False
    return "local.ciaobot.menubar" in text or any(
        bundle_id in text for bundle_id in _OUR_BUNDLE_IDS
    )


def _remove_legacy_app_shortcuts(app_dir: Path) -> bool:
    """Remove stale launcher bundles written before ``Ciaobot Server.app``.

    Browser-installed PWAs may also be named ``Ciaobot.app``. Only bundles
    carrying one of our native bundle identifiers are touched.
    """

    candidates = {app_dir / name for name in _LEGACY_APP_BUNDLE_NAMES}
    home_apps = Path.home() / "Applications"
    if app_dir != home_apps:
        candidates.update(home_apps / name for name in _LEGACY_APP_BUNDLE_NAMES)
    removed = False
    for legacy in candidates:
        try:
            if not _is_our_app_bundle(legacy):
                continue
            _unregister_app_with_launchservices(legacy)
            shutil.rmtree(legacy)
            removed = True
        except OSError:
            print(f"Could not remove legacy app shortcut at {legacy}", file=sys.stderr)
    return removed


_LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


def _unregister_app_with_launchservices(app_root: Path) -> None:
    """Remove a retired native launcher from Launch Services, best-effort."""

    if sys.platform != "darwin" or not os.path.exists(_LSREGISTER):
        return
    try:
        subprocess.run(
            [_LSREGISTER, "-u", str(app_root)],
            check=False,
            capture_output=True,
        )
    except OSError:
        pass


def _disable_legacy_menubar_agent(launch_agents_dir: Path | None = None) -> bool:
    """Unload and delete the retired rumps menu-bar LaunchAgent.

    Ciaobot.app is the menu bar now; the ``com.ciao.menubar`` agent launched a
    Python helper that no longer exists, so leaving it registered means launchd
    retrying a missing executable forever. Called from setup so an upgrade
    cleans up after itself. Returns whether anything was removed.

    Custom test/install directories are cleaned on disk but never touched in
    the user's real launchd domain.
    """

    backend = service_backend.current_backend()
    launch_dir = (launch_agents_dir or backend.agents_dir()).expanduser()
    plist_path = launch_dir / "com.ciao.menubar.plist"
    if not plist_path.exists():
        return False

    if launch_dir == backend.live_agents_dir():
        backend.bootout_agent("com.ciao.menubar")
    try:
        plist_path.unlink()
    except OSError:
        return False
    return True


from ciao.workspace_guide import guide_path

# Paths a workspace snapshot must never pick up. No `.codex/` entry: codex is
# retired (`sync_skills` only prunes what older versions left behind, it never
# writes there), so ignoring it would be dead config.
#
# The runtime root is written as its contents plus one re-include rather than as
# the directory, because the backup scope commits `.runtime/schedules.json` —
# the durable automation store — and refuses every other path under that root
# (#734). Git never descends into an ignored directory, so a `.runtime/` line
# would make that carve-out inert no matter what followed it.
#
# Both halves are spelled the way they have to be, and neither spelling is
# obvious. The glob is `**/.runtime/*` rather than `.runtime/*` because a
# pattern with an interior slash is anchored to the repository root: the
# unanchored `.runtime/` matched a `.runtime` directory at *any* depth, and
# `.runtime/*` would quietly stop matching `client/.runtime` and `a/b/.runtime`
# — a real regression, since those hold state such as `bootstrap-auth-token`
# and the manual sync path still stages the whole tree. The re-include is
# `!/.runtime/schedules.json` for the mirror-image reason: it pins the
# root-level file the scope commits, while a nested `sub/.runtime/
# schedules.json` stays ignored with everything else in that directory.
_RUNTIME_IGNORE_ENTRIES = ("**/.runtime/*", "!/.runtime/schedules.json")

#: The hand-written spellings of that same rule, repaired in place rather than
#: appended to. Each ignores the runtime root well enough that a re-include
#: written beside it is dead — the failure the repair exists to undo — and these
#: are the forms a person writes by hand, so they are what an existing install
#: is most likely to carry.
#:
#: The limit, stated rather than engineered around: git cannot tell a file from
#: a directory in one pattern except by the trailing slash, so after the repair
#: a *plain file* named `.runtime` would no longer be ignored. The runtime root
#: is always a directory (``state_path.parent``), so there is nothing to lose.
_RUNTIME_IGNORE_DIRS = (".runtime/", ".runtime", "/.runtime/", "/.runtime")
_WORKSPACE_GITIGNORE_ENTRIES = (
    ".env",
    ".envrc",
    ".direnv/",
    "secrets/",
    *_RUNTIME_IGNORE_ENTRIES,
    ".claude/",
    ".agents/",
    ".opencode/",
    "opencode.json",
    "*.log",
)


def _ensure_workspace_gitignore(root: Path) -> None:
    """Make sure `git add -A` snapshots never pick up secrets or runtime state.

    Also repairs the one entry whose meaning needs a rewrite rather than an
    append. A ``.runtime/`` line ignores the *directory*, so a re-include
    written beside it does nothing: git never descends into an ignored
    directory, which is what left the backup scope's carve-out of
    ``.runtime/schedules.json`` inert on every install scaffolded before #734
    while the status page listed the file as backed up. Appending cannot fix
    that, so the hand-written spellings in :data:`_RUNTIME_IGNORE_DIRS` are
    rewritten in place as the pair.

    Rewriting is safe whichever hand wrote the line, because the pair covers
    everything each of them covered — every ``.runtime`` at any depth — and
    re-includes one root-level file. A rule scoped to somebody else's path, such
    as ``client/.runtime/``, is not one of those spellings and is left alone.
    """
    gitignore = root / ".gitignore"
    # `newline=""` on both ends: the default text mode would translate a CRLF
    # file to LF on the way in, so the line ending would be gone before the
    # check below could see it, and the rewrite would convert the whole file.
    # `open` rather than `read_text` for the `newline` argument, which
    # `read_text` only grew in 3.13 and the type stubs here predate.
    existing = ""
    if gitignore.exists():
        with gitignore.open(encoding="utf-8", newline="") as handle:
            existing = handle.read()
    original = existing.splitlines()
    lines: list[str] = []
    repaired = False
    for line in original:
        if line.strip() in _RUNTIME_IGNORE_DIRS:
            if not repaired:
                lines.extend(_RUNTIME_IGNORE_ENTRIES)
                repaired = True
            # A second spelling of a rule the pair now covers: dropping it is
            # what keeps the pair from being written twice.
            continue
        lines.append(line)
    present = {line.strip() for line in lines}
    missing = [e for e in _WORKSPACE_GITIGNORE_ENTRIES if e not in present]
    if not missing and lines == original:
        return
    # Rebuild in whatever line ending the file already had: a CRLF `.gitignore`
    # is a Windows editor's file, and rewriting it wholesale into LF is a
    # gratuitous whole-file diff on a file this function only appended to.
    newline = "\r\n" if "\r\n" in existing else "\n"
    if lines:
        text = newline.join(lines) + newline
    else:
        header = "# Ciaobot: keep secrets and runtime state out of git snapshots"
        text = header + newline
    if missing:
        text += newline.join(missing) + newline
    with gitignore.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def ensure_workspace_git(root: Path) -> None:
    """Make sure the workspace is a git repository with a protective .gitignore.

    Snapshots and sync rely on git; a fresh workspace gets `git init` plus an
    initial commit. An existing repo is left untouched apart from appending
    missing .gitignore guards. Missing git binary is a non-fatal skip.
    """
    root = Path(root).expanduser().resolve()
    if shutil.which("git") is None:
        print("git not found; skipping workspace git init", file=sys.stderr)
        return
    _ensure_workspace_gitignore(root)
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if probe.returncode == 0 and probe.stdout.strip() == "true":
        return
    init = subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if init.returncode != 0:
        print(f"git init failed for {root}: {init.stderr.strip()}", file=sys.stderr)
        return
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    commit = subprocess.run(
        [
            "git", "-C", str(root),
            "-c", "user.name=Ciaobot", "-c", "user.email=ciaobot@localhost",
            "commit", "-m", "Initialize Ciaobot workspace",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if commit.returncode != 0:
        print(
            f"initial workspace commit failed for {root}: {commit.stderr.strip()}",
            file=sys.stderr,
        )


_VAULT_GITIGNORE_ENTRIES = (".DS_Store", ".obsidian/workspace*")


def _ensure_vault_gitignore(root: Path) -> None:
    """Keep OS litter and volatile Obsidian state out of vault snapshots."""
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [e for e in _VAULT_GITIGNORE_ENTRIES if e not in present]
    if not missing:
        return
    if existing:
        text = existing if existing.endswith("\n") else existing + "\n"
    else:
        text = "# Ciaobot: keep OS and editor litter out of vault snapshots\n"
    gitignore.write_text(text + "\n".join(missing) + "\n", encoding="utf-8", newline="")


def ensure_vault_git(root: Path) -> None:
    """Make sure the vault is (in) a git repository.

    Matters when the vault lives outside the workspace (an existing notes
    folder): a fresh vault gets `git init -b main`, a minimal .gitignore, and
    an initial commit. A vault that is already inside a git work tree is not
    re-initialized: when the work tree is rooted at the vault itself only
    missing .gitignore entries are appended; when the vault sits deeper inside
    another repo (the default vault-inside-workspace layout) nothing is
    touched at all. Missing git binary is a non-fatal skip.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        # Nothing to initialise, and nothing to invent. After the re-rooting the
        # shared vault path is gone and each root holds its own inside the same
        # install repo, so scaffolding one here would recreate exactly the
        # directory the migration removed. `_ensure_vault_gitignore` used to try,
        # and `ciao setup` died on the FileNotFoundError.
        print(f"vault {root} does not exist; skipping vault git init", file=sys.stderr)
        return
    if shutil.which("git") is None:
        print("git not found; skipping vault git init", file=sys.stderr)
        return
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if probe.returncode == 0:
        toplevel = Path(probe.stdout.strip())
        if toplevel == root:
            _ensure_vault_gitignore(root)
        return
    _ensure_vault_gitignore(root)
    init = subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if init.returncode != 0:
        print(f"git init failed for {root}: {init.stderr.strip()}", file=sys.stderr)
        return
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    commit = subprocess.run(
        [
            "git", "-C", str(root),
            "-c", "user.name=Ciaobot", "-c", "user.email=ciaobot@localhost",
            "commit", "-m", "Initialize Ciaobot vault",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if commit.returncode != 0:
        print(
            f"initial vault commit failed for {root}: {commit.stderr.strip()}",
            file=sys.stderr,
        )


def detect_vault_mode(workspace: Path | str) -> str:
    """Infer the vault content mode from what the chosen folder holds.

    Empty (or missing) folder -> "scratch": scaffold a fresh vault at
    memory-vault/. Anything with visible content -> "existing": the folder is
    the user's notes, the vault lives in place, and the onboarding agent
    adapts the contents. Dotfiles don't count as content, so a folder that
    only carries e.g. .DS_Store or .obsidian still starts from scratch.
    """
    root = Path(workspace).expanduser()
    try:
        entries = [p for p in root.iterdir() if not p.name.startswith(".")]
    except OSError:
        return "scratch"
    return "existing" if entries else "scratch"


def _setup_registry_vaults(
    registry_path: Path,
    *,
    workspace_root: Path,
    configured_vault_root: Path,
) -> list[tuple[str, Path]] | None:
    """Resolve an existing setup registry without rediscovering vaults.

    A setup rerun must be idempotent even when the configured vault is a
    container full of named workspaces. Rediscovering that container and then
    scaffolding its root created a second MEMORY.md/INDEX.md/Logs layout.
    Reuse the same resolver as the running app so legacy one-segment and
    setup-selected roots keep the location already recorded for them.
    """
    if not registry_path.exists():
        return None
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"existing workspace registry is unreadable: {exc}") from exc

    if isinstance(payload, dict):
        items = [
            {"name": name, **value}
            for name, value in payload.items()
            if isinstance(value, dict)
        ]
    elif isinstance(payload, list):
        items = [item for item in payload if isinstance(item, dict)]
    else:
        items = []

    from ciao.config import CiaoConfig, WorkspaceConfig

    workspaces: dict[str, WorkspaceConfig] = {}
    for item in items:
        name = str(item.get("name", "")).strip()
        if not _WORKSPACE_NAME_RE.fullmatch(name):
            continue
        raw_root = str(item.get("vault_root", "")).strip()
        if not raw_root:
            continue
        workspaces[name] = WorkspaceConfig(name=name, vault_root=raw_root)
    if not workspaces:
        raise ValueError("existing workspace registry has no valid workspaces")

    config = CiaoConfig(
        pwa_auth_token="setup-registry",
        workspace_root=workspace_root,
        vault_root=configured_vault_root,
        state_path=workspace_root / ".runtime" / "state.json",
        media_root=workspace_root / ".runtime" / "media",
        workspaces=workspaces,
    )
    return [
        (name, config.workspace_vault_root(name))
        for name in config.workspace_names()
    ]


def _service_platform() -> bool:
    """True where ``ciao setup`` installs a per-user background service.

    macOS (a LaunchAgent) and Windows (a Task Scheduler logon task) both have
    one; Linux does not, and there ``--load-launchd`` is refused rather than
    silently ignored.
    """

    return sys.platform in ("darwin", "win32")


def setup_workspace(
    workspace: Path | str,
    *,
    auth_token: str | None = None,
    auth_required: bool = True,
    vault_root: Path | str | None = None,
    vault_mode: str = "scratch",
    workspace_name: str | None = None,
    default_provider: str = "claude",
    python_path: str | None = None,
    port: int = 8443,
    launch_agents_dir: Path | str | None = None,
    app_dir: Path | str | None = None,
    confirm_repoint: bool = False,
    sync_failures: list[str] | None = None,
) -> list[Path]:
    requested_name = (workspace_name or "").strip()
    if workspace_name is not None and not _WORKSPACE_NAME_RE.fullmatch(
        requested_name
    ):
        raise ValueError(
            "workspace name must use letters, numbers, dashes, or underscores"
        )
    root = Path(workspace).expanduser().resolve()
    # Guard before any mutation: do not create/append to an arbitrary
    # existing notes folder when the live LaunchAgent would be hijacked.
    # The later `_write_launchd_plist` guard is defense-in-depth; this one
    # makes refusal non-mutating for `setup_workspace` and `/api/setup/finish`.
    write_launchd = _service_platform() or launch_agents_dir is not None
    if write_launchd and not confirm_repoint:
        allow_env = os.environ.get("CIAO_ALLOW_LAUNCH_AGENT_REPOINT", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if not allow_env:
            backend = service_backend.current_backend()
            _early_launch = (
                Path(launch_agents_dir)
                if launch_agents_dir is not None
                else backend.agents_dir()
            )
            if backend.is_live_agents_dir(_early_launch):
                existing = _registered_service_workspace(_early_launch)
                if existing is not None and existing != root:
                    raise RuntimeError(
                        f"Refusing to repoint live LaunchAgent from {existing} to {root}: "
                        f"the real {_early_launch / 'com.ciao.server.plist'} already points elsewhere. "
                        "Pass confirm_repoint=True (or --yes via CLI) or set "
                        "CIAO_ALLOW_LAUNCH_AGENT_REPOINT=1 to allow, or pass "
                        "launch_agents_dir to isolate."
                    )
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    setup_selected_vault = vault_root is not None
    vault_value = str(vault_root) if vault_root is not None else "memory-vault"
    if vault_root is None and vault_mode == "existing":
        # Single-folder setup: the chosen workspace IS the user's existing
        # notes folder. A previously scaffolded vault keeps its place;
        # otherwise the folder itself is the vault and the onboarding agent
        # adapts its contents into the Ciaobot structure.
        if not (root / "memory-vault").is_dir():
            vault_value = "."

    env_path = root / ".env"
    existing_env: dict[str, str] = {}
    if env_path.exists():
        # An existing .env — the user's own, or a previous install — is the
        # source of truth: every variable already in it wins over setup
        # arguments. In particular the recorded vault root must keep
        # scaffolding anchored (re-running setup with a stale or blank
        # vault_root argument must not re-scatter MEMORY.md/INDEX.md at a
        # bogus location).
        from dotenv import dotenv_values

        existing_env = {
            key: (value or "") for key, value in dotenv_values(env_path).items()
        }
        existing_root = existing_env.get("CIAO_VAULT_ROOT", "").strip()
        if existing_root:
            vault_value = existing_root
            setup_selected_vault = True

    vault_path = Path(vault_value).expanduser()
    if vault_path.is_absolute():
        # Record the expanded path so .env stays unambiguous when the vault
        # lives outside the workspace (e.g. "~/ciaobot-brain").
        vault_value = str(vault_path)
    else:
        vault_path = root / vault_path
    workspaces_registry = root / ".runtime" / "workspaces.json"
    if existing_env.get("CIAO_WORKSPACES", "").strip() and not workspaces_registry.exists():
        # An install that still configures workspaces through the retired
        # variable usually has no registry file (the server never persisted
        # one while the variable was set). The upgrade installer reruns setup
        # before the new server first starts, so writing a synthetic
        # single-workspace registry here would make the server's one-time
        # import keep that synthetic entry and drop the variable's real
        # vault_root / disallowed_tools / allowlist for the same name. Import
        # the variable first so the registry setup sees is the real one.
        _import_legacy_workspaces_for_setup(root, existing_env)
    registered_vaults = _setup_registry_vaults(
        workspaces_registry,
        workspace_root=root,
        configured_vault_root=vault_path,
    )
    name = requested_name or "personal"

    token = auth_token or secrets.token_urlsafe(32)
    # Always pin PWA_AUTH_REQUIRED: an unset value is read as "protect when a
    # token exists" (see CiaoConfig.from_env), and a setup that deliberately
    # opted out must survive that default.
    desired_env: list[tuple[str, str]] = [
        ("PWA_AUTH_TOKEN", token),
        ("PWA_AUTH_REQUIRED", "true" if auth_required else "false"),
    ]
    desired_env.extend([
        ("CIAO_WORKSPACE", "."),
        ("CIAO_VAULT_ROOT", vault_value),
        ("CIAO_VAULT_MODE", vault_mode),
        ("CIAO_RUNTIME_ROOT", ".runtime"),
        ("PWA_PORT", str(port)),
    ])
    if not existing_env and not env_path.exists():
        # The password is in here in clear text: owner-only from creation.
        write_private_text(
            env_path, "\n".join(f"{key}={value}" for key, value in desired_env) + "\n"
        )
        written.append(env_path)
        # First-time setup: stamp when this workspace was provisioned so the
        # post-setup restart can hold system-routine catch-up for a grace
        # period. The onboarding chat should be the first thing a new user
        # sees, not four parallel routine chats replaying missed runs.
        from ciao.setup_marker import write_setup_marker

        written.append(
            write_setup_marker(root / ".runtime")
        )
    else:
        # Merge into the user's file: keep every existing line untouched
        # (values, comments, unknown variables) and append only the Ciaobot
        # variables that are missing entirely.
        additions = [
            f"{key}={value}"
            for key, value in desired_env
            if key not in existing_env
        ]
        if additions:
            original = env_path.read_text(encoding="utf-8")
            prefix = "" if not original or original.endswith("\n") else "\n"
            env_path.write_text(
                original
                + prefix
                + "# Added by Ciaobot setup\n"
                + "\n".join(additions)
                + "\n",
                encoding="utf-8", newline="",
            )
            written.append(env_path)

    runtime_value = existing_env.get("CIAO_RUNTIME_ROOT", "").strip() or ".runtime"
    runtime_root = Path(runtime_value).expanduser()
    if not runtime_root.is_absolute():
        runtime_root = root / runtime_root

    # A brand-new install is created in the PER-ROOT layout directly, rather than
    # in the shared one and then migrated. Setup used to scaffold
    # `memory-vault/personal` plus agent assets at the install root, so every new
    # user was manufactured into exactly the state the re-rooting exists to fix —
    # and met a blocking "migrate now" tile on first boot. The migration engine
    # then had an audience that regenerated itself.
    #
    # The receipt is written here, before anything reads `agent_roots_for`, because
    # it is what makes `agent_root()` answer per-root. Files in the nested layout
    # with a gate that still says "shared" is the one combination that breaks
    # everything downstream.
    fresh_per_root = (
        registered_vaults is None
        and vault_mode != "existing"
        and not setup_selected_vault
        and not detect_nested_workspaces(vault_path)
        and not vault_path.exists()
    )
    if fresh_per_root:
        from ciao.workspace_reroot import mark_born_per_root

        vault_path = root / name / vault_path.name
        written.extend(mark_born_per_root(root, runtime_root, [name]))
        # The registry has to exist before the asset loop too: `agent_roots_for`
        # reads the receipt for the gate and the REGISTRY for the names, and with
        # no registry it falls back to the install root — which is how the first
        # attempt at this still put `.claude/`, `commands/` and a stock CLAUDE.md
        # beside the nested vault instead of inside the workspace's own folder.
        # The later branch is `_write_if_missing`, so this does not fight it.
        _write_if_missing(
            root / ".runtime" / "workspaces.json",
            json.dumps(
                [{
                    "name": name,
                    "vault_root": f"{name}/{vault_path.name}",
                    "default_provider": default_provider,
                    # No Google account is linked at scaffold time; the user
                    # chooses in Settings → Workspaces after setup.
                    "gws_profile": "",
                }],
                indent=2,
            ) + "\n",
        )
        written.append(root / ".runtime" / "workspaces.json")

    stock = resources.files("ciao.stock")
    stock_workspace = stock.joinpath("workspace")

    # Canonical user-authored asset sources (mirrored into .claude/ by
    # sync-skills). App plumbing, not vault content: pre-creating them keeps
    # the Workspace Health checks warning-free on a fresh or adopted setup.
    from ciao.config import agent_roots_for
    from ciao.sync_skills import (
        _ensure_workspace_guide,
        _install_stock_agents,
        _seed_stock_commands,
        sync_workspace_skills,
    )

    # Agent assets go to the AGENT ROOTS, which is the install root before the
    # re-rooting and one directory per workspace after it. Scaffolding the
    # install root unconditionally put a stock CLAUDE.md, stock commands and a
    # subagents/ directory beside the real per-root ones on every migrated
    # install — `ciao setup --load-launchd` is what the installer runs, so it
    # happened on every reinstall.
    for asset_root, _name in agent_roots_for(root, runtime_root):
        asset_root.mkdir(parents=True, exist_ok=True)
        for asset_dir in ("subagents", "commands"):
            (asset_root / asset_dir).mkdir(parents=True, exist_ok=True)
        _install_stock_agents(asset_root)
        written.append(asset_root / ".claude" / "agents")
        _seed_stock_commands(asset_root)
        written.append(asset_root / "commands")
        written.extend(_copy_tree_if_missing(stock_workspace, asset_root))
        _ensure_workspace_guide(asset_root)
        # Build the generated catalogs too, so setup leaves a HEALTHY install
        # rather than one that only becomes healthy after its first boot. Without
        # this a brand-new install showed nine Workspace Health warnings and an
        # operator tile about missing assets, on a install where nothing was
        # wrong — it just had not synced yet. Local only: no upstream refresh, so
        # setup still does not touch the network.
        try:
            sync_result = sync_workspace_skills(
                asset_root,
                refresh_upstream=False,
                workspace_name=_name or None,
            )
        except Exception as exc:  # noqa: BLE001 — a scaffold step, never fatal
            print(f"skill sync failed for {asset_root}: {exc}", file=sys.stderr)
        else:
            if sync_result.memory_error and sync_failures is not None:
                sync_failures.append(
                    f"memory regions not set up for {asset_root}: "
                    f"{sync_result.memory_error}"
                )

    runtime_schedules = root / ".runtime" / "schedules.json"
    _write_if_missing(
        runtime_schedules,
        json.dumps({"schedules": []}, indent=2) + "\n",
    )
    written.append(runtime_schedules)

    # First-run wizard names the user's first logical workspace. Writing a
    # real registry (instead of relying on the legacy personal+work fallback)
    # means new installs start with exactly one workspace; more are added in
    # Settings → Workspaces. The explicit vault_root also keeps the legacy
    # personal/work nested-vault special case from ever triggering.
    #
    # If the vault already holds nested workspace directories (e.g.
    # memory-vault/personal/, memory-vault/work/), adopt them as the logical
    # workspace registry instead of creating one synthetic workspace that points
    # at the whole vault.
    provider = (default_provider or "claude").strip().lower()
    scaffold_vaults: list[tuple[str, Path]]
    if registered_vaults is not None:
        # The registry, not today's CLI defaults or filesystem discovery, is
        # authoritative on a rerun. Repair missing scaffold files only inside
        # the vaults it already names.
        scaffold_vaults = registered_vaults
    else:
        nested = detect_nested_workspaces(vault_path)
        scaffold_vaults = []
    if registered_vaults is None and nested:
        entries: list[dict[str, str]] = []
        for ws_name in nested:
            nested_vault = vault_path / ws_name
            scaffold_vaults.append((ws_name, nested_vault))
            try:
                stored_root = nested_vault.relative_to(root).as_posix()
            except ValueError:
                stored_root = str(nested_vault)
            entries.append(
                {
                    "name": ws_name,
                    "vault_root": stored_root,
                    "default_provider": provider,
                    # No Google account is linked at scaffold time: which
                    # accounts exist is the user's choice, made in Settings →
                    # Workspaces after setup.
                    "gws_profile": "",
                }
            )
            _write_if_missing(
                nested_vault / "projects" / "active" / "general" / "general.md",
                "---\ntype: project\ntitle: General\ndescription: Default project.\nstatus: active\ntags: [project]\n---\n\n# General\n",
            )
        _write_if_missing(
            workspaces_registry,
            json.dumps(entries, indent=2) + "\n",
        )
        written.append(workspaces_registry)
    elif registered_vaults is None:
        # A fresh logical workspace always gets its own named folder beneath
        # the configured vault container. Existing-folder onboarding is the
        # compatibility exception: keep the selected notes in place so the
        # onboarding chat can inspect them before proposing a migration.
        scaffold_vault_path = vault_path
        if vault_mode != "existing" and not setup_selected_vault and not fresh_per_root:
            # `fresh_per_root` already resolved the vault to `<name>/<leaf>`;
            # appending the name again would give `<name>/<leaf>/<name>`.
            scaffold_vault_path = vault_path / name
        scaffold_vaults.append((name, scaffold_vault_path))
        try:
            stored_root = scaffold_vault_path.relative_to(root).as_posix()
        except ValueError:
            stored_root = str(scaffold_vault_path)
        _write_if_missing(
            workspaces_registry,
            json.dumps(
                [
                    {
                        "name": name,
                        "vault_root": stored_root,
                        "default_provider": provider,
                        "gws_profile": "",
                    }
                ],
                indent=2,
            )
            + "\n",
        )
        written.append(workspaces_registry)

    for _, scaffold_vault_path in scaffold_vaults:
        _write_if_missing(
            scaffold_vault_path / "MEMORY.md",
            "# Memory\n\nDurable workspace memory lives here.\n",
        )
        _write_if_missing(
            scaffold_vault_path / "INDEX.md",
            "# Vault Index\n\nGenerated by `ciao vault-index`.\n",
        )
        _write_if_missing(
            scaffold_vault_path / "projects" / "active" / "general" / "general.md",
            "---\ntype: project\ntitle: General\ndescription: Default project.\nstatus: active\ntags: [project]\n---\n\n# General\n",
        )
        (scaffold_vault_path / "Logs" / "Chats").mkdir(parents=True, exist_ok=True)
        written.append(scaffold_vault_path)
        # Onboarding an existing folder is the one path that adopts notes this
        # app did not write, so it is also the one that can inherit the retired
        # link dialect. Surface it here rather than waiting for the weekly audit:
        # the user is looking at setup output right now, and the conversion is
        # far cheaper before they have built on top of it.
        if vault_mode == "existing":
            try:
                from ciao.vault_migrate_links import has_unmigrated_links

                example = has_unmigrated_links(scaffold_vault_path)
            except Exception:  # noqa: BLE001 — never fail setup over an advisory
                example = ""
            if example:
                print(
                    f"\nNote: {scaffold_vault_path} uses `[[wikilinks]]`, which "
                    "Ciaobot no longer reads as links.\n"
                    "      Preview the conversion with `ciao vault-migrate-links`, "
                    "apply it with `--apply`.\n"
                    "      It is reversible: `ciao vault-unmigrate-links --apply`.",
                )

    app_root_dir = Path(app_dir) if app_dir is not None else _default_app_dir()
    # The bundled launcher exports its own entrypoint so onboarding does not
    # write the embedded interpreter directly into launchd as ``python run``.
    resolved_engine = (
        python_path
        or os.environ.get("CIAO_ENGINE_PATH", "").strip()
        or sys.executable
    )
    # The one-time login token for the PWA. Written unconditionally: the setup
    # summary prints it as a login URL, and the first client redeems it on first
    # launch. It used to be created as a side effect of writing the launcher
    # bundle, which no longer exists.
    _ensure_setup_token(root)
    if write_launchd:
        launch_dir = (
            Path(launch_agents_dir)
            if launch_agents_dir is not None
            else service_backend.current_backend().agents_dir()
        )
        if sys.platform == "win32":
            from ciao import windows_service

            # The Windows definition is Task Scheduler XML written by the module
            # that owns the task, not a plist. The repoint guard already ran
            # above, through `_registered_service_workspace`.
            written.append(windows_service.write_task_definition(
                workspace=root,
                python=os.environ.get("CIAO_ENGINE_PATH", "").strip() or None,
                directory=launch_dir,
            ))
        else:
            written.append(_write_launchd_plist(
                workspace=root,
                launch_agents_dir=launch_dir,
                engine_path=resolved_engine,
                runtime_root=runtime_root,
                port=port,
                path=os.environ.get("PATH", ""),
                plist_name="com.ciao.server.plist",
                confirm_repoint=confirm_repoint,
            ))
            # Explicit --launch-agents-dir also permits offline plist generation.
            _remove_legacy_app_shortcuts(app_root_dir)
            _disable_legacy_menubar_agent(launch_dir)

    ensure_workspace_git(root)
    # A vault outside the workspace (existing notes folder) gets its own
    # repo. Runs after the workspace init so the default nested vault is
    # never double-initialized.
    ensure_vault_git(vault_path)

    return written


def _looks_like_source_checkout(path: Path) -> bool:
    """True if ``path`` is the Ciaobot source repo or a git worktree of it.

    ``ciao setup`` treats the target directory as the workspace and repoints
    the LaunchAgents at it, so running it inside the code checkout silently
    hijacks the real workspace. A workspace never contains the app's own
    source tree, so the packaged markers are a safe signal.
    """

    if (path / "pyproject.toml").is_file() and (path / "ciao" / "__init__.py").is_file():
        return True
    return "/.claude/worktrees/" in path.as_posix()


def _plist_workspace(launch_agents_dir: Path) -> Path | None:
    """Workspace the server LaunchAgent currently points at, if set up."""

    plist = launch_agents_dir.expanduser() / "com.ciao.server.plist"
    try:
        with plist.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError):
        return None
    workspace = (data.get("EnvironmentVariables") or {}).get("CIAO_WORKSPACE")
    if not workspace:
        return None
    try:
        return Path(str(workspace)).expanduser().resolve()
    except OSError:
        return None


def _registered_service_workspace(definitions_dir: Path) -> Path | None:
    """Workspace the installed service definition serves, if it says one.

    The platform's own answer: the plist's ``CIAO_WORKSPACE`` on macOS, the
    task XML's ``Exec/WorkingDirectory`` on Windows. Both resolve the path, so a
    caller can compare it with a requested workspace.
    """

    if sys.platform == "win32":
        from ciao import windows_service

        workspace = windows_service.task_workspace(
            definitions_dir.expanduser() / windows_service.TASK_FILE_NAME
        )
        try:
            return workspace.expanduser().resolve() if workspace is not None else None
        except OSError:
            return workspace
    return _plist_workspace(definitions_dir)


def _service_definition(launch_dir: Path | str | None) -> Path | None:
    """The service definition this platform keeps in ``launch_dir``, or None.

    The server LaunchAgent on macOS, the Task Scheduler XML on Windows.
    """

    if launch_dir is None:
        return None
    if sys.platform == "win32":
        from ciao import windows_service

        return Path(launch_dir).expanduser() / windows_service.TASK_FILE_NAME
    return Path(launch_dir).expanduser() / "com.ciao.server.plist"


def _setup_command(args: argparse.Namespace) -> int:
    root = Path(args.workspace).expanduser().resolve()
    if args.load_launchd and not _service_platform():
        print(
            "Error: --load-launchd requires macOS or Windows. "
            "On Linux use `ciao linux-service`.",
            file=sys.stderr,
        )
        return 2
    launch_dir = args.launch_agents_dir
    if launch_dir is None and _service_platform():
        launch_dir = service_backend.current_backend().agents_dir()

    # Guard against the two ways `ciao setup` silently hijacks the workspace:
    # running it inside the source checkout, or re-pointing an already
    # configured workspace to the current directory. --yes overrides.
    if not args.yes:
        from ciao.setup_status import tcc_protected_location

        protected = tcc_protected_location(root)
        if protected:
            print(
                f"Error: {root} is inside ~/{protected}, which macOS privacy "
                "protection blocks the background launchd agent from reading "
                "(the server and menu bar fail with 'Operation not permitted').\n"
                "Choose a workspace outside ~/Desktop, ~/Documents, and "
                "~/Downloads (e.g. ~/ciaobot), or grant Full Disk Access to the "
                "interpreter and re-run with --yes.",
                file=sys.stderr,
            )
            return 1
        if _looks_like_source_checkout(root):
            print(
                f"Error: {root} looks like the Ciaobot source checkout, not a "
                "workspace.\ncd to your workspace folder and run `ciao setup` "
                "there, or pass --workspace <path> (or --yes to override).",
                file=sys.stderr,
            )
            return 1
        existing = (
            _registered_service_workspace(Path(launch_dir))
            if launch_dir is not None else None
        )
        if existing is not None and existing != root:
            allow_env = os.environ.get(
                "CIAO_ALLOW_LAUNCH_AGENT_REPOINT", ""
            ).strip().lower() in ("1", "true", "yes")
            if not allow_env:
                print(
                    f"Error: Ciaobot is already set up with workspace {existing}.\n"
                    f"Running setup here would move it to {root}.\nRe-run from the "
                    "existing workspace, pass --workspace, or add --yes to confirm "
                    "the move.",
                    file=sys.stderr,
                )
                return 1

    auth_required = not args.no_auth
    env_path = root / ".env"
    try:
        had_token = "PWA_AUTH_TOKEN=" in env_path.read_text(encoding="utf-8")
    except OSError:
        had_token = False
    sync_failures: list[str] = []
    try:
        written = setup_workspace(
            args.workspace,
            auth_token=args.auth_token,
            auth_required=auth_required,
            workspace_name=args.workspace_name,
            python_path=args.python,
            port=args.port,
            launch_agents_dir=args.launch_agents_dir,
            app_dir=args.app_dir,
            confirm_repoint=args.yes,
            sync_failures=sync_failures,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    for failure in sync_failures:
        print(
            f"Warning: {failure}. Skills were synced; fix the error and "
            "re-run `ciao setup`.",
            file=sys.stderr,
        )
    setup_rc = SETUP_MEMORY_FAILED_RC if sync_failures else 0
    if auth_required and not args.auth_token and not had_token:
        print(
            "\nPassword protection is on. No --auth-token was given, so a random "
            f"password was written to {root / '.env'} (PWA_AUTH_TOKEN).\n"
            "Open Ciaobot with the login URL below and change it in "
            "Settings -> PWA password."
        )
    # One agent now: setup deletes the retired com.ciao.menubar plist rather
    # than writing it, so there is nothing else here to load.
    definition = _service_definition(launch_dir)
    definitions = [definition] if definition is not None and definition.is_file() else []
    if args.load_launchd:
        rc = setup_rc
        backend = service_backend.current_backend()
        for agent_definition in definitions:
            # Keep a real load failure visible to the installer and preserve
            # its status as the setup result - except that launchctl's own 3
            # would be read as the tolerated memory warning, so the installer
            # would continue with the agent never loaded. Anything load
            # returns is a hard failure: report it as 1.
            lrc = backend.load_agent(agent_definition)
            if lrc:
                rc = 1 if lrc == SETUP_MEMORY_FAILED_RC else lrc
        _print_setup_summary(root, _pwa_port_from_env(root, args.port))
        return rc
    for agent_definition in definitions:
        if sys.platform == "win32":
            print(
                "Task not registered. To register it: "
                f"ciao setup --workspace {root} --load-launchd"
            )
        else:
            print(
                f"LaunchAgent not loaded. To load it: launchctl load -w {agent_definition}"
            )
    if sys.platform.startswith("linux") and not definitions:
        print(
            "Workspace ready. Run `ciao run` from the workspace, or use "
            "`ciao linux-service` to render a systemd unit."
        )
    _print_setup_summary(root, _pwa_port_from_env(root, args.port))
    return setup_rc


def _setup_url_command(args: argparse.Namespace) -> int:
    """Print the localhost login URL for a workspace, minting a fresh one-time
    setup token by default (``--no-rotate`` reuses the existing token)."""

    root = Path(args.workspace).expanduser().resolve()
    port = _pwa_port_from_env(root, args.port)
    if args.rotate:
        token = _rotate_setup_token(root)
    else:
        token = _ensure_setup_token(root)
    print(f"Workspace: {root}")
    print(f"http://localhost:{port}/?setup={token}")
    return 0


def _auth_command_for_provider(
    provider: str, *, device_auth: bool = False
) -> list[str]:
    # Runtime providers carry their own login command in the registry.
    # is a routing backend with no provider module, so it stays inline.
    from ciao import provider_registry

    descriptor = provider_registry.get(provider)
    if descriptor is not None:
        return descriptor.auth_command(device_auth=device_auth)
    raise ValueError(f"Unknown provider '{provider}'")


def _runtime_provider_choices() -> tuple[str, ...]:
    """Runtime providers accepted by ``--provider`` flags."""
    from ciao import provider_registry

    return provider_registry.provider_ids()


def _auth_provider_choices() -> list[str]:
    """Providers ``ciao auth`` accepts."""
    return list(_runtime_provider_choices())


def _auth_command(args: argparse.Namespace) -> int:
    try:
        command = _auth_command_for_provider(
            args.provider,
            device_auth=bool(getattr(args, "device_auth", False)),
        )
    except FileNotFoundError as exc:
        # ``--print-only`` is useful for setup instructions even on a machine
        # where the provider CLI is not installed yet. Keep the real command
        # path strict, but provide opencode's documented executable name for
        # the copy/paste form.
        if args.print_only and args.provider == "opencode":
            print("opencode auth login")
            return 0
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.print_only:
        print(" ".join(command))
        return 0
    try:
        proc = subprocess.run(command, check=False)
    except OSError as exc:
        print(f"Error: failed to run {' '.join(command)}: {exc}", file=sys.stderr)
        return 1
    return int(proc.returncode)


def _resolve_vault_root(raw: Path | str | None = None) -> Path:
    """Locate the vault.

    A relative value resolves against `CIAO_WORKSPACE`, not the current
    directory, for the same reason `_resolve_runtime_root` does: the bundled
    engine's launcher `cd`s into `Ciaobot.app/.../ciao-runtime` before exec'ing
    Python, so a relative `CIAO_VAULT_ROOT=memory-vault` resolved against the cwd
    pointed inside the app bundle. Every vault command run from a routine — whose
    prompts deliberately pass no `--vault-root` — failed with a
    FileNotFoundError under the runtime directory.
    """
    if raw is not None:
        root = Path(raw).expanduser()
    else:
        env_root = os.environ.get("CIAO_VAULT_ROOT", "").strip()
        root = Path(env_root).expanduser() if env_root else Path("memory-vault")
    if not root.is_absolute():
        workspace = os.environ.get("CIAO_WORKSPACE", "").strip()
        base = Path(workspace).expanduser() if workspace else Path.cwd()
        root = base / root
    return root.resolve()


def _resolve_runtime_root(raw: Path | str | None = None) -> Path:
    """Locate `.runtime`, where migration receipts live.

    A relative value resolves against `CIAO_WORKSPACE` rather than the current
    directory: the receipt has to land in the same `.runtime` the server uses, and
    a CLI invoked from anywhere else would otherwise mark a `.runtime` beside the
    shell's cwd as migrated.
    """
    if raw is not None:
        root = Path(raw).expanduser()
    else:
        env_root = os.environ.get("CIAO_RUNTIME_ROOT", "").strip()
        root = Path(env_root).expanduser() if env_root else Path(".runtime")
    if not root.is_absolute():
        workspace = os.environ.get("CIAO_WORKSPACE", "").strip()
        base = Path(workspace).expanduser() if workspace else Path.cwd()
        root = base / root
    return root.resolve()


def _vault_search_command(args: argparse.Namespace) -> int:
    from ciao import fts_search

    vault_root = _resolve_vault_root(args.vault_root)
    # The re-rooting promotes Logs/ out of the vault, so the archive root cannot
    # be derived from the vault root on a migrated install.
    from ciao.config import logs_root_for

    # The install root defines BOTH the stored-key base and the database: an
    # explicit --runtime-root / CIAO_RUNTIME_ROOT names `<install>/.runtime`, so
    # its parent is the authoritative install root. Deriving the key base from
    # `--vault-root` instead (e.g. `/install/personal` from
    # `/install/personal/memory-vault`) opened the install's live database while
    # writing keys, and `_ensure_path_base` then cleared every workspace's rows
    # because the base did not match the server's `/install`.
    runtime_arg = getattr(args, "runtime_root", None)
    env_runtime = os.environ.get("CIAO_RUNTIME_ROOT", "").strip()
    if runtime_arg or env_runtime:
        runtime_root = _resolve_runtime_root(runtime_arg)
        key_base = runtime_root.parent
    else:
        # Keys are relative to the install root, so one database can hold several
        # agent roots each with a vault of the same name.
        key_base = Path(
            os.environ.get("CIAO_WORKSPACE", "").strip() or vault_root.parent
        ).expanduser().resolve()
        runtime_root = (key_base / ".runtime").resolve()
    logs_root = logs_root_for(key_base, vault_root, runtime_root)
    # Install-owned, exactly like the MCP tools and startup indexing: with the
    # legacy global `~/.ciao/vault-fts.db`, a `ciao vault-search` run from a dev
    # checkout cleared the production install's index (and vice versa) because
    # the key base differs between installs.
    db_path = fts_search.get_db_path(runtime_root)

    if args.rebuild and db_path.exists():
        try:
            db_path.unlink()
            print("Dropped index database for rebuild.", file=sys.stderr)
        except OSError as exc:
            print(f"Error dropping index database: {exc}", file=sys.stderr)

    conn = sqlite3.connect(db_path)
    try:
        fts_search.init_db(conn)
        if not args.query:
            vault_indexed, vault_removed = fts_search.index_vault(conn, vault_root, path_base=key_base)
            logs_indexed, logs_removed = fts_search.index_logs(conn, vault_root, logs_root=logs_root, path_base=key_base)
            if vault_indexed or vault_removed or logs_indexed or logs_removed:
                print(
                    "FTS Index updated: "
                    f"vault ({vault_indexed} indexed, {vault_removed} removed), "
                    f"logs ({logs_indexed} indexed, {logs_removed} removed).",
                    file=sys.stderr,
                )
            return 0

        try:
            if args.logs:
                indexed, removed = fts_search.index_logs(conn, vault_root, logs_root=logs_root, path_base=key_base)
                if indexed or removed:
                    print(
                        f"Transcripts index: {indexed} indexed, {removed} removed.",
                        file=sys.stderr,
                    )
            else:
                indexed, removed = fts_search.index_vault(conn, vault_root, path_base=key_base)
                if indexed or removed:
                    print(
                        f"Vault index: {indexed} indexed, {removed} removed.",
                        file=sys.stderr,
                    )
        except Exception as exc:  # noqa: BLE001 - search can still use existing index.
            print(f"Incremental indexing error: {exc}", file=sys.stderr)

        # Scope the query to this vault's key prefix. The database is shared and
        # the migration rebuild deliberately fills it with every re-rooted
        # workspace's rows, while the prune now preserves sibling roots — so an
        # unscoped query returned another workspace's note titles and snippets to
        # whoever ran `ciao vault-search` here. Same filter the control plane's
        # vault_search applies.
        results = (
            fts_search.search_logs(
                conn,
                args.query,
                limit=args.limit,
                # Same reason the vault query is scoped: the FTS database
                # deliberately holds rows from every re-rooted root, so an
                # unscoped search returns another workspace's transcripts.
                path_prefix=fts_search.logs_key_prefix(logs_root, key_base),
            )
            if args.logs
            else fts_search.search_vault(
                conn,
                args.query,
                limit=args.limit,
                path_prefix=fts_search.vault_key_prefix(vault_root, key_base),
            )
        )
    finally:
        conn.close()

    if not results:
        print(f"No matches found for: {args.query}")
        return 0

    print(f"### Search Results for: {args.query}\n")
    for result in results:
        # Keys are relative to `key_base`, not to the vault's parent: on a
        # re-rooted install those differ by the workspace segment, so joining
        # against the parent printed a `file://` link that does not exist.
        abs_path = key_base / result["path"]
        link = f"file://{abs_path.as_posix()}"
        print(f"- **[{result['title']}]({link})** (rank: {result['rank']})")
        if result["snippet"]:
            snippet = result["snippet"].replace("<<<", "**`").replace(">>>", "`**")
            print(f"  *{snippet}*")
        print()
    return 0


def _vault_migrate_command(args: argparse.Namespace) -> int:
    """Bring a vault's frontmatter types onto the canonical vocabulary.

    Dry-run by default, like ``vault-index`` needing ``--write``: this rewrites
    the user's notes, so applying is opt-in even though the substitution is
    mechanical.
    """
    from ciao.vault_migration import migrate_vault_vocabulary, retain_retired_stock_types

    vault_root = _resolve_vault_root(args.vault_root)
    if not vault_root.is_dir():
        print(
            f"Vault root is missing or not a directory: `{vault_root}`",
            file=sys.stderr,
        )
        return 1

    # First, so the renames below are judged against the categories this vault
    # keeps: a retired stock type still in use becomes a category of its own.
    retention = retain_retired_stock_types(vault_root, apply=args.apply)
    summary = migrate_vault_vocabulary(vault_root, apply=args.apply)
    summary["retained"] = retention["retained"]
    if not args.apply:
        # Nothing is written yet, so the types the retained categories will
        # claim still read as unknown; they are not a decision for the user.
        summary["unresolved"] = {
            raw: paths
            for raw, paths in summary["unresolved"].items()
            if raw.lower() not in retention["covers"]
        }
    if "failed" in retention:
        summary["failed"].append({"path": "entity-types.yaml", "error": retention["failed"]})
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1 if summary["unresolved"] or summary["failed"] else 0

    if retention["retained"]:
        verb = "Kept" if args.apply else "Would keep"
        print(
            f"{verb} retired stock categories this vault still uses: "
            f"{', '.join(retention['retained'])}."
        )

    changes = summary["renamed"] if args.apply else summary["planned"]
    verb = "Renamed" if args.apply else "Would rename"
    if changes:
        print(f"{verb} {len(changes)} note(s):")
        for change in changes:
            print(f"  {change['from']} -> {change['to']}  {change['path']}")
    else:
        print("No aliased types to rename.")

    if summary["unresolved"]:
        print("\nNo canonical equivalent — categorise these yourself:")
        for raw_type, paths in sorted(summary["unresolved"].items()):
            print(f"  type: {raw_type}")
            for path in paths:
                print(f"    {path}")

    if summary["failed"]:
        print("\nFailed:", file=sys.stderr)
        for change in summary["failed"]:
            print(f"  {change['path']}: {change['error']}", file=sys.stderr)

    if not args.apply and changes:
        print("\nRe-run with --apply to write these changes.")
    return 1 if summary["unresolved"] or summary["failed"] else 0


def _print_link_migration_skip(summary: dict) -> int:
    print(f"Nothing done: {summary['skipped']}.", file=sys.stderr)
    if summary["skipped"] == "vault has uncommitted changes":
        print(
            "Commit or stash the vault first, or pass --force to rewrite anyway.",
            file=sys.stderr,
        )
    elif summary["skipped"] == "already migrated":
        print(
            f"Receipt: {summary.get('receipt_path', '')} "
            f"({summary.get('migrated_at', 'unknown date')}). "
            "Pass --force to migrate again.",
            file=sys.stderr,
        )
    return 1


def _enclosing_vault_root(vault_root: Path) -> Path | None:
    """The configured vault, when ``vault_root`` is a directory *inside* it.

    A workspace subtree looks enough like a vault to run on — it has notes and
    folders — but it is not one, and the migration has no way to notice. Refs
    resolve against a filename index built from the root it is handed, so
    pointing it at `memory-vault/work` makes every link into the vault's shared
    `People/` and root notes unresolvable. Those get converted anyway, to a
    destination relative to a root that does not contain them, and reported as
    links that were already dead — so the run both corrupts working links and
    describes the corruption as pre-existing.
    """
    configured = _resolve_vault_root(None)
    if not configured.is_dir() or vault_root == configured:
        return None
    return configured if configured in vault_root.parents else None


def _vault_migrate_links_command(args: argparse.Namespace) -> int:
    """Convert a vault's `[[wikilinks]]` to relative markdown links.

    Dry-run by default, like ``vault-migrate``: this rewrites the prose of the
    user's own notes, so applying is opt-in. Three extra rails, because unlike a
    frontmatter type swap this touches every line — it refuses on an existing
    receipt (whose reverse map a second pass would overwrite), on a vault with
    uncommitted changes (so `git checkout` stays a working undo), and on a root
    nested inside the configured vault.

    The nesting rail gates the *preview* too, unlike the other two. They protect
    a write, so gating the dry run would have meant reaching for `--force` just
    to look. This one is different: a too-narrow root does not make the write
    unsafe and the preview fine, it makes the preview itself wrong — working
    links are listed as dead — so a report nobody should act on is not worth
    printing.
    """
    from ciao.vault_migrate_links import migrate_links

    vault_root = _resolve_vault_root(args.vault_root)
    if not vault_root.is_dir():
        print(
            f"Vault root is missing or not a directory: `{vault_root}`",
            file=sys.stderr,
        )
        return 1

    enclosing = _enclosing_vault_root(vault_root)
    if enclosing is not None and not args.force:
        print(
            f"`{vault_root}` is a directory inside the vault at `{enclosing}`, "
            "not a vault of its own. Refs resolve against the root passed here, "
            "so every link to a note outside it would be reported as dead and "
            "rewritten to a path that resolves nowhere.\n"
            "Re-run without `--vault-root` to convert the whole vault. The "
            "receipt is per install, so a partial run would also mark the vault "
            "migrated and stop the app from offering the rest.\n"
            "Pass `--force` if you really mean this root.",
            file=sys.stderr,
        )
        return 1

    summary = migrate_links(
        vault_root,
        _resolve_runtime_root(args.runtime_root),
        apply=args.apply,
        force=args.force,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1 if summary.get("skipped") or summary.get("failed") else 0
    if "skipped" in summary:
        return _print_link_migration_skip(summary)

    verb = "Rewrote" if args.apply else "Would rewrite"
    rewrites = summary["rewrites"]
    if rewrites:
        print(
            f"{verb} {len(rewrites)} link(s) in {summary['files_rewritten']} "
            f"of {summary['files_scanned']} note(s):"
        )
        for change in rewrites:
            print(
                f"  {change['path']}:{change['line']}  "
                f"{change['from']} -> {change['to']}"
            )
    elif summary["failed"]:
        # "No wikilinks found" would be a flat lie here: wikilinks WERE found,
        # every note carrying them failed to write, and the failures are printed
        # to stderr just below. A retry hit exactly this and told the operator
        # the vault was clean.
        print(
            f"Rewrote nothing: every note with wikilinks failed to write "
            f"({summary['files_scanned']} scanned)."
        )
    else:
        print(f"No wikilinks found in {summary['files_scanned']} note(s).")

    if summary["unresolved"]:
        # Deliberately not "these were already dead wikilinks". Nothing here
        # establishes that: unresolved means the ref matched no note *under the
        # root this run was given*, which is also what a perfectly good link
        # looks like when the root is too narrow. Asserting pre-existing rot let
        # the tool label its own broken output as damage it had found.
        print(
            f"\nConverted but resolving to nothing — no note under `{vault_root}` "
            "matches these refs, so they now report as broken markdown links. "
            "A link that works in Obsidian and appears here means the root is "
            "too narrow, not that the link was dead:"
        )
        for item in summary["unresolved"]:
            print(f"  {item['path']}:{item['line']}  [[{item['ref']}]]")

    if summary["anchors_dropped"]:
        print("\nHeading anchors dropped (recorded in the receipt):")
        for item in summary["anchors_dropped"]:
            print(f"  {item['path']}:{item['line']}  [[{item['ref']}#{item['anchor']}]]")

    if summary["failed"]:
        print("\nFailed:", file=sys.stderr)
        for item in summary["failed"]:
            print(f"  {item['path']}: {item['error']}", file=sys.stderr)

    if args.apply and rewrites:
        print(f"\nReceipt: {summary.get('receipt_path', '')}")
        print("Reverse it exactly with `ciao vault-unmigrate-links --apply`.")
        if not summary.get("complete", True):
            # The receipt is real and the undo works, but the migration is not
            # done. Printing only the success trailer read as "finished".
            print(
                "This run did NOT finish: the notes listed under Failed still "
                "use wikilinks. Fix the cause and re-run — the reverse map "
                "carries forward, so nothing already converted is lost."
            )
    elif not args.apply and rewrites:
        print("\nRe-run with --apply to write these changes.")
    return 1 if summary["failed"] else 0


def _learnings_migrate_command(args: argparse.Namespace) -> int:
    """Convert a workspace's legacy ``Learnings.md`` entries to canonical records.

    Dry-run by default, like every other one-off migration here: this rewrites
    lines the user wrote, so applying is opt-in and the preview *is* the apply
    rather than a description of it. A receipt records an exact reverse map, and
    ``--revert`` restores the original bytes from it — so the write is not a
    one-way door even though the file is not under git.

    The exit code reports findings rather than just failure: a file with a line
    this code cannot read has been migrated as far as it safely can be, and the
    operator needs to know that from the status alone. A clean run over clean
    content exits 0.
    """
    from ciao.learnings_migrate import (
        migrate_learnings_file,
        new_receipt_path,
        read_receipt,
        unmigrate_learnings_file,
        write_receipt,
    )

    vault_root = _resolve_vault_root(args.vault_root)
    if not vault_root.is_dir():
        print(f"Vault root is missing or not a directory: `{vault_root}`", file=sys.stderr)
        return 1

    if args.revert:
        receipt = read_receipt(Path(args.revert))
        if receipt is None:
            print(
                f"Not a readable learnings-migrate receipt: `{args.revert}`",
                file=sys.stderr,
            )
            return 1
        if receipt.get("vault_root") and str(receipt["vault_root"]) != str(vault_root):
            # Refused rather than attempted: the receipt's spans are offsets into
            # one specific file, so reversing from a different root reads the
            # wrong bytes at those offsets and the offset check would then be
            # checking the wrong document.
            print(
                f"Receipt records a migration of `{receipt['vault_root']}`, not "
                f"`{vault_root}`. Re-run with `--vault-root "
                f"{receipt['vault_root']}`.",
                file=sys.stderr,
            )
            return 1
        summary = unmigrate_learnings_file(vault_root, receipt, apply=args.apply)
    else:
        summary = migrate_learnings_file(
            vault_root,
            workspace=vault_root.name,
            apply=args.apply,
        )

    # Recorded before anything is printed, and only for a run that actually
    # wrote: a receipt for a dry run would reverse spans that are still in their
    # original place, and `--revert` on it would corrupt the file rather than
    # restore it. A run whose write failed is the same thing and is gated by the
    # same count — `migrate_learnings_file` reports nothing migrated when the
    # bytes did not land, so the two cannot disagree here.
    receipt_path = ""
    if args.apply and not args.revert and summary.get("entries_migrated"):
        receipt_path = str(
            write_receipt(
                new_receipt_path(_resolve_runtime_root(args.runtime_root)), summary
            )
        )
        summary["receipt_path"] = receipt_path

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return _learnings_migration_status(summary)

    for problem in summary.get("diagnostics") or []:
        # A line this code could not read is a line nothing downstream can count,
        # so it is the one thing in an otherwise successful run the owner has to
        # look at. Printed to stderr and reflected in the exit code, not folded
        # into a summary that reads as finished.
        print(f"  kept as written: {problem}", file=sys.stderr)
    if summary.get("failed"):
        print("\nFailed:", file=sys.stderr)
        for item in summary["failed"]:
            print(f"  {item['path']}: {item['error']}", file=sys.stderr)

    if args.revert:
        _print_learnings_revert(summary, apply=args.apply)
    else:
        _print_learnings_migration(summary, apply=args.apply)
    if receipt_path:
        print(f"\nReceipt: {receipt_path}")
        print(
            "Reverse it exactly with `ciao learnings-migrate "
            f"--revert {receipt_path} --apply`."
        )
    return _learnings_migration_status(summary)


def _print_learnings_migration(summary: dict[str, Any], *, apply: bool) -> None:
    """What a migration run did, or would do, with every span it touched."""
    count = summary.get("entries_migrated", 0)
    if count:
        verb = "Migrated" if apply else "Would migrate"
        print(f"{verb} {count} learning entr(y/ies) in Workspace/Learnings.md:")
        for change in summary.get("rewrites") or []:
            print(f"  {change['from']}")
            print(f"    -> {change['to']}")
        if not apply:
            print("\nRe-run with --apply to write these changes.")
        return
    if summary.get("failed"):
        # The count is zero because nothing was written, and the spans are still
        # a plan — so falling through to "already canonical" would be the one
        # claim in the output nobody could check against the file, because the
        # file is exactly as it was. Not the spans either: after a concurrent
        # write they were computed against a revision that no longer holds, and
        # a stale diff reads as a fresh one. The reason is on stderr, and a dry
        # run prints the plan against whatever the file holds now.
        print("Nothing was written: the file is as it was.")
        return
    if "skipped" in summary:
        print(f"Nothing to migrate: {summary['skipped']}.")
        return
    scanned = summary.get("entries_scanned", 0)
    print(f"Nothing to migrate: {scanned} entr(y/ies) already canonical.")


def _print_learnings_revert(summary: dict[str, Any], *, apply: bool) -> None:
    """What a revert did, or would do."""
    if summary.get("entries_reverted"):
        verb = "Reverted" if apply else "Would revert"
        print(f"{verb} {summary['entries_reverted']} learning entr(y/ies):")
        if not apply:
            print("\nRe-run with --apply to write these changes.")
        return
    if "skipped" in summary:
        print(f"Nothing to revert: {summary['skipped']}.")
    else:
        print("Nothing to revert.")


def _learnings_migration_status(summary: dict[str, Any]) -> int:
    """1 for anything the operator has to look at, 0 only for a fully clean run.

    Not "1 for failure": a migration that rewrote what it could and reported a
    line it could not read did not fail, and a status of 0 would tell a script it
    needs no attention — which is precisely the condition that leaves the unreadable
    line unreadable.
    """
    if summary.get("failed") or summary.get("diagnostics"):
        return 1
    return 0


# ── learnings-cleanup ───────────────────────────────────────────────────────

#: The width of the two text columns that are allowed to be truncated. The key
#: and the reason are what a reviewer reads across a row; the statement is what
#: they read *below* it, in full, so it is never cut.
_CLEANUP_KEY_WIDTH = 22
_CLEANUP_REASON_WIDTH = 34


def _shorten(value: str, width: int) -> str:
    text = " ".join(value.split())
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _print_cleanup_table(
    plan: Any, *, stale: list[str], applied: bool
) -> None:
    """The whole Active list, one row per entry, with the decision beside it.

    Every row, not just the removable ones. The rows this command refuses to act
    on are the ones an operator most needs to see: an entry nothing has ever
    asked about, an entry whose finding is still open, an entry this code cannot
    read at all. A table of only the candidates would answer all of those by
    omission, and "it isn't in the list" is not an answer a person can act on.
    """
    from ciao.learnings_cleanup import CONFLICT

    counts = plan.counts
    print(
        f"{plan.active} Active entr(y/ies) in {plan.path} for workspace "
        f"{plan.workspace}:"
    )
    if plan.blocked:
        print(f"  nothing may be removed: {plan.blocked}")
        return
    header = (
        f"  {'KEY':<{_CLEANUP_KEY_WIDTH}}  {'ID':<12}  {'STATE':<8}  "
        f"{'REASON':<{_CLEANUP_REASON_WIDTH}}  EVIDENCE"
    )
    print(header)
    for row in plan.rows:
        state = "REMOVE" if row.action == "remove" else row.action.upper()
        if row.action == "keep" and row.detail.startswith("eligible;"):
            # Eligible, but not this run's — capped, or settled and not approved.
            # Still real work, and printing it as `REMOVE` in a table whose apply
            # left it in place would be a lie.
            state = "LATER"
        if row.action == CONFLICT:
            # A conflict's detail is the parser's whole diagnostic, which is far
            # too long for a column a reviewer scans across; it goes to stderr
            # underneath in full, and the column says the one thing that matters.
            reason = "this code cannot read it; kept as written"
        else:
            reason = row.detail or row.reason
        print(
            f"  {_shorten(row.key or '(no identity)', _CLEANUP_KEY_WIDTH):<{_CLEANUP_KEY_WIDTH}}  "
            f"{_shorten(row.learning_id or '-', 12):<12}  {state:<8}  "
            f"{_shorten(reason, _CLEANUP_REASON_WIDTH):<{_CLEANUP_REASON_WIDTH}}  "
            f"{_shorten(row.evidence or row.destination, 60)}"
        )
        print(f"      {_shorten(row.line, 100)}")
        if row.learning_id:
            # The two values an approval names, in full and unabbreviated. An
            # approval is bound to the exact bytes it was reviewed at, so a
            # truncated id or a truncated revision would be an approval of nothing —
            # which is why the scan-friendly columns above are abbreviated and this
            # line is not.
            print(f"      id {row.learning_id}")
            print(f"      rev {row.entry_revision}")
    print(
        f"\n{counts['remove']} removable, {counts['keep']} kept, "
        f"{counts['conflict']} unreadable, {counts['routes']} routed upstream "
        f"(waiting on another maintainer), {counts['unmatched']} not linked to "
        "anything yet."
    )
    if plan.over_cap:
        print(
            "More are settled than one run retires; the rest wait for the next "
            "one."
        )
    spoken = " ".join(row.detail for row in plan.conflicts)
    for row in plan.conflicts:
        # The whole diagnostic, not the truncated reason column: this is the line a
        # person has to go and look at, and the table's width is not enough to say
        # why.
        print(f"  kept as written: {row.line.strip()}", file=sys.stderr)
        for problem in row.detail.split("; "):
            print(f"    {problem}", file=sys.stderr)
    for problem in plan.diagnostics:
        # An Active problem is already spoken for by its conflict row; this loop is
        # for the rest (a Promoted entry, say), so the two cannot print the same
        # sentence twice.
        if problem not in spoken:
            print(f"  kept as written: {problem}", file=sys.stderr)
    for note in stale:
        print(f"  approval not used: {note}", file=sys.stderr)
    if applied:
        return
    if counts["remove"]:
        print(
            "\nNothing was written. Copy the ID and entry revision of each row "
            "you want retired into an approval file, then re-run with --apply."
        )
    else:
        print("\nNothing to remove.")


def _read_approval_file(path: Path) -> dict[str, dict[str, Any]]:
    """The approved rows, keyed by ``learning_id``.

    A JSON list of objects, each naming a ``learning_id``, the ``entry_revision``
    it was reviewed at, and a ``reason`` and ``evidence``. Both words are
    required: a removal decided by a tool rather than by a person is exactly the
    thing the attended workflow exists to replace, and an approval file with an
    empty reason is a removal nobody owned.

    Returned whole or not at all. A partially-read approval file is an approval
    of a subset nobody chose, so the file is refused rather than trimmed.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read the approval file: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("the approval file must hold a list of approved rows")
    approved: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"approval {index} must be an object")
        learning_id = str(item.get("learning_id") or "").strip()
        revision = str(item.get("entry_revision") or "").strip()
        reason = str(item.get("reason") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        missing = [
            name
            for name, value in (
                ("learning_id", learning_id),
                ("entry_revision", revision),
                ("reason", reason),
                ("evidence", evidence),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"approval {index} needs a non-empty "
                + ", ".join(f'"{name}"' for name in missing)
            )
        if learning_id in approved:
            raise ValueError(f"approval {index} names {learning_id} twice")
        approved[learning_id] = {
            "learning_id": learning_id,
            "entry_revision": revision,
            "reason": reason,
            "evidence": evidence,
            "reapprove": bool(item.get("reapprove")),
        }
    return approved


def _apply_approvals(
    plan: Any, approved: dict[str, dict[str, Any]]
) -> tuple[list[Any], list[str]]:
    """Narrow a plan to the approved rows, and say what was left out.

    An approval is bound to the exact bytes it was reviewed against, so one that
    no longer matches is not honoured — the entry moved, the settlement changed,
    or the review was of a different document. That is the "stale decision"
    case, and the answer is to re-run the dry run and approve again rather than
    to apply the closest thing: the operator's judgement was about specific
    text, and text that has since changed is not that text.

    Both halves come back together because they are one answer: a row that is
    not in the approved set has to say *why*, or a reviewer who approved three of
    four rows cannot tell which one they forgot.
    """
    allowed: list[Any] = []
    stale: list[str] = []
    matched: set[str] = set()
    for row in plan.rows:
        approval = approved.get(row.learning_id)
        if approval is None or not row.learning_id:
            continue
        matched.add(row.learning_id)
        if approval["entry_revision"] != row.entry_revision:
            stale.append(
                f"{row.key or row.learning_id}: approved at revision "
                f"{approval['entry_revision'][:12]}…, this entry is now "
                f"{row.entry_revision[:12]}…"
            )
            continue
        if not row.removable and not approval["reapprove"]:
            stale.append(
                f"{row.key or row.learning_id}: kept for {row.reason}, and an "
                "approval to retire a kept entry has to say reapprove"
            )
            continue
        allowed.append(row)
    for learning_id in approved:
        if learning_id not in matched:
            stale.append(
                f"{learning_id[:12]}…: no Active entry in this document has that "
                "learning id any more"
            )
    return allowed, stale


def _learnings_cleanup_command(args: argparse.Namespace) -> int:
    """Retire settled ``Workspace/Learnings.md`` entries, one reviewed row at a time.

    Dry-run by default, like ``learnings-migrate`` and for the same reason: this
    removes lines the user wrote. The dry run is the same computation the apply
    performs, and it lists **every** Active entry with the decision beside it, so
    the thing being removed is a decision somebody saw rather than a diff they
    were told about.

    ``--apply`` refuses without ``--approval-file``. That is the whole attended
    contract: a cleanup that runs because a flag was passed has had its judgement
    from a flag, and the rows that matter — a legacy entry nothing has ever
    proposed, an entry a person decided was obsolete — are exactly the rows a
    flag cannot judge. The file names the entry, the exact revision it was
    reviewed at, a reason and the evidence for it, and the receipt keeps all four
    so the decision outlives the run.

    ``--apply-settled`` is the other half, and it is the one the nightly pass
    names. It removes only the rows the reconciliation *already* proposed —
    ``actor="system"``, no approval file, no ``reapprove``, and the same
    :data:`~ciao.curation_run.LEARNINGS_CLEANUP_MAX_ITEMS` cap the worklist
    budget gives the pass — so what it can remove is exactly what a fold over the
    proposal queue and the draft sidecar already answered, which is settlement
    rather than judgement. It is a separate flag rather than a form of
    ``--apply`` because it is a different decision: every row it removes is
    reversible from a receipt, and none of them carries anybody's reason, so it
    never produces the reviewed no-op receipt and never lifts a suppression.

    ``--revert`` restores the removed bytes from a receipt. It does not lift the
    suppression, so the next nightly pass does not undo the undo; the entry
    becomes eligible again when it is edited, or when somebody approves it with
    ``reapprove``.

    The receipt is written by the apply, before the document, so a run whose
    receipt could not be persisted removes nothing at all rather than printing
    that the removals landed without a way back.

    The exit code reports what a person has to look at rather than only what
    failed: an unreadable line, an approval that was not used, or a removal that
    was refused all exit 1, because in each case something is still outstanding
    and a script told "0" would stop looking.
    """
    from ciao.curation_run import LEARNINGS_CLEANUP_MAX_ITEMS
    from ciao.learnings_cleanup import (
        KEEP,
        apply_cleanup,
        new_receipt_path,
        plan_cleanup,
        read_receipt,
        unmigrate_cleanup,
    )

    vault_root = _resolve_vault_root(args.vault_root)
    if not vault_root.is_dir():
        print(f"Vault root is missing or not a directory: `{vault_root}`", file=sys.stderr)
        return 1
    workspace = args.workspace or vault_root.name
    config = _curation_config(vault_root.parent, vault_root)

    # The three write flags are three different decisions, and a run may make one.
    # Refused up here, before any of them does any work, because the point of
    # refusing is that nothing happened — not that something happened and was then
    # undone. (``--revert --apply`` is not a conflict: that pair is how an undo is
    # written at all, and `--apply` is what tells it to write.)
    if args.revert and args.apply_settled:
        print(
            "--revert restores a previous run from its receipt; it takes no "
            "--apply-settled, and the two say opposite things about a removed "
            "entry.",
            file=sys.stderr,
        )
        return 1
    if args.apply_settled and args.approval_file:
        print(
            "--apply-settled retires the rows the reconciliation already proposed "
            "and reads no approval file, so the two cannot be combined. Use "
            "--apply --approval-file for the attended rows.",
            file=sys.stderr,
        )
        return 1
    if args.apply_settled and args.apply:
        print(
            "--apply and --apply-settled are two different decisions in one run; "
            "pick the one this run is making.",
            file=sys.stderr,
        )
        return 1

    if args.revert:
        receipt = read_receipt(Path(args.revert))
        if receipt is None:
            print(
                f"Not a readable learnings-cleanup receipt: `{args.revert}`",
                file=sys.stderr,
            )
            return 1
        recorded = str(receipt.get("vault_root") or "")
        if recorded and recorded != str(vault_root):
            print(
                f"Receipt records a cleanup of `{recorded}`, not `{vault_root}`. "
                f"Re-run with `--vault-root {recorded}`.",
                file=sys.stderr,
            )
            return 1
        summary = unmigrate_cleanup(vault_root, receipt, apply=args.apply)
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 1 if summary.get("failed") else 0
        _print_cleanup_revert(summary, apply=args.apply)
        return 1 if summary.get("failed") else 0

    approved: dict[str, dict[str, Any]] = {}
    if args.approval_file:
        try:
            approved = _read_approval_file(Path(args.approval_file))
        except ValueError as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
    if args.apply and args.approval_file is None:
        # The file's *presence* is the approval, not its contents: an empty list is
        # a person saying "I read the whole table and nothing should go", which is
        # a decision, and a decision that deserves a receipt.
        print(
            "--apply needs --approval-file. Run without it to see the table, "
            "then approve the rows you want retired with their entry revision, "
            "a reason and the evidence for it. An empty list records that you "
            "reviewed the table and nothing should be removed. To retire only the "
            "rows the reconciliation already proposed, unattended, use "
            "--apply-settled instead.",
            file=sys.stderr,
        )
        return 1

    plan = plan_cleanup(
        vault_root,
        workspace=workspace,
        config=config,
        # The unattended mode is the nightly pass, and the pass is capped at the
        # same number of removals. Capped on the *plan* as well as on the apply, so
        # the table cannot print `REMOVE` beside a row this run will not remove —
        # the capped rows become `LATER` here, and the run says a backlog exists.
        max_removals=LEARNINGS_CLEANUP_MAX_ITEMS if args.apply_settled else None,
    )
    stale: list[str] = []
    if args.approval_file:
        allowed, stale = _apply_approvals(plan, approved)
        permitted = {row.learning_id for row in allowed}
        # An approval with ``reapprove`` on a row the planner kept makes it a
        # candidate: the planner had no evidence for it, and a person with a reason
        # and the evidence for it is exactly how an obsolete classification gets
        # retired. The row keeps its span, so the splice and the receipt are the
        # same shape as any other removal.
        selected = list(allowed)
        extra = {row.learning_id for row in allowed if row.action == KEEP}
        deferred: list[Any] = []
        for row in plan.removals:
            if row.learning_id in permitted:
                continue
            # Settled and removable, but not approved. That is a decision the
            # reviewer made, not an outstanding question, so it is neither a stale
            # approval nor an error — but it must not print as `REMOVE` in a table
            # whose apply left it in place.
            deferred.append(
                replace(
                    row,
                    action=KEEP,
                    reason="pending",
                    detail="eligible; not approved in this run",
                )
            )
        plan = replace(
            plan,
            removals=tuple(selected),
            kept=tuple(row for row in plan.kept if row.learning_id not in extra)
            + tuple(deferred),
        )

    if not args.apply and not args.apply_settled:
        # The dry run stops here. Not "apply and then describe it": a preview that
        # has already written is not a preview, and the promise the command makes
        # is that this computation is the same one the apply performs — not that
        # it is performed.
        if args.json:
            print(json.dumps({"plan": plan.as_dict(), "result": None}, indent=2, sort_keys=True))
            return 1 if stale or plan.diagnostics or plan.conflicts else 0
        _print_cleanup_table(plan, stale=stale, applied=False)
        return _cleanup_status(None, stale, plan)

    # The receipt path is allocated here and handed to the apply, which writes it
    # *before* the document. A run that cannot record the reverse map removes
    # nothing, which is the whole point of having this be one call rather than a
    # write the command does afterwards.
    receipt_path = new_receipt_path(_resolve_runtime_root(args.runtime_root))
    result = apply_cleanup(
        vault_root,
        plan,
        workspace=workspace,
        config=config,
        actor="system" if args.apply_settled else "operator",
        reapprove=not args.apply_settled,
        approvals=approved,
        # An approval file *was* supplied, even an empty one: that is a person
        # saying they read the table, and the receipt is the only durable record
        # of it. ``--apply-settled`` supplies none, and gets no such receipt.
        reviewed=not args.apply_settled and args.approval_file is not None,
        max_removals=LEARNINGS_CLEANUP_MAX_ITEMS if args.apply_settled else None,
        receipt_path=receipt_path,
    )
    if args.json:
        print(
            json.dumps(
                {"plan": plan.as_dict(), "result": result.as_dict()},
                indent=2,
                sort_keys=True,
            )
        )
        return _cleanup_status(result, stale, plan)

    _print_cleanup_table(plan, stale=stale, applied=result.applied)
    if result.applied:
        print(f"\nRemoved {result.removed_count} entr(y/ies).")
        if result.receipt_path:
            print(f"Receipt: {result.receipt_path}")
            print(
                "Reverse it exactly with `ciao learnings-cleanup --revert "
                f"{result.receipt_path} --apply`."
            )
    elif result.receipt is not None:
        print("\nNothing was removed; the review itself is recorded.")
        if result.receipt_path:
            print(f"Receipt: {result.receipt_path}")
    for note in result.conflicts:
        print(f"  not removed: {note}", file=sys.stderr)
    for failure in result.failed:
        print(f"  failed: {failure}", file=sys.stderr)
    if result.skipped:
        print(f"Nothing was written: {result.skipped}")
    return _cleanup_status(result, stale, plan)


def _cleanup_status(result: Any, stale: list[str], plan: Any) -> int:
    """1 while anything about this document still needs a person.

    ``result`` is ``None`` for a dry run, which is the same question asked before
    anything happened: a kept row is a question answered, an unreadable line and an
    unused approval are not.
    """
    if plan.diagnostics or plan.conflicts or stale or plan.blocked:
        return 1
    if result is not None and (result.failed or result.conflicts):
        return 1
    return 0


def _print_cleanup_revert(summary: dict[str, Any], *, apply: bool) -> None:
    """What an undo did, or would do.

    A failure is printed first whatever else came of it. An undo that restored the
    file and could not record the restored line as removed has left the next pass
    free to take it straight out again, and that is the one thing about the run
    the operator has to read — so it is not swallowed by the success message.
    """
    for item in summary.get("failed") or []:
        print(f"  {item.get('path')}: {item.get('error')}", file=sys.stderr)
    if summary.get("entries_reverted"):
        verb = "Restored" if apply else "Would restore"
        print(f"{verb} {summary['entries_reverted']} learning entr(y/ies).")
        for item in summary.get("suppressions_kept") or []:
            print(
                f"  still recorded as removed: {item['learning_id'][:12]}… — the "
                "nightly pass will not remove it again until it changes or is "
                "reapproved"
            )
        if not apply:
            print("\nRe-run with --apply to write these changes.")
        return
    if summary.get("skipped"):
        print(f"Nothing to restore: {summary['skipped']}.")
    else:
        print("Nothing to restore.")


def _vault_unmigrate_links_command(args: argparse.Namespace) -> int:
    """Restore the wikilinks recorded in the migration receipt.

    Exact rather than a re-derivation: only the spans the receipt names are put
    back, so markdown links the user wrote by hand are never converted into
    wikilinks they never had.
    """
    from ciao.vault_migrate_links import unmigrate_links

    vault_root = _resolve_vault_root(args.vault_root)
    if not vault_root.is_dir():
        print(
            f"Vault root is missing or not a directory: `{vault_root}`",
            file=sys.stderr,
        )
        return 1

    summary = unmigrate_links(
        vault_root,
        _resolve_runtime_root(args.runtime_root),
        apply=args.apply,
        force=args.force,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1 if summary.get("skipped") or summary.get("failed") else 0
    if "skipped" in summary:
        return _print_link_migration_skip(summary)

    verb = "Restored" if args.apply else "Would restore"
    if summary["restored"]:
        print(f"{verb} {summary['files_restored']} note(s):")
        for path in summary["restored"]:
            print(f"  {path}")
    else:
        print("Nothing to restore.")

    if summary["failed"]:
        # Not "changed since the migration" unconditionally: a wrong root fails
        # every file with a read error, and naming a cause we have not
        # established sent the reader looking for edits nobody made.
        print("\nLeft untouched:", file=sys.stderr)
        for item in summary["failed"]:
            print(f"  {item['path']}: {item['error']}", file=sys.stderr)
        recorded = summary.get("receipt_vault_root", "")
        if recorded and recorded != str(vault_root) and not summary["restored"]:
            print(
                f"\nNothing was restored, and the migration ran against "
                f"`{recorded}`. The receipt's paths are relative to that root — "
                f"re-run with `--vault-root {recorded}`.",
                file=sys.stderr,
            )

    if not args.apply and summary["restored"]:
        print("\nRe-run with --apply to write these changes.")
    return 1 if summary["failed"] else 0


def _vault_rehome_command(args: argparse.Namespace) -> int:
    """Re-file person notes a global curation run filed in the wrong workspace.

    Dry-run by default, like the other two vault migrations: this moves the user's
    own notes between workspaces and rewrites every reference to them, so applying
    is opt-in. Only the tag-obvious cases move; a note with no workspace-naming tag
    is queued in that workspace's `Workspace/Memory-Proposals.md` for review and
    left exactly where it is.

    ``--workspace-name`` names the registered workspaces. Without it they are derived
    from the vault's own directories, which is right for a CLI run but is *not*
    the registry — a caller with a `CiaoConfig` should pass
    ``config.workspace_names()`` to the library function instead.
    """
    from ciao.vault_rehome import rehome_people

    vault_root = _resolve_vault_root(args.vault_root)
    if not vault_root.is_dir():
        print(
            f"Vault root is missing or not a directory: `{vault_root}`",
            file=sys.stderr,
        )
        return 1

    summary = rehome_people(
        vault_root,
        _resolve_runtime_root(args.runtime_root),
        apply=args.apply,
        force=args.force,
        workspaces=args.workspace_name or None,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1 if summary.get("skipped") or summary.get("failed") else 0
    if "skipped" in summary:
        return _print_link_migration_skip(summary)

    verb = "Moved" if args.apply else "Would move"
    if summary["moves"]:
        print(
            f"{verb} {len(summary['moves'])} person note(s), rewriting "
            f"{len(summary['rewrites'])} reference(s) in "
            f"{summary['files_rewritten']} of {summary['notes_scanned']} note(s):"
        )
        for candidate in summary["mechanical"]:
            print(
                f"  {candidate['path']} -> {candidate['destination']}  "
                f"({candidate['reason']})"
            )
    elif summary["mechanical"]:
        # `mechanical` names the notes the run FOUND; `moves` names the ones it
        # managed to move. When every move failed, printing the clean-vault line
        # told the operator there was nothing misfiled while stderr listed the
        # failures — and it named the notes it had just found.
        print(
            f"Moved nothing: all {len(summary['mechanical'])} tag-obvious "
            f"move(s) failed ({summary['notes_scanned']} note(s) scanned):"
        )
        for candidate in summary["mechanical"]:
            print(f"  {candidate['path']} -> {candidate['destination']}")
    else:
        print(f"No tag-obvious misfiled people in {summary['notes_scanned']} note(s).")

    if summary["needs_judgement"]:
        queued = "Queued for review" if args.apply else "Would queue for review"
        print(f"\n{queued} — not moved, the tags do not decide it:")
        for candidate in summary["needs_judgement"]:
            destination = candidate["destination"] or "(no destination)"
            print(f"  {candidate['path']} -> {destination}  ({candidate['reason']})")
        for path in summary["proposals"]:
            print(f"  written to {path}")

    if summary["conflicts"]:
        # Each conflict carries its own reason, and there are now two: a note
        # already at the destination, or two candidates in this run racing for
        # it. The old fixed header asserted the first and so mis-described the
        # second - printing the reason is what the `failed` block below does.
        print("\nSkipped:", file=sys.stderr)
        for candidate in summary["conflicts"]:
            print(
                f"  {candidate['path']} -> {candidate['destination']}: "
                f"{candidate.get('error') or 'destination unavailable'}",
                file=sys.stderr,
            )

    if summary["failed"]:
        print("\nFailed:", file=sys.stderr)
        for item in summary["failed"]:
            print(f"  {item['path']}: {item['error']}", file=sys.stderr)

    # Keyed on the receipt, not on `moves`: a run whose moves all failed still
    # wrote one (its rewrites are real and reversible), and saying nothing left
    # a receipt on disk that the operator had never been told about.
    if args.apply and summary.get("receipt_path"):
        print(f"\nReceipt: {summary.get('receipt_path', '')}")
        print("Reverse it exactly with `ciao vault-unrehome --apply`.")
        if not summary.get("complete", True):
            print(
                "This run did NOT finish: the notes listed under Failed are "
                "still misfiled. Fix the cause and re-run — the reverse map "
                "carries forward, so nothing already done is lost."
            )
    elif not args.apply and (summary["moves"] or summary["needs_judgement"]):
        print("\nRe-run with --apply to write these changes.")
    return 1 if summary["failed"] or summary["conflicts"] else 0


def _vault_unrehome_command(args: argparse.Namespace) -> int:
    """Move the re-homed notes back and restore the references, from the receipt.

    Exact rather than a re-derivation: only the moves and spans the receipt names
    are reversed, so a note the user filed by hand is never dragged back with
    them. Deliberately not gated on a clean vault — the re-homing is what made it
    dirty.
    """
    from ciao.vault_rehome import unrehome_people

    vault_root = _resolve_vault_root(args.vault_root)
    if not vault_root.is_dir():
        print(
            f"Vault root is missing or not a directory: `{vault_root}`",
            file=sys.stderr,
        )
        return 1

    summary = unrehome_people(
        vault_root,
        _resolve_runtime_root(args.runtime_root),
        apply=args.apply,
        force=args.force,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1 if summary.get("skipped") or summary.get("failed") else 0
    if "skipped" in summary:
        return _print_link_migration_skip(summary)

    verb = "Moved back" if args.apply else "Would move back"
    if summary["moves_reverted"]:
        print(f"{verb} {len(summary['moves_reverted'])} person note(s):")
        for move in summary["moves_reverted"]:
            print(f"  {move['from']} -> {move['to']}")
    else:
        print("No notes to move back.")

    if summary["restored"]:
        restored = "Restored" if args.apply else "Would restore"
        print(f"\n{restored} references in {summary['files_restored']} note(s):")
        for path in summary["restored"]:
            print(f"  {path}")

    if summary["failed"]:
        print("\nLeft untouched (changed since the re-homing):", file=sys.stderr)
        for item in summary["failed"]:
            print(f"  {item['path']}: {item['error']}", file=sys.stderr)

    if not args.apply and (summary["moves_reverted"] or summary["restored"]):
        print("\nRe-run with --apply to write these changes.")
    return 1 if summary["failed"] else 0


def _vault_export_command(args: argparse.Namespace) -> int:
    """Write a portable OKF bundle from the vault or one workspace of it."""
    from ciao.okf import export_bundle

    vault_root = _resolve_vault_root(args.vault_root)
    summary = export_bundle(
        vault_root,
        args.dest,
        workspace=(args.workspace_name or "").strip(),
        force=args.force,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary.get("written") else 1

    if summary.get("skipped"):
        print(f"Nothing written: {summary['skipped']}", file=sys.stderr)
        if summary.get("example"):
            print(
                f"  First example: {summary['example']}\n"
                "  Convert with `ciao vault-migrate-links --apply`, or pass "
                "--force to ship a bundle whose links no consumer can follow.",
                file=sys.stderr,
            )
        return 1

    scope = summary["workspace"] or "whole vault"
    print(
        f"Wrote {summary['dest']} — {summary['concepts']} concept(s) "
        f"from {scope}, OKF {summary['okf_version']}."
    )
    if summary["cross_workspace_links"]:
        print(
            f"  {summary['cross_workspace_links']} link(s) point outside this "
            "workspace and will be dangling inside the bundle."
        )
    return 0


def _install_root_for_vault(vault_root: Path) -> Path | None:
    """The install a vault belongs to, for cross-root link validation.

    Per-root layout puts a vault at ``<install>/<workspace>/memory-vault``, so
    the install is two levels up — and only when the receipt says this install
    re-rooted. Guessing on a shared-layout install would excuse links that
    really do escape the vault.
    """
    from ciao.workspace_reroot import read_receipt  # noqa: PLC0415

    candidate = vault_root.parent.parent
    runtime = candidate / ".runtime"
    if read_receipt(runtime) is None:
        return None
    return candidate


def _vault_lint_command(args: argparse.Namespace) -> int:
    from ciao.vault_lint import VaultTraversalError, run_validation

    vault_root = _resolve_vault_root(args.vault_root)
    # `--migrate-links` is the in-place remedy for the one finding the linter
    # cannot fix by reporting: a vault still written in the retired dialect. It
    # delegates rather than reimplementing, so the receipt and both safety rails
    # behave exactly as they do from `vault-migrate-links`.
    if getattr(args, "migrate_links", False):
        return _vault_migrate_links_command(
            argparse.Namespace(
                vault_root=args.vault_root,
                runtime_root=None,
                apply=args.apply,
                force=args.force,
                json=False,
            )
        )
    if not vault_root.is_dir():
        print(
            f"Vault root is missing or not a directory: `{vault_root}`",
            file=sys.stderr,
        )
        return 1
    try:
        issues = run_validation(
            vault_root, install_root=_install_root_for_vault(vault_root)
        )
    except VaultTraversalError as exc:
        print(f"Vault inspection failed: {exc}", file=sys.stderr)
        return 1

    has_issues = False
    # No "Dead Wikilinks" section: with markdown links the only dialect, every
    # dead link lands in "Broken Markdown Links" below. The count did not change,
    # only the bucket it is reported in.
    if issues["frontmatter_errors"]:
        has_issues = True
        print("### Frontmatter Errors\n")
        for item in issues["frontmatter_errors"]:
            print(
                f"- `{item['source']}`: {item['message']} "
                f"(`{item['kind']}`)"
            )
        print()

    if issues["broken_markdown_links"]:
        has_issues = True
        print("### Broken Markdown Links\n")
        for item in issues["broken_markdown_links"]:
            print(
                f"- `{item['source']}` links to `{item['target']}`: "
                f"`{item['kind']}` (resolved: `{item['resolved']}`)"
            )
        print()

    if issues["orphans"]:
        has_issues = True
        print("### Orphan Pages\n")
        for path in issues["orphans"]:
            print(f"- `{path}` has no incoming links and is not in MEMORY files")
        print()

    if issues["duplicates"]:
        has_issues = True
        print("### Near-Duplicate Pages\n")
        for paths in issues["duplicates"]:
            print(f"- Overlapping paths: {', '.join(f'`{p}`' for p in paths)}")
        print()

    if not has_issues:
        print("Vault is clean!")
        return 0
    return 1


def _os_audit_command(args: argparse.Namespace) -> int:
    from ciao.os_audit import format_audit_markdown, run_os_audit

    workspace_raw = args.workspace or os.environ.get("CIAO_WORKSPACE") or Path(".")
    workspace = Path(workspace_raw).expanduser().resolve()
    # An explicit --workspace scopes the whole audit. Consulting the ambient
    # environment for the runtime and vault roots then lets an absolute
    # CIAO_RUNTIME_ROOT from the surrounding install escape the directory the
    # caller named, so the audit silently reports on the wrong workspace: its
    # registry, its job runs, its migration receipts. Auditing a second
    # workspace from inside a running Ciaobot chat hits this every time, because
    # the chat exports CIAO_RUNTIME_ROOT for its own install.
    explicit_workspace = args.workspace is not None

    def resolve_under_workspace(
        explicit: Path | None,
        env_name: str,
        default: str,
    ) -> Path:
        env_raw = None if explicit_workspace else os.environ.get(env_name)
        raw = explicit or env_raw or default
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace / path
        return path.resolve()

    runtime = resolve_under_workspace(
        args.runtime_root,
        "CIAO_RUNTIME_ROOT",
        ".runtime",
    )
    # `memory-vault` is the SHARED layout's answer. Post-migration there is no
    # vault at the install root, so a plain `ciao os-audit` resolved a path that
    # does not exist and reported `missing_vault_root` — on every correctly
    # migrated install, permanently. The audit is the release backstop; a
    # standing false error in it is how real findings stop being read.
    from ciao.config import agent_roots_for  # noqa: PLC0415

    roots = agent_roots_for(workspace, runtime)
    default_vault = "memory-vault"
    if roots and roots[0][1]:
        default_vault = str(Path(roots[0][1]) / "memory-vault")
    vault = resolve_under_workspace(
        args.vault_root,
        "CIAO_VAULT_ROOT",
        default_vault,
    )
    from ciao.config import CiaoConfig

    config_source = dict(os.environ)
    config_source.update({
        "CIAO_WORKSPACE": str(workspace),
        "CIAO_VAULT_ROOT": str(vault),
        "CIAO_RUNTIME_ROOT": str(runtime),
        # Loading config for a read-only audit must not create a session
        # secret merely because the CLI was invoked outside the server env.
        "PWA_AUTH_TOKEN": config_source.get("PWA_AUTH_TOKEN", "") or "os-audit",
    })
    audit_config = CiaoConfig.from_env(config_source)
    # Defaults from the dispatch env so the per-workspace hygiene routine needs
    # no prompt templating: its packaged prompt is one static string, and the
    # fanned-out entry already exports its workspace.
    workspace_name = (
        args.workspace_name or os.environ.get("CIAO_ACTIVE_WORKSPACE") or ""
    ).strip()
    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
        config=audit_config,
        workspace_name=workspace_name,
        scope=args.scope,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_audit_markdown(report))

    return {
        "healthy": 0,
        "needs_attention": 1,
        "error": 2,
    }.get(report["status"], 2)


def _print_vault_relocate_result(payload: dict[str, Any], *, applied: bool) -> None:
    verb = "Moved" if applied and payload.get("status") == "relocated" else "Would move"
    print(f"{payload.get('workspace', '')}: {payload.get('source', '')} -> {payload.get('destination', '')}")
    entries = payload.get("entries") or []
    moves = [e for e in entries if e.get("action") == "move"]
    skips = [e for e in entries if e.get("action") == "skip"]
    unclassified = [e for e in entries if e.get("action") == "unclassified"]
    if payload.get("whole_directory"):
        print(f"{verb} the whole vault directory.")
    elif moves:
        print(f"{verb} {len(moves)} item(s):")
        for entry in moves:
            print(f"  {entry['name']}")
    if skips:
        print(f"Left in place ({len(skips)}):")
        for entry in skips:
            print(f"  {entry['name']} — {entry['reason']}")
    if unclassified:
        print("\nCould not classify — resolve by hand, or ask the operator only about these:")
        for entry in unclassified:
            print(f"  {entry['name']} — {entry['reason']}")
    if payload.get("refusals"):
        print("\nRefused:", file=sys.stderr)
        for reason in payload["refusals"]:
            print(f"  {reason}", file=sys.stderr)
    if applied and payload.get("status") == "relocated":
        print(f"\nReceipt: {payload.get('receipt_path', '')}")
        print(f"Reverse it exactly with `ciao vault-relocate {payload.get('workspace', '')} --undo`.")
        if payload.get("restart_note"):
            print(f"\nRestart required: {payload['restart_note']}")
    elif not applied and not payload.get("refused") and (moves or payload.get("whole_directory")):
        print("\nRe-run with --apply to write this change.")


def _vault_relocate_command(args: argparse.Namespace) -> int:
    """Move one workspace's vault to its standard folder.

    Dry-run by default: prints the plan and changes nothing. --apply moves the
    vault and repoints that workspace's registry entry. --undo reverses the
    last completed relocation from its receipt.

    Distinct from `workspace-reroot`, which migrates EVERY registered
    workspace into its own agent root in one shot. This fixes one workspace
    whose vault sits at a non-standard path — the case the "vault is not in
    its standard folder" housekeeping card flags — and touches only that
    workspace. It moves this workspace's own content automatically and
    refuses on anything it cannot classify (a symlink, most often) rather than
    guessing, so the operator or an agent only has to resolve those, not the
    move as a whole.
    """
    from ciao import vault_relocate
    from ciao.config import CiaoConfig

    workspace = Path(args.workspace or os.environ.get("CIAO_WORKSPACE") or ".").expanduser().resolve()
    config_source = {}
    dotenv_path = workspace / ".env"
    if dotenv_path.is_file():
        from dotenv import dotenv_values

        config_source.update(
            {key: value for key, value in dotenv_values(dotenv_path).items() if value is not None}
        )
    if args.workspace is None:
        config_source.update(os.environ)
    # Anchored to the already-resolved `workspace`, not `_resolve_runtime_root`'s
    # ambient-env base: an explicit --workspace must win over CIAO_WORKSPACE the
    # same way `_workspace_reroot_command` insists on, or a relative
    # CIAO_RUNTIME_ROOT (or the bare ".runtime" default) would resolve against
    # the wrong install when the two disagree. Still honors CIAO_RUNTIME_ROOT
    # when --runtime-root is not passed, matching the CLI help text.
    if args.runtime_root is not None:
        runtime = Path(args.runtime_root).expanduser()
    else:
        env_runtime = config_source.get("CIAO_RUNTIME_ROOT", "").strip()
        runtime = Path(env_runtime).expanduser() if env_runtime else Path(".runtime")
    if not runtime.is_absolute():
        runtime = workspace / runtime
    runtime = runtime.resolve()
    effective_source = {
        **config_source,
        "CIAO_WORKSPACE": str(workspace),
        # Must match `runtime` exactly: vault_relocate reads/writes
        # workspaces.json under `runtime`, and if CiaoConfig loaded its own
        # `self.workspaces` from a different runtime root the two would
        # silently disagree about what is registered.
        "CIAO_RUNTIME_ROOT": str(runtime),
        "PWA_AUTH_TOKEN": os.environ.get("PWA_AUTH_TOKEN") or "vault-relocate",
    }
    config = CiaoConfig.from_env(effective_source)

    if args.name not in set(config.workspace_names()):
        print(f"No registered workspace named '{args.name}'.", file=sys.stderr)
        return 1

    if args.undo:
        result = vault_relocate.undo(config, args.name, runtime)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] in {"undone", "nothing_to_undo"} else 1

    plan_result = vault_relocate.plan(config, args.name)

    if args.apply:
        result = vault_relocate.apply(
            config,
            args.name,
            runtime,
            plan_result=plan_result,
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _print_vault_relocate_result(result, applied=True)
        return 0 if result["status"] == "relocated" else 1

    payload = plan_result.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_vault_relocate_result(payload, applied=False)
    return 1 if payload["refused"] else 0


def _workspace_reroot_command(args: argparse.Namespace) -> int:
    """Plan, rehearse, apply, or undo the per-workspace agent-root migration.

    Planning and rehearsing are read only. Applying refuses outright rather than
    stopping halfway, because a half-rooted install has no filter over a still
    prefixed index and would make every entity visible in every session. --undo
    stays CLI only: reverting the architecture is not a housekeeping button.

    --apply must be run with the app stopped. It moves the vault and writes the
    chat state file, so a live server would both read a path that no longer
    exists and overwrite the handover flags from its in-memory copy.

    And the app the operator runs afterwards must ALREADY contain this migration.
    An engine without ``CiaoConfig.agent_vault_root`` resolves the vault from a
    relative ``CIAO_VAULT_ROOT`` to ``<install>/memory-vault``, which this
    migration empties, so it boots with no vault and its skill sync then prunes
    the links that now dangle. The receipt gating makes the flip atomic for code
    that HAS it; it cannot help code that predates it. That is why the design
    runs this from ``sync_workspace_skills`` at upgrade rather than by hand.
    """
    from ciao import workspace_reroot

    workspace = Path(args.workspace or os.environ.get("CIAO_WORKSPACE") or ".").expanduser().resolve()
    runtime = workspace / ".runtime"
    # An explicit --workspace must win over the environment, the same rule the
    # os-audit command needed: a running Ciaobot chat exports CIAO_VAULT_ROOT and
    # CIAO_WORKSPACE for its OWN install, so resolving the vault from the ambient
    # environment while writing the named install's registry would migrate one
    # install's layout using another install's vault. Here it merely refused,
    # because the ambient relative default landed outside the named root — but a
    # colleague with an absolute CIAO_VAULT_ROOT would have got the dangerous
    # version of the same mistake.
    if args.workspace is not None and args.vault_root is None:
        vault = (workspace / "memory-vault").resolve()
    else:
        vault = _resolve_vault_root(args.vault_root)
    if not vault.is_absolute():
        vault = (workspace / vault).resolve()
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({
        **os.environ,
        "CIAO_WORKSPACE": str(workspace),
        "PWA_AUTH_TOKEN": os.environ.get("PWA_AUTH_TOKEN") or "workspace-reroot",
    })
    names = sorted(config.workspace_names())

    if args.undo:
        result = workspace_reroot.undo(workspace, runtime)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] in {"undone", "nothing_to_undo"} else 1

    if args.mark_migrated:
        # For a vault migrated by hand. The receipt is what `agent_root` reads,
        # so without it the install keeps resolving the shared layout while the
        # files sit in the new one — the one combination that
        # breaks every layout-dependent path. Verified, not asserted: the folders
        # have to actually be there, or this would tell the app a comforting lie.
        from ciao.workspace_reroot import mark_born_per_root, read_receipt

        if read_receipt(runtime) is not None:
            print("Already recorded as migrated; nothing to do.")
            return 0
        # Straight off the registry file, the way `agent_roots_for` does: this
        # command runs before any config exists that would answer per-root.
        try:
            entries = json.loads(
                (runtime / "workspaces.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            entries = []
        names = [
            str(e.get("name", "")).strip()
            for e in entries
            if isinstance(e, dict) and str(e.get("name", "")).strip()
        ]
        if not names:
            print(
                f"Refusing: no workspaces registered in {runtime / 'workspaces.json'}.",
                file=sys.stderr,
            )
            return 1
        missing = [
            n for n in names
            if not (workspace / n / vault.name).is_dir()
        ]
        if missing:
            # #812: this used to send the reader to docs/VAULT_MIGRATION_PROMPT.md
            # as the place to move the vaults by hand, and that document was
            # rewritten to stop teaching exactly that (#800/#815) — so the refusal
            # and the reader it named contradicted each other, and the document
            # was the right one. Name the command that does the move instead,
            # with the one caveat the refusal cannot check for the operator: an
            # `--apply` run from the wrong engine boots with no vault at all. The
            # document stays named, because it is still the reader for what
            # `--apply` refuses on and for an install already moved by hand.
            print(
                "Refusing: these workspaces have no "
                f"<workspace>/{vault.name} directory yet: {', '.join(missing)}.\n"
                "The move is `ciao workspace-reroot --apply`, run from the engine "
                "that will serve this install and with the app stopped.\n"
                "docs/VAULT_MIGRATION_PROMPT.md is the reader for what it refuses "
                "on.\n"
                "Only if these vaults are already where they belong because "
                "someone moved them by hand: finish the directories named above, "
                "then re-run this.",
                file=sys.stderr,
            )
            return 1
        written = mark_born_per_root(workspace, runtime, names, origin="hand")
        for path in written:
            print(f"Recorded the per-workspace layout: {path}")
        print("Run `ciao workspace-reroot --repair` next to rebuild the derived files.")
        return 0

    if args.repair:
        result = workspace_reroot.repair(workspace, runtime, names)
        print(json.dumps(result, indent=2))
        # Exit 1 when something still needs a human: a root with no vault or a
        # stale .mcp.json is reported rather than guessed, so a script gating on
        # this must not read it as clean.
        if result["status"] == "not_rerooted" or result["errors"]:
            return 1
        return 1 if result["reported"] else 0

    if args.apply:
        result = workspace_reroot.apply(
            workspace, vault, names, runtime, primary=config.primary_workspace()
        )
        if result["status"] == "migrated":
            # The leaf of the vault that was just moved, not a constant: an
            # install whose CIAO_VAULT_ROOT does not end in `memory-vault` would
            # otherwise rebuild nothing and report success.
            leaf = vault.name
            result["indexes"] = workspace_reroot.rebuild_indexes(
                workspace, names, vault_name=leaf
            )
            result["search"] = workspace_reroot.rebuild_search_index(
                workspace, names, runtime_root=runtime, vault_name=leaf
            )
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "migrated" else 1

    if args.rehearse:
        print(json.dumps(workspace_reroot.rehearse(workspace, vault, names, runtime), indent=2))
        return 0

    plan_result = workspace_reroot.plan(workspace, vault, names)
    primary = config.primary_workspace()
    triage = workspace_reroot.plan_skills_triage(workspace, primary)
    guides = workspace_reroot.guide_moves(workspace, primary)
    payload = plan_result.as_dict()
    # The dry run is what a person reads before approving, so it has to show
    # EVERY move the apply would make. Printing only the vault moves understated
    # it by four on the reference install, which is exactly the kind of gap that
    # makes a plan untrustworthy.
    payload["primary"] = primary
    payload["skills_triage"] = triage.as_dict()
    payload["guide_moves"] = [
        {"source": m.source, "destination": m.destination, "workspace": m.workspace}
        for m in guides
    ]
    payload["total_moves"] = len(plan_result.moves) + len(triage.moves) + len(guides)
    if triage.refusals:
        payload["refusals"] = [*payload["refusals"], *triage.refusals]
        payload["refused"] = True
    print(json.dumps(payload, indent=2))
    # Exit 1 on a refusal so a script can gate on it, 0 when the plan is clean.
    return 1 if payload["refused"] else 0


def _workspace_census_command(args: argparse.Namespace) -> int:
    """Survey a vault root and print the reported shapes.

    Read-only by design: this is the survey the per-workspace migration's
    fixtures must match, not a check. It always exits 0, because an unregistered
    directory is information the migration needs, not a failure for the caller.
    """
    from ciao.workspace_census import format_census, survey_vault

    vault_root = _resolve_vault_root(args.vault_root)
    census = survey_vault(vault_root)

    if args.json:
        print(json.dumps(census.as_dict(), indent=2))
    else:
        print(format_census(census))
    return 0


def _memory_audit_command(args: argparse.Namespace) -> int:
    """Audit only the bounded-memory regions.

    ``os-audit`` covers this too, but it also lints the whole vault, which is
    far too slow to run from a daily routine. This entry point reads one guide
    per registered workspace and reports over-cap per guide, not as one global
    number that hides which workspace is over budget.
    """
    from ciao.config import CiaoConfig
    from ciao.memory_tool import DEFAULT_MEMORY_CHAR_LIMIT, DEFAULT_USER_CHAR_LIMIT
    from ciao.os_audit import (
        _aggregate_memory_guides,
        _memory_guide_specs,
        _scan_memory_guide,
        _scan_proposals,
        memory_actionable_count,
    )

    workspace_raw = args.workspace or os.environ.get("CIAO_WORKSPACE") or Path(".")
    workspace = Path(workspace_raw).expanduser().resolve()
    vault_raw = args.vault_root or os.environ.get("CIAO_VAULT_ROOT") or "memory-vault"
    vault = Path(vault_raw).expanduser()
    if not vault.is_absolute():
        vault = workspace / vault
    vault = vault.resolve()

    config_source = dict(os.environ)
    config_source.update({
        "CIAO_WORKSPACE": str(workspace),
        "CIAO_VAULT_ROOT": str(vault),
        # Loading config for a read-only audit must not create a session
        # secret merely because the CLI was invoked outside the server env.
        "PWA_AUTH_TOKEN": config_source.get("PWA_AUTH_TOKEN", "") or "memory-audit",
    })
    config = CiaoConfig.from_env(config_source)

    specs = _memory_guide_specs(config, workspace)
    guides = [
        _scan_memory_guide(
            guide,
            workspace=name,
            workspace_dir=workspace,
            current=datetime.date.today(),
            region_limits={
                "memory": DEFAULT_MEMORY_CHAR_LIMIT,
                "profile": DEFAULT_USER_CHAR_LIMIT,
            },
        )
        for name, guide in specs
    ]
    proposals_count, proposal_files, proposal_errors = _scan_proposals(
        vault if vault.exists() else None, None, ""
    )
    report = _aggregate_memory_guides(
        guides,
        pending_memory_proposals=proposals_count,
        proposal_files=proposal_files,
        errors=proposal_errors,
    )
    # Optional vault pass: the regions are cheap and this command exists for
    # the daily routine, so the vault walk is opt-in rather than default —
    # the same reason it was split from os-audit in the first place.
    if args.with_vault:
        from ciao.memory_audit import find_stale_notes
        from ciao.vault_index import scan_vault

        try:
            # The stamp only feeds graph scoping; the staleness detector keys
            # on type and dates, not workspace. The render prefix is passed
            # explicitly so the stored-key rebuild below strips exactly what
            # scan_vault rendered, instead of hardcoding its default.
            render_prefix = "memory-vault"
            entries = scan_vault(
                vault, workspace="personal", path_prefix=Path(render_prefix)
            )
            report["stale_notes"] = find_stale_notes(
                entries,
                vault_root=vault,
                # The same prefix scan_vault rendered: left to its default,
                # find_stale_notes only works while its own internal default
                # happens to equal render_prefix, and a drifted prefix makes
                # every mtime stat miss silently.
                path_prefix=Path(render_prefix),
                today=datetime.date.today(),
            )
            # Decay-by-disuse: mark whether recall has returned each stale
            # note recently. Stale AND unretrieved is the strongest demotion
            # candidate; no evidence at all (no hits log yet) marks nothing.
            from ciao.fts_search import (
                NO_MATCH_KEY_PREFIX,
                read_search_hit_paths,
                vault_key_prefix,
            )

            # The writer (control_plane.vault_search) appends beside
            # state.json, which honours CIAO_RUNTIME_ROOT — a hardcoded
            # `.runtime` would silently read an empty location on installs
            # with a custom runtime root.
            hit_paths = read_search_hit_paths(Path(config.state_path).parent)
            # Compare in the index's own key space: hits are stored relative
            # to the install root with the vault directory's real name, while
            # scan_vault renders every path under the render prefix above.
            # Rebuilding the stored key per finding keeps a same-named note in
            # another workspace (or a vault not named `memory-vault`) from
            # producing a false match. The fail-closed prefix means this vault
            # has no identifiable rows: no evidence, so mark nothing.
            # workspace_root is a required config field, populated above from
            # the same value injected into config_source — no fallback.
            key_prefix = vault_key_prefix(vault, Path(config.workspace_root))
            # Keys, prefix and rendered paths are all `/`-spelled on every OS
            # (fts_search.KEY_SEPARATOR, Entry.path_key), so they compare as is.
            if hit_paths is not None and key_prefix != NO_MATCH_KEY_PREFIX:
                # A prefix that no hit carries means the log's keys were
                # written against a different base (the audit invoked with
                # another workspace root than the server's). That is missing
                # evidence, not proof of disuse — marking everything false
                # would nominate actively-used notes for demotion. An empty
                # prefix (vault == workspace root) takes the same rule: every
                # hit trivially carries it, so marking is skipped only when
                # the log has no usable hits at all.
                if any(hit.startswith(key_prefix) for hit in hit_paths):
                    for finding in report["stale_notes"]["stale_notes"]:
                        rel = str(finding["path"]).removeprefix(render_prefix + "/")
                        finding["retrieved_recently"] = (key_prefix + rel) in hit_paths
        except Exception as exc:  # noqa: BLE001 — advisory section
            report["stale_notes"] = {
                "stale_notes": [],
                "notes_checked": 0,
                "notes_exempt": 0,
            }
            report["errors"].append(
                {
                    "type": "note_staleness_scan_failed",
                    "path": str(vault),
                    "message": f"note staleness scan failed: {exc}",
                }
            )
    # Same definition os-audit exits on, so the two commands cannot disagree
    # about whether these regions are clean.
    findings = memory_actionable_count(report)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Bounded memory: {report['memory_entries']} memory / "
              f"{report['profile_entries']} profile entries across "
              f"{len(guides)} guide(s)")
        for finding in report["over_cap"]:
            where = f"[{finding['workspace']}] " if finding.get("workspace") else ""
            print(
                f"  {where}ciao:{finding['region']} over cap: "
                f"{finding['used']}/{finding['limit']} chars"
            )
        if report["over_cap"]:
            print(
                "  Fix: open a chat in that workspace and ask the agent to "
                'consolidate the region (e.g. "consolidate my ciao:memory '
                'region under its cap").'
            )
        print(f"Event-shaped entries: {len(report['event_shaped_entries'])}")
        for finding in report["event_shaped_entries"]:
            print(f"  [{finding['region']}] {finding['entry']}")
        print(
            f"Entries citing a missing path: {len(report['stale_path_entries'])} "
            f"({report['paths_checked']} checked, "
            f"{report['paths_unverifiable']} not verifiable here)"
        )
        for finding in report["stale_path_entries"]:
            print(f"  [{finding['region']}] {finding['path']} :: {finding['entry']}")
        print(
            "Superseded-state candidates (informational): "
            f"{len(report['superseded_state_candidates'])}"
        )
        for finding in report["superseded_state_candidates"]:
            print(f"  [{finding['region']}] {finding['subject']}")
        aging = report.get("aging_state_entries", [])
        print(f"Aging dated entries to re-verify (informational): {len(aging)}")
        for finding in aging:
            print(
                f"  [{finding['region']}] {finding['kind']} {finding['date']} "
                f"({finding['age_days']}d ≥ {finding['threshold_days']}d) :: "
                f"{finding['entry']}"
            )
        stale = report.get("stale_notes") or {}
        if stale:
            print(
                "Notes not verified within their type's horizon (informational): "
                f"{len(stale.get('stale_notes', []))} of "
                f"{stale.get('notes_checked', 0)} dated notes"
            )
            for finding in stale.get("stale_notes", []):
                print(
                    f"  [{finding['type']}] {finding['path']} :: "
                    f"{finding['age_days']}d since last check "
                    f"(horizon {finding['threshold_days']}d)"
                )

    if report["marker_errors"] or report["errors"]:
        return 2
    return 1 if findings else 0


def _resolve_workspace_and_vault(args: argparse.Namespace) -> tuple[Path, Path]:
    """Shared workspace/vault resolution for the memory-proposal commands."""
    workspace, vault, _registry_root, _name = _resolve_workspace_and_vaults(args)
    return workspace, vault


def _resolve_workspace_and_vaults(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path, str | None]:
    """``(workspace, notes vault, agent vault root, workspace name)`` for one CLI run.

    A scheduled run exports ``CIAO_ACTIVE_WORKSPACE`` (the logical workspace
    name) next to a ``CIAO_VAULT_ROOT`` that points at the install-wide
    shared vault on layouts that have not re-rooted yet. Appending to that
    raw value would file every workspace's proposals into one stray queue
    the review UI never reads, so an active workspace name is resolved
    through the workspace registry instead — the same authority the PWA's
    ``workspace_vault_root`` reads with. Explicit arguments still win for
    manual invocations.

    The third value is where ``entity-types.yaml`` and ``VOCABULARY.md`` live:
    the agent vault root, which is NOT the notes root before the re-rooting. A
    registry read from the notes root is the stock list on every such install,
    so a caller that measures notes against it (the category-cluster pass) sees
    every category the owner already added as unlisted. In the explicit-argument
    path there is no per-workspace split to resolve, so the vault the caller
    named is both.

    The fourth is the name the *registry* knows this vault's workspace by, and
    it is returned rather than left to each caller to re-derive, because every
    operation that consumes an entry identity resolves the vault through this
    same registry and mints that identity under the name it knows: a caller that
    guessed the name from the directory would mint identities nothing resolves.
    It is ``None`` when this invocation named a directory with no registry to ask
    (``--workspace``/``--vault-root` from a shell), and the one case that may use
    the directory's own name is an explicit ``--vault-root`` — where the operator
    pointed at a directory and no workspace name exists to resolve. The entry pass
    is skipped and reported in that case rather than planned under a guess.
    """
    active = os.environ.get("CIAO_ACTIVE_WORKSPACE", "").strip()
    if not getattr(args, "vault_root", None) and not getattr(args, "workspace", None):
        if active:
            try:
                from ciao.config import CiaoConfig

                # A read-only resolution must not mint a session secret just
                # because the CLI runs outside the server env (same rule as
                # the memory-audit command).
                env_source = dict(os.environ)
                env_source.setdefault("PWA_AUTH_TOKEN", "memory-proposals")
                config = CiaoConfig.from_env(env_source)
                if config.workspace(active) is not None:
                    return (
                        config.workspace_root,
                        Path(config.workspace_vault_root(active)),
                        Path(config.agent_vault_root(active)),
                        active,
                    )
            except Exception:  # noqa: BLE001 — fall through to the legacy path
                pass
    workspace_raw = args.workspace or os.environ.get("CIAO_WORKSPACE") or Path(".")
    workspace = Path(workspace_raw).expanduser().resolve()
    vault_raw = args.vault_root or os.environ.get("CIAO_VAULT_ROOT") or "memory-vault"
    vault = Path(vault_raw).expanduser()
    if not vault.is_absolute():
        vault = workspace / vault
    resolved = vault.resolve()
    # `--vault-root` is the operator naming a directory in person, with no
    # workspace to resolve, and it is the ONLY case where the directory's own
    # name stands in for the registry's. Anything else says it does not know.
    name = resolved.name if getattr(args, "vault_root", None) else None
    return workspace, resolved, resolved, name


def _memory_proposals_command(args: argparse.Namespace) -> int:
    """List pending memory proposals in a workspace's review queue.

    Read-only. Each pending proposal bullet is emitted with its kind, text,
    and optional source. The curation agent lists this queue, decides each
    item (promote via a region Edit, or dismiss via ``memory-proposal-dismiss``),
    and thereby keeps memory improving across sessions.
    """
    from ciao.memory_proposals import list_proposals

    workspace, vault = _resolve_workspace_and_vault(args)
    path = vault / "Workspace" / "Memory-Proposals.md"
    rows = list_proposals(path)
    if args.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        if not rows:
            print("No memory proposals are pending.")
        for row in rows:
            tag = f" [{row['source']}]" if row["source"] else ""
            print(f"- [{row['kind']}] {row['text']}{tag}")
    return 0


#: What a ``--request`` identifier may contain. Narrow on purpose: it is cited on
#: a ``Workspace/Learnings.md`` line as ``req:<id>`` and has to survive a round
#: trip through a one-line Markdown bullet and back, so anything that would end
#: the bullet's ``_(request: …)_`` tail — a newline, a ``)``, a backtick — is
#: refused at the door rather than mangled into a citation that names something
#: else.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _memory_proposal_add_command(args: argparse.Namespace) -> int:
    """File a fact into a workspace's memory-proposal review queue.

    The nightly curator discovers durable facts by reading archived chats that
    the memory pass never got to, so nothing routed them. Filing here puts the
    fact in the machine queue (``ciao memory-proposals``, the PWA review panel)
    where it can be promoted or dismissed like any queued item, instead of
    surviving only as prose in one nightly report. Re-filing an identical fact
    is a no-op; the queue dedupes by text.

    ``--kind learnings --request ID`` is how a ``/remember`` of a reusable
    lesson keeps its provenance. A ``/remember`` usually happens in a chat that
    is never archived, so there is no transcript turn to cite — and the two ways
    out are not equivalent: citing a fabricated turn is a reference to a
    conversation that never happened, whereas the request id is the real,
    re-readable origin of the sighting. The accepted line renders ``req:<id>``
    and the learning model deduplicates on it, so a retry of the same
    ``/remember`` cannot inflate the recurrence count.
    """
    from ciao.memory_proposals import (
        DESTINATIONS,
        MemoryProposal,
        append_proposals,
        was_dismissed,
        was_promoted,
    )

    workspace, vault = _resolve_workspace_and_vault(args)
    text_file = (getattr(args, "text_file", "") or "").strip()
    text = (args.text or "").strip()
    # The payload is a project doc path or a person's name — user-controlled
    # like the fact, and just as unsafe to hand to a shell. `--payload-file` is
    # the same door `--text-file` opens for the text.
    payload_file = (getattr(args, "payload_file", "") or "").strip()
    payload = (getattr(args, "payload", "") or "").strip()
    if payload and payload_file:
        print(
            "pass the payload either as --payload or via --payload-file, not both",
            file=sys.stderr,
        )
        return 2
    if payload_file:
        try:
            payload = Path(payload_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            print(f"could not read {payload_file}: {exc}", file=sys.stderr)
            return 2
    if text and text_file:
        print(
            "pass the fact either as text or via --text-file, not both",
            file=sys.stderr,
        )
        return 2
    if text_file:
        fact_path = Path(text_file)
        try:
            text = fact_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            print(f"could not read {text_file}: {exc}", file=sys.stderr)
            return 2
    if not text:
        print("a proposal text is required", file=sys.stderr)
        return 2
    # One bullet = one line: the queue file is line-oriented Markdown, so an
    # embedded newline would parse as a truncated bullet, strand the
    # continuation lines, and dodge text dedupe. Flatten every whitespace run
    # (newlines included) into a single space.
    text = " ".join(text.split())
    kind = args.kind.strip().lower()
    if kind not in DESTINATIONS:
        print(
            f"unknown kind {kind!r}; expected one of: {', '.join(DESTINATIONS)}",
            file=sys.stderr,
        )
        return 2
    if kind in {"people", "project"} and not payload:
        # The PWA accept handlers refuse these bullets ("the bullet names no
        # person" / "...no project doc"), so queueing one would create a row
        # nobody can ever promote.
        print(
            f"kind {kind!r} requires a --payload naming its target "
            "(person name for people, doc path for project)",
            file=sys.stderr,
        )
        return 2
    # `--request` is lesson provenance: it is what a `[learnings]` accept hands
    # to `append_learning` so the line can cite the user request the sighting
    # came from instead of an archive turn that does not exist. A request on any
    # other kind would be a field the accept cannot act on, so it is refused by
    # name rather than stored where nothing reads it.
    request = (getattr(args, "request", "") or "").strip()
    if request and kind != "learnings":
        print(
            f"--request is provenance for a [learnings] fact, not kind {kind!r}: "
            "the only accept that records one is the learnings append",
            file=sys.stderr,
        )
        return 2
    if request and not _REQUEST_ID_RE.match(request):
        print(
            f"--request {request!r} is not a plain request identifier (letters, "
            "digits, dot, dash and underscore only): it is cited on a learnings "
            "line, so it has to survive a round trip through the queue",
            file=sys.stderr,
        )
        return 2
    proposal = MemoryProposal(
        target=kind,
        text=text,
        source_section=args.source.strip() or "curation",
        payload=payload,
        request=request,
    )
    path = append_proposals(
        [proposal], vault, allow_dismissed=bool(getattr(args, "allow_dismissed", False))
    )
    # `append_proposals` returns None for two different situations and the
    # difference matters to whoever asked: a fact already queued is waiting for
    # them, while a fact they dismissed before will never come back on its own.
    # Reporting both as "already in the queue" told a user reconsidering an
    # earlier decision that their request had landed when no row exists.
    dismissed = path is None and was_dismissed(vault, text)
    promoted = path is None and not dismissed and was_promoted(vault, text)
    if args.json:
        json.dump(
            {
                "queued": path is not None,
                "duplicate": path is None,
                "dismissed_before": dismissed,
                "promoted_before": promoted,
                "path": str(path) if path else None,
                "text": text,
                "request": request,
                # argparse supplies a Path when --workspace is explicit, and
                # json.dump cannot serialize one; report the resolved root.
                "workspace": str(workspace),
            },
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
    elif promoted:
        print(
            f"Already promoted, so NOT queued: {text!r}. "
            "It should already be live in its destination."
        )
    elif dismissed:
        print(
            f"Previously dismissed, so NOT queued: {text!r}. "
            "Say so explicitly to file it again."
        )
    elif path is None:
        print(f"Already in the queue; nothing added for {text!r}.")
    else:
        print(f"Queued [{kind}] proposal in {path}.")
    return 0


def _memory_proposal_dismiss_command(args: argparse.Namespace) -> int:
    """Dismiss (delete) one memory proposal from the review queue.

    Removing a proposal is a review decision, never a memory write: promotion
    into a ``ciao:memory`` / ``ciao:profile`` region is an explicit ``Edit`` of
    the workspace guide first, then this dismiss removes the resolved item
    so the queue stops re-asking. TEXT matches one proposal by a unique
    substring.
    """
    from ciao import proposal_actions
    from ciao.memory_proposals import (
        find_proposal_matches,
        remove_proposal_by_substring,
    )

    workspace, vault = _resolve_workspace_and_vault(args)
    path = vault / "Workspace" / "Memory-Proposals.md"
    # Same resolution as `memory-proposal-add`. A proposal's text is arbitrary
    # user prose, so putting it in argv is the hazard `--text-file` exists to
    # avoid there; a dismissal names the same text and needed the same door.
    text_file = (getattr(args, "text_file", "") or "").strip()
    needle = (args.text or "").strip()
    if needle and text_file:
        print(
            "pass the substring either as text or via --text-file, not both",
            file=sys.stderr,
        )
        return 2
    if text_file:
        needle_path = Path(text_file)
        try:
            # Stripped, like the add path: a trailing newline is an artifact of
            # writing the file, never part of the proposal it has to match.
            needle = needle_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            # UnicodeDecodeError is a ValueError, not an OSError, so an
            # undecodable file used to escape as a traceback and abort an
            # unattended curation run.
            print(f"could not read {text_file}: {exc}", file=sys.stderr)
            return 2
    if not needle:
        print("a proposal text or unique substring is required", file=sys.stderr)
        return 2
    # Two needle forms, because rows reach the queue two ways:
    # `memory-proposal-add` flattens what it writes, while the Workspace care schedule prompt
    # appends `[review]` questions directly and may keep repeated whitespace.
    # A needle read from the very file a fact was filed from needs the flattened
    # form; a directly written row needs the raw one.
    #
    # Both are resolved to ROW IDENTITIES and unioned, rather than tried in
    # turn. Trying in turn is wrong twice over: the remover reports "no match"
    # and "ambiguous" identically, so a fallback after an ambiguous first pass
    # can uniquely hit a differently spaced row; and a raw form that uniquely
    # matches one row while the flattened form uniquely matches a DIFFERENT one
    # would silently delete whichever was tried first. Either way the outcome is
    # recorded against a proposal nobody named. One row or nothing.
    flattened = " ".join(needle.split())
    # Resolve the unique match and remove it under the queue lock, so the
    # proposal the receipt names is the one this call actually removes: a
    # concurrent archive or CLI writer landing between the match scan and the
    # indexed reread could otherwise point the saved line at a different
    # proposal. The receipt names the *resolved full parsed text* while the
    # removal re-uses the needle form that matched (see `removal_needle`
    # below), so both name the same row without the remover having to match on
    # a text that is a prefix of another queued bullet's.
    from ciao.memory_receipts import queue_lock, queue_resolution
    from ciao.proposal_kinds import parse_bullet

    with queue_lock(path):
        raw_matches = find_proposal_matches(path, needle)
        flat_matches = (
            find_proposal_matches(path, flattened) if flattened != needle else raw_matches
        )
        union = set(raw_matches) | set(flat_matches)
        if len(union) > 1:
            print(
                f"{len(union)} memory proposals match {needle!r}; "
                "pass a longer, unique substring.",
                file=sys.stderr,
            )
            return 1
        if not union:
            print(
                f"No unique memory proposal matched {needle!r} "
                "(the text may be ambiguous or absent).",
                file=sys.stderr,
            )
            return 1
        # The row is fixed while this lock is held, so its parsed identity is
        # the one the removal below acts on.
        try:
            target_line = path.read_text(encoding="utf-8").splitlines()[
                next(iter(union))
            ]
        except (OSError, IndexError, StopIteration):
            target_line = ""
        parsed = parse_bullet(target_line) if target_line else None
        if parsed is None:
            print(
                f"No unique memory proposal matched {needle!r} "
                "(the text may be ambiguous or absent).",
                file=sys.stderr,
            )
            return 1
        resolved_text = parsed.text
        resolved_kind = parsed.kind
        # The removal keeps using the caller's needle (in whichever form
        # matched), not the resolved text: `remove_proposal_by_substring`
        # matches substrings over whole lines, so handing it the row's full
        # text refuses to remove a row whose text is a prefix of another
        # queued bullet's — exactly the row the union above just resolved
        # uniquely. The receipt still names the resolved text.
        removal_needle = needle
        if not raw_matches and flat_matches:
            removal_needle = flattened
        # Bracket the removal with a receipt so a crash between the queue
        # rewrite and the decision record is recoverable, and so the History
        # surface can reverse a dismissal the curator made.
        from ciao.memory_receipts import QueueReceiptUnavailable

        try:
            with queue_resolution(
                path,
                removed_text=resolved_text,
                kind=resolved_kind,
                promoted=bool(args.promoted),
                actor="agent",
                source="cli",
                workspace=os.environ.get("CIAO_ACTIVE_WORKSPACE", "").strip(),
                vault_root=vault,
            ):
                removed = remove_proposal_by_substring(path, removal_needle)
        except QueueReceiptUnavailable as exc:
            print(
                f"the memory receipt journal is unavailable; "
                f"the proposal was not removed: {exc}",
                file=sys.stderr,
            )
            return 1
    if removed is None:
        print(
            f"No unique memory proposal matched {needle!r} "
            "(the text may be ambiguous or absent).",
            file=sys.stderr,
        )
        return 1
    kind, removed_text = removed
    # One handler for both ledgers, shared with the PWA's accept/dismiss
    # routes (`ciao/proposal_actions.py`). Preserve what was decided, not just
    # that something was: append-time dedupe consults the decision history, so
    # without it the next curator pass that re-reads the same transcript
    # re-files the fact the user just rejected.
    #
    # A curator-promoted fact is a PROMOTION, not a dismissal: recording it
    # under `dismissed_at` used to make `was_promoted()` false for anything
    # the agent filed itself, and hid it from the review page's History tab
    # as an accepted row. The curator files a fact first and dismisses second,
    # so that flow is a promotion; only a bare rejection is a dismissal.
    proposal_actions.record_decision(
        path,
        action="accept" if args.promoted else "dismiss",
        text=removed_text,
        kind=kind,
        via="agent",
    )
    if args.json:
        # `text` is the resolved bullet, not the caller's needle: a row can be
        # dismissed by a disambiguating fragment (`(from: Alpha)`), and handing
        # an automation that fragment back as the dismissed fact is wrong. It
        # matches `removed_text` on the receipt.
        json.dump(
            {"removed": True, "text": removed_text, "workspace": str(workspace)},
            sys.stdout,
        )
        sys.stdout.write("\n")
    else:
        verb = "Promoted" if args.promoted else "Dismissed"
        print(f"{verb} memory proposal matching {needle!r}.")
    return 0


def _vault_index_command(args: argparse.Namespace) -> int:
    from ciao import vault_index

    module_args: list[str] = []
    if args.workspace != "all":
        module_args.extend(["--workspace", args.workspace])
    if args.vault_root is not None:
        module_args.extend(["--vault-root", str(args.vault_root)])
    for entry_type in args.types:
        module_args.extend(["--type", entry_type])
    for tag in args.tags:
        module_args.extend(["--tag", tag])
    if args.name:
        module_args.extend(["--name", args.name])
    if args.related_to:
        module_args.extend(["--related-to", args.related_to])
    if args.neighbors:
        module_args.extend(["--neighbors", args.neighbors])
    if args.depth != 2:
        module_args.extend(["--depth", str(args.depth)])
    if args.format != "tsv":
        module_args.extend(["--format", args.format])
    if args.write:
        module_args.append("--write")
    return vault_index.main(module_args)


def _cleanup_sdk_blobs_command(args: argparse.Namespace) -> int:
    from ciao import cleanup_sdk_blobs

    module_args = ["--workspace", str(args.workspace)]
    if args.apply:
        module_args.append("--apply")
    return cleanup_sdk_blobs.main(module_args)


def _label_hygiene_command(args: argparse.Namespace) -> int:
    from ciao import label_hygiene

    module_args = ["--repo", args.repo, "--limit", str(args.limit)]
    if args.apply:
        module_args.append("--apply")
    if args.json:
        module_args.append("--json")
    return label_hygiene.main(module_args)


def _eval_command(args: argparse.Namespace) -> int:
    """Versioned behavioral evaluations for prompts, providers, and guides.

    Three verbs: ``contracts`` runs the deterministic, model-free guard checks
    (CI half); ``run`` performs the bounded model-backed probe; ``compare``
    diffs a baseline and a candidate report. Nothing here reads a live vault —
    the packaged synthetic scenario catalog is the only input.
    """
    from ciao import behavioral_eval

    action = getattr(args, "eval_action", "")
    if action == "contracts":
        contract_report = behavioral_eval.run_contract_checks()
        if args.json:
            print(json.dumps(contract_report.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(behavioral_eval.render_contract_text(contract_report))
        return 0 if contract_report.ok() else 1
    if action == "compare":
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
        comparison = behavioral_eval.compare_reports(baseline, candidate)
        if args.json:
            print(json.dumps(comparison, indent=2, ensure_ascii=False))
        else:
            print(behavioral_eval.render_comparison_text(comparison))
        return 0
    if action == "run":
        import asyncio

        catalog = behavioral_eval.load_scenarios()
        budget = behavioral_eval.EvalBudget.from_env()
        if args.max_calls is not None:
            budget.max_calls = max(1, args.max_calls)
        if args.max_cost_usd is not None:
            budget.max_cost_usd = max(0.0, args.max_cost_usd)
        if args.cost_per_call is not None:
            budget.cost_per_call_usd = max(0.0, args.cost_per_call)
        include = tuple(
            item.strip() for item in (args.include or "").split(",") if item.strip()
        )
        catalog_text = (
            args.catalog_file.read_text(encoding="utf-8")
            if getattr(args, "catalog_file", None) else None
        )
        core_prompt_text = (
            args.core_prompt_file.read_text(encoding="utf-8")
            if getattr(args, "core_prompt_file", None) else None
        )
        eval_report = asyncio.run(
            behavioral_eval.run_model_eval(
                catalog,
                provider=args.provider,
                model=args.model,
                label=args.label,
                budget=budget,
                repeats=args.repeats,
                concurrency=args.concurrency,
                include=include,
                timeout_s=args.timeout,
                catalog_text=catalog_text,
                core_prompt_text=core_prompt_text,
            )
        )
        out = Path(args.out) if args.out else behavioral_eval.default_report_path(args.label)
        behavioral_eval.write_report(eval_report, out)
        if args.json:
            print(json.dumps(eval_report.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(behavioral_eval.render_report_text(eval_report))
            print(f"Wrote {out}")
        # Nonzero on any zero-tolerance failure OR when no probe succeeded:
        # an evaluation that measured nothing (all calls failed/timed out/)
        # was unparseable) must not look like a successful run to automation.
        failed = bool(eval_report.zero_tolerance_failures)
        measured_nothing = eval_report.sample_size == 0
        if measured_nothing and not failed:
            print(
                "error: no probe succeeded; nothing was measured",
                file=sys.stderr,
            )
        return 1 if (failed or measured_nothing) else 0
    print("error: unknown eval action", file=sys.stderr)
    return 2


# A busy lease is not an error the caller should retry immediately, and it is
# not success either. 75 is EX_TEMPFAIL, which is what a scheduled run that
# found the vault already being curated actually means.
CURATION_BUSY_EXIT = 75


def _add_curation_arguments(parser: argparse.ArgumentParser) -> None:
    """Workspace/vault/guide resolution and budget flags, shared by the four."""
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
    )
    parser.add_argument(
        "--guide",
        type=Path,
        default=None,
        help=(
            "Workspace guide holding the bounded regions. Defaults to the "
            "workspace's AGENTS.md, or a legacy CLAUDE.md on an install that "
            "has not run the guide migration."
        ),
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="Most worklist items one run may take. Defaults to the built-in budget.",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="Longest one run may hold the lease. Doubles as the lease TTL.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")


def _curation_context(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path, Any, Path, str | None]:
    from ciao.curation_run import RunBudget

    workspace, vault, registry_root, name = _resolve_workspace_and_vaults(args)
    guide = Path(args.guide).expanduser().resolve() if args.guide else guide_path(workspace)
    defaults = RunBudget()
    budget = RunBudget(
        max_items=args.max_items if args.max_items is not None else defaults.max_items,
        max_seconds=args.max_seconds if args.max_seconds is not None else defaults.max_seconds,
    )
    return workspace, vault, guide, budget, registry_root, name


def _curation_config(workspace: Path, vault: Path) -> Any:
    """A one-workspace registry that resolves exactly the vault being planned.

    The skill-proposal queue and the upstream draft sidecar are addressed through
    ``config.workspace_vault_root(workspace)``, and the skills-cleanup fold needs
    both — so the registry has to name *this* vault, not the install-wide one the
    server would have loaded. The vault directory's own name is the workspace
    name, which is the same identity ``_learning_items`` already parses with, so
    the cleanup pass and the promote/prune pass cannot disagree about which
    learning an id belongs to.
    """
    from ciao.config import CiaoConfig, WorkspaceConfig

    name = vault.name
    return CiaoConfig(
        pwa_auth_token="curation",
        workspace_root=workspace,
        state_path=workspace / ".runtime" / "state.json",
        media_root=workspace / ".runtime" / "media",
        vault_root=vault,
        workspaces={name: WorkspaceConfig(name=name, vault_root=str(vault))},
    )


def _curation_plan(args: argparse.Namespace) -> tuple[dict[str, Any], Any]:
    from ciao.curation_run import build_worklist, load_state, plan_run
    from ciao.entity_types import load_entity_types
    from ciao.vault_index import VAULT_RENDER_PREFIX

    workspace, vault, guide, budget, registry_root, name = _curation_context(args)
    state = load_state(vault)
    worklist = build_worklist(
        vault_root=vault,
        guide_path=guide,
        workspace_dir=workspace,
        done_keys=frozenset(state.done_keys),
        category_registry=load_entity_types(registry_root),
        # The one pass that folds the proposal queue needs the registry; every
        # other pass reads files, and saying so is cheaper than making the whole
        # planner conditional.
        config=_curation_config(workspace, vault),
        # Named where `scan_vault` renders it rather than restated here: a
        # drifted prefix makes every mtime `stat` miss silently, which reads as
        # "no note is stale" rather than as an error.
        path_prefix=VAULT_RENDER_PREFIX,
        # The registered name the same registry read resolved the vault under,
        # or None when this invocation named a directory and there is no
        # registry to ask: the entry pass mints identities that digest it, so a
        # guess would plan work every operation refuses. None skips the pass and
        # says so in the worklist notes.
        workspace=name,
    )
    plan = plan_run(worklist, budget)
    payload: dict[str, Any] = {
        "workspace": str(workspace),
        "vault_root": str(vault),
        **worklist.as_dict(),
        **plan.as_dict(),
        "last_run": state.last_run,
    }
    return payload, worklist


def _print_curation(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return
    if payload.get("empty"):
        print("Nothing to curate: every mechanical check is clear.")
        return
    print(f"Weekly pass due: {'yes' if payload.get('weekly_due') else 'no'}")
    for item in payload.get("planned", []):
        print(f"- [{item['pass']}] {item['label']} ({item['count']}) — {item['reason']}")
        for key in item["keys"]:
            print(f"    {key}")
    for item in payload.get("deferred", []):
        print(f"- deferred [{item['pass']}] {item['label']} ({item['count']}) — {item['reason']}")


def _curation_plan_command(args: argparse.Namespace) -> int:
    """Print the deterministic worklist without starting a run.

    Read-only on purpose: the plan is also how a human (or a test) checks what
    the nightly run would do without taking the lease away from it.
    """
    payload, _ = _curation_plan(args)
    _print_curation(payload, as_json=args.json)
    return 0


def _curation_begin_command(args: argparse.Namespace) -> int:
    """Take the lease and print this run's plan.

    An empty worklist releases the lease again before returning: a quiet night
    must not hold the lease until the TTL expires.
    """
    from ciao.curation_run import CurationBusy, begin_run, end_run

    _workspace, vault, _guide, budget, _registry_root, _name = _curation_context(args)
    try:
        lease = begin_run(vault, holder=args.holder, ttl_s=budget.max_seconds)
    except CurationBusy as exc:
        print(f"curation is already running: {exc}", file=sys.stderr)
        return CURATION_BUSY_EXIT

    payload, _worklist = _curation_plan(args)
    payload["lease"] = lease.as_dict()
    if payload.get("empty"):
        end_run(vault, holder=lease.holder, status="ok", planned=0, completed=0, deferred=0)
        payload["lease"] = {}
    _print_curation(payload, as_json=args.json)
    return 0


def _curation_holder(args: argparse.Namespace) -> str:
    """The lease holder a follow-up command must carry, or "" with an error.

    `record_done` and `end_run` skip their ownership check when the holder is
    empty, so a follow-up command that omitted it was not leased at all: an
    over-budget run kept writing the vault after its lease expired, and could
    go on to clear the lease a *newer* run had since taken. Required rather
    than defaulted, because only `curation-begin` knows the value — guessing
    `host:pid` here would match nothing and reject every honest run.
    """
    holder = (getattr(args, "holder", "") or "").strip()
    if not holder:
        print(
            "pass --holder with the value `curation-begin` reported as lease.holder",
            file=sys.stderr,
        )
    return holder


def _curation_progress_command(args: argparse.Namespace) -> int:
    """Record finished worklist keys and renew the lease."""
    from ciao.curation_run import CurationBusy, record_done, renew_run

    _workspace, vault, _guide, budget, _registry_root, _name = _curation_context(args)
    holder = _curation_holder(args)
    if not holder:
        return 2
    keys = [key.strip() for key in (args.key or []) if key.strip()]
    if not keys:
        print("pass at least one --key from `curation-begin`", file=sys.stderr)
        return 2
    try:
        added = record_done(vault, keys, holder=holder)
    except CurationBusy as exc:
        print(f"curation lease lost: {exc}", file=sys.stderr)
        return CURATION_BUSY_EXIT
    renew_run(vault, holder=holder, ttl_s=budget.max_seconds)
    payload = {"recorded": len(keys), "newly_done": added}
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"recorded {len(keys)} item(s), {added} newly done")
    return 0


def _curation_end_command(args: argparse.Namespace) -> int:
    """Release the lease, record the counts, and stamp the weekly marker.

    The marker is stamped by code rather than by the agent because the rule —
    both weekly checks reliably done — is mechanical, and an agent that stamped
    it after a failed audit made the weekly pass skip a week with nothing to
    show for it. `end_run` does the stamping, so it happens only for the run
    that still owns the lease.
    """
    from ciao.curation_run import CurationBusy, end_run

    _workspace, vault, _guide, _budget, _registry_root, _name = _curation_context(args)
    holder = _curation_holder(args)
    if not holder:
        return 2
    payload, worklist = _curation_plan(args)
    live_keys = frozenset(key for item in worklist.items for key in item.keys)
    deferred = int(payload.get("deferred_count", 0))
    try:
        # `end_run` stamps the marker itself, inside the lease check. Stamping
        # it out here let a run whose lease had expired — one this call then
        # rejected with 75 — still suppress the weekly passes for seven days.
        # The worklist above already excludes the two hygiene keys, since they
        # are recorded done, so the prune inside `end_run` still forgets them
        # and next week's pass is planned again.
        summary = end_run(
            vault,
            holder=holder,
            status=args.status,
            planned=args.planned,
            completed=args.completed,
            deferred=deferred,
            reasons=args.reason or [],
            live_keys=live_keys,
            advance_marker=args.status == "ok",
        )
    except CurationBusy as exc:
        print(f"curation lease lost: {exc}", file=sys.stderr)
        return CURATION_BUSY_EXIT
    advanced = bool(summary.get("full_pass_advanced"))
    if args.json:
        json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(
            f"run {summary['status']}: planned {summary['planned']}, "
            f"completed {summary['completed']}, deferred {summary['deferred']}; "
            f"weekly marker {'advanced' if advanced else 'left due'}"
        )
    return 0


def _skill_proposal_remove_command(args: argparse.Namespace) -> int:
    """Settle a resolved skill proposal in a workspace's review queue.

    The curation schedule reviews ``Workspace/Skill-Proposals/``; once a
    proposal's decision is made (implemented, or decided against) it is settled
    here so the queue stops re-asking. NAME matches the record's skill or a
    unique substring of it.

    Settled, not deleted: this used to unlink the file, which left no record that
    anyone had decided anything, so the next pass that saw the same evidence
    filed the same proposal again. ``ciao.skill_proposals`` records the
    decision in the workspace's sidecar and flips the record's lifecycle; the
    proposal stays readable and keeps accumulating evidence, and stays settled.

    ``--applied`` records the other outcome — the change landed and was checked.
    It is not a decoration: ``applied`` is recorded as a promotion and everything
    else as a dismissal, so the History tab can tell "we improved this" from
    "we decided against this". ``--not-applicable`` records the third, which is
    why it exists: a finding the implementation chat re-read the skill and found
    no longer holds is not the same as one a person rejected — and the prompt the
    accept route seeds tells the chat to say so, so the command has to exist.
    ``--interrupted`` records that the work stopped part-way, which is none of
    those: the record stays QUEUED and its chat stays bound to it, because an
    unfinished edit is not an answer and must not archive an open question as
    though a person had rejected it.

    ``--verification`` and ``--learning-id``/``--finding`` are how a record that
    links learnings is settled honestly. ``--applied`` on such a record is
    refused without a verification, because the two things this command can see
    for itself — the chat finished, the row left the queue — are not the lesson
    being in the skill. A selector settles one finding and leaves its siblings
    queued, which is the point: a record is one row per skill, so a person who
    dealt with one of its findings has not dealt with the rest.
    """
    # Deleting a proposal file is a review decision, not a session write;
    # loading config outside the server env must not mint a session secret.
    config = _proposal_config(args, "skill-proposal-remove")

    # Which workspace the proposal lives in: the active one, falling back to the
    # primary, so the decision lands in the same queue the proposal was filed
    # into.
    name = _active_workspace_name(config)

    needle = args.name.strip()
    if not needle:
        print("a skill proposal name or substring is required", file=sys.stderr)
        return 2

    from ciao import skill_proposals

    if not skill_proposals.queue_dir(config, name).is_dir():
        print("No skill proposals are queued.", file=sys.stderr)
        return 1

    queued = skill_proposals.read_queue(config, name)
    matches = [p for p in queued if needle.casefold() in p.skill.casefold()]
    if not matches:
        print(f"No skill proposal matched {needle!r}.", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(
            f"The name matched more than one skill proposal; use a longer substring: "
            + ", ".join(p.skill for p in matches),
            file=sys.stderr,
        )
        return 1

    target = matches[0]
    # A chat records the outcome of the work it did, so a proposal that was
    # never accepted has nothing to interrupt. Accepting is the route's job.
    if args.interrupted and not target.chat_id:
        print(
            f"{target.skill} has no implementing chat, so there is no work to "
            "record as interrupted; accept it first.",
            file=sys.stderr,
        )
        return 1
    if args.applied and args.interrupted:
        print("--applied and --interrupted are opposite outcomes; pass one.", file=sys.stderr)
        return 2
    if args.not_applicable and (args.applied or args.interrupted):
        print(
            "--not-applicable is a third outcome, not a modifier on the other "
            "two; pass it on its own.",
            file=sys.stderr,
        )
        return 2
    lifecycle = (
        skill_proposals.INTERRUPTED
        if args.interrupted
        else skill_proposals.APPLIED
        if args.applied
        else skill_proposals.NOT_APPLICABLE
        if args.not_applicable
        else skill_proposals.DISMISSED
    )
    if args.finding and not args.learning_id:
        print(
            "--finding names one finding of a learning, so it needs --learning-id "
            "with it.",
            file=sys.stderr,
        )
        return 2
    selectors = (
        [skill_proposals.OriginRef(args.learning_id.strip(), args.finding.strip())]
        if args.learning_id.strip()
        else None
    )
    try:
        settled = skill_proposals.mark_outcome(
            config,
            target.id,
            lifecycle,
            args.reason.strip(),
            via="cli",
            selectors=selectors,
            verification=args.verification.strip(),
        )
    except (OSError, ValueError) as exc:
        print(f"could not settle {target.skill}: {exc}", file=sys.stderr)
        return 1
    if settled is None:
        print(f"{target.skill} is no longer queued.", file=sys.stderr)
        return 1

    if args.json:
        payload: dict[str, Any] = {
            "settled": True,
            "name": target.skill,
            "workspace": name,
            # Which outcome this was, because the command now records three
            # and only one of them closes the question: `--interrupted`
            # leaves the proposal queued.
            "lifecycle": settled.lifecycle,
        }
        if settled.origins:
            # What each linked finding now says, because a per-finding
            # settlement leaves the record queued and the caller needs to see
            # which part is still outstanding. Omitted rather than emitted
            # empty: a record filed before origins existed must keep producing
            # the payload it always produced.
            payload["origins"] = [
                {
                    "learning_id": origin.learning_id,
                    "finding": origin.finding,
                    "state": origin.state,
                }
                for origin in settled.origins
            ]
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif settled.lifecycle == skill_proposals.INTERRUPTED:
        print(
            f"Recorded {settled.skill} in {name} as interrupted; it stays queued "
            "so the work can be picked up again."
        )
    elif selectors and settled.lifecycle not in skill_proposals.SETTLED_LIFECYCLES:
        outstanding = [
            origin.finding
            for origin in settled.origins
            if origin.state not in skill_proposals.CLEARED_ORIGINS
        ]
        print(
            f"Settled one finding of {settled.skill} in {name}; it stays queued "
            f"for {len(outstanding)} more."
        )
    else:
        print(f"Settled skill proposal {settled.skill} in {name}.")
    return 0


def _read_skill_proposal_input(path: str) -> tuple[dict[str, Any] | None, str]:
    """The finding in *path* as ``(payload, "")``, or ``(None, why)`` if unusable.

    Structured in, so the reader is a real parse rather than a delimiter: a
    finding is several fields and a list of evidence rows, and a format whose
    boundaries are a model has to reproduce exactly is a format that will
    eventually be reproduced slightly wrong. Every complaint names the field it
    is about, because the caller is a model that can only fix what it is told.

    ``origins`` is optional and validated strictly when present. A finding may
    have come from one entry in ``Workspace/Learnings.md``, and the link to it is
    the only thing that will later let a settlement say *which* lesson landed —
    so a malformed one is refused rather than dropped, since a half-read link is
    a learning that looks settled and is not. Its absence is not an error: most
    findings are a correction the user made, and only a routed learning gets a
    link. An entry that carries a ``state`` or a ``verification`` is refused too,
    because a filing is a question and only a settlement answers one.

    **``sources`` may be absent when ``origins`` is present**, and that is the
    lesson-routing path rather than a loosened check. A lesson can apply to a
    skill the conversation never loaded, and the honest record of that finding is
    the link to the learning — a ``sources`` entry would have to name a ``turn``
    the transcript never contained, which is a fabricated source. So the
    requirement is *one or the other*: a payload carrying neither is a finding
    nobody can check, and is refused by name.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, f"could not read {path}: {exc}"
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return None, f"{path} is not valid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, f"{path} must hold one JSON object, not a {type(payload).__name__}"
    for field in ("title", "problem", "change"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            return None, f'{path} needs a non-empty "{field}"'
    problem = _origin_input_problem(payload.get("origins"), path)
    if problem:
        return None, problem
    sources = payload.get("sources")
    if sources is None and payload.get("origins"):
        # A routed lesson is its evidence. The absence of `sources` here says
        # "this finding came from Workspace/Learnings.md, not from a turn of this
        # conversation", which is exactly what it is.
        return payload, ""
    if not isinstance(sources, list) or not sources:
        return None, (
            f'{path} needs a non-empty "sources" list — every entry carrying the '
            "chat_id, archive, turn and a short verbatim excerpt — or a non-empty "
            '"origins" list linking the learning it came from. A finding with '
            "neither is not reviewable"
        )
    for index, item in enumerate(sources):
        if not isinstance(item, dict):
            return None, f"{path} sources[{index}] must be an object"
        if not isinstance(item.get("excerpt"), str) or not item["excerpt"].strip():
            return None, (
                f'{path} sources[{index}] needs a non-empty "excerpt"'
            )
    return payload, ""


def _origin_input_problem(origins: Any, path: str) -> str:
    """What is wrong with the ``origins`` list in a filed finding, or ``""``.

    One complaint at a time and it names the entry, for the same reason the rest
    of this reader does: the caller is a model that can only fix what it is told.
    An unknown key fails the entry rather than being ignored, because a key this
    code does not understand is a field whose loss nobody would notice until a
    learning had been declared settled on a partial read — which is also why
    ``state`` and ``verification`` are unknown here (:data:`_ORIGIN_INPUT_FIELDS`).
    """
    if origins is None:
        return ""
    if not isinstance(origins, list):
        return f'{path} "origins" must be a list of objects'
    for index, item in enumerate(origins):
        where = f'{path} origins[{index}]'
        if not isinstance(item, dict):
            return f"{where} must be an object"
        unknown = sorted(set(item) - _ORIGIN_INPUT_FIELDS)
        if unknown:
            return f"{where} has unknown field(s) {', '.join(unknown)}"
        for field in ("workspace", "source_revision", "summary"):
            if field in item and not isinstance(item[field], str):
                return f'{where} "{field}" must be a string'
        for field in ("learning_id", "finding"):
            value = item.get(field)
            if value is None:
                return f'{where} needs a non-empty "{field}"'
            if not isinstance(value, str):
                return f'{where} "{field}" must be a string'
            if not value.strip():
                return f'{where} needs a non-empty "{field}"'
    return ""


#: The keys one ``origins`` entry in a filed finding may carry. Deliberately
#: fewer than :data:`ciao.skill_proposals.SkillOrigin` holds: a ``state`` and a
#: ``verification`` are a decision, and a finding filed by a model or by hand is a
#: question. Accepting them here would let a payload declare its own finding
#: ``applied`` — skipping the verification that an applied needs and the explicit
#: rejection a dismissed needs — and
#: :func:`ciao.skill_proposals.learning_cleanup_eligibility` would then retire a
#: lesson nobody applied or rejected. So they are unknown fields here, refused by
#: name, and only ``ciao skill-proposal-remove`` writes a state.
_ORIGIN_INPUT_FIELDS = frozenset({
    "schema",
    "workspace",
    "learning_id",
    "source_revision",
    "finding",
    "summary",
})


def _filing_origins(
    payload: dict[str, Any], workspace: str
) -> tuple[tuple["skill_proposals.SkillOrigin", ...] | None, str]:
    """The finding's learning links as records, or ``(None, why)`` if unusable.

    The workspace is decided here, not taken from the payload. The finding names
    a learning by its id, and an id is only meaningful inside the workspace that
    minted it, so a payload naming a different one is a link this queue cannot
    honour — refused by name rather than rewritten, because silently filing it
    under this workspace would make an id from somewhere else look like a link
    this queue had verified.

    Every link is filed ``pending`` with no verification, and the reader above is
    what enforces it: a filing is a question, so there is nothing here to set a
    state from even if a caller reached past the reader and tried.
    """
    from ciao import skill_proposals

    raw = payload.get("origins") or []
    origins: list[skill_proposals.SkillOrigin] = []
    for index, item in enumerate(raw):
        stated = str(item.get("workspace") or "").strip()
        if stated and stated != workspace:
            return None, (
                f'origins[{index}] names the {stated} workspace, but this finding '
                f"is filed in {workspace}; a learning id only means something in "
                "the workspace that minted it"
            )
        origins.append(
            skill_proposals.SkillOrigin(
                workspace=workspace,
                learning_id=str(item.get("learning_id") or "").strip(),
                source_revision=str(item.get("source_revision") or "").strip(),
                finding=" ".join(str(item.get("finding") or "").split()),
                summary=" ".join(str(item.get("summary") or "").split()),
            )
        )
    return tuple(origins), ""


def _skill_proposal_add_command(args: argparse.Namespace) -> int:
    """File one supported skill-improvement proposal in a workspace's queue.

    The memory pass finds a finding in a conversation it has just read, and a
    person can hand-author one; both go through here, so a proposal only ever
    names a target ``skills_inventory.resolve_owned_skill`` accepts and only
    ever lands through ``skill_proposals.upsert_proposal``. The finding is read
    from a file because every field in it is conversation-derived prose, and
    ``$()``, backticks or quotes in a shell argument would run or mangle.

    Ownership is resolved, never asserted. An installed stock copy, a provider
    mirror, the install-wide shared source, another workspace's catalog and an
    unknown name are all refused here by name, and the path and revision the
    record carries come from the resolved source rather than from the caller —
    so a hand-authored name cannot aim the writer at a file it does not
    own, and a reviewer can tell which bytes the proposal was written against.

    Settling stays out of here: this proposes, and ``skill-proposal-remove``
    decides.
    """
    from ciao.skill_proposals import (
        PENDING,
        SkillEvidence,
        SkillProposal,
        proposal_id,
        proposal_path,
        upsert_proposal,
    )
    from ciao.skills_inventory import resolve_owned_skill

    payload, problem = _read_skill_proposal_input(args.input_file)
    if payload is None:
        print(problem, file=sys.stderr)
        return 2

    # Filing a proposal is a review-queue write, not a session write; loading
    # config outside the server env must not mint a session secret.
    config = _proposal_config(args, "skill-proposal-add")

    # Which workspace the proposal belongs to: the active one, falling back to
    # the primary, matching `skill-proposal-remove`'s routing so both ends of a
    # proposal's life land in the same queue.
    name = _active_workspace_name(config)

    skill = args.skill.strip()
    try:
        owned = resolve_owned_skill(config, name, skill)
    except ValueError as exc:
        print(f"cannot file a proposal for {skill!r}: {exc}", file=sys.stderr)
        return 1

    origins, problem = _filing_origins(payload, name)
    if origins is None:
        print(problem, file=sys.stderr)
        return 2

    sources = tuple(
        SkillEvidence(
            chat_id=str(item.get("chat_id") or "").strip(),
            archive=str(item.get("archive") or "").strip(),
            turn=str(item.get("turn") or "").strip(),
            excerpt=str(item.get("excerpt") or "").strip(),
        )
        for item in (payload.get("sources") or [])
    )
    stored = upsert_proposal(
        config,
        SkillProposal(
            id=proposal_id(name, owned.name),
            workspace=name,
            skill=owned.name,
            # The source this was resolved against and the revision of exactly
            # those bytes, so a reviewer can tell the skill has moved on since.
            canonical_path=str(owned.path),
            reviewed_revision=owned.revision,
            title=payload["title"].strip(),
            problem=payload["problem"].strip(),
            change=payload["change"].strip(),
            rationale=str(payload.get("rationale") or "").strip(),
            sources=sources,
            lifecycle=PENDING,
            # No implementing chat: this is a finding nobody has accepted yet.
            # The conversation that justified it is in the evidence.
            chat_id="",
            updated_at="",
            origins=origins,
        ),
    )
    path = proposal_path(config, name, stored.skill)
    if args.json:
        json.dump(
            {
                "filed": True,
                "id": stored.id,
                "skill": stored.skill,
                "workspace": name,
                "lifecycle": stored.lifecycle,
                "path": str(path),
                "evidence": len(stored.sources),
                "origins": len(stored.origins),
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        print(f"Filed skill proposal for {stored.skill} in {name}: {path}")
        if stored.origins:
            # The links are what a later settlement folds, so the person filing
            # gets to see that they landed rather than discovering a missing
            # one when a learning never goes quiet.
            print(
                f"Linked {len(stored.origins)} learning finding(s); they stay "
                "Active until each one is applied or rejected."
            )
        if stored.lifecycle != PENDING:
            # A decision already stands for this skill. The merge keeps it
            # settled and only adds the evidence, so the pass is told the
            # finding did not reopen the queue.
            print(
                f"Note: {stored.skill} is already {stored.lifecycle}; the "
                "evidence was added to the settled record."
            )
    return 0


def _skills_list_command(args: argparse.Namespace) -> int:
    from ciao.skills_inventory import build_skill_inventory

    workspace_root = Path(args.workspace).expanduser().resolve()
    inventory = build_skill_inventory(workspace_root)
    json.dump(inventory, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _proposal_config(args: argparse.Namespace, purpose: str) -> CiaoConfig:
    """The config a queue-writing review command runs against.

    One function for every command that writes a workspace's review queue,
    because the workspace/vault/env resolution and the ``PWA_AUTH_TOKEN``
    stand-in are all the same and two copies of that resolution is how a CLI
    write ends up in a different vault than the surface that reads it. The token
    stand-in is what keeps a review-queue write from minting a session secret
    outside the server env.
    """
    from ciao.config import CiaoConfig as _CiaoConfig

    workspace_raw = args.workspace or os.environ.get("CIAO_WORKSPACE") or Path(".")
    workspace = Path(workspace_raw).expanduser().resolve()
    vault_raw = args.vault_root or os.environ.get("CIAO_VAULT_ROOT") or "memory-vault"
    vault = Path(vault_raw).expanduser()
    if not vault.is_absolute():
        vault = workspace / vault
    vault = vault.resolve()

    config_source = dict(os.environ)
    config_source.update({
        "CIAO_WORKSPACE": str(workspace),
        "CIAO_VAULT_ROOT": str(vault),
        "PWA_AUTH_TOKEN": config_source.get("PWA_AUTH_TOKEN", "") or purpose,
    })
    return _CiaoConfig.from_env(config_source)


def _active_workspace_name(config: CiaoConfig) -> str:
    """Which workspace a review command is about, matching the proposal CLI.

    The active one, falling back to the primary, so both ends of a record's life
    land in the same vault.
    """
    name = os.environ.get("CIAO_ACTIVE_WORKSPACE", "").strip()
    if config.workspace(name) is None:
        name = config.primary_workspace()
    return name


def _skill_draft_add_command(args: argparse.Namespace) -> int:
    """File one ``[review]`` draft for a lesson this workspace cannot edit in place.

    The two destinations a routing pass has when the skill a lesson applies to is
    not an owned source: an upstream issue for somebody else's packaged skill,
    and a new skill for a workflow no skill covers. Both are drafts for a person,
    because filing an issue is a public action and creating a skill writes a file
    every session of the workspace will load.

    Nothing here reaches GitHub and nothing here creates a skill. ``--input-file``
    holds a JSON object — ``target`` (``upstream_issue`` or ``new_skill``),
    ``skill``, ``title``, ``change``, and for an issue a ``body`` that is the
    sanitized lesson plus the optional ``repository`` and ``version``. Private
    evidence goes in ``private_evidence`` and stays in this workspace; ``body`` is
    refused if it carries a transcript excerpt, a path, a name or a credential,
    because a public issue is public.
    """
    from ciao.upstream_drafts import DraftError, file_draft

    payload, problem = _read_skill_draft_input(args.input_file)
    if payload is None:
        print(problem, file=sys.stderr)
        return 2
    config = _proposal_config(args, "skill-draft-add")
    name = _active_workspace_name(config)
    origins, problem = _draft_origins(payload, name)
    if origins is None:
        print(problem, file=sys.stderr)
        return 2
    try:
        stored = file_draft(
            config,
            name,
            target=str(payload["target"]),
            skill=str(payload["skill"]),
            title=str(payload["title"]),
            change=str(payload["change"]),
            body=str(payload.get("body") or ""),
            repository=str(payload.get("repository") or ""),
            version=str(payload.get("version") or ""),
            private_evidence=str(payload.get("private_evidence") or ""),
            origins=origins,
        )
    except DraftError as exc:
        print(f"cannot file the draft: {exc}", file=sys.stderr)
        return 2
    if args.json:
        json.dump(
            {
                "filed": True,
                "id": stored.id,
                "target": stored.target,
                "skill": stored.skill,
                "workspace": name,
                "lifecycle": stored.lifecycle,
                "origins": len(stored.origins),
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        print(f"Filed {stored.target} draft {stored.id} for {stored.skill} in {name}.")
        _print_route_note(config, name, stored)
        if stored.lifecycle != "pending":
            print(
                f"Note: this change is already {stored.lifecycle}; the evidence "
                "was added to the settled record and nothing was re-queued."
            )
    return 0


def _print_route_note(config: CiaoConfig, workspace: str, draft) -> None:
    """Say so when the filed target disagrees with where the name actually lives.

    Advisory rather than a refusal, because both mismatches have a legitimate
    reading: a workspace that forked a packaged skill owns the fork and may
    still want the change reported upstream, and a name a pass believes is new
    may be a typo of one that exists. What is not legitimate is filing blind, so
    the note names the real answer and the command that acts on it.
    """
    from ciao.upstream_drafts import NEW_SKILL, route_for_skill

    try:
        actual = route_for_skill(config, workspace, draft.skill)
    except Exception:  # noqa: BLE001 — a note is never a reason to lose the filing
        return
    if actual == "owned" and draft.target == NEW_SKILL:
        print(
            f"Note: skills/{draft.skill}/SKILL.md already exists here, so this is "
            "not a new skill. `ciao skill-draft-approve` will refuse the "
            "creation; an improvement to the existing source is a skill "
            "proposal (ciao skill-proposal-add)."
        )
    elif actual == "owned":
        print(
            f"Note: {draft.skill} is a source this workspace owns under skills/, "
            "so an improvement to it can be made here directly with "
            "`ciao skill-proposal-add` rather than filed upstream. Keep this "
            "draft if the change belongs to whoever maintains the packaged copy."
        )
    elif actual == "new" and draft.target != NEW_SKILL:
        print(
            f"Note: there is no skill called {draft.skill} in this workspace or "
            "in its installed catalog, so there is no packaged copy to report "
            "upstream either. If this is a reusable workflow with no skill yet, "
            'file it with "target": "new_skill".'
        )


def _read_skill_draft_input(path: str) -> tuple[dict[str, Any] | None, str]:
    """The draft in *path* as ``(payload, "")``, or ``(None, why)`` if unusable.

    The same structured reader the skill-proposal filer uses, and for the same
    reason: a draft is a target, a title, a change and (for an issue) a body
    somebody has to read before approving it, so its boundaries cannot be
    delimiters a model has to reproduce. Every complaint names the field, because
    the caller is a model that can only fix what it is told.
    """
    from ciao.upstream_drafts import DRAFT_KIND, TARGETS

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, f"could not read {path}: {exc}"
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return None, f"{path} is not valid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, f"{path} must hold one JSON object, not a {type(payload).__name__}"
    for field in ("target", "skill", "title", "change"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            return None, f'{path} needs a non-empty "{field}"'
    if payload["target"].strip() not in TARGETS:
        return None, (
            f'{path} "target" must be one of: {", ".join(TARGETS)} — the draft '
            f"is filed as a [{DRAFT_KIND}] row a person routes, not as a new queue kind"
        )
    for field in ("body", "repository", "version", "private_evidence", "rationale"):
        if field in payload and not isinstance(payload[field], str):
            return None, f'{path} "{field}" must be a string'
    return payload, ""


def _draft_origins(
    payload: dict[str, Any], workspace: str
) -> tuple[list[dict[str, str]] | None, str]:
    """The draft's learning links, or ``(None, why)`` if one cannot be honoured.

    The workspace is decided here rather than taken from the payload, for the
    reason the skill-proposal filer gives: a learning id only means something in
    the workspace that minted it, so a payload naming a different one is a link
    this queue cannot hold and is refused by name rather than rewritten.
    """
    raw = payload.get("origins") or []
    if not isinstance(raw, list):
        return None, f'{payload.get("skill")!r}: "origins" must be a list of objects'
    links: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, f"origins[{index}] must be an object"
        stated = str(item.get("workspace") or "").strip()
        if stated and stated != workspace:
            return None, (
                f"origins[{index}] names the {stated} workspace, but this draft is "
                f"filed in {workspace}; a learning id only means something in the "
                "workspace that minted it"
            )
        links.append({k: v for k, v in item.items() if k != "workspace"})
    return links, ""


#: A draft decision an unattended run asked for. Distinct from 1 ("the command
#: could not do it") because nothing failed: the request was well-formed, the
#: draft exists, and the answer is that this run has no reviewer. 4 is EPERM,
#: and a scheduled agent that sees it can report the item as deferred rather than
#: retrying an action it is not allowed to take.
UNATTENDED_REFUSED_EXIT = 4


def _refuse_unattended_draft(
    config: CiaoConfig, workspace: str, action: str
) -> int | None:
    """Refuse ``action`` when a run holds this vault's curation lease.

    The check lives here as well as inside the decision helpers so the CLI fails
    *before* it reads ``--content-file`` or reports a draft nobody may act on,
    and so a caller can tell "you are not allowed" from "this draft could not be
    acted on" by the exit code alone. The signal is the run's own lease — see
    :func:`ciao.upstream_drafts.unattended_run` — and there is no flag that turns
    it off, which is the whole point: the unattended Workspace care run shells
    this same command a person would.
    """
    from ciao.upstream_drafts import UnattendedRefused, _refuse_if_unattended

    try:
        _refuse_if_unattended(config, workspace, action)
    except UnattendedRefused as exc:
        print(f"refused unattended: {exc}", file=sys.stderr)
        return UNATTENDED_REFUSED_EXIT
    return None


def _skill_draft_approve_command(args: argparse.Namespace) -> int:
    """Approve one draft: link a matching upstream issue, or create a new skill.

    Two operations behind one verb, because both are "a person approved this
    draft" and the draft says which one it is. The upstream path searches before
    it creates and records the URL it linked or filed; the new-skill path creates
    the owned source, reads it back, checks its frontmatter and syncs, and only
    settles the row once the file exists and the sync finished.

    Attended only, and read from the run rather than from a flag: there is
    deliberately no ``--unattended` here, and no argument the caller could set to
    override :func:`ciao.upstream_drafts.unattended_run`. An automation that
    shells this command while it holds the vault's curation lease is refused with
    :data:`UNATTENDED_REFUSED_EXIT`, so it can only prepare the draft and report
    it.
    """
    from ciao.upstream_drafts import (
        DraftError,
        UnattendedRefused,
        approve_draft,
        create_new_skill,
        find_draft,
    )

    config = _proposal_config(args, "skill-draft-approve")
    name = _active_workspace_name(config)
    refused = _refuse_unattended_draft(config, name, "approving a skill draft")
    if refused is not None:
        return refused
    draft_id = args.draft_id.strip()
    draft = find_draft(config, draft_id)
    if draft is None:
        print(f"no open draft has id {draft_id!r}", file=sys.stderr)
        return 1
    content = ""
    if draft.target == "new_skill":
        content, problem = _read_skill_file(args.content_file)
        if problem:
            print(problem, file=sys.stderr)
            return 2
    try:
        stored = (
            create_new_skill(config, draft_id, content=content)
            if draft.target == "new_skill"
            else approve_draft(config, draft_id, reason=args.reason or "")
        )
    except UnattendedRefused as exc:
        # The run context is re-read inside the decision helper, so a lease taken
        # between the check above and this call is caught here rather than acted
        # on. Same answer, same code, from whichever side sees it first.
        print(f"cannot act on draft {draft_id}: {exc}", file=sys.stderr)
        return UNATTENDED_REFUSED_EXIT
    except DraftError as exc:
        print(f"cannot act on draft {draft_id}: {exc}", file=sys.stderr)
        return 1
    if stored is None:
        print(f"no open draft has id {draft_id!r}", file=sys.stderr)
        return 1
    _report_draft(stored, name, args)
    return 0


def _read_skill_file(path: str) -> tuple[str, str]:
    """The new skill's own text, or ``("", why)`` if it cannot be read."""
    if not path.strip():
        return "", (
            "a new-skill draft needs --content-file holding the SKILL.md text: "
            "the file is created from exactly those bytes, so a creation with no "
            "content would write an empty skill"
        )
    try:
        return Path(path).read_text(encoding="utf-8"), ""
    except (OSError, UnicodeError) as exc:
        return "", f"could not read {path}: {exc}"


def _report_draft(stored, workspace: str, args: argparse.Namespace) -> None:
    """Print what a decision did, in the form each reader needs."""
    if args.json:
        json.dump(
            {
                "id": stored.id,
                "target": stored.target,
                "skill": stored.skill,
                "workspace": workspace,
                "lifecycle": stored.lifecycle,
                "issue_url": stored.issue_url,
                "reason": stored.reason,
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return
    if stored.lifecycle == "filed":
        print(f"Draft {stored.id} is filed: {stored.issue_url or stored.skill}")
        if stored.reason:
            print(stored.reason)
        if stored.target == "upstream_issue":
            print(
                "That is a report upstream, not a local change: nothing in this "
                "workspace changed, and the lesson is still not in any skill here."
            )
    elif stored.lifecycle == "rejected":
        print(f"Draft {stored.id} rejected. It will not be offered again.")
    else:
        print(f"Draft {stored.id} is still {stored.lifecycle}.")
        if stored.reason:
            print(stored.reason)


def _skill_draft_reject_command(args: argparse.Namespace) -> int:
    """Turn one draft down, so the next pass does not offer it again.

    A rejection settles exactly as an approval does, so it is refused in an
    unattended run for the same reason: the draft's own ground rules say
    settlement follows a person, and a run that could reject would archive an
    unanswered question as an answer.
    """
    from ciao.upstream_drafts import reject_draft

    config = _proposal_config(args, "skill-draft-reject")
    name = _active_workspace_name(config)
    refused = _refuse_unattended_draft(config, name, "rejecting a skill draft")
    if refused is not None:
        return refused
    stored = reject_draft(config, args.draft_id.strip(), reason=args.reason or "")
    if stored is None:
        print(f"no open draft has id {args.draft_id!r}", file=sys.stderr)
        return 1
    _report_draft(stored, name, args)
    return 0


def _skill_drafts_command(args: argparse.Namespace) -> int:
    """List the workspace's open drafts, settled ones included under ``--all``."""
    from ciao.upstream_drafts import read_queue, read_records

    config = _proposal_config(args, "skill-drafts")
    name = _active_workspace_name(config)
    drafts = read_records(config, name) if args.all else read_queue(config, name)
    rows = [
        {
            "id": draft.id,
            "target": draft.target,
            "skill": draft.skill,
            "title": draft.title,
            "lifecycle": draft.lifecycle,
            "repository": draft.repository,
            "version": draft.version,
            "issue_url": draft.issue_url,
            "origins": len(draft.origins),
        }
        for draft in drafts
    ]
    if args.json:
        json.dump(
            {"workspace": name, "drafts": rows}, sys.stdout, indent=2, ensure_ascii=False
        )
        sys.stdout.write("\n")
        return 0
    if not rows:
        print(f"No {'' if args.all else 'open '}skill drafts in {name}.")
        return 0
    for row in rows:
        where = f" → {row['issue_url']}" if row["issue_url"] else ""
        print(f"[{row['lifecycle']}] {row['id']} {row['target']} {row['skill']}: {row['title']}{where}")
    return 0


def _health_command(args: argparse.Namespace) -> int:
    from ciao.config import CiaoConfig
    from ciao.web.agent_assets import repair_workspace_health, workspace_health

    config = CiaoConfig.from_env()
    if args.action == "fix":
        result = repair_workspace_health(config)
    else:
        result = workspace_health(config)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _sync_skills_command(args: argparse.Namespace) -> int:
    from ciao import sync_skills

    return sync_skills.main(
        [
            "--workspace",
            str(args.workspace),
            *(["--skip-upstream"] if args.skip_upstream else []),
            *(["--verbose"] if args.verbose else []),
        ]
    )


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
            continue
        key, value = cleaned.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


def _make_json_request(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    data: dict | None = None,
    method: str = "GET",
) -> dict | list:
    request = urllib.request.Request(url, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(data).encode("utf-8")
    try:
        with opener.open(request) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            payload = json.loads(body)
            message = payload.get("error", body) if isinstance(payload, dict) else body
        except json.JSONDecodeError:
            message = body
        print(f"Error {exc.code} for {method} {url}: {message}", file=sys.stderr)
        return {"_error": True}
    except OSError as exc:
        print(f"Connection error to {url}: {exc}", file=sys.stderr)
        return {"_error": True}

    if not body:
        return {}
    parsed: dict | list = json.loads(body)
    return parsed


def _resolve_project(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    workspace: str,
    project_arg: str | None,
) -> str | None:
    projects = _make_json_request(
        opener, f"{base_url}/api/projects?workspace={workspace}"
    )
    if isinstance(projects, dict) and projects.get("_error"):
        return None
    if not isinstance(projects, list) or not projects:
        print(f"Error: No projects found in workspace '{workspace}'.", file=sys.stderr)
        return None

    if project_arg:
        for project in projects:
            if project.get("project_id") == project_arg:
                return project_arg
        matches = [
            project
            for project in projects
            if project_arg.lower() in project.get("name", "").lower()
        ]
        if len(matches) == 1:
            return cast(str, matches[0]["project_id"])
        if len(matches) > 1:
            names = ", ".join(
                f"'{project['name']}' ({project['project_id']})"
                for project in matches
            )
            print(
                f"Error: Project '{project_arg}' is ambiguous. Matches: {names}",
                file=sys.stderr,
            )
            return None
        print(f"Error: Project matching '{project_arg}' not found.", file=sys.stderr)
        return None

    env_project = os.environ.get("CIAO_ACTIVE_PROJECT")
    if env_project:
        for project in projects:
            if project.get("project_id") == env_project:
                return env_project

    for project in projects:
        if project.get("is_auto") or project.get("name") == "General":
            return cast(str, project["project_id"])
    return cast(str, projects[0]["project_id"])


def _create_chat_command(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    _load_env_file(workspace_root / ".env")

    # PWA_HOST is the server's *bind* address. For a loopback/wildcard bind we
    # emit "localhost" so the printed chat link matches the host the browser is
    # authenticated on (the menu bar, setup, and login URLs all use localhost).
    # The session cookie is host-only, so a "127.0.0.1" link would not carry the
    # "localhost" cookie and every /ws and authed /api request would be rejected.
    host = os.environ.get("PWA_HOST", "localhost")
    if host in ("0.0.0.0", "127.0.0.1", "::", "::1", ""):
        host = "localhost"
    port = os.environ.get("PWA_PORT", "8443")
    base_url = args.base_url or f"http://{host}:{port}"
    auth_token = os.environ.get("PWA_AUTH_TOKEN", "")
    if not auth_token:
        print("Error: PWA_AUTH_TOKEN not found in environment or .env file.", file=sys.stderr)
        return 1

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    auth = _make_json_request(
        opener, f"{base_url}/api/auth", data={"token": auth_token}, method="POST"
    )
    if isinstance(auth, dict) and auth.get("_error"):
        return 1

    workspace = args.workspace or os.environ.get("CIAO_ACTIVE_WORKSPACE") or "default"

    project_id = _resolve_project(opener, base_url, workspace, args.project)
    if project_id is None:
        return 1

    payload = {
        "title": args.title or "New Chat",
        "model": args.model or os.environ.get("CIAO_MODEL") or None,
        "provider": args.provider or os.environ.get("CIAO_PROVIDER") or None,
    }
    chat_info = _make_json_request(
        opener,
        f"{base_url}/api/projects/{project_id}/chats",
        data={key: value for key, value in payload.items() if value is not None},
        method="POST",
    )
    if isinstance(chat_info, dict) and chat_info.get("_error"):
        return 1
    if not isinstance(chat_info, dict) or "chat_id" not in chat_info:
        print("Error: chat creation returned an unexpected response.", file=sys.stderr)
        return 1

    chat_id = chat_info["chat_id"]
    prompt_result = _make_json_request(
        opener,
        f"{base_url}/api/chats/{chat_id}/prompt",
        data={"prompt": args.prompt},
        method="POST",
    )
    if isinstance(prompt_result, dict) and prompt_result.get("_error"):
        return 1

    print(f"Success: Created chat '{chat_info['title']}' ({chat_id})")
    print(f"Workspace: {workspace} | Project: {chat_info.get('project_id')}")
    print(f"Model: {chat_info.get('model')} ({chat_info.get('provider')})")
    print(f"PWA URL: {base_url}/chat/{chat_id}")
    return 0


def _register_launchd_service(workspace: Path) -> Path:
    """Write the server LaunchAgent for an already set-up workspace."""
    from ciao import macos_service

    root = workspace.expanduser().resolve()
    if not (root / ".env").is_file():
        raise RuntimeError(
            f"{root} is not a Ciaobot workspace (no .env). Run `ciao setup --workspace {root}` first."
        )
    if _looks_like_source_checkout(root):
        raise RuntimeError(
            f"{root} looks like the Ciaobot source checkout, not a workspace. "
            "Pass your workspace folder to --workspace."
        )
    from ciao.setup_status import tcc_protected_location

    protected = tcc_protected_location(root)
    if protected:
        raise RuntimeError(
            f"{root} is inside ~/{protected}, which launchd cannot read. "
            "Move the workspace out of Desktop/Documents/Downloads first."
        )

    from dotenv import dotenv_values

    runtime_value = (dotenv_values(root / ".env").get("CIAO_RUNTIME_ROOT") or "").strip() or ".runtime"
    runtime_root = Path(runtime_value).expanduser()
    if not runtime_root.is_absolute():
        runtime_root = root / runtime_root
    return _write_launchd_plist(
        workspace=root,
        launch_agents_dir=default_launch_agents_dir(),
        engine_path=os.environ.get("CIAO_ENGINE_PATH", "").strip() or sys.executable,
        runtime_root=runtime_root,
        port=_pwa_port_from_env(root, macos_service.DEFAULT_PORT),
        path=os.environ.get("PATH", ""),
    )


def _service_command(args: argparse.Namespace) -> int:
    from ciao import macos_service

    action = args.service_action
    as_json = bool(args.as_json)
    if getattr(args, "deprecated_alias", False) and not as_json:
        print("`ciao desktop-service` is deprecated; use `ciao service`.", file=sys.stderr)
    if sys.platform == "win32":
        return _windows_service_command(args, as_json)
    if sys.platform != "darwin":
        return macos_service.print_result(
            macos_service.ServiceResult(
                False,
                str(action),
                "`ciao service` manages the macOS LaunchAgent and the Windows "
                "logon task. On Linux use `ciao linux-service` and systemctl.",
                {},
            ),
            as_json=as_json,
        )
    if action == "status":
        result = macos_service.service_status()
    elif action == "start":
        runtime = macos_service.discover_runtime()
        workspace = getattr(args, "workspace", None)
        if workspace is not None and Path(runtime.server_plist).is_file():
            installed = _plist_workspace(default_launch_agents_dir())
            requested = Path(workspace).expanduser().resolve()
            if installed is not None and installed != requested:
                return macos_service.print_result(
                    macos_service.ServiceResult(
                        False,
                        "start",
                        f"The installed LaunchAgent serves {installed}, not {requested}. "
                        f"Run `ciao setup --workspace {requested} --load-launchd --yes` to repoint it.",
                        {"installed_workspace": str(installed), "requested_workspace": str(requested)},
                    ),
                    as_json=as_json,
                )
        if workspace is not None and not Path(runtime.server_plist).is_file():
            try:
                _register_launchd_service(Path(workspace))
            except (RuntimeError, OSError) as exc:
                return macos_service.print_result(
                    macos_service.ServiceResult(False, "start", str(exc), {"setup_required": True}),
                    as_json=as_json,
                )
            runtime = macos_service.discover_runtime()
        result = macos_service.start_service(runtime=runtime)
    elif action == "restart":
        result = macos_service.restart_service(force=bool(args.force))
    elif action == "stop":
        result = macos_service.stop_service(force=bool(args.force))
    elif action == "login":
        result = macos_service.set_login_enabled(args.login_action == "enable")
    elif action == "update-engine":
        result = macos_service.update_engine(force=bool(args.force))
    elif action == "migrate":
        result = macos_service.migrate_legacy_companion(running_app=args.app_bundle)
    elif action == "migration-classify":
        # The bridge Ciaobot.app needs to decide whether to hand this Mac's
        # engine over to the terminal installer (#604). The classifier is a
        # module of its own, not a desktop-service action, and it already runs
        # from the verified wheel inside install-engine.sh; importing it here
        # rather than shelling out to `python -I -m ciao.engine_migration` is
        # the smaller change, and it keeps the app off a second interpreter.
        # It reads and never writes, so a Mac the app cannot interpret comes
        # back as `desktop_invalid` and the app asks instead of guessing.
        from ciao.engine_migration import classify

        classification = classify()
        result = macos_service.ServiceResult(
            True,
            "migration-classify",
            f"this Mac classifies as {classification.kind}",
            asdict(classification),
        )
    elif action == "rollback":
        result = macos_service.rollback_legacy_companion()
    else:  # pragma: no cover - argparse constrains the action.
        parser_error = macos_service.ServiceResult(
            False,
            str(action),
            "Unknown service action.",
            {},
        )
        return macos_service.print_result(parser_error, as_json=as_json)
    return macos_service.print_result(result, as_json=as_json)


# The macOS desktop shell and the engine updater. They have no Windows
# equivalent yet, so they fail with a clean result instead of half-running.
_WINDOWS_UNAVAILABLE_ACTIONS = (
    "login",
    "update-engine",
    "migrate",
    "migration-classify",
    "rollback",
)


def _validate_service_workspace(workspace: Path) -> Path:
    """Resolve and check a workspace the engine service may serve.

    The two checks `_register_launchd_service` makes before writing a plist: a
    Ciaobot workspace has a ``.env``, and the app's own source checkout never is
    one. The macOS TCC check is left out -- it names launchd and macOS privacy
    protection, neither of which exists on Windows.
    """

    root = workspace.expanduser().resolve()
    if not (root / ".env").is_file():
        raise RuntimeError(
            f"{root} is not a Ciaobot workspace (no .env). Run `ciao setup --workspace {root}` first."
        )
    if _looks_like_source_checkout(root):
        raise RuntimeError(
            f"{root} looks like the Ciaobot source checkout, not a workspace. "
            "Pass your workspace folder to --workspace."
        )
    return root


def _windows_service_port() -> int:
    """Port the Windows engine answers on: the task workspace's ``.env``, else the default."""

    from ciao import macos_service, windows_service

    workspace = windows_service.task_workspace(
        windows_service.default_task_dir() / windows_service.TASK_FILE_NAME
    )
    if workspace is None:
        return macos_service.DEFAULT_PORT
    return _pwa_port_from_env(workspace, macos_service.DEFAULT_PORT)


def _windows_service_command(args: argparse.Namespace, as_json: bool) -> int:
    """`ciao service` on Windows: the engine's Task Scheduler logon task.

    Only the generic lifecycle exists there. The macOS path answers with the
    same `ServiceResult`, so plain and `--json` output are one shape everywhere.
    """

    from ciao import macos_service, windows_service

    action = str(args.service_action)
    if action == "status":
        result = windows_service.service_status(_windows_service_port())
    elif action == "start":
        workspace = getattr(args, "workspace", None)
        if workspace is not None:
            requested: Path | None = None
            try:
                requested = _validate_service_workspace(Path(workspace))
            except (RuntimeError, OSError) as exc:
                return macos_service.print_result(
                    macos_service.ServiceResult(
                        False, "start", str(exc), {"setup_required": True}
                    ),
                    as_json=as_json,
                )
            # Refuse to repoint a task that already serves another workspace,
            # before anything is written. Same wording as the macOS path.
            installed = _registered_service_workspace(
                windows_service.default_task_dir()
            )
            if installed is not None and installed != requested:
                return macos_service.print_result(
                    macos_service.ServiceResult(
                        False,
                        "start",
                        f"The registered task serves {installed}, not {requested}. "
                        f"Run `ciao setup --workspace {requested} --load-launchd --yes` to repoint it.",
                        {
                            "installed_workspace": str(installed),
                            "requested_workspace": str(requested),
                        },
                    ),
                    as_json=as_json,
                )
        result = windows_service.start_service(workspace)
    elif action == "stop":
        result = windows_service.stop_service(
            _windows_service_port(), force=bool(args.force)
        )
    elif action == "restart":
        result = windows_service.restart_service(
            _windows_service_port(), force=bool(args.force)
        )
    elif action in _WINDOWS_UNAVAILABLE_ACTIONS:
        result = macos_service.ServiceResult(
            False,
            action,
            f"`ciao service {action}` is not available on Windows yet.",
            {},
        )
    else:  # pragma: no cover - argparse constrains the action.
        result = macos_service.ServiceResult(
            False,
            action,
            "Unknown service action.",
            {},
        )
    return macos_service.print_result(result, as_json=as_json)


def _linux_service_command(args: argparse.Namespace) -> int:
    from ciao.linux_service import render_service

    try:
        unit = render_service(
            workspace=args.workspace, user=args.user, home=args.home, python=args.python,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(unit, end="")
    return 0


def _desktop_command(args: argparse.Namespace) -> int:
    from ciao import desktop_install

    as_json = bool(getattr(args, "as_json", False))
    explicit_dir = getattr(args, "app_dir", None)
    if explicit_dir:
        app_dir = Path(explicit_dir).expanduser()
    else:
        app_dir = _default_app_dir()
        system_app_dir = Path("/Applications")
        if not (app_dir / desktop_install.APP_BUNDLE_NAME).exists() and (
            system_app_dir / desktop_install.APP_BUNDLE_NAME
        ).exists():
            # Older installs used /Applications. Keep uninstall able to find
            # those bundles while new installs consistently use ~/Applications.
            app_dir = system_app_dir

    def report(payload: dict[str, object], lines: list[str]) -> int:
        if as_json:
            print(json.dumps(payload, indent=2))
        else:
            for line in lines:
                print(line)
        return 0

    try:
        result = desktop_install.uninstall_desktop_app(app_dir=app_dir)
        lines = [
            f"Removed {result['path']}"
            if result["removed"]
            else f"Nothing to remove at {result['path']}"
        ]
        # Named explicitly: when the bundle was already dragged to the Trash
        # the headline reads "Nothing to remove", yet this run may still have
        # booted out and deleted launchd plists. Staying silent about that
        # tells the user nothing happened when something did.
        for agent in result.get("removed_agents") or []:
            lines.append(f"Removed {agent}")
        if result.get("removed_shim"):
            lines.append(f"Removed {result['removed_shim']}")
        return report(result, lines)
    except desktop_install.InstallError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(str(exc), file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ciao", description="Ciaobot local assistant CLI.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the Ciaobot server.")
    run_parser.add_argument(
        "--supervised",
        action="store_true",
        help="Exit with the restart code instead of re-execing; for `ciao supervise`.",
    )
    run_parser.set_defaults(func=lambda args: _run_server(supervised=args.supervised))

    supervise_parser = subparsers.add_parser(
        "supervise",
        help="Run the server as a child process and relaunch it when it asks to restart.",
    )
    supervise_parser.add_argument(
        "child_args", nargs=argparse.REMAINDER, help="Extra arguments passed to `ciao run`."
    )
    supervise_parser.set_defaults(func=_supervise_command)

    def add_service_parser(
        name: str,
        help_text: str,
        *,
        deprecated: bool,
    ) -> None:
        service_parser = subparsers.add_parser(name, help=help_text)
        service_sub = service_parser.add_subparsers(dest="service_action", required=True)
        for action in (
            "status",
            "start",
            "restart",
            "stop",
            "update-engine",
            "migrate",
            "migration-classify",
            "rollback",
        ):
            action_parser = service_sub.add_parser(action)
            action_parser.add_argument("--json", action="store_true", dest="as_json")
            if action in {"restart", "stop", "update-engine"}:
                action_parser.add_argument(
                    "--force",
                    action="store_true",
                    help="Proceed even when chats are active (after UI confirmation).",
                )
            if action == "migrate":
                action_parser.add_argument(
                    "--app-bundle",
                    type=Path,
                    required=True,
                    help="Installed Ciaobot.app bundle requesting migration.",
                )
            if action == "start":
                action_parser.add_argument(
                    "--workspace",
                    type=Path,
                    default=None,
                    help="Register the LaunchAgent for this workspace first if it is not installed.",
                )
            action_parser.set_defaults(
                func=_service_command,
                deprecated_alias=deprecated,
            )
        login_parser = service_sub.add_parser("login")
        login_parser.add_argument("login_action", choices=("enable", "disable"))
        login_parser.add_argument("--json", action="store_true", dest="as_json")
        login_parser.set_defaults(
            func=_service_command,
            deprecated_alias=deprecated,
        )

    add_service_parser(
        "service",
        "Start, stop and inspect the launchd-managed Ciaobot engine (macOS).",
        deprecated=False,
    )
    add_service_parser(
        "desktop-service",
        "Deprecated alias of `ciao service` (used by Ciaobot.app).",
        deprecated=True,
    )

    # Separate from `service`, which controls the launchd engine. This group
    # only removes an old app bundle, for the compatibility window: the app
    # itself is retired, and installation and updates are owned by
    # scripts/install-engine.sh.
    desktop_parser = subparsers.add_parser(
        "desktop",
        help="Remove an installed Ciaobot.app desktop bundle.",
    )
    desktop_sub = desktop_parser.add_subparsers(dest="desktop_action", required=True)
    desktop_uninstall_parser = desktop_sub.add_parser(
        "uninstall",
        help="Remove the installed Ciaobot.app bundle.",
    )
    desktop_uninstall_parser.add_argument(
        "--app-dir",
        type=Path,
        default=None,
        help="Directory holding Ciaobot.app (defaults to ~/Applications, "
        "falling back to /Applications when the bundle is only there).",
    )
    desktop_uninstall_parser.add_argument("--json", action="store_true", dest="as_json")
    desktop_uninstall_parser.set_defaults(func=_desktop_command)

    linux_service_parser = subparsers.add_parser(
        "linux-service",
        help="Print a systemd service unit for a Linux host (does not install it).",
    )
    linux_service_parser.add_argument("--workspace", type=Path, required=True)
    linux_service_parser.add_argument(
        "--user", required=True, help="Unprivileged Linux service account.",
    )
    linux_service_parser.add_argument(
        "--home", type=Path, required=True,
        help="Service account home (provider credentials live here).",
    )
    linux_service_parser.add_argument(
        "--python", type=Path, default=Path(sys.executable),
        help="Absolute path to the installed virtualenv Python.",
    )
    linux_service_parser.set_defaults(func=_linux_service_command)

    setup_parser = subparsers.add_parser(
        "setup",
        help="Scaffold a local Ciaobot workspace from packaged stock assets.",
    )
    setup_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace directory to initialize.",
    )
    setup_parser.add_argument(
        "--workspace-name",
        type=_workspace_name_arg,
        default=None,
        help=(
            "Name of the first logical workspace created under the vault "
            "(default: personal)."
        ),
    )
    setup_parser.add_argument(
        "--auth-token",
        help=(
            "PWA password to write when .env is new (a random one is generated "
            "when omitted)."
        ),
    )
    setup_parser.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "Write PWA_AUTH_REQUIRED=false instead of protecting the dashboard "
            "with a password. Only for a machine nobody else can reach."
        ),
    )
    setup_parser.add_argument(
        "--python",
        default=None,
        help=(
            "Executable used by the generated LaunchAgent (defaults to the "
            "bundled ciao launcher, or the current Python outside a bundle)."
        ),
    )
    setup_parser.add_argument(
        "--port",
        type=int,
        default=8443,
        help="Server port to record in a new workspace configuration.",
    )
    setup_parser.add_argument(
        "--launch-agents-dir",
        type=Path,
        default=None,
        help="Directory for an explicit launchd plist export (generated by default only on macOS).",
    )
    setup_parser.add_argument(
        "--app-dir",
        type=Path,
        default=None,
        help=(
            "Directory to scan for legacy launcher bundles during migration. "
            "Defaults to ~/Applications."
        ),
    )
    setup_parser.add_argument(
        "--load-launchd",
        action="store_true",
        help="Run launchctl unload/load after writing the LaunchAgent.",
    )
    setup_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip safety guards (setting up inside the source checkout, or "
        "moving an already-configured workspace to this directory).",
    )
    setup_parser.set_defaults(func=_setup_command)

    setup_url_parser = subparsers.add_parser(
        "setup-url",
        help="Print the localhost login URL, minting a fresh setup token.",
    )
    setup_url_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(os.environ.get("CIAO_WORKSPACE", ".")),
        help="Workspace directory (defaults to $CIAO_WORKSPACE or cwd).",
    )
    setup_url_parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("CIAO_PORT", "8443")),
        help="Fallback port when the workspace .env has no PWA_PORT.",
    )
    setup_url_parser.add_argument(
        "--rotate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mint a fresh token (default); --no-rotate reuses the existing one.",
    )
    setup_url_parser.set_defaults(func=_setup_url_command)

    auth_parser = subparsers.add_parser(
        "auth",
        help="Run a provider OAuth/login command for first-run setup.",
    )
    auth_parser.add_argument("provider", choices=_auth_provider_choices())
    auth_parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print the terminal command without running it.",
    )
    auth_parser.add_argument(
        "--device-auth",
        action="store_true",
        help="Use provider device authorization when supported.",
    )
    auth_parser.set_defaults(func=_auth_command)

    dev_parser = subparsers.add_parser(
        "dev",
        help="Run the local backend plus Vite frontend for development.",
    )
    dev_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="App checkout/workspace root. Defaults to current directory.",
    )
    dev_parser.add_argument("--backend-port", type=int, default=8543)
    dev_parser.add_argument("--frontend-port", type=int, default=5173)
    dev_parser.add_argument(
        "--no-install",
        action="store_true",
        help="Do not run npm install when web/node_modules is missing.",
    )
    dev_parser.set_defaults(
        func=lambda args: dev.main(
            [
                "--workspace",
                str(args.workspace),
                "--backend-port",
                str(args.backend_port),
                "--frontend-port",
                str(args.frontend_port),
                *(["--no-install"] if args.no_install else []),
            ]
        )
    )

    public_parser = subparsers.add_parser(
        "public-preflight",
        help="Export or scan a public Ciaobot tree.",
    )
    public_parser.add_argument("args", nargs=argparse.REMAINDER)
    public_parser.set_defaults(func=lambda args: public_release.main(args.args))

    smoke_parser = subparsers.add_parser(
        "package-smoke",
        help="Build, install, and smoke-test the Ciaobot package.",
    )
    smoke_parser.add_argument("args", nargs=argparse.REMAINDER)
    smoke_parser.set_defaults(func=lambda args: package_smoke.main(args.args))

    release_parser = subparsers.add_parser(
        "prepare-release",
        help="Prepare a release branch, changelog, and draft PR.",
    )
    release_parser.add_argument("args", nargs=argparse.REMAINDER)
    release_parser.set_defaults(func=lambda args: release.main(args.args))

    search_parser = subparsers.add_parser(
        "vault-search",
        help="Full-text search over the vault or transcript logs.",
    )
    search_parser.add_argument("query", nargs="?", default=None, help="Search keywords.")
    search_parser.add_argument(
        "--logs",
        action="store_true",
        help="Search transcript and meeting logs instead of vault notes.",
    )
    search_parser.add_argument(
        "--limit", type=int, default=10, help="Maximum number of results."
    )
    search_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Drop and rebuild the search index before searching.",
    )
    search_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    search_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help=(
            "Install runtime root that owns the search database. Defaults to "
            "CIAO_RUNTIME_ROOT or <workspace>/.runtime; the index is install-owned "
            "so two installs cannot clear each other's derived state."
        ),
    )
    search_parser.set_defaults(func=_vault_search_command)

    index_parser = subparsers.add_parser(
        "vault-index",
        help="Build or query the vault frontmatter/link index.",
    )
    index_parser.add_argument("--workspace", default="all")
    index_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    index_parser.add_argument("--type", dest="types", action="append", default=[])
    index_parser.add_argument("--tag", dest="tags", action="append", default=[])
    index_parser.add_argument("--name", default=None)
    index_parser.add_argument("--related-to", dest="related_to", default=None)
    index_parser.add_argument("--neighbors", default=None)
    index_parser.add_argument("--depth", type=int, default=2)
    index_parser.add_argument("--format", choices=["tsv", "md", "json"], default="tsv")
    index_parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate INDEX.md under the configured vault root.",
    )
    index_parser.set_defaults(func=_vault_index_command)

    export_parser = subparsers.add_parser(
        "vault-export",
        help="Export the vault as a portable OKF bundle (.tar.gz).",
        description=(
            "Package the vault, or one workspace of it, as an Open Knowledge "
            "Format bundle: the notes plus a bundle-root index.md carrying "
            "okf_version. Refuses a vault still written in wikilinks, whose "
            "edges no other consumer can follow."
        ),
    )
    export_parser.add_argument("dest", type=Path, help="Destination .tar.gz path.")
    export_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    export_parser.add_argument(
        "--workspace-name",
        default="",
        help=(
            "Export only this logical workspace; its subtree becomes the bundle "
            "root. Omit to export the whole vault."
        ),
    )
    export_parser.add_argument(
        "--force",
        action="store_true",
        help="Export even when the vault still uses wikilinks.",
    )
    export_parser.add_argument(
        "--json",
        action="store_true",
        help="Output the raw summary as JSON.",
    )
    export_parser.set_defaults(func=_vault_export_command)

    # Registered for discoverability only; `main` intercepts "critique" before
    # argparse so the panel's own flags reach `ciao.critique` untouched (the
    # same reason `gws` is intercepted — argparse eats `--`-prefixed args before
    # a subparser's REMAINDER can see them). Do not give it a `func`.
    #
    # It needs a `ciao` entry point at all because a packaged install puts only
    # a `ciao` wrapper on PATH: `python3 -m ciao.critique` resolves some external
    # interpreter that has neither `ciao` nor its dependencies, so /critique
    # failed for every user who had not installed from source.
    subparsers.add_parser(
        "critique",
        help="Run the multi-model critique panel on an artifact.",
        add_help=False,
    )

    lint_parser = subparsers.add_parser(
        "vault-lint",
        help="Run vault hygiene checks.",
        description="Vault hygiene linter for markdown files.",
    )
    lint_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    lint_parser.add_argument(
        "--migrate-links",
        action="store_true",
        help=(
            "Instead of linting, convert `[[wikilinks]]` to relative Markdown "
            "links. Dry-run unless --apply is also passed."
        ),
    )
    lint_parser.add_argument(
        "--apply",
        action="store_true",
        help="With --migrate-links, write the conversion.",
    )
    lint_parser.add_argument(
        "--force",
        action="store_true",
        help="With --migrate-links --apply, override the safety refusals.",
    )
    lint_parser.set_defaults(func=_vault_lint_command)

    migrate_parser = subparsers.add_parser(
        "vault-migrate",
        help="Rename non-canonical frontmatter types onto the vocabulary.",
        description=(
            "One-off migration for an existing vault: keeps the stock categories "
            "that no longer ship (product, feature, automation, document, "
            "reference, content) when notes still use them, renames aliased "
            "types (project-log -> journal) and reports types with no "
            "canonical equivalent. Dry-run unless --apply is passed."
        ),
    )
    migrate_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    migrate_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the renames. Without this, only report what would change.",
    )
    migrate_parser.add_argument(
        "--json",
        action="store_true",
        help="Output the raw summary as JSON.",
    )
    migrate_parser.set_defaults(func=_vault_migrate_command)

    for name, help_text, description, handler in (
        (
            "vault-migrate-links",
            "Convert vault [[wikilinks]] to relative markdown links.",
            (
                "One-off migration for an existing vault: rewrites body "
                "[[wikilinks]] as relative markdown links and normalizes "
                "frontmatter related: values to bare refs, skipping code spans, "
                "escaped links, Logs/, Templates/, and generated files. Records "
                "an exact reverse map under .runtime/migration/. Dry-run unless "
                "--apply is passed."
            ),
            _vault_migrate_links_command,
        ),
        (
            "vault-unmigrate-links",
            "Undo vault-migrate-links from its receipt.",
            (
                "Restores exactly the spans recorded by vault-migrate-links. "
                "Dry-run unless --apply is passed."
            ),
            _vault_unmigrate_links_command,
        ),
        (
            "vault-rehome",
            "Re-file person notes filed in the wrong workspace.",
            (
                "One-off cleanup for a vault whose people were all filed into one "
                "workspace by a global memory-curation run: moves the notes whose "
                "tags name another workspace and repoints every reference to them "
                "(wikilinks, relative markdown links, frontmatter refs). Notes "
                "with no workspace-naming tag are queued in that workspace's "
                "Workspace/Memory-Proposals.md and never moved. Records an exact "
                "reverse map under .runtime/migration/. Dry-run unless --apply "
                "is passed."
            ),
            _vault_rehome_command,
        ),
        (
            "vault-unrehome",
            "Undo vault-rehome from its receipt.",
            (
                "Moves back exactly the notes vault-rehome moved and restores the "
                "reference spans it rewrote. Dry-run unless --apply is passed."
            ),
            _vault_unrehome_command,
        ),
    ):
        links_parser = subparsers.add_parser(
            name, help=help_text, description=description
        )
        links_parser.add_argument(
            "--vault-root",
            type=Path,
            default=None,
            help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
        )
        links_parser.add_argument(
            "--runtime-root",
            type=Path,
            default=None,
            help=(
                "Runtime root holding the migration receipt. Defaults to "
                "CIAO_RUNTIME_ROOT or <workspace>/.runtime."
            ),
        )
        links_parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without this, only report what would change.",
        )
        links_parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Proceed despite a dirty vault git tree, or despite an existing "
                "receipt (which is moved aside, not overwritten)."
            ),
        )
        links_parser.add_argument(
            "--json",
            action="store_true",
            help="Output the raw summary as JSON.",
        )
        if name == "vault-rehome":
            # Repeatable, and only here: workspace names are the user's, so the
            # registry has to be passed in rather than guessed. Omitted, the
            # command derives them from the vault's own directories.
            links_parser.add_argument(
                "--workspace-name",
                action="append",
                default=[],
                help=(
                    "A registered workspace name; repeat for each one. Defaults "
                    "to the vault's workspace directories."
                ),
            )
        links_parser.set_defaults(func=handler)

    learnings_parser = subparsers.add_parser(
        "learnings-migrate",
        help="Convert legacy Learnings.md entries to canonical records.",
        description=(
            "One-off migration for an existing workspace: rewrites the entries "
            "under `## Active` in Workspace/Learnings.md into the canonical "
            "record shape, preserving every other byte — frontmatter, format "
            "notes, the `## Promoted / Resolved` section, the BOM and CRLF line "
            "endings included. A line whose shape cannot be read is reported and "
            "kept exactly as written, and no entry is ever dropped. Records an "
            "exact reverse map under .runtime/migration/. Dry-run unless --apply "
            "is passed."
        ),
    )
    learnings_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    learnings_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help=(
            "Runtime root holding the migration receipt. Defaults to "
            "CIAO_RUNTIME_ROOT or <workspace>/.runtime."
        ),
    )
    learnings_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes. Without this, only report what would change.",
    )
    learnings_parser.add_argument(
        "--revert",
        type=Path,
        default=None,
        help=(
            "Reverse a previous run from its receipt instead of migrating. The "
            "spans are restored from the recorded bytes, and a file that changed "
            "since is left entirely untouched."
        ),
    )
    learnings_parser.add_argument(
        "--json",
        action="store_true",
        help="Output the raw summary as JSON.",
    )
    learnings_parser.set_defaults(func=_learnings_migrate_command)

    cleanup_parser = subparsers.add_parser(
        "learnings-cleanup",
        help="Retire Learnings.md entries whose findings are durably settled.",
        description=(
            "Reconciles one workspace's Workspace/Learnings.md against the "
            "skill-proposal queue and the upstream draft sidecar, and lists "
            "every Active entry with the decision beside it: settled and "
            "removable, kept and why, or unreadable and untouched. Entries whose "
            "findings are pending, implementing, never proposed, held back by an "
            "unattributable finding, or routed only to an upstream issue are "
            "never removed. The write is lock-serialized, revision-checked, and "
            "reversible from a receipt under .runtime/migration/; every other "
            "byte of the file, the `## Promoted / Resolved` section, the format "
            "notes, the BOM and CRLF endings included, is preserved except the "
            "frontmatter's `updated:`. Dry-run unless --apply or --apply-settled "
            "is passed; --apply refuses without --approval-file, and "
            "--apply-settled retires only the rows the reconciliation already "
            "proposed."
        ),
    )
    cleanup_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    cleanup_parser.add_argument(
        "--workspace",
        default=None,
        help=(
            "Workspace name whose queue is folded. Defaults to the vault "
            "directory's own name, which is the identity its learning ids were "
            "minted under."
        ),
    )
    cleanup_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help=(
            "Runtime root holding the cleanup receipt. Defaults to "
            "CIAO_RUNTIME_ROOT or <workspace>/.runtime."
        ),
    )
    cleanup_parser.add_argument(
        "--approval-file",
        type=Path,
        default=None,
        help=(
            "JSON list of approved rows: a learning_id, the entry_revision it "
            "was reviewed at, a reason and the evidence for retiring it. "
            "Required by --apply; an approval naming a revision the entry no "
            "longer has is not honoured."
        ),
    )
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write the removals named in --approval-file. Without both flags "
            "nothing is written."
        ),
    )
    cleanup_parser.add_argument(
        "--apply-settled",
        action="store_true",
        help=(
            "Retire only the entries the reconciliation already proposed, "
            "unattended and without an approval file: no reapproval, no judgement "
            "of a kept row, and no more removals than "
            "LEARNINGS_CLEANUP_MAX_ITEMS. This is what the nightly cleanup pass "
            "names; everything that needs a person is still --apply "
            "--approval-file. Cannot be combined with --apply or "
            "--approval-file."
        ),
    )
    cleanup_parser.add_argument(
        "--revert",
        type=Path,
        default=None,
        help=(
            "Restore a previous run's removals from its receipt instead of "
            "reconciling. The bytes come back exactly, and the suppression stays "
            "recorded so the nightly pass does not remove them again."
        ),
    )
    cleanup_parser.add_argument(
        "--json",
        action="store_true",
        help="Output the plan and the result as JSON.",
    )
    cleanup_parser.set_defaults(func=_learnings_cleanup_command)

    os_audit_parser = subparsers.add_parser(
        "os-audit",
        help="Run AI OS context hygiene and setup audit.",
        description="Comprehensive auditor for vault links, skill budgets, rule clashes, and memory health.",
    )
    os_audit_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
    )
    os_audit_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
    )
    os_audit_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help="Runtime root. Defaults to CIAO_RUNTIME_ROOT or <workspace>/.runtime.",
    )
    os_audit_parser.add_argument(
        "--workspace-name",
        default="",
        help=(
            "Logical workspace to scope per-workspace evidence to (its MEMORY.md "
            "and proposal queue). Defaults to CIAO_ACTIVE_WORKSPACE; empty audits "
            "every workspace. Note --workspace is a filesystem path, not this."
        ),
    )
    os_audit_parser.add_argument(
        "--scope",
        choices=["all", "workspace", "global"],
        default="all",
        help=(
            "Which half of the report to compute. 'workspace' drops the sections "
            "whose subject is the global runtime directory (background "
            "automation, upgrade actions); 'global' drops the ones describing a "
            "single workspace. The per-workspace hygiene routine passes "
            "'workspace' so N runs stop reporting the same global findings N "
            "times."
        ),
    )
    os_audit_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON audit report.",
    )
    os_audit_parser.set_defaults(func=_os_audit_command)

    reroot_parser = subparsers.add_parser(
        "workspace-reroot",
        help="Plan or apply the per-workspace agent-root migration.",
        description=(
            "Move each registered workspace's vault into its own agent root. "
            "Run --apply only from the engine that will SERVE the install, with "
            "the app stopped: an older engine has no per-root vault resolution "
            "and boots with no vault at all. "
            "Prints the plan by default and changes nothing; --rehearse records a "
            "receipt without moving; --apply performs the migration, moves the "
            "skill catalog to the primary root with a blank triage sheet, and "
            "rebuilds each root's index and search database, and flags every open "
            "chat for a context handover; --repair reconciles "
            "an already-migrated install back to the registry; --undo restores "
            "the layout exactly, leaving only the rebuilt per-root index and "
            "vocabulary behind for git status to report."
        ),
    )
    reroot_parser.add_argument("--workspace", type=Path, default=None, help="Install root.")
    reroot_parser.add_argument("--vault-root", type=Path, default=None, help="Vault root.")
    reroot_parser.add_argument("--rehearse", action="store_true", help="Record a survey receipt, move nothing.")
    reroot_parser.add_argument("--apply", action="store_true", help="Perform the migration.")
    reroot_parser.add_argument("--undo", action="store_true", help="Reverse a completed migration.")
    reroot_parser.add_argument(
        "--repair",
        action="store_true",
        help=(
            "Reconcile the filesystem to the registry after a completed "
            "migration: missing roots, an unlinked AGENTS.md, un-mirrored "
            "skills, a prefixed INDEX.md, a search index at moved paths. "
            "Idempotent. A root with no vault and a stale .mcp.json are reported, "
            "never guessed, and make it exit 1."
        ),
    )
    reroot_parser.add_argument(
        "--mark-migrated",
        action="store_true",
        help=(
            "Record that this install is ALREADY in the per-workspace layout, "
            "without moving anything. For a vault migrated by hand: `agent_root` "
            "answers per-root only when a receipt says so, so without this the "
            "install keeps resolving the old layout and --repair refuses. "
            "docs/VAULT_MIGRATION_PROMPT.md is the reader for this flag and for "
            "what to do when it refuses. Verifies the layout is actually in "
            "place first and refuses if it is not."
        ),
    )
    reroot_parser.set_defaults(func=_workspace_reroot_command)

    relocate_parser = subparsers.add_parser(
        "vault-relocate",
        help="Move one workspace's vault to its standard folder.",
        description=(
            "Fix one workspace whose vault sits at a non-standard path — the "
            "case the 'vault is not in its standard folder' housekeeping card "
            "flags — by moving it to its standard location and repointing the "
            "registry. Prints the plan by default and changes nothing; --apply "
            "performs the move; --undo reverses the last completed relocation "
            "from its receipt. Distinct from workspace-reroot, which migrates "
            "every registered workspace into its own agent root at once — this "
            "touches only the named workspace."
        ),
    )
    relocate_parser.add_argument("name", help="The registered workspace name.")
    relocate_parser.add_argument("--workspace", type=Path, default=None, help="Install root.")
    relocate_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help=(
            "Runtime root holding the relocation receipt. Defaults to "
            "CIAO_RUNTIME_ROOT or <workspace>/.runtime."
        ),
    )
    relocate_parser.add_argument("--apply", action="store_true", help="Perform the move.")
    relocate_parser.add_argument("--undo", action="store_true", help="Reverse a completed relocation.")
    relocate_parser.add_argument("--json", action="store_true", help="Output the raw result as JSON.")
    relocate_parser.set_defaults(func=_vault_relocate_command)

    census_parser = subparsers.add_parser(
        "workspace-census",
        help="Survey a vault root into migration fixture shapes.",
        description=(
            "Read-only census of a vault root: note and non-markdown counts per "
            "top-level directory, symlinks, max depth, duplicate stems, "
            "frontmatter-less notes, and registered vs unregistered directories."
        ),
    )
    census_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    census_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON census report.",
    )
    census_parser.set_defaults(func=_workspace_census_command)

    memory_audit_parser = subparsers.add_parser(
        "memory-audit",
        help="Audit bounded memory for rot (events stored as state, dead paths).",
        description=(
            "Reads the ciao:memory and ciao:profile regions of the workspace "
            "guide and reports entries that record a chat event instead of "
            "current state, entries citing a path that no longer exists, and "
            "subjects carrying more than one value. With --with-vault, also "
            "reports vault notes whose facts have gone unverified past their "
            "type's horizon. Read-only. Exit 0 when clean, 1 when there are "
            "findings, 2 when a region could not be read."
        ),
    )
    memory_audit_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
    )
    memory_audit_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
    )
    memory_audit_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON audit report.",
    )
    memory_audit_parser.add_argument(
        "--with-vault",
        action="store_true",
        help=(
            "Also age the vault's notes (frontmatter `updated:` or mtime "
            "against per-type horizons) and report stale ones. Informational: "
            "they never change the exit code."
        ),
    )
    memory_audit_parser.set_defaults(func=_memory_audit_command)

    memory_proposals_parser = subparsers.add_parser(
        "memory-proposals",
        help="List pending memory proposals in a workspace's review queue.",
        description=(
            "Lists the reviewable memory proposals produced from archived "
            "chats. Read-only. Each pending bullet is emitted with its kind, "
            "text, and source. Decide each item (promote via a region Edit, "
            "or dismiss with `ciao memory-proposal-dismiss --text-file <file>`), keeping "
            "the queue clean so the nightly curator has real signal to work "
            "with."
        ),
    )
    memory_proposals_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
    )
    memory_proposals_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
    )
    memory_proposals_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured rows as JSON instead of text.",
    )
    memory_proposals_parser.set_defaults(func=_memory_proposals_command)

    memory_proposal_add_parser = subparsers.add_parser(
        "memory-proposal-add",
        help="File a fact into a workspace's memory-proposal review queue.",
        description=(
            "Appends one destination-addressed fact to a workspace's "
            "`Workspace/Memory-Proposals.md`. This is how the nightly curator "
            "queues a durable fact it discovered by reading a chat that never "
            "grew a session-insights section, so the fact becomes reviewable "
            "and promotable like any other queue row. Re-filing "
            "identical text is a no-op."
        ),
    )
    memory_proposal_add_parser.add_argument(
        "text",
        nargs="?",
        default="",
        help="The durable fact to queue. Omit when --text-file supplies it.",
    )
    memory_proposal_add_parser.add_argument(
        "--payload-file",
        default="",
        help=(
            "Read --payload from this file. The payload is a document path or a "
            "person's name, both user-controlled, so it needs the same door out "
            "of argv that --text-file gives the fact."
        ),
    )
    memory_proposal_add_parser.add_argument(
        "--text-file",
        default="",
        help=(
            "Read the fact verbatim from this file instead of the positional "
            "text. Transcript-derived facts routinely contain $(), backticks, "
            "or quotes; writing them to a file first keeps the shell from "
            "interpreting them before the CLI sees them."
        ),
    )
    memory_proposal_add_parser.add_argument(
        "--kind",
        default="memory",
        help=(
            "Destination kind: memory, profile, project, people, learnings, "
            "review. Defaults to memory."
        ),
    )
    memory_proposal_add_parser.add_argument(
        "--payload",
        default="",
        help=(
            "Kind payload: person name for people, doc path for project. "
            "Required for those two kinds."
        ),
    )
    memory_proposal_add_parser.add_argument(
        "--source",
        default="curation",
        help="Provenance label recorded on the bullet. Defaults to curation.",
    )
    memory_proposal_add_parser.add_argument(
        "--request",
        default="",
        help=(
            "The user-request identifier this fact was asked for under. "
            "Learnings only: it is what the accepted line cites as `req:<id>` "
            "when the `/remember` has no archived turn behind it, so the "
            "sighting keeps a real origin instead of a manufactured transcript "
            "one. Plain letters, digits, dot, dash and underscore."
        ),
    )
    memory_proposal_add_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
    )
    memory_proposal_add_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
    )
    memory_proposal_add_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured result as JSON instead of text.",
    )
    memory_proposal_add_parser.add_argument(
        "--allow-dismissed",
        action="store_true",
        help="Re-file a previously dismissed fact for review; promoted facts remain deduped.",
    )
    memory_proposal_add_parser.set_defaults(func=_memory_proposal_add_command)

    memory_proposal_dismiss_parser = subparsers.add_parser(
        "memory-proposal-dismiss",
        help="Dismiss one memory proposal from the review queue.",
        description=(
            "Removes one pending memory proposal from a workspace's queue, "
            "matched by a unique text substring. Removing is a review "
            "decision, never a memory write: promote into a bounded region "
            "with an explicit Edit first, then dismiss here so the queue "
            "stops re-asking."
        ),
    )
    memory_proposal_dismiss_parser.add_argument(
        "text",
        nargs="?",
        default="",
        help="Proposal text or unique substring. Omit when --text-file supplies it.",
    )
    memory_proposal_dismiss_parser.add_argument(
        "--text-file",
        default="",
        help=(
            "Read the substring from this file instead of the argument. Use it "
            "whenever the text is not known to be free of shell metacharacters "
            "-- the same reason `memory-proposal-add` has one."
        ),
    )
    memory_proposal_dismiss_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
    )
    memory_proposal_dismiss_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
    )
    memory_proposal_dismiss_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured result as JSON instead of text.",
    )
    memory_proposal_dismiss_parser.add_argument(
        "--promoted",
        action="store_true",
        help=(
            "The fact was already filed into its destination (the "
            "promote-then-dismiss flow); record the outcome as promoted "
            "instead of dismissed."
        ),
    )
    memory_proposal_dismiss_parser.set_defaults(func=_memory_proposal_dismiss_command)

    curation_plan_parser = subparsers.add_parser(
        "curation-plan",
        help="Show what tonight's Workspace care would do, without starting it.",
        description=(
            "Computes the nightly curation worklist from files alone — pending "
            "proposals, region usage, aging entries, learnings, the weekly "
            "marker, log sizes, skill proposals — and prints it with the run "
            "budget applied. Read-only: takes no lease and changes nothing. "
            "Exit 0 always; read `empty` to tell a quiet night from a busy one."
        ),
    )
    _add_curation_arguments(curation_plan_parser)
    curation_plan_parser.set_defaults(func=_curation_plan_command)

    curation_begin_parser = subparsers.add_parser(
        "curation-begin",
        help="Take the curation lease and print this run's planned worklist.",
        description=(
            "Serializes the nightly run: one curation run per vault at a time, "
            "and the run that holds it is mid-consolidation while the lease is "
            "held. Prints the same plan as `curation-plan`. Exit 0 when the "
            "lease was taken, 75 when another run holds it (do not curate), "
            "and 0 with `\"empty\": true` when there is nothing to do — the "
            "lease is released again in that case. Pass the reported "
            "`lease.holder` to every `curation-progress` and `curation-end` "
            "of this run; they require it and refuse to act for another owner."
        ),
    )
    _add_curation_arguments(curation_begin_parser)
    curation_begin_parser.add_argument(
        "--holder",
        default="",
        help="Label recorded as the lease owner. Defaults to host:pid.",
    )
    curation_begin_parser.set_defaults(func=_curation_begin_command)

    curation_progress_parser = subparsers.add_parser(
        "curation-progress",
        help="Record finished curation items so the next run does not redo them.",
        description=(
            "Marks worklist keys done and renews the lease. Keys come from "
            "`curation-begin`'s output. A budget-limited run records what it "
            "finished, so the next run resumes at the remainder instead of "
            "starting again at the top of pass 1."
        ),
    )
    _add_curation_arguments(curation_progress_parser)
    curation_progress_parser.add_argument(
        "--key",
        action="append",
        default=[],
        help="A worklist key from `curation-begin`. Repeatable.",
    )
    curation_progress_parser.add_argument(
        "--holder",
        required=True,
        help="Required: the `lease.holder` value `curation-begin` returned.",
    )
    curation_progress_parser.set_defaults(func=_curation_progress_command)

    curation_end_parser = subparsers.add_parser(
        "curation-end",
        help="Release the curation lease and record the run's counts.",
        description=(
            "Releases the lease, records planned/completed/deferred counts and "
            "reasons so a no-op run is distinguishable from skipped or failed "
            "work, and — only when both weekly checks are recorded done and "
            "the status is ok — stamps `last_full_pass`. An unreliable weekly "
            "pass stays due."
        ),
    )
    _add_curation_arguments(curation_end_parser)
    curation_end_parser.add_argument(
        "--status",
        choices=["ok", "failed"],
        default="ok",
        help="Whether the run completed its planned work reliably.",
    )
    curation_end_parser.add_argument(
        "--planned", type=int, default=0, help="Items this run planned to do."
    )
    curation_end_parser.add_argument(
        "--completed", type=int, default=0, help="Items this run actually finished."
    )
    curation_end_parser.add_argument(
        "--reason",
        action="append",
        default=[],
        help="Why work was deferred or failed. Repeatable.",
    )
    curation_end_parser.add_argument(
        "--holder",
        required=True,
        help="Required: the `lease.holder` value `curation-begin` returned.",
    )
    curation_end_parser.set_defaults(func=_curation_end_command)

    skill_proposal_add_parser = subparsers.add_parser(
        "skill-proposal-add",
        help="File one supported skill-improvement proposal in the review queue.",
        description=(
            "Merges one finding into a workspace's Workspace/Skill-Proposals/ "
            "record for a skill, through the same validated writer the review "
            "surface and the weekly pass use. The target is resolved, not "
            "trusted: only a source this workspace owns under its own `skills/` "
            "directory is accepted, so an installed stock copy, a provider "
            "mirror, a shared source and an unknown name are refused. Nothing "
            "is edited — this files a proposal for a person to review, and "
            "`ciao skill-proposal-remove` is what settles one.\n\n"
            "--input-file holds a JSON object with `title`, `problem`, `change`, "
            "`rationale` and a non-empty `sources` list, each entry carrying "
            "`chat_id`, `archive`, `turn` and a short verbatim `excerpt`. The "
            "finding is read from a file because every field is text from a "
            "conversation, and `$()`, backticks or quotes in a shell argument "
            "would run or mangle."
        ),
    )
    skill_proposal_add_parser.add_argument(
        "skill",
        help="The owned skill this proposal is about. Resolved, so a name the workspace cannot edit is refused.",
    )
    skill_proposal_add_parser.add_argument(
        "--input-file",
        required=True,
        help=(
            "Read the finding from this file as JSON. Never pass the finding "
            "as an argument: it is conversation-derived prose."
        ),
    )
    skill_proposal_add_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
    )
    skill_proposal_add_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
    )
    skill_proposal_add_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured result as JSON instead of text.",
    )
    skill_proposal_add_parser.set_defaults(func=_skill_proposal_add_command)

    def _add_queue_workspace_arguments(parser: argparse.ArgumentParser) -> None:
        """The workspace/vault pair every review-queue command resolves the same way.

        One helper, because :func:`_proposal_config` reads both of these and two
        commands spelling them differently is how a CLI write lands in a
        different vault than the surface that reads it.
        """
        parser.add_argument(
            "--workspace",
            type=Path,
            default=None,
            help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
        )
        parser.add_argument(
            "--vault-root",
            type=Path,
            default=None,
            help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the structured result as JSON instead of text.",
        )

    skill_draft_add_parser = subparsers.add_parser(
        "skill-draft-add",
        help="File one [review] draft for a lesson this workspace cannot edit in place.",
        description=(
            "Files one deduplicated [review] draft in a workspace's review queue "
            "for a reusable lesson whose target is not a source this workspace "
            "owns. `target` is `upstream_issue` for a packaged, mirrored or "
            "shared skill — the change belongs to whoever maintains it — or "
            "`new_skill` for a workflow no existing skill covers.\n\n"
            "This files and nothing more: no issue is opened and no skill is "
            "created. `ciao skill-draft-approve` is the attended step, and it "
            "searches GitHub for a matching issue before it creates one.\n\n"
            "--input-file holds a JSON object with `target`, `skill`, `title` and "
            "`change`, plus for an issue a `body` — the sanitized lesson a "
            "stranger could reproduce — and optionally `repository`, `version`, "
            "private_evidence` and `origins`. `title`, `skill` and `version` reach "
            "`gh` too, so they go through the same gate as the body: any of them "
            "carrying a transcript excerpt, a path, a name or a credential is "
            "refused. `repository` must be `owner/name` when given, and an issue "
            "whose draft names none stays pending rather than guessing a public "
            "target. The private evidence stays in `private_evidence` and never "
            "leaves the vault."
        ),
    )
    skill_draft_add_parser.add_argument(
        "--input-file",
        required=True,
        help=(
            "Read the draft from this file as JSON. Never pass the draft as an "
            "argument: it is lesson-derived prose."
        ),
    )
    _add_queue_workspace_arguments(skill_draft_add_parser)
    skill_draft_add_parser.set_defaults(func=_skill_draft_add_command)

    skill_draft_approve_parser = subparsers.add_parser(
        "skill-draft-approve",
        help="Act on an approved skill draft: file its issue, or create its skill.",
        description=(
            "Approves one draft in a workspace's queue. An `upstream_issue` draft "
            "is searched for in the repository the draft names, linked to a "
            "matching open issue when the search finds one and filed as a new "
            "issue when it does not; the URL is recorded on the record either "
            "way. A `new_skill` draft is created under this workspace's own "
            "`skills/` directory — refusing a name that already exists — then read "
            "back, checked and synced, and the row is settled only once the file is "
            "there and the sync finished.\n\n"
            "Attended only, and read from the run rather than from a flag. There is "
            "no unattended mode to turn off: while a run holds this vault's "
            "curation lease the command exits 4 without filing, creating or "
            "settling anything, so the nightly Workspace care run can only report "
            "the draft. Opening a public issue is a deferred action, and creating a "
            "skill writes a file every session of this workspace loads.\n\n"
            "A failed or ambiguous GitHub request, an issue whose draft names no "
            "owning repository, and a sync that did not finish all leave the draft "
            "pending with the reason recorded, and a retry resumes rather than "
            "starting over."
        ),
    )
    skill_draft_approve_parser.add_argument(
        "draft_id",
        help="The draft id `ciao skill-drafts` printed.",
    )
    skill_draft_approve_parser.add_argument(
        "--content-file",
        default="",
        help=(
            "A new-skill draft only: the SKILL.md text to create, read from this "
            "file. Required for `new_skill` and ignored for an upstream issue."
        ),
    )
    skill_draft_approve_parser.add_argument(
        "--reason",
        default="",
        help="Why you approved it, in your own words. Recorded on the draft.",
    )
    _add_queue_workspace_arguments(skill_draft_approve_parser)
    skill_draft_approve_parser.set_defaults(func=_skill_draft_approve_command)

    skill_draft_reject_parser = subparsers.add_parser(
        "skill-draft-reject",
        help="Turn one skill draft down, so it is not offered again.",
        description=(
            "Records an attended decision against one skill draft, takes it out "
            "of the review queue and keeps the record on disk. A rejected change "
            "is not re-filed: the next pass that reaches the same conclusion adds "
            "its evidence to the settled record instead of opening a second row.\n\n"
            "A rejection settles exactly as an approval does, so it is refused the "
            "same way: while a run holds this vault's curation lease the command "
            "exits 4 and the draft stays queued for a person."
        ),
    )
    skill_draft_reject_parser.add_argument(
        "draft_id",
        help="The draft id `ciao skill-drafts` printed.",
    )
    skill_draft_reject_parser.add_argument(
        "--reason",
        default="",
        help="Why you rejected it, in your own words. Recorded on the draft.",
    )
    _add_queue_workspace_arguments(skill_draft_reject_parser)
    skill_draft_reject_parser.set_defaults(func=_skill_draft_reject_command)

    skill_drafts_parser = subparsers.add_parser(
        "skill-drafts",
        help="List a workspace's skill drafts.",
        description=(
            "Lists the [review] drafts a routing pass filed in one workspace: the "
            "upstream-issue and new-skill proposals for lessons this workspace "
            "cannot apply by editing one of its own skills. Open ones by default, "
            "settled ones with --all."
        ),
    )
    skill_drafts_parser.add_argument(
        "--all",
        action="store_true",
        help="Include settled drafts, which stay on disk as decision records.",
    )
    _add_queue_workspace_arguments(skill_drafts_parser)
    skill_drafts_parser.set_defaults(func=_skill_drafts_command)

    skill_proposal_parser = subparsers.add_parser(
        "skill-proposal-remove",
        help="Settle a resolved skill proposal in the review queue.",
        description=(
            "Records the decision for one proposal in a workspace's "
            "Workspace/Skill-Proposals/ and takes it out of the queue, after its "
            "decision is made (implemented, or decided against). The record stays "
            "on disk and readable. NAME matches the proposal's skill or a unique "
            "substring of it."
        ),
    )
    skill_proposal_parser.add_argument(
        "name",
        help="Skill name or unique substring of the queued proposal to settle.",
    )
    skill_proposal_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
    )
    skill_proposal_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
    )
    skill_proposal_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured result as JSON instead of text.",
    )
    skill_proposal_parser.add_argument(
        "--applied",
        action="store_true",
        help=(
            "Record that the change landed and was verified. Recorded as a "
            "promotion, so History reads it as an accept rather than a refusal."
        ),
    )
    skill_proposal_parser.add_argument(
        "--not-applicable",
        action="store_true",
        help=(
            "Record that the finding no longer holds against the current skill, "
            "so the chat implementing it settled it without an edit. A decision, "
            "recorded as a dismissal."
        ),
    )
    skill_proposal_parser.add_argument(
        "--interrupted",
        action="store_true",
        help=(
            "Record that the implementation stopped part-way. Not a decision: the "
            "proposal stays queued with its chat bound to it, so the work is "
            "recoverable. Needs an accepted proposal."
        ),
    )
    skill_proposal_parser.add_argument(
        "--reason",
        default="",
        help="Free-text note recorded with the outcome (the History 'outcome' field).",
    )
    skill_proposal_parser.add_argument(
        "--verification",
        default="",
        help=(
            "What proves the lesson is in the skill: a managed write receipt id, or "
            "the readback you recorded of the file. Required by --applied when the "
            "proposal links a learning, because a finished chat and a row leaving "
            "the queue are not evidence that anything landed."
        ),
    )
    skill_proposal_parser.add_argument(
        "--learning-id",
        default="",
        help=(
            "Settle only the finding linked to this learning id, leaving the "
            "proposal's other findings queued. Implies a per-finding settlement."
        ),
    )
    skill_proposal_parser.add_argument(
        "--finding",
        default="",
        help=(
            "Narrow --learning-id to one finding. Needs --learning-id: a finding "
            "text on its own names nothing."
        ),
    )
    skill_proposal_parser.set_defaults(func=_skill_proposal_remove_command)

    chat_parser = subparsers.add_parser(
        "create-chat",
        help="Create a chat through the running Ciaobot server and send an initial prompt.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    chat_parser.add_argument("--prompt", required=True, help="Initial prompt.")
    chat_parser.add_argument("--title", help="Chat title. Defaults to 'New Chat'.")
    chat_parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path("."),
        help="Workspace directory containing .env.",
    )
    chat_parser.add_argument(
        "--workspace",
        help="Logical chat workspace. Inherits CIAO_ACTIVE_WORKSPACE.",
    )
    chat_parser.add_argument("--project", help="Project ID or case-insensitive name.")
    chat_parser.add_argument("--model", help="Model override. Inherits CIAO_MODEL.")
    chat_parser.add_argument(
        "--provider",
        choices=list(_runtime_provider_choices()),
        help="Provider override. Inherits CIAO_PROVIDER.",
    )
    chat_parser.add_argument(
        "--base-url",
        help="Ciaobot server URL. Defaults to PWA_HOST/PWA_PORT.",
    )
    chat_parser.set_defaults(func=_create_chat_command)

    cleanup_parser = subparsers.add_parser(
        "cleanup-sdk-blobs",
        help="Dry-run or delete archived Claude SDK JSONL blobs.",
    )
    cleanup_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace root. Defaults to current directory.",
    )
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete matching blobs. Default is dry-run.",
    )
    cleanup_parser.set_defaults(func=_cleanup_sdk_blobs_command)

    label_hygiene_parser = subparsers.add_parser(
        "label-hygiene",
        help="Audit open-issue labels against the title-prefix convention. Dry-run by default.",
    )
    label_hygiene_parser.add_argument(
        "--repo",
        default="raffaelefarinaro/ciaobot",
        help="Target GitHub repo (owner/name).",
    )
    label_hygiene_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum open issues to scan.",
    )
    label_hygiene_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually add missing labels via gh issue edit. Default is dry-run.",
    )
    label_hygiene_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured report as JSON instead of text.",
    )
    label_hygiene_parser.set_defaults(func=_label_hygiene_command)

    eval_parser = subparsers.add_parser(
        "eval",
        help=(
            "Versioned behavioral evaluations for prompts, providers, and "
            "guides. 'contracts' is deterministic CI; 'run' is boundedly "
            "model-backed."
        ),
    )
    eval_sub = eval_parser.add_subparsers(dest="eval_action", required=True)
    eval_contracts = eval_sub.add_parser(
        "contracts",
        help="Run the deterministic, model-free guard checks over the scenario catalog.",
    )
    eval_contracts.add_argument(
        "--json", action="store_true", help="Emit the structured report as JSON."
    )
    eval_contracts.set_defaults(func=_eval_command)

    eval_run = eval_sub.add_parser(
        "run",
        help="Run the bounded model-backed probe (explicit; enforces a call/cost ceiling).",
    )
    eval_run.add_argument("--provider", default="claude", choices=["claude", "opencode"])
    eval_run.add_argument(
        "--model",
        required=True,
        help="Provider model id to evaluate. Required; the runner never defaults it.",
    )
    eval_run.add_argument(
        "--label",
        default="candidate",
        help="Report label (baseline or candidate). Recorded with provenance.",
    )
    eval_run.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Runs per scenario; >1 exposes model noise. Report shows mean and stdev.",
    )
    eval_run.add_argument(
        "--concurrency", type=int, default=1, help="Concurrent scenario probes."
    )
    eval_run.add_argument(
        "--include",
        default="",
        help="Comma-separated scenario ids to run (default: the whole catalog).",
    )
    eval_run.add_argument(
        "--max-calls", type=int, default=None,
        help="Hard call ceiling for this run (default: CIAO_EVAL_MAX_CALLS or 40).",
    )
    eval_run.add_argument(
        "--max-cost-usd", type=float, default=None,
        help="Hard estimated-cost ceiling in USD (default: CIAO_EVAL_MAX_COST_USD or 2.00).",
    )
    eval_run.add_argument(
        "--cost-per-call", type=float, default=None,
        help="Declared per-call upper-bound cost used to enforce --max-cost-usd.",
    )
    eval_run.add_argument(
        "--timeout", type=float, default=120.0, help="Per-call timeout in seconds."
    )
    eval_run.add_argument(
        "--out", type=Path, default=None, help="Report path (default: .runtime/evals/...)."
    )
    eval_run.add_argument(
        "--json", action="store_true", help="Emit the structured report as JSON."
    )
    eval_run.add_argument(
        "--catalog-file", type=Path, default=None,
        help=(
            "Render this file's text as the agent surface instead of the MCP tool "
            "list (the MCP-versus-CLI comparison). Recorded in provenance."
        ),
    )
    eval_run.add_argument(
        "--core-prompt-file", type=Path, default=None,
        help="Swap the shipped system_prompt.md text for this file's text in the probe.",
    )
    eval_run.set_defaults(func=_eval_command)

    eval_compare = eval_sub.add_parser(
        "compare",
        help="Diff a baseline and a candidate report on provenance and quality.",
    )
    eval_compare.add_argument("--baseline", type=Path, required=True)
    eval_compare.add_argument("--candidate", type=Path, required=True)
    eval_compare.add_argument(
        "--json", action="store_true", help="Emit the structured comparison as JSON."
    )
    eval_compare.set_defaults(func=_eval_command)

    skills_parser = subparsers.add_parser(
        "skills",
        help="Inspect skills (stock, custom, installed) with provider availability.",
    )
    skills_sub = skills_parser.add_subparsers(dest="action", required=True)
    skills_list_parser = skills_sub.add_parser(
        "list", help="List skills as JSON (former skills_list MCP tool)."
    )
    skills_list_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace root. Defaults to current directory.",
    )
    skills_list_parser.set_defaults(func=_skills_list_command)

    health_parser = subparsers.add_parser(
        "health",
        help="Check or repair canonical agent assets and provider mirrors.",
    )
    health_sub = health_parser.add_subparsers(dest="action", required=True)
    health_get_parser = health_sub.add_parser(
        "get", help="Report workspace health as JSON (former workspace_health_get)."
    )
    health_get_parser.set_defaults(func=_health_command, action="get")
    health_fix_parser = health_sub.add_parser(
        "fix", help="Repair scaffolding and provider mirrors (former workspace_health_fix)."
    )
    health_fix_parser.set_defaults(func=_health_command, action="fix")

    sync_skills_parser = subparsers.add_parser(
        "sync-skills",
        help="Install and mirror local skills, commands, and agents.",
    )
    sync_skills_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace root. Defaults to current directory.",
    )
    sync_skills_parser.add_argument(
        "--skip-upstream",
        action="store_true",
        help="Deprecated: no-op. Skills are local folders under skills/.",
    )
    sync_skills_parser.add_argument("--verbose", action="store_true")
    sync_skills_parser.set_defaults(func=_sync_skills_command)

    scaffold_parser = subparsers.add_parser(
        "scaffold",
        help="Scaffold a new subagent package.",
    )
    scaffold_parser.add_argument(
        "type",
        choices=["subagent"],
        help="Type of asset to scaffold.",
    )
    scaffold_parser.add_argument(
        "name",
        help="Name of the asset.",
    )
    scaffold_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace root. Defaults to current directory.",
    )
    scaffold_parser.set_defaults(func=_scaffold_command)

    # Help-only stub: `main()` intercepts `gws` before build_parser() runs, so
    # this parser is never parsed against. It exists so `ciao --help` still
    # lists the subcommand; the interception is what makes `ciao gws --version`
    # reach the real CLI instead of being eaten by argparse. Do not give it a
    # `func` — nothing can dispatch through it.
    subparsers.add_parser(
        "gws",
        help="Profile-aware passthrough to the gws CLI (replaces scripts/gws-profile.sh).",
        add_help=False,
    )

    gws_helper_parser = subparsers.add_parser(
        "gws-auth-helper",
        help="Interactive headless GWS OAuth re-auth (replaces scripts/gws-auth-helper.py).",
    )
    gws_helper_parser.add_argument("profile", help="GWS profile (Google account name) to authenticate")
    gws_helper_parser.add_argument(
        "--redirect-url",
        help="Full redirect URL from the browser; skip the interactive prompt.",
    )
    gws_helper_parser.add_argument(
        "--scopes",
        help="Space-separated OAuth scopes to request instead of the full default set.",
    )
    gws_helper_parser.set_defaults(func=_gws_auth_helper_command)

    return parser


def _scaffold_command(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    target_type = args.type
    name = args.name

    if target_type == "subagent":
        from ciao.subagent_loader import scaffold_subagent
        folder = scaffold_subagent(workspace, name)
        print(f"Scaffolded subagent package at {folder}")
    else:
        print(f"Unknown scaffold target type: {target_type}", file=sys.stderr)
        return 1
    return 0


def _gws_auth_helper_command(args: argparse.Namespace) -> int:
    from ciao import gws_auth_helper

    argv = [args.profile]
    if getattr(args, "redirect_url", None):
        argv += ["--redirect-url", args.redirect_url]
    if getattr(args, "scopes", None):
        argv += ["--scopes", args.scopes]
    return gws_auth_helper.main_entry(argv)


def _resolve_critique_paths(args: list[str]) -> list[str]:
    """Make a relative ``--input`` absolute against the caller's directory.

    The bundled launcher `cd`s into the runtime root before exec'ing Python, so
    a relative path — including the `memory-vault/...` form the command doc
    explicitly supports — would resolve against the app bundle and be reported
    missing. The launcher records where
    the caller actually stood in ``CIAO_INVOCATION_CWD``; from source there is
    no cd and the current directory is already right.
    """
    base = os.environ.get("CIAO_INVOCATION_CWD", "").strip()
    if not base:
        return args
    out = list(args)
    for index, token in enumerate(out):
        value = ""
        if token == "--input" and index + 1 < len(out):
            target = index + 1
            value = out[target]
        elif token.startswith("--input="):
            target = index
            value = token.split("=", 1)[1]
        else:
            continue
        # A tilde path is never relative to the caller's directory. Quoted, it
        # reaches here unexpanded and `Path("~/x").is_absolute()` is False, so
        # rebasing would produce `<cwd>/~/x` and defeat the `expanduser()` that
        # `ciao.critique` does later — turning an artifact that resolved fine
        # into one reported missing. Left alone, that expansion still works.
        if not value or value.startswith("~") or Path(value).is_absolute():
            continue
        resolved = str(Path(base) / value)
        out[target] = resolved if target != index else f"--input={resolved}"
    return out


def main(argv: list[str] | None = None) -> int:
    use_utf8_stdio()
    os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    os.environ.setdefault("CLAUDE_CODE_DISABLE_ARTIFACT", "1")
    argv_list = list(sys.argv[1:] if argv is None else argv)
    # The agent surface (`ciao memory …`, `ciao vault search …`): dispatch
    # before building the operator parser so a call from a chat costs
    # interpreter start-up, not this module's import graph. ``ciao.agent_cli``
    # owns which words route there (``ciao run`` alone stays the server).
    from ciao.agent_cli import is_agent_invocation, main as agent_main

    if is_agent_invocation(argv_list):
        return agent_main(argv_list)
    if argv_list[:1] == ["public-preflight"]:
        return public_release.main(argv_list[1:])
    if argv_list[:1] == ["package-smoke"]:
        return package_smoke.main(argv_list[1:])
    if argv_list[:1] == ["prepare-release"]:
        return release.main(argv_list[1:])
    if argv_list[:1] == ["update"]:
        from ciao.engine_update import main as update_main

        return update_main(argv_list[1:])
    if argv_list[:1] == ["critique"]:
        from ciao.critique import main as critique_main

        return critique_main(_resolve_critique_paths(argv_list[1:]))
    if argv_list[:1] == ["gws"]:
        # Passthrough: forward everything (including leading gws options such as
        # `--version`) untouched, keeping argparse out of the way.
        return gws_wrapper.main(argv_list[1:])
    parser = build_parser()
    args = parser.parse_args(argv_list)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
