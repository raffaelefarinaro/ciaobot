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
from importlib import resources
from pathlib import Path
from typing import Any, cast
import urllib.error
import urllib.request

from ciao import dev, gws_wrapper, package_smoke, public_release, release
from ciao.setup_status import detect_nested_workspaces
from ciao.macos_service import default_launch_agents_dir

_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _workspace_name_arg(value: str) -> str:
    name = value.strip()
    if not _WORKSPACE_NAME_RE.fullmatch(name):
        raise argparse.ArgumentTypeError(
            "workspace name must use letters, numbers, dashes, or underscores"
        )
    return name


def _restart_exit_code() -> int:
    """The exit code the server uses to request a restart (config default 75).

    Read from the environment after the server ran: ``CiaoConfig.from_env``
    loads the workspace ``.env`` into ``os.environ``, so an override set there
    is visible here too.
    """
    raw = (
        os.environ.get("CIAO_RESTART_EXIT_CODE", "").strip()
        or "75"
    )
    try:
        return int(raw)
    except ValueError:
        return 75


def _relaunch_argv() -> list[str]:
    """argv for re-execing the CLI: a fresh interpreter picks up new code
    after a package update."""
    return [sys.executable, "-m", "ciao.cli", *sys.argv[1:]]


def _run_server() -> int:
    from ciao.main import main as server_main

    try:
        server_main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    else:
        code = 0
    if code == _restart_exit_code():
        # The setup wizard and package updates request a restart by exiting
        # with this code. Under launchd KeepAlive relaunches us anyway, but a
        # foreground `ciao run` would just die and leave the site unreachable.
        # Re-exec (rather than loop) so the relaunch picks up new code.
        print("Restart requested — relaunching Ciaobot…", file=sys.stderr)
        sys.stderr.flush()
        os.execv(sys.executable, _relaunch_argv())
    return code


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


def _write_if_missing(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding="utf-8")


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


def _bundled_sidecar_path(executable: str) -> str:
    """Return the sidecar beside a bundled engine, when one is identifiable."""

    executable_path = Path(executable).expanduser()
    for ancestor in (executable_path, *executable_path.parents):
        if ancestor.suffix == ".app":
            return str(ancestor / "Contents" / "MacOS" / "ciaobot-native")
    return ""


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
        "{{CIAO_NATIVE_SIDECAR}}": html.escape(
            _bundled_sidecar_path(executable), quote=False
        ),
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
) -> Path:
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
        encoding="utf-8",
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
    path.write_text(token + "\n", encoding="utf-8")
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
    """An ``export PATH=...`` line for the running interpreter's bin dir, or
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
    return f'export PATH="{bin_dir}:$PATH"'


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


def _default_app_dir() -> Path:
    """Return the per-user app directory used by the release installer."""

    return Path.home() / "Applications"


_OUR_BUNDLE_IDS = ("local.ciao.app", "local.ciaobot.app")
# Launcher bundles previous versions wrote. Nothing creates these any more —
# Ciaobot.app is the menu bar — but installs upgrading from an older version
# still have one on disk, so setup removes them. "Ciaobot.app" is in the list
# because the pre-rename launcher used that name; _is_our_app_bundle keeps the
# Tauri app of the same name safe by checking the executable inside.
_LEGACY_APP_BUNDLE_NAMES = (
    "Ciao.app",
    "Ciaobot.app",
    "Ciaobot Menu Bar.app",
    "Ciaobot Server.app",
)
# Executable inside the Tauri desktop app.
_DESKTOP_EXECUTABLE_NAME = "ciaobot-desktop"


def _is_our_app_bundle(app_root: Path) -> bool:
    """Whether ``app_root`` is a launcher bundle created by Ciaobot.

    The Tauri desktop app ships as ``Ciaobot.app`` under the same
    ``local.ciaobot.app`` identifier our pre-rename launcher used, so the
    bundle id cannot tell them apart. Misidentifying it is destructive rather
    than merely wasteful: the launcher we write is named ``Ciaobot Server.app``,
    so ``_remove_legacy_app_shortcuts`` must never delete the native app and
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

    real_launch_dir = Path.home() / "Library" / "LaunchAgents"
    launch_dir = (launch_agents_dir or default_launch_agents_dir()).expanduser()
    plist_path = launch_dir / "com.ciao.menubar.plist"
    if not plist_path.exists():
        return False

    if sys.platform == "darwin" and launch_dir == real_launch_dir:
        label = f"gui/{os.getuid()}/com.ciao.menubar"
        try:
            subprocess.run(
                ["launchctl", "bootout", label], check=False, capture_output=True
            )
        except OSError:
            pass
    try:
        plist_path.unlink()
    except OSError:
        return False
    return True


_WORKSPACE_GITIGNORE_ENTRIES = (
    ".env",
    ".runtime/",
    ".claude/",
    ".agents/",
    ".codex/",
    ".opencode/",
    "opencode.json",
    "*.log",
)


def _ensure_workspace_gitignore(root: Path) -> None:
    """Make sure `git add -A` snapshots never pick up secrets or runtime state."""
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [e for e in _WORKSPACE_GITIGNORE_ENTRIES if e not in present]
    if not missing:
        return
    if existing:
        text = existing if existing.endswith("\n") else existing + "\n"
    else:
        text = "# Ciaobot: keep secrets and runtime state out of git snapshots\n"
    gitignore.write_text(text + "\n".join(missing) + "\n", encoding="utf-8")


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
        capture_output=True, text=True,
    )
    if probe.returncode == 0 and probe.stdout.strip() == "true":
        return
    init = subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        capture_output=True, text=True,
    )
    if init.returncode != 0:
        print(f"git init failed for {root}: {init.stderr.strip()}", file=sys.stderr)
        return
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"],
        capture_output=True, text=True,
    )
    commit = subprocess.run(
        [
            "git", "-C", str(root),
            "-c", "user.name=Ciaobot", "-c", "user.email=ciaobot@localhost",
            "commit", "-m", "Initialize Ciaobot workspace",
        ],
        capture_output=True, text=True,
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
    gitignore.write_text(text + "\n".join(missing) + "\n", encoding="utf-8")


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
        capture_output=True, text=True,
    )
    if probe.returncode == 0:
        toplevel = Path(probe.stdout.strip())
        if toplevel == root:
            _ensure_vault_gitignore(root)
        return
    _ensure_vault_gitignore(root)
    init = subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        capture_output=True, text=True,
    )
    if init.returncode != 0:
        print(f"git init failed for {root}: {init.stderr.strip()}", file=sys.stderr)
        return
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"],
        capture_output=True, text=True,
    )
    commit = subprocess.run(
        [
            "git", "-C", str(root),
            "-c", "user.name=Ciaobot", "-c", "user.email=ciaobot@localhost",
            "commit", "-m", "Initialize Ciaobot vault",
        ],
        capture_output=True, text=True,
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


def setup_workspace(
    workspace: Path | str,
    *,
    auth_token: str | None = None,
    auth_required: bool = True,
    push_contact: str | None = None,
    vault_root: Path | str | None = None,
    vault_mode: str = "scratch",
    workspace_name: str | None = None,
    default_provider: str = "claude",
    python_path: str | None = None,
    port: int = 8443,
    launch_agents_dir: Path | str | None = None,
    app_dir: Path | str | None = None,
) -> list[Path]:
    requested_name = (workspace_name or "").strip()
    if workspace_name is not None and not _WORKSPACE_NAME_RE.fullmatch(
        requested_name
    ):
        raise ValueError(
            "workspace name must use letters, numbers, dashes, or underscores"
        )
    root = Path(workspace).expanduser().resolve()
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
    registered_vaults = _setup_registry_vaults(
        workspaces_registry,
        workspace_root=root,
        configured_vault_root=vault_path,
    )
    name = requested_name or "personal"

    token = auth_token or secrets.token_urlsafe(32)
    # Empty contact = Web Push disabled until configured in Settings;
    # never invent a fake default.
    contact = (push_contact or "").strip()
    # Always pin PWA_AUTH_REQUIRED: an unset value is read as "protect when a
    # token exists" (see CiaoConfig.from_env), and a setup that deliberately
    # opted out must survive that default.
    desired_env: list[tuple[str, str]] = [
        ("PWA_AUTH_TOKEN", token),
        ("PWA_AUTH_REQUIRED", "true" if auth_required else "false"),
    ]
    desired_env.extend([
        ("CIAO_PUSH_CONTACT", contact),
        ("CIAO_WORKSPACE", "."),
        ("CIAO_VAULT_ROOT", vault_value),
        ("CIAO_VAULT_MODE", vault_mode),
        ("CIAO_RUNTIME_ROOT", ".runtime"),
    ])
    if not existing_env and not env_path.exists():
        env_path.write_text(
            "\n".join(f"{key}={value}" for key, value in desired_env) + "\n",
            encoding="utf-8",
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
                encoding="utf-8",
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
    stock_commands = stock.joinpath("commands")
    stock_workspace = stock.joinpath("workspace")

    # Canonical user-authored asset sources (mirrored into .claude/ by
    # sync-skills). App plumbing, not vault content: pre-creating them keeps
    # the Workspace Health checks warning-free on a fresh or adopted setup.
    from ciao.config import agent_roots_for
    from ciao.sync_skills import (
        _ensure_linked_workspace_guides,
        _install_stock_agents,
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
        written.extend(_copy_tree_if_missing(stock_commands, asset_root / "commands"))
        written.append(asset_root / "commands")
        written.extend(_copy_tree_if_missing(stock_workspace, asset_root))
        _ensure_linked_workspace_guides(asset_root)
        # Build the generated catalogs too, so setup leaves a HEALTHY install
        # rather than one that only becomes healthy after its first boot. Without
        # this a brand-new install showed nine Workspace Health warnings and an
        # operator tile about missing assets, on a install where nothing was
        # wrong — it just had not synced yet. Local only: no upstream refresh, so
        # setup still does not touch the network.
        try:
            sync_workspace_skills(
                asset_root,
                refresh_upstream=False,
                workspace_name=_name or None,
            )
        except Exception as exc:  # noqa: BLE001 — a scaffold step, never fatal
            print(f"skill sync failed for {asset_root}: {exc}", file=sys.stderr)

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
                stored_root = str(nested_vault.relative_to(root))
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
            stored_root = str(scaffold_vault_path.relative_to(root))
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

    launch_dir = (
        Path(launch_agents_dir)
        if launch_agents_dir is not None
        else default_launch_agents_dir()
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
    # summary prints it as a login URL, and the Tauri app redeems it on first
    # launch. It used to be created as a side effect of writing the launcher
    # bundle, which no longer exists.
    _ensure_setup_token(root)
    written.append(_write_launchd_plist(
        workspace=root,
        launch_agents_dir=launch_dir,
        engine_path=resolved_engine,
        runtime_root=runtime_root,
        port=port,
        path=os.environ.get("PATH", ""),
        plist_name="com.ciao.server.plist",
    ))
    # Existing installs may still carry the launcher bundle and its agent from
    # a previous version; remove them rather than leaving orphans behind.
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


def _setup_command(args: argparse.Namespace) -> int:
    root = Path(args.workspace).expanduser().resolve()

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
        existing = _plist_workspace(Path(args.launch_agents_dir))
        if existing is not None and existing != root:
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
    written = setup_workspace(
        args.workspace,
        auth_token=args.auth_token,
        auth_required=auth_required,
        push_contact=args.push_contact,
        workspace_name=args.workspace_name,
        python_path=args.python,
        port=args.port,
        launch_agents_dir=args.launch_agents_dir,
        app_dir=args.app_dir,
    )
    for path in written:
        print(path)
    if auth_required and not args.auth_token and not had_token:
        print(
            "\nPassword protection is on. No --auth-token was given, so a random "
            f"password was written to {root / '.env'} (PWA_AUTH_TOKEN).\n"
            "Open Ciaobot with the login URL below and change it in "
            "Settings -> PWA password."
        )
    # One agent now: setup deletes the retired com.ciao.menubar plist rather
    # than writing it, so there is nothing else here to load.
    server_plist = Path(args.launch_agents_dir).expanduser() / "com.ciao.server.plist"
    plists = [server_plist] if server_plist.is_file() else []
    if args.load_launchd:
        rc = 0
        for plist in plists:
            # The unload is a probe: during an install the agent is normally
            # not loaded, and launchctl says so on stderr ("Unload failed: 5:
            # Input/output error"). check=False swallows the status but not the
            # output, and install.sh redirects only stdout - so that expected
            # non-event was the first line a user saw when re-running the
            # installer over a configured workspace, ahead of the success lines.
            subprocess.run(
                ["launchctl", "unload", str(plist)],
                check=False,
                stderr=subprocess.DEVNULL,
            )
            # Keep a real load failure visible to the installer and preserve
            # its status as the setup result.
            rc = subprocess.run(
                ["launchctl", "load", "-w", str(plist)],
                check=False,
            ).returncode or rc
        _print_setup_summary(root, args.port)
        return rc
    for plist in plists:
        print(f"LaunchAgent not loaded. To load it: launchctl load -w {plist}")
    _print_setup_summary(root, args.port)
    return 0


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
    # Keys are relative to the install root, so one database can hold several
    # agent roots each with a vault of the same name.
    key_base = Path(
        os.environ.get("CIAO_WORKSPACE", "").strip() or vault_root.parent
    ).expanduser().resolve()
    # The re-rooting promotes Logs/ out of the vault, so the archive root cannot
    # be derived from the vault root on a migrated install.
    from ciao.config import logs_root_for

    logs_root = logs_root_for(key_base, vault_root, key_base / ".runtime")
    db_path = fts_search.get_db_path()

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
    from ciao.vault_migration import migrate_vault_vocabulary

    vault_root = _resolve_vault_root(args.vault_root)
    if not vault_root.is_dir():
        print(
            f"Vault root is missing or not a directory: `{vault_root}`",
            file=sys.stderr,
        )
        return 1

    summary = migrate_vault_vocabulary(vault_root, apply=args.apply)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1 if summary["unresolved"] or summary["failed"] else 0

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
    # Whether workspaces.json is what `config` actually sourced its workspaces
    # from, per CiaoConfig.from_env's own precedence — read off the SAME
    # merged environment that built `config` (target .env, then ambient env
    # only when --workspace was not explicit), not the raw process
    # environment, which can disagree with it when CIAO_WORKSPACES is set
    # only in the target install's .env.
    registry_authoritative = not effective_source.get("CIAO_WORKSPACES", "").strip()

    if args.name not in set(config.workspace_names()):
        print(f"No registered workspace named '{args.name}'.", file=sys.stderr)
        return 1

    if args.undo:
        result = vault_relocate.undo(
            config, args.name, runtime, registry_authoritative=registry_authoritative
        )
        print(json.dumps(result, indent=2))
        return 0 if result["status"] in {"undone", "nothing_to_undo"} else 1

    plan_result = vault_relocate.plan(config, args.name)

    if args.apply:
        result = vault_relocate.apply(
            config,
            args.name,
            runtime,
            plan_result=plan_result,
            registry_authoritative=registry_authoritative,
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
        # For a vault migrated by hand or by a model. The receipt is what
        # `agent_root` reads, so without it the install keeps resolving the shared
        # layout while the files sit in the new one — the one combination that
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
            print(
                "Refusing: these workspaces have no "
                f"<workspace>/{vault.name} directory yet: {', '.join(missing)}.\n"
                "Move the vaults first (see docs/VAULT_MIGRATION_PROMPT.md), then "
                "re-run this.",
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
                workspace, names, vault_name=leaf
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
            region_limits={"memory": config.memory_char_limit, "profile": config.user_char_limit},
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
            # on type and dates, not workspace.
            entries = scan_vault(vault, workspace="personal")
            report["stale_notes"] = find_stale_notes(
                entries, vault_root=vault, today=datetime.date.today()
            )
            # Decay-by-disuse: mark whether recall has returned each stale
            # note recently. Stale AND unretrieved is the strongest demotion
            # candidate; no evidence at all (no hits log yet) marks nothing.
            from ciao.fts_search import read_search_hit_paths

            hit_paths = read_search_hit_paths(workspace / ".runtime")
            if hit_paths is not None:
                normalized_hits = {hit.replace(os.sep, "/") for hit in hit_paths}
                for finding in report["stale_notes"]["stale_notes"]:
                    rel = str(finding["path"]).replace(os.sep, "/")
                    finding["retrieved_recently"] = any(
                        hit == rel or hit.endswith("/" + rel)
                        for hit in normalized_hits
                    )
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
                'region under its cap"), or raise CIAO_MEMORY_CHAR_LIMIT / '
                "CIAO_USER_CHAR_LIMIT in .env and restart Ciaobot if every "
                "entry is high-signal."
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
    """Shared workspace/vault resolution for the memory-proposal commands.

    A scheduled run exports ``CIAO_ACTIVE_WORKSPACE`` (the logical workspace
    name) next to a ``CIAO_VAULT_ROOT`` that points at the install-wide
    shared vault on layouts that have not re-rooted yet. Appending to that
    raw value would file every workspace's proposals into one stray queue
    the review UI never reads, so an active workspace name is resolved
    through the workspace registry instead — the same authority the PWA's
    ``workspace_vault_root`` reads with. Explicit arguments still win for
    manual invocations.
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
                    return config.workspace_root, Path(config.workspace_vault_root(active))
            except Exception:  # noqa: BLE001 — fall through to the legacy path
                pass
    workspace_raw = args.workspace or os.environ.get("CIAO_WORKSPACE") or Path(".")
    workspace = Path(workspace_raw).expanduser().resolve()
    vault_raw = args.vault_root or os.environ.get("CIAO_VAULT_ROOT") or "memory-vault"
    vault = Path(vault_raw).expanduser()
    if not vault.is_absolute():
        vault = workspace / vault
    return workspace, vault.resolve()


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


def _memory_proposal_add_command(args: argparse.Namespace) -> int:
    """File a fact into a workspace's memory-proposal review queue.

    The nightly curator discovers durable facts by reading archived chats that
    never grew a ``## Session insights`` section, so archive-time routing never
    saw them. Filing here puts the fact in the machine queue (``ciao
    memory-proposals``, the PWA review panel) where it can be promoted or
    dismissed like any queued item, instead of surviving only as prose in one
    nightly report. Re-filing an identical fact is a no-op; the queue dedupes
    by text.
    """
    from ciao.memory_proposals import DESTINATIONS, MemoryProposal, append_proposals

    workspace, vault = _resolve_workspace_and_vault(args)
    text_file = (getattr(args, "text_file", "") or "").strip()
    text = (args.text or "").strip()
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
        except OSError as exc:
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
    payload = args.payload.strip()
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
    proposal = MemoryProposal(
        target=kind,
        text=text,
        source_section=args.source.strip() or "curation",
        payload=payload,
    )
    path = append_proposals([proposal], vault)
    if args.json:
        json.dump(
            {
                "queued": path is not None,
                "duplicate": path is None,
                "path": str(path) if path else None,
                "text": text,
                # argparse supplies a Path when --workspace is explicit, and
                # json.dump cannot serialize one; report the resolved root.
                "workspace": str(workspace),
            },
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
    elif path is None:
        print(f"Already in the queue; nothing added for {text!r}.")
    else:
        print(f"Queued [{kind}] proposal in {path}.")
    return 0


def _memory_proposal_dismiss_command(args: argparse.Namespace) -> int:
    """Dismiss (delete) one memory proposal from the review queue.

    Removing a proposal is a review decision, never a memory write: promotion
    into a ``ciao:memory`` / ``ciao:profile`` region is an explicit ``Edit`` of
    the workspace CLAUDE.md first, then this dismiss removes the resolved item
    so the queue stops re-asking. TEXT matches one proposal by a unique
    substring.
    """
    from ciao import proposal_outcomes
    from ciao.memory_proposals import record_dismissal, remove_proposal_by_substring

    workspace, vault = _resolve_workspace_and_vault(args)
    path = vault / "Workspace" / "Memory-Proposals.md"
    needle = args.text.strip()
    if not needle:
        print("a proposal text or unique substring is required", file=sys.stderr)
        return 2
    removed = remove_proposal_by_substring(path, needle)
    if removed is None:
        print(
            f"No unique memory proposal matched {needle!r} "
            "(the text may be ambiguous or absent).",
            file=sys.stderr,
        )
        return 1
    kind, removed_text = removed
    # Preserve what was decided, not just that something was: append-time
    # dedupe consults this history, so without it the next curator pass that
    # re-reads the same transcript re-files the fact the user just rejected.
    record_dismissal(path, text=removed_text, kind=kind)
    # Pin the outcome log to the same .runtime the server uses before
    # recording: a CLI run from an arbitrary cwd must not scatter events into
    # a .runtime beside the shell. Precedence: explicit --runtime-root, then
    # CIAO_RUNTIME_ROOT, then this workspace's own .runtime.
    proposal_outcomes.configure(
        _resolve_runtime_root(
            args.runtime_root
            or os.environ.get("CIAO_RUNTIME_ROOT", "").strip()
            or workspace / ".runtime"
        )
    )
    # The curator files a fact first and dismisses second, so that flow is a
    # PROMOTION; only a bare rejection is a dismissal. The logical workspace
    # name rides in CIAO_ACTIVE_WORKSPACE on scheduled runs (same convention
    # as os-audit --workspace-name); a manual run without it lands in the
    # shared bucket rather than recording a filesystem path as a name.
    # Rehome rows are vault-hygiene decisions, not extraction outcomes.
    if proposal_outcomes.is_extraction_kind(kind):
        proposal_outcomes.record(
            kind=kind,
            action="promoted" if args.promoted else "dismissed",
            workspace=os.environ.get("CIAO_ACTIVE_WORKSPACE", "").strip(),
            via="agent",
        )
    if args.json:
        json.dump(
            {"removed": True, "text": needle, "workspace": str(workspace)},
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


def _skill_proposal_remove_command(args: argparse.Namespace) -> int:
    """Delete a resolved skill proposal from a workspace's review queue.

    The curation schedule reviews ``Workspace/Skill-Proposals/``; once a
    proposal's decision is made (implemented, or decided against) it is removed
    here so the queue stops re-asking. NAME matches the file's stem or a unique
    substring of it.
    """
    from ciao.config import CiaoConfig

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
        # Deleting a proposal file is a review decision, not a session write;
        # loading config outside the server env must not mint a session secret.
        "PWA_AUTH_TOKEN": config_source.get("PWA_AUTH_TOKEN", "") or "skill-proposal-remove",
    })
    config = CiaoConfig.from_env(config_source)

    # Which workspace the proposal lives in: the active one, falling back to the
    # primary, matching skill_evolution's routing so evidence and queue stay
    # aligned per workspace.
    name = os.environ.get("CIAO_ACTIVE_WORKSPACE", "").strip()
    if config.workspace(name) is None:
        name = config.primary_workspace()
    queue = config.workspace_vault_root(name) / "Workspace" / "Skill-Proposals"

    needle = args.name.strip()
    if not needle:
        print("a skill proposal name or substring is required", file=sys.stderr)
        return 2

    if not queue.is_dir():
        print("No skill proposals are queued.", file=sys.stderr)
        return 1

    candidates = sorted(p for p in queue.iterdir() if p.is_file() and p.suffix == ".md")
    matches = [p for p in candidates if needle.casefold() in p.stem.casefold()]
    if not matches:
        print(f"No skill proposal matched {needle!r}.", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(
            f"The name matched more than one skill proposal; use a longer substring: "
            + ", ".join(p.stem for p in matches),
            file=sys.stderr,
        )
        return 1

    target = matches[0]
    try:
        target.unlink()
    except OSError as exc:
        print(f"could not delete {target.name}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        json.dump({"removed": True, "name": target.stem, "workspace": name}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"Removed skill proposal {target.stem} from {name}.")
    return 0


def _skills_list_command(args: argparse.Namespace) -> int:
    from ciao.skills_inventory import build_skill_inventory

    workspace_root = Path(args.workspace).expanduser().resolve()
    inventory = build_skill_inventory(workspace_root)
    json.dump(inventory, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
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


def _desktop_service_command(args: argparse.Namespace) -> int:
    from ciao import macos_service

    action = args.desktop_service_action
    if action == "status":
        result = macos_service.service_status()
    elif action == "start":
        result = macos_service.start_service()
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
    elif action == "rollback":
        result = macos_service.rollback_legacy_companion()
    else:  # pragma: no cover - argparse constrains the action.
        parser_error = macos_service.ServiceResult(
            False,
            str(action),
            "Unknown desktop service action.",
            {},
        )
        return macos_service.print_result(parser_error, as_json=bool(args.as_json))
    return macos_service.print_result(result, as_json=bool(args.as_json))


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
        return report(
            result,
            [
                f"Removed {result['path']}"
                if result["removed"]
                else f"Nothing to remove at {result['path']}"
            ],
        )
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
    run_parser.set_defaults(func=lambda _args: _run_server())

    desktop_service_parser = subparsers.add_parser(
        "desktop-service",
        help="Control the launchd-managed engine for Ciaobot.app.",
    )
    desktop_service_sub = desktop_service_parser.add_subparsers(
        dest="desktop_service_action",
        required=True,
    )
    for action in ("status", "start", "restart", "stop", "update-engine", "migrate", "rollback"):
        action_parser = desktop_service_sub.add_parser(action)
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
        action_parser.set_defaults(func=_desktop_service_command)
    login_parser = desktop_service_sub.add_parser("login")
    login_parser.add_argument("login_action", choices=("enable", "disable"))
    login_parser.add_argument("--json", action="store_true", dest="as_json")
    login_parser.set_defaults(func=_desktop_service_command)

    # Separate from `desktop-service`, which controls the launchd engine. This
    # group only manages removal of an old app bundle. Installation and updates
    # are owned by scripts/install.sh and the signed Tauri updater.
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
        help="Directory holding Ciaobot.app (defaults to /Applications, "
        "or ~/Applications on a non-admin account).",
    )
    desktop_uninstall_parser.add_argument("--json", action="store_true", dest="as_json")
    desktop_uninstall_parser.set_defaults(func=_desktop_command)

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
    setup_parser.add_argument("--push-contact", help="Web Push contact to write when .env is new.")
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
        help="Localhost port used by the LaunchAgent and app shortcut.",
    )
    setup_parser.add_argument(
        "--launch-agents-dir",
        type=Path,
        default=default_launch_agents_dir(),
        help="Directory where com.ciao.server.plist is written.",
    )
    setup_parser.add_argument(
        "--app-dir",
        type=Path,
        default=None,
        help=(
            "Directory to scan for legacy launcher bundles during migration. "
            "Defaults to /Applications when writable, else ~/Applications."
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
            "One-off migration for an existing vault: renames aliased types "
            "(doc -> document, project-log -> log) and reports types with no "
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
            "without moving anything. For a vault migrated by hand or by a model "
            "following docs/VAULT_MIGRATION_PROMPT.md: `agent_root` answers "
            "per-root only when a receipt says so, so without this the install "
            "keeps resolving the old layout and --repair refuses. Verifies the "
            "layout is actually in place first and refuses if it is not."
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
            "CLAUDE.md and reports entries that record a chat event instead of "
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
            "or dismiss with `ciao memory-proposal-dismiss <text>`), keeping "
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
            "and promotable like any archive-time proposal. Re-filing "
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
        help="Proposal text or unique substring to dismiss.",
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
    memory_proposal_dismiss_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help="Runtime root for the outcome log. Defaults to CIAO_RUNTIME_ROOT or <workspace>/.runtime.",
    )
    memory_proposal_dismiss_parser.set_defaults(func=_memory_proposal_dismiss_command)

    skill_proposal_parser = subparsers.add_parser(
        "skill-proposal-remove",
        help="Remove a resolved skill proposal from the review queue.",
        description=(
            "Deletes one file from a workspace's Workspace/Skill-Proposals/ "
            "after its decision is made (implemented, or decided against). "
            "NAME matches the proposal file's stem or a unique substring of it."
        ),
    )
    skill_proposal_parser.add_argument(
        "name",
        help="Skill proposal file stem or unique substring to remove.",
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


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    os.environ.setdefault("CLAUDE_CODE_DISABLE_ARTIFACT", "1")
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if argv_list[:1] == ["public-preflight"]:
        return public_release.main(argv_list[1:])
    if argv_list[:1] == ["package-smoke"]:
        return package_smoke.main(argv_list[1:])
    if argv_list[:1] == ["prepare-release"]:
        return release.main(argv_list[1:])
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
