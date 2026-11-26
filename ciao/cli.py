"""Command-line entrypoint for the packaged Ciaobot app."""

from __future__ import annotations

import argparse
import datetime
import html
import http.cookiejar
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast
import urllib.error
import urllib.request

from ciao import dev, package_version, service_backend
from ciao.server_host import (
    BUNDLE_ID,
    EXIT_TIMEOUT_SECONDS,
    HostOwnership,
    ServerHostError,
    default_bundle_path,
    host_service_argv,
    verify_owned_host,
)
from ciao.setup_marker import RUNTIME_DIR_NAME
from ciao.setup_status import detect_nested_workspaces
from ciao.macos_service import default_launch_agents_dir, hosted_service_python, load_server_plist
from ciao.jsonio import write_private_text
from ciao.os_support.child_env import child_env
from ciao.os_support.private import make_private, open_private
from ciao.os_support.processes import python_isolation_flags
from ciao.git_proc import EXACT_BYTES
from ciao.os_support.shell_hints import path_hint
from ciao.os_support.tool_path import dedupe_path
from ciao.sync_skills import SETUP_MEMORY_FAILED_RC

if TYPE_CHECKING:  # only ever a type here; the queue model is imported locally.
    from ciao.config import CiaoConfig, WorkspaceConfig

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
    return [sys.executable, *python_isolation_flags(), "-m", "ciao.cli", *sys.argv[1:]]


def _run_command(args: argparse.Namespace) -> int:
    if args.port is not None and not args.app_engine:
        print("--port requires --app-engine", file=sys.stderr)
        return 2
    if args.app_engine_ready_fd is not None and not args.app_engine:
        print("--app-engine-ready-fd requires --app-engine", file=sys.stderr)
        return 2
    if args.app_engine:
        if not package_version.running_from_app_bundle():
            print("--app-engine runs only from the engine inside Ciaobot.app", file=sys.stderr)
            return 2
        if args.port is not None and not 1024 <= args.port <= 65535:
            print("--port must be between 1024 and 65535", file=sys.stderr)
            return 2
        if args.app_engine_ready_fd is None:
            print("--app-engine requires an inherited readiness pipe", file=sys.stderr)
            return 2
        from ciao.app_engine import protect_ready_fd

        try:
            protect_ready_fd(args.app_engine_ready_fd)
        except (OSError, OverflowError, ValueError):
            print("Ciaobot app engine readiness pipe is invalid", file=sys.stderr)
            return 2
        return _run_server(
            supervised=args.supervised,
            app_engine=True,
            app_engine_port=args.port,
            app_engine_ready_fd=args.app_engine_ready_fd,
        )
    # Ordinary `ciao run` keeps its exact call shape.
    return _run_server(supervised=args.supervised)


def _run_server(
    *,
    supervised: bool = False,
    app_engine: bool = False,
    app_engine_port: int | None = None,
    app_engine_ready_fd: int | None = None,
) -> int:
    from ciao.config import RESTART_EXIT_CODE
    from ciao.main import main as server_main

    try:
        if app_engine and app_engine_port is not None:
            server_main(
                supervised=supervised,
                app_engine=True,
                app_engine_port=app_engine_port,
                app_engine_ready_fd=app_engine_ready_fd,
            )
        elif app_engine:
            # Before any configuration is loaded, no server, threads or locks
            # of its own: the recorded move (this boot is its restart) moves
            # the folder and repoints the pointer here, and an interrupted
            # move is rolled back here. The engine boots either way.
            from ciao import workspace_move_app

            workspace_move_app.before_boot()
            server_main(supervised=supervised, app_engine=True, app_engine_ready_fd=app_engine_ready_fd)
        else:
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
        if app_engine:
            # In app mode the pointer is the only workspace source
            # (`installed_workspace_env` returns early for an exported
            # CIAO_WORKSPACE): without this pop a post-move restart would pin
            # the vanished pre-move folder, and the fresh process — reading
            # os.environ before the pointer — would never boot the new one.
            os.environ.pop("CIAO_WORKSPACE", None)
            os.environ.pop("CIAO_RUNTIME_ROOT", None)
        if app_engine_ready_fd is not None:
            from ciao.app_engine import exec_with_ready_fd

            exec_with_ready_fd(app_engine_ready_fd, sys.executable, _relaunch_argv(), os.execv)
        else:
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


def _import_legacy_workspaces_for_setup(root: Path, existing_env: dict[str, str], *, owner: Any = None) -> None:
    """Run the one-time ``CIAO_WORKSPACES`` import against ``root``'s ``.env``.

    Built from the install's own ``.env`` (not the ambient environment) so the
    import targets the same runtime root the server will use.
    """
    from ciao.config import CiaoConfig

    source = {
        **existing_env,
        "CIAO_WORKSPACE": str(root),
        "CIAO_RUNTIME_ROOT": str((root / RUNTIME_DIR_NAME).resolve()),
        "PWA_AUTH_TOKEN": existing_env.get("PWA_AUTH_TOKEN") or "setup",
    }
    CiaoConfig.from_env(source).import_legacy_workspaces_env(owner=owner)


def _write_if_missing(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding="utf-8", newline="")


def _host_service_arguments(
    host: HostOwnership, python: str | None
) -> tuple[str, ...]:
    """The exact hosted argv for a verified host, or an actionable refusal.

    ``host`` must be a :class:`ciao.server_host.HostOwnership` returned by
    :func:`ciao.server_host.verify_owned_host` — the rendered command is built
    from that canonical ``bundle_path``, never from a raw caller path, because
    :func:`host_service_argv` can see names but not the bytes behind them.

    ``python`` is the interpreter the host will run. A host cannot serve the
    ``ciao`` console entry point (its own parser demands a python executable),
    so a basename that is not ``python``/``python3``/``python3.N`` — or a
    missing interpreter — is refused here with the reason, rather than being
    written into launchd to fail at the next sign-in.
    """
    if python is None:
        raise ValueError(
            "cannot render the Ciaobot Server host service: a verified host "
            "needs the interpreter it will serve. Pass the absolute python "
            "the host runs, not the ciao console entry point."
        )
    try:
        return host_service_argv(
            PurePosixPath(host.bundle_path), PurePosixPath(python)
        )
    except ServerHostError as exc:
        raise ValueError(
            f"cannot render the Ciaobot Server host service: {exc}"
        ) from exc


def _direct_service_arguments(executable: str) -> tuple[str, ...]:
    """The direct argv tail: ``-m ciao.cli run`` for a python, ``run`` otherwise."""

    name = Path(executable).name.lower()
    if name == "python" or name.startswith("python3"):
        return ("-m", "ciao.cli", "run")
    return ("run",)


def _launchd_program_arguments(arguments: tuple[str, ...]) -> str:
    """Render the argv tail (everything after argv[0]) as ``<string>`` lines.

    argv[0] is substituted into ``{{CIAO_EXECUTABLE}}`` by the caller.
    """

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
    host: HostOwnership | None = None,
    host_python: str | None = None,
) -> str:
    """Render the server LaunchAgent plist.

    The default direct shape is byte-identical to before: ``python_path`` /
    ``engine_path`` name the program and the arguments follow the launcher
    name. When ``host`` is given — a snapshot from
    ``ciao.server_host.verify_owned_host``, the only proof the bytes are ours —
    the program becomes the native ``Ciaobot Server`` running
    ``serve --python <host_python>`` and the job gains ``ExitTimeOut``
    ``EXIT_TIMEOUT_SECONDS`` (45 s), so launchd never sweeps the job group out
    from under the host's own 35 s stop grace. The caller supplying ``host`` in
    production is the installer (child E); nothing here invents a host.
    """

    if host is not None:
        host_argv = _host_service_arguments(host, host_python)
        executable, arguments = host_argv[0], host_argv[1:]
    else:
        executable = engine_path or python_path or sys.executable
        arguments = _direct_service_arguments(executable)
    template = resources.files("ciao.stock").joinpath(
        "deploy", template_name
    ).read_text(encoding="utf-8")
    # Under launchd the default PATH is minimal. Bake the user's development
    # PATH from setup time into the plist for optional deploy tooling. The
    # caller's PATH is deduped here, the one place it is persisted: a setup run
    # can inherit a PATH that an earlier plist baked, so an unnormalised value
    # would carry any repeat forward into every later write.
    resolved_path = dedupe_path(path or os.environ.get("PATH", ""))
    replacements = {
        "{{ASSOCIATED_BUNDLE_ID}}": html.escape(
            BUNDLE_ID if host is not None else "local.ciaobot.app", quote=False
        ),
        "{{CIAO_WORKSPACE}}": html.escape(str(workspace), quote=False),
        "{{CIAO_RUNTIME_ROOT}}": html.escape(
            str((runtime_root or (workspace / ".runtime")).resolve()), quote=False
        ),
        "{{CIAO_EXECUTABLE}}": html.escape(executable, quote=False),
        "{{LAUNCHD_PROGRAM_ARGUMENTS}}": _launchd_program_arguments(arguments),
        "{{CIAO_PORT}}": html.escape(str(port), quote=False),
        "{{CIAO_PATH}}": html.escape(resolved_path, quote=False),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    if host is not None:
        # The template has no ExitTimeOut placeholder: a bare direct
        # definition never has one, and only a hosted definition must exceed
        # the host's stop grace. Insert it as the last key of the job dict.
        exit_timeout = (
            "    <key>ExitTimeOut</key>\n"
            f"    <integer>{EXIT_TIMEOUT_SECONDS}</integer>\n"
        )
        head, marker, tail = template.rpartition("</dict>")
        template = f"{head}{exit_timeout}{marker}{tail}"
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
    host: HostOwnership | None = None,
    host_python: str | None = None,
) -> Path:
    """Render the server plist and write it."""
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
    rendered = _render_launchd_plist(
        workspace=workspace,
        python_path=python_path,
        engine_path=engine_path,
        runtime_root=runtime_root,
        port=port,
        path=path,
        template_name=f"{plist_name}.tmpl",
        host=host,
        host_python=host_python,
    )
    plist.write_text(rendered, encoding="utf-8", newline="")
    return plist


def _verified_host_if_installed() -> HostOwnership | None:
    """This machine's verified server host, or ``None`` when there is none.

    The one decision setup and registration make about activating the native
    host: only a snapshot from :func:`ciao.server_host.verify_owned_host` —
    which reads the owner-only record and proves the installed
    ``Ciaobot Server.app`` bytes against it — selects the hosted service. A
    missing record, a foreign or tampered bundle and every non-macOS platform
    read as "no host", so the current direct shape is written instead. Nothing
    here writes, signs, launches or starts anything, and no path renders a host
    without that proof: verification is not inferred from a filename.

    The bundle is :func:`ciao.server_host.default_bundle_path`, which is exactly
    where the E1 installer installs and records the host, so the path the
    installer used is the one verified.
    """
    if sys.platform != "darwin":
        return None
    try:
        return verify_owned_host(default_bundle_path())
    except (ServerHostError, OSError):
        return None


def _host_engine_interpreter(resolved_engine: str) -> str:
    """The python a selected host must serve, from the direct path's program.

    The direct shape writes ``resolved_engine`` as its program: the engine
    interpreter itself when setup was handed one, or the ``ciao`` console the
    release installer passes as ``--python``. A host cannot serve that console
    — its own parser demands a python executable — so the console resolves to
    this process's interpreter, which is the engine interpreter behind it when
    setup runs from the installed ``ciao``. Anything else is passed through
    untouched, so :func:`_host_service_arguments` refuses it with the reason
    instead of a made-up interpreter being written into launchd.
    """
    if Path(resolved_engine).name.lower() == "ciao":
        return sys.executable
    return resolved_engine


def _hosted_definition_program(data: dict[str, Any]) -> str | None:
    """argv[0] of a strict hosted definition in *data*, or ``None``.

    Recognition only: the hosted shape is confirmed through
    :func:`ciao.macos_service.hosted_service_python`, the one recognizer B2
    shares across consumers, and then argv[0] — the host executable — is read
    off the same argv so a caller can ask whether it still exists. A direct,
    legacy, unknown or malformed argv answers ``None`` and never raises.
    """
    arguments = data.get("ProgramArguments")
    if hosted_service_python(arguments) is None:
        return None
    if not isinstance(arguments, (list, tuple)) or not arguments:
        return None
    program = arguments[0]
    return program if isinstance(program, str) else None


def _existing_hosted_definition_serves(launch_agents_dir: Path, workspace: Path) -> bool:
    """Whether the installed definition is hosted, live and serves *workspace*.

    The preservation half of activation. When no verified host is selected this
    run, re-rendering would silently downgrade a live hosted job to the direct
    shape, so the existing ``com.ciao.server.plist`` is read back first. Only a
    definition that parses as the strict hosted shape, names the requested
    workspace, **and** still names a host executable that exists as a regular
    file is preserved; a missing, unreadable, direct, unknown/legacy,
    other-workspace or dead definition answers ``False``.

    The existence check is the narrow guard that gives a dead definition a
    repair path: if the ``Ciaobot Server.app`` was deleted or moved, keeping the
    hosted plist would leave launchd a job it cannot run and the documented
    ``ciao setup && ciao service start`` could never write the working direct
    shape. Requiring existence (and nothing stronger — no record, no native
    probe) keeps a live hosted definition byte-for-byte. Recognition never
    raises. Non-macOS platforms never have a hosted definition, so the check is
    skipped there and their offline export stays byte-identical to before.
    """
    if sys.platform != "darwin":
        return False
    data = load_server_plist(launch_agents_dir)
    if data is None:
        return False
    program = _hosted_definition_program(data)
    if program is None:
        return False
    if _plist_data_workspace(data) != workspace:
        return False
    return Path(program).is_file()


def _setup_token_path(workspace: Path) -> Path:
    return workspace / ".runtime" / "setup-token"


def _ensure_setup_token(workspace: Path) -> str:
    path = _setup_token_path(workspace)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            make_private(path)  # a file from an older version may be world-readable
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
    path.unlink(missing_ok=True)
    fd = open_private(path, os.O_WRONLY | os.O_EXCL, follow_symlinks=False)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(token + "\n")
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
    """A shell line that puts the directory holding ``ciao`` on PATH, or
    ``None`` when it is already on PATH.

    An installer engine's ``ciao`` is the launcher the receipt names, in uv's
    bin dir (``~/.local/bin``), which the installer puts on PATH; the uv tool
    env's own bin dir is never on PATH, so hinting at it told every installed
    user to add a directory they do not need. Without a receipt (a checkout's
    venv) the entry point sits next to the interpreter.
    """

    from ciao import install_receipt

    receipt = install_receipt.running_receipt()
    # Not .resolve(): a venv's bin/python is a symlink to the base interpreter,
    # and resolving it would report the base interpreter's bin dir instead of
    # the venv's own bin/ where the `ciao` entry point actually lives.
    if receipt is not None and receipt.executable:
        bin_dir = Path(receipt.executable).parent
    else:
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
        print("To run `ciao` from a shell, add its directory to PATH:")
        print(f"  {hint}")


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


def _ensure_exact_bytes(root: Path) -> None:
    """Have ``root``'s own git config store exact bytes (``core.autocrlf=false``).

    Only when the repo does not set it already: a value the user chose for this
    repo stands. See ``git_proc.EXACT_BYTES`` for why; the engine passes the same
    setting on every call, so a repo this has not touched yet behaves the same.
    """
    current = subprocess.run(
        ["git", "-C", str(root), "config", "--local", "--get", "core.autocrlf"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if current.returncode == 0:
        return
    subprocess.run(
        ["git", "-C", str(root), "config", "--local", "core.autocrlf", "false"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def ensure_workspace_git(root: Path, *, initialize_if_missing: bool = True) -> None:
    """Make sure the workspace is a git repository with a protective .gitignore.

    Snapshots and sync rely on git; a fresh workspace gets `git init` plus an
    initial commit. An existing repo is left untouched apart from appending
    missing .gitignore guards. Missing git binary is a non-fatal skip.
    """
    root = Path(root).expanduser().resolve()
    if shutil.which("git") is None:
        print("git not found; skipping workspace git init", file=sys.stderr)
        return
    probe = subprocess.run(
        ["git", *EXACT_BYTES, "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if probe.returncode == 0 and probe.stdout.strip() == "true":
        _ensure_workspace_gitignore(root)
        _ensure_exact_bytes(root)
        return
    if not initialize_if_missing:
        return
    _ensure_workspace_gitignore(root)
    init = subprocess.run(
        ["git", *EXACT_BYTES, "init", "-b", "main", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if init.returncode != 0:
        print(f"git init failed for {root}: {init.stderr.strip()}", file=sys.stderr)
        return
    _ensure_exact_bytes(root)
    subprocess.run(
        ["git", *EXACT_BYTES, "-C", str(root), "add", "-A"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    commit = subprocess.run(
        [
            "git", *EXACT_BYTES, "-C", str(root),
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


def ensure_vault_git(root: Path, *, initialize_if_missing: bool = True) -> None:
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
        ["git", *EXACT_BYTES, "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if probe.returncode == 0:
        toplevel = Path(probe.stdout.strip())
        if toplevel == root:
            _ensure_vault_gitignore(root)
            _ensure_exact_bytes(root)
        return
    if not initialize_if_missing:
        return
    _ensure_vault_gitignore(root)
    init = subprocess.run(
        ["git", *EXACT_BYTES, "init", "-b", "main", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if init.returncode != 0:
        print(f"git init failed for {root}: {init.stderr.strip()}", file=sys.stderr)
        return
    _ensure_exact_bytes(root)
    subprocess.run(
        ["git", *EXACT_BYTES, "-C", str(root), "add", "-A"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    commit = subprocess.run(
        [
            "git", *EXACT_BYTES, "-C", str(root),
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


def _is_real_file(path: Path) -> bool:
    """A regular file that is not a symlink. Symlinks never count as evidence."""
    return path.is_file() and not path.is_symlink()


def detect_existing_vault_root(workspace: Path | str) -> Path:
    """Return the absolute first-setup vault for an existing folder.

    Bounded to the folder and its visible immediate children; nothing recurses.
    A real direct ``memory-vault/`` wins outright. Otherwise the candidates are
    the folder itself (``entity-types.yaml``), each child with its own
    ``entity-types.yaml``, and each child's real ``memory-vault/``. Exactly one
    wins, none means the folder is adopted as its own vault, and more than one
    raises ``ValueError`` listing every candidate. Symlinks never count, and a
    symlinked child is skipped entirely.
    """
    root = Path(workspace).expanduser().resolve()
    direct = root / "memory-vault"
    if direct.is_dir() and not direct.is_symlink():
        return direct
    candidates: set[Path] = set()
    if _is_real_file(root / "entity-types.yaml"):
        candidates.add(root)
    try:
        children = sorted(root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        children = []
    for child in children:
        if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
            continue
        if _is_real_file(child / "entity-types.yaml"):
            candidates.add(child)
        nested = child / "memory-vault"
        if nested.is_dir() and not nested.is_symlink():
            candidates.add(nested)
    if not candidates:
        return root
    if len(candidates) == 1:
        return next(iter(candidates))
    ordered = sorted(candidates, key=lambda path: path.as_posix())
    raise ValueError(
        f"More than one existing vault was found in {root.as_posix()}: "
        f"{', '.join(path.as_posix() for path in ordered)}. Setup will not choose "
        "between them. To use one, set the main Ciaobot folder to that vault "
        f"folder itself (for example {ordered[0].as_posix()}) and run setup again."
    )


def _read_setup_registry(registry_path: Path) -> dict[str, WorkspaceConfig]:
    """Parse an existing setup registry without writing it.

    Raises ``ValueError`` for an unreadable file, invalid JSON, or no valid
    workspace entry. Setup and the folder inspection both read it before any
    write, so an unusable registry is refused rather than rebuilt over.
    """
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

    from ciao.config import WorkspaceConfig

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
    return workspaces


def _setup_config(
    workspace_root: Path,
    configured_vault_root: Path,
    workspaces: dict[str, WorkspaceConfig],
) -> CiaoConfig:
    """An in-memory config for resolving targets. Nothing is persisted from it."""
    from ciao.config import CiaoConfig

    return CiaoConfig(
        pwa_auth_token="setup-registry",
        workspace_root=workspace_root,
        vault_root=configured_vault_root,
        state_path=workspace_root / ".runtime" / "state.json",
        media_root=workspace_root / ".runtime" / "media",
        workspaces=workspaces,
    )


def _resolve_setup_registry(
    workspaces: dict[str, WorkspaceConfig],
    *,
    workspace_root: Path,
    configured_vault_root: Path,
) -> list[tuple[str, Path]]:
    """Resolve targets with the running app's own resolver, in memory only."""
    config = _setup_config(workspace_root, configured_vault_root, workspaces)
    return [(name, config.workspace_vault_root(name)) for name in config.workspace_names()]


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
    return _resolve_setup_registry(
        _read_setup_registry(registry_path),
        workspace_root=workspace_root,
        configured_vault_root=configured_vault_root,
    )


@dataclass(frozen=True)
class SetupVaultSelection:
    """Where a setup run will put the vault, decided before any write.

    ``vault_value`` is the global ``CIAO_VAULT_ROOT`` text and ``vault_path`` its
    absolute form. ``registered_vaults`` holds the per-workspace targets when a
    registry or legacy configuration exists, otherwise ``None``. ``import_legacy``
    says the one-time ``CIAO_WORKSPACES`` import runs after validation.
    """

    root: Path
    existing_env: dict[str, str]
    vault_value: str
    vault_path: Path
    registered_vaults: list[tuple[str, Path]] | None
    source: str
    import_legacy: bool


def _preview_legacy_workspaces(
    workspace_root: Path, raw: str, configured_vault_root: Path
) -> dict[str, WorkspaceConfig]:
    """What the one-time ``CIAO_WORKSPACES`` import would add, without importing.

    Uses the importer's own parser and archived-name exclusion, so the preview
    and the import agree. Writes nothing.
    """
    from ciao.config import _parse_workspaces_json

    legacy = _parse_workspaces_json(raw)
    config = _setup_config(workspace_root, configured_vault_root, legacy)
    archived = config._archived_workspace_names()
    return {
        name: workspace
        for name, workspace in config.workspaces.items()
        if name.casefold() not in archived
    }


def validate_external_setup_vault(workspace: Path | str, vault: Path | str) -> Path:
    """Validate a first-run notes folder without creating or changing it."""
    from ciao.setup_marker import is_set_up_workspace

    root = Path(workspace).expanduser().resolve()
    try:
        notes = Path(vault).expanduser().resolve(strict=True)
        if not notes.is_dir() or not os.access(notes, os.R_OK | os.W_OK | os.X_OK):
            raise ValueError("notes folder must be an accessible, writable directory")
        with os.scandir(notes) as entries:
            next(entries, None)
    except OSError as exc:
        raise ValueError(f"notes folder is unavailable: {exc}") from exc
    if notes == root or notes in root.parents or root in notes.parents:
        raise ValueError("notes folder and Ciaobot folder must be separate")
    selection = select_setup_vault(root, vault_mode="scratch")
    from ciao.setup_transaction import pending_matches

    pending = pending_matches(root, notes)
    if is_set_up_workspace(root) and not pending:
        raise ValueError("an existing installation cannot be rebound during setup")
    if selection.source in {"env", "registry", "legacy"} and not pending:
        raise ValueError("an existing installation cannot be rebound during setup")
    return notes


def _check_external_setup_identity(path: Path, expected: tuple[int, int] | None) -> None:
    try:
        current = path.stat()
    except OSError as exc:
        raise ValueError(f"notes folder is unavailable: {exc}") from exc
    if expected is None or (current.st_dev, current.st_ino) != expected:
        raise ValueError("notes folder changed during setup; inspect it again")


def select_setup_vault(
    workspace: Path | str,
    *,
    vault_mode: str,
    vault_root: Path | str | None = None,
) -> SetupVaultSelection:
    """Decide the setup vault from existing configuration, reading only.

    Inspection, finish preflight and direct CLI setup all call this, so they
    select the same location. Precedence: a nonblank ``.env`` ``CIAO_VAULT_ROOT``,
    then the registry, then legacy ``CIAO_WORKSPACES``, then an explicit
    argument, then the default. An unusable registry raises ``ValueError``.
    """
    from ciao.config import LEGACY_WORKSPACES_IMPORT_MARKER

    root = Path(workspace).expanduser().resolve()
    existing_env: dict[str, str] = {}
    env_path = root / ".env"
    if env_path.exists():
        from dotenv import dotenv_values

        existing_env = {key: (value or "") for key, value in dotenv_values(env_path).items()}
    registry_path = root / RUNTIME_DIR_NAME / "workspaces.json"
    workspaces = _read_setup_registry(registry_path) if registry_path.exists() else None
    legacy_raw = existing_env.get("CIAO_WORKSPACES", "").strip()
    legacy_configured = workspaces is None and bool(legacy_raw)
    # A completed import wrote its marker and its entries now live in the
    # registry or were removed on purpose: only a pending import is previewed.
    import_pending = legacy_configured and not (
        root / RUNTIME_DIR_NAME / LEGACY_WORKSPACES_IMPORT_MARKER
    ).exists()
    env_root = existing_env.get("CIAO_VAULT_ROOT", "").strip()
    explicit = str(vault_root) if vault_root is not None else None

    if env_root:
        source = "env"
    elif workspaces is not None:
        source = "registry"
    elif legacy_configured:
        source = "legacy"
    elif explicit is not None:
        source = "explicit"
    else:
        source = "default"

    if env_root:
        vault_value = env_root
    elif explicit is not None:
        vault_value = explicit
    elif workspaces is not None or legacy_configured:
        vault_value = "memory-vault"
    else:
        vault_value = "memory-vault"
        if vault_mode == "existing":
            found = detect_existing_vault_root(root)
            if found == root:
                # Single-folder setup: the chosen folder is the user's notes.
                vault_value = "."
            else:
                vault_value = found.relative_to(root).as_posix()
                source = "discovery"
    vault_path = Path(vault_value).expanduser()
    if vault_path.is_absolute():
        vault_value = str(vault_path)
    else:
        vault_path = root / vault_path

    registered: list[tuple[str, Path]] | None = None
    if workspaces is not None:
        registered = _resolve_setup_registry(
            workspaces, workspace_root=root, configured_vault_root=vault_path
        )
    elif import_pending:
        previewed = _preview_legacy_workspaces(root, legacy_raw, vault_path)
        if previewed:
            registered = _resolve_setup_registry(
                previewed, workspace_root=root, configured_vault_root=vault_path
            )
    return SetupVaultSelection(
        root=root,
        existing_env=existing_env,
        vault_value=vault_value,
        vault_path=vault_path,
        registered_vaults=registered,
        source=source,
        import_legacy=import_pending,
    )


def _service_platform() -> bool:
    """True where ``ciao setup`` installs a per-user background service: macOS (a LaunchAgent).

    Linux has none, and there ``--load-launchd`` is refused rather than
    silently ignored.
    """

    return sys.platform == "darwin"


def setup_workspace(
    workspace: Path | str,
    *,
    auth_token: str | None = None,
    vault_root: Path | str | None = None,
    vault_mode: str = "scratch",
    workspace_name: str | None = None,
    default_provider: str = "claude",
    python_path: str | None = None,
    port: int = 8443,
    launch_agents_dir: Path | str | None = None,
    app_dir: Path | str | None = None,
    confirm_repoint: bool = False,
    initialize_git: bool = False,
    sync_failures: list[str] | None = None,
    _vault_selection: SetupVaultSelection | None = None,
    _adopt_exact_vault: bool = False,
    _external_identity: tuple[int, int] | None = None,
    _complete_external_setup: bool = True,
    _transaction: Any = None,
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
    # In app mode the Mac app owns this engine, so it writes the app-owned
    # workspace pointer below instead of planting a LaunchAgent — launchd
    # would otherwise start a second engine at the next login.
    write_launchd = (
        _service_platform() or launch_agents_dir is not None
    ) and not package_version.running_from_app_bundle()
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
    # The one host decision, shared by the guard below and the plist write
    # further down, so the check and the write can never disagree. The native
    # bootstrap passes the snapshot it already verified at capture, so setup
    # cannot silently downgrade to the direct shape if the host changed while
    # the wizard was open; the native recorder re-verifies the bundle after the
    # write.
    host: HostOwnership | None = None
    if write_launchd:
        from ciao.setup_status import tcc_protected_location

        host = _verified_host_if_installed()
        # Non-None only on macOS.
        protected = tcc_protected_location(root)
        if protected and host is None:
            tcc_launch_dir = (
                Path(launch_agents_dir)
                if launch_agents_dir is not None
                else service_backend.current_backend().agents_dir()
            )
            # An installed definition that already serves this workspace, in
            # either shape, keeps working (a Mac set up before this guard moved);
            # only a workspace the service does not yet serve is refused.
            if (
                not _existing_hosted_definition_serves(tcc_launch_dir, root)
                and _registered_service_workspace(tcc_launch_dir) != root
            ):
                raise RuntimeError(
                    f"{root} is inside ~/{protected}. macOS lets the background engine into "
                    "that folder only when it runs as the Ciaobot Server app, and no verified "
                    "Ciaobot Server is installed on this Mac, so the engine would fail with "
                    "'Operation not permitted'. Re-run the Ciaobot installer to add it, or "
                    "choose a folder outside ~/Desktop, ~/Documents and ~/Downloads, such as "
                    "~/Ciaobot."
                )
    # Every read that decides the vault happens here, before the first write.
    # A registry this run cannot read, or an ambiguous location, refuses now.
    if _vault_selection is None:
        selection = select_setup_vault(root, vault_mode=vault_mode, vault_root=vault_root)
    elif _vault_selection.root != root:
        raise ValueError("the validated setup selection belongs to another folder")
    else:
        selection = _vault_selection

    if _adopt_exact_vault:
        validate_external_setup_vault(root, selection.vault_path)
        if initialize_git:
            raise ValueError("separate notes setup cannot initialize Git history")
        _check_external_setup_identity(selection.vault_path, _external_identity)
        if _transaction is None:
            raise ValueError("separate notes setup requires a transaction")
        if auth_token is not None and "PWA_AUTH_TOKEN" in selection.existing_env:
            if selection.existing_env["PWA_AUTH_TOKEN"] != auth_token:
                raise ValueError("Use the password chosen when setup began.")

    from ciao.workspaces import acquire_registry_at, WorkspaceRegistryConflict

    with acquire_registry_at(root / RUNTIME_DIR_NAME) as owner:
        current = select_setup_vault(root, vault_mode=vault_mode, vault_root=vault_root)
        if current != selection:
            raise WorkspaceRegistryConflict("setup selection changed during preflight")
        return _setup_selected_workspace(
            root, current, owner, auth_token=auth_token, vault_root=vault_root,
            vault_mode=vault_mode, requested_name=requested_name, default_provider=default_provider,
            python_path=python_path, port=port, launch_agents_dir=launch_agents_dir,
            app_dir=app_dir, confirm_repoint=confirm_repoint, initialize_git=initialize_git,
            sync_failures=sync_failures, write_launchd=write_launchd, host=host,
            _adopt_exact_vault=_adopt_exact_vault, _transaction=_transaction,
            _external_identity=_external_identity,
            _complete_external_setup=_complete_external_setup,
        )


def _setup_selected_workspace(
    root: Path,
    selection: SetupVaultSelection,
    owner: Any,
    *,
    auth_token: str | None,
    vault_root: Path | str | None,
    vault_mode: str,
    requested_name: str,
    default_provider: str,
    python_path: str | None,
    port: int,
    launch_agents_dir: Path | str | None,
    app_dir: Path | str | None,
    confirm_repoint: bool,
    initialize_git: bool,
    sync_failures: list[str] | None,
    write_launchd: bool,
    host: HostOwnership | None,
    _adopt_exact_vault: bool,
    _transaction: Any,
    _external_identity: tuple[int, int] | None,
    _complete_external_setup: bool,
) -> list[Path]:
    from ciao.onboarding import ensure_receipt, read_receipt
    from ciao.setup_marker import is_set_up_workspace

    runtime_root = root / ".runtime"
    receipt_created = read_receipt(runtime_root) is None
    receipt = ensure_receipt(runtime_root, established_install=is_set_up_workspace(root))
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if receipt_created:
        written.append(runtime_root / "onboarding.json")
    # An existing .env — the user's own, or a previous install — is the source
    # of truth: every variable already in it wins over setup arguments. The
    # recorded vault root keeps scaffolding anchored.
    existing_env = selection.existing_env
    setup_selected_vault = vault_root is not None or selection.source == "env"
    vault_value = selection.vault_value
    vault_path = selection.vault_path
    env_path = root / ".env"
    workspaces_registry = root / ".runtime" / "workspaces.json"
    def create_registry(text: str) -> None:
        # The setup transaction publishes its own registry copy, so the file may
        # already exist without this owner having loaded it.
        if _transaction is not None and workspaces_registry.exists():
            return
        if owner.current is None:
            owner.replace(text.encode("utf-8"), expected=None)

    if selection.import_legacy:
        # An install that still configures workspaces through the retired
        # variable usually has no registry file (the server never persisted
        # one while the variable was set). The upgrade installer reruns setup
        # before the new server first starts, so writing a synthetic
        # single-workspace registry here would make the server's one-time
        # import keep that synthetic entry and drop the variable's real
        # vault_root / disallowed_tools for the same name. Import
        # the variable first so the registry setup sees is the real one.
        _import_legacy_workspaces_for_setup(root, existing_env, owner=owner)
    # No registry yet means this folder has never been set up, even when it
    # already holds a `.env` of its own (a notes folder or a project checkout).
    first_setup = not workspaces_registry.exists()
    if first_setup and receipt.installation.status == "in_progress":
        from ciao.app_settings import AppSettingsStore, needs_new_install_backup_seed

        settings_path = runtime_root / "app_settings.json"
        if needs_new_install_backup_seed(settings_path):
            AppSettingsStore(settings_path).seed_new_install_backup_default()
    registered_vaults = _setup_registry_vaults(
        workspaces_registry,
        workspace_root=root,
        configured_vault_root=vault_path,
    )
    name = requested_name or "personal"
    fresh_adopted_notes = (
        first_setup
        and receipt.installation.status == "in_progress"
        and registered_vaults is None
        and vault_mode == "existing"
        and (_adopt_exact_vault or not detect_nested_workspaces(vault_path))
    )
    if (
        _transaction is not None
        and not fresh_adopted_notes
        and not _transaction.data["registry"].get("published")
    ):
        # Any other branch would write the registry outside the journal, and
        # complete() would then refuse the binding for good. Refuse before the
        # first binding is written.
        raise ValueError(
            "separate notes setup cannot be recorded for this folder; "
            "nothing was written to the registry"
        )

    generated_password = (
        (_transaction is not None and _transaction.data.get("generated_token") is not None)
        or (not auth_token and "PWA_AUTH_TOKEN" not in existing_env)
    )
    token = auth_token or secrets.token_urlsafe(32)
    env_port, env_vault_value = str(port), vault_value
    if _transaction is not None:
        pinned_env = _transaction.pin("env", {"port": env_port, "vault_value": env_vault_value})
        env_port, env_vault_value = pinned_env["port"], pinned_env["vault_value"]
    desired_env: list[tuple[str, str]] = [
        ("PWA_AUTH_TOKEN", token),
    ]
    # CIAO_WORKSPACE and CIAO_RUNTIME_ROOT are not written: the service
    # definition carries both, and a `ciao run` started inside the workspace
    # finds it through its registry (see `CiaoConfig.from_env`).
    # Nor CIAO_VAULT_MODE: only the first-run onboarding chat reads it, so it
    # is recorded in the setup marker below.
    desired_env.extend([
        ("CIAO_VAULT_ROOT", env_vault_value),
        ("PWA_PORT", env_port),
    ])
    fresh_env = not existing_env and not env_path.exists()
    if _transaction is not None:
        original = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        additions = [f"{key}={value}" for key, value in desired_env if key not in existing_env]
        if additions:
            prefix = "" if not original or original.endswith("\n") else "\n"
            suffix = ("# Added by Ciaobot setup\n" if original else "") + "\n".join(additions) + "\n"
            _transaction.publish("env", original + prefix + suffix)
            written.append(env_path)
    elif fresh_env:
        # The password is in here in clear text: owner-only from creation.
        write_private_text(
            env_path, "\n".join(f"{key}={value}" for key, value in desired_env) + "\n"
        )
        written.append(env_path)
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
    if package_version.running_from_app_bundle():
        # The workspace pointer replaces the service definition in app mode:
        # this bundled engine is restarted by the app, and the next start
        # resolves CIAO_WORKSPACE/CIAO_RUNTIME_ROOT from this file instead of
        # a LaunchAgent plist. Written after .env so the engine's next start
        # reads the finished workspace.
        from ciao import app_engine

        written.append(app_engine.write_pointer(root, runtime_root))
    if first_setup or fresh_env:
        # First-time setup: stamp when this workspace was provisioned so the
        # post-setup restart can hold system-routine catch-up for a grace
        # period. The onboarding chat should be the first thing a new user
        # sees, not four parallel routine chats replaying missed runs. The
        # marker also carries the vault mode the onboarding chat reads, so it
        # is written for a first setup into a folder with a `.env` of its own
        # too (the merge branch above), not only for a brand-new `.env`.
        from ciao.setup_marker import write_setup_marker

        written.append(
            write_setup_marker(root / RUNTIME_DIR_NAME, vault_mode=vault_mode)
        )
    if generated_password:
        # Home shows the generated password once, on this computer.
        from ciao.setup_marker import write_welcome_pending

        written.append(write_welcome_pending(root / RUNTIME_DIR_NAME))
    elif auth_token:
        # A chosen password replaces any generated one the card would show.
        from ciao.setup_marker import clear_welcome_pending

        clear_welcome_pending(root / RUNTIME_DIR_NAME)

    runtime_root = root / RUNTIME_DIR_NAME

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
    # A retry after the journal published the registry: first_setup is false and
    # the registry now exists, so neither flag above holds. The receipt write is
    # not fsynced like the registry, so a crash can keep the registry and lose
    # the receipt; record it again then (and only then).
    published_retry = (
        _transaction is not None
        and bool(_transaction.data["registry"].get("published"))
        and registered_vaults is not None
        and vault_mode == "existing"
    )
    if published_retry and not (fresh_per_root or fresh_adopted_notes):
        from ciao.workspace_reroot import mark_born_per_root, read_receipt as read_rooting_receipt

        if read_rooting_receipt(runtime_root) is None:
            written.extend(mark_born_per_root(root, runtime_root, [name]))
    if fresh_per_root or fresh_adopted_notes:
        from ciao.workspace_reroot import mark_born_per_root

        if fresh_per_root:
            vault_path = root / name / vault_path.name
        # Adopting notes is also a new install, not an upgrade target. Keep its
        # vault path exactly where the user chose it; only agent assets get a
        # per-workspace root. Otherwise first startup snapshots their notes in
        # Git and tries to relocate them through the legacy re-root migration.
        written.extend(mark_born_per_root(root, runtime_root, [name]))
        resolved_vault_path = vault_path.resolve()
        try:
            stored_vault_root = resolved_vault_path.relative_to(root).as_posix()
        except ValueError:
            stored_vault_root = str(resolved_vault_path)
        # The registry has to exist before the asset loop too: `agent_roots_for`
        # reads the receipt for the gate and the REGISTRY for the names, and with
        # no registry it falls back to the install root — which is how the first
        # attempt at this still put `.claude/`, `commands/` and a stock CLAUDE.md
        # beside the nested vault instead of inside the workspace's own folder.
        # The later branch is `_write_if_missing`, so this does not fight it.
        registry_text = json.dumps(
                [{
                    "name": name,
                    "vault_root": stored_vault_root,
                    "default_provider": default_provider,
                }],
                indent=2,
            ) + "\n"
        if _transaction is not None:
            _transaction.publish("registry", registry_text, owner=owner)
        else:
            create_registry(registry_text)
        written.append(root / ".runtime" / "workspaces.json")

    # `.env` always receives CIAO_VAULT_ROOT, including the generated default,
    # so that variable alone cannot say who owns INDEX.md. Persist the setup
    # decision once, bound to the final vault path; unrecorded/changed roots
    # remain conservatively external to index replacement.
    ownership_path = runtime_root / "vault-index-ownership.json"
    if not ownership_path.exists() and (first_setup or fresh_env or _transaction is not None):
        from ciao.setup_marker import write_vault_index_ownership

        index_owner = "ciao" if selection.source == "default" and vault_mode != "existing" else "external"
        owned_targets = (
            [path for _workspace, path in registered_vaults]
            if registered_vaults is not None
            else [vault_path]
        )
        written.append(
            write_vault_index_ownership(runtime_root, owned_targets, owner=index_owner)
        )

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
            )
        except Exception as exc:  # noqa: BLE001 — a scaffold step, never fatal
            print(f"skill sync failed for {asset_root}: {exc}", file=sys.stderr)
        else:
            if sync_result.memory_error and sync_failures is not None:
                sync_failures.append(
                    f"memory regions not set up for {asset_root}: "
                    f"{sync_result.memory_error}"
                )

    from ciao.schedules import ScheduleStore  # noqa: PLC0415

    runtime_schedules = root / ".runtime" / "schedules.json"
    # Under the store's writer lock, so a store another process created a
    # moment ago is never replaced by this empty one.
    ScheduleStore(root / ".runtime").ensure_store()
    written.append(runtime_schedules)

    # First-run wizard names the user's first logical workspace. Writing a
    # real registry (instead of relying on the legacy personal+work fallback)
    # means new installs start with exactly one workspace; more are added in
    # Settings → Overview → All workspaces. The explicit vault_root also keeps the legacy
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
        nested = [] if _adopt_exact_vault else detect_nested_workspaces(vault_path)
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
                }
            )
            _write_if_missing(
                nested_vault / "projects" / "active" / "general" / "general.md",
                "---\ntype: project\ntitle: General\ndescription: Default project.\nstatus: active\ntags: [project]\n---\n\n# General\n",
            )
        create_registry(
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
        create_registry(
            json.dumps(
                [
                    {
                        "name": name,
                        "vault_root": stored_root,
                        "default_provider": provider,
                    }
                ],
                indent=2,
            )
            + "\n",
        )
        written.append(workspaces_registry)

    from ciao.setup_marker import read_vault_index_owned_roots

    owned_indexes = read_vault_index_owned_roots(runtime_root)
    for _, scaffold_vault_path in scaffold_vaults:
        if _adopt_exact_vault:
            # Setup never creates inside an externally managed notes root.
            continue
        _write_if_missing(
            scaffold_vault_path / "MEMORY.md",
            "# Memory\n\nDurable workspace memory lives here.\n",
        )
        if vault_mode != "existing" and str(scaffold_vault_path.resolve()) in owned_indexes:
            _write_if_missing(
                scaffold_vault_path / "INDEX.md",
                "# Vault Index\n\nGenerated by `ciao vault-index`.\n",
            )
        _write_if_missing(
            scaffold_vault_path / "projects" / "active" / "general" / "general.md",
            "---\ntype: project\ntitle: General\ndescription: Default project.\nstatus: active\ntags: [project]\n---\n\n# General\n",
        )
        # The memory pass types every new note from VOCABULARY.md's Categories
        # section, and nothing else writes that file before the first vault
        # index run. A failure here is reported, never fatal to setup.
        try:
            from ciao.vault_index import ensure_vocabulary_file

            vocabulary = ensure_vocabulary_file(scaffold_vault_path)
        except Exception as exc:  # noqa: BLE001 — a scaffold step, never fatal
            print(
                f"vocabulary not written for {scaffold_vault_path}: {exc}",
                file=sys.stderr,
            )
        else:
            if vocabulary is not None:
                written.append(vocabulary)
        (scaffold_vault_path / "Logs" / "Chats").mkdir(parents=True, exist_ok=True)
        written.append(scaffold_vault_path)

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
        # Activation (E2): only a verified host selects the native hosted
        # service; an absent, foreign or unverified host is not claimed and
        # the direct shape is written. When no host is selected this run but
        # the installed definition is already hosted and serves this
        # workspace, it is left exactly as it is: re-rendering would silently
        # downgrade a live hosted job to the direct shape.
        if host is None and _existing_hosted_definition_serves(launch_dir, root):
            # A hosted definition already owns this workspace; setup is a
            # faithful no-op for the service definition, not a downgrade.
            written.append(launch_dir.expanduser() / "com.ciao.server.plist")
        else:
            try:
                written.append(_write_launchd_plist(
                    workspace=root,
                    launch_agents_dir=launch_dir,
                    engine_path=resolved_engine,
                    runtime_root=runtime_root,
                    port=port,
                    path=os.environ.get("PATH", ""),
                    plist_name="com.ciao.server.plist",
                    confirm_repoint=confirm_repoint,
                    host=host,
                    host_python=(
                        _host_engine_interpreter(resolved_engine)
                        if host is not None
                        else None
                    ),
                ))
            except ValueError as exc:
                # The render-time refusal B2 defines (a host given without
                # the python it must serve). Both `ciao setup` callers catch
                # RuntimeError and report it as a message, so it is never a
                # traceback.
                raise RuntimeError(str(exc)) from exc
        # Explicit --launch-agents-dir also permits offline plist generation.
        _remove_legacy_app_shortcuts(app_root_dir)
        _disable_legacy_menubar_agent(launch_dir)

    ensure_workspace_git(root, initialize_if_missing=initialize_git)
    # A vault outside the workspace (existing notes folder) gets its own
    # repo. Runs after the workspace init so the default nested vault is
    # never double-initialized.
    if _adopt_exact_vault:
        _check_external_setup_identity(vault_path, _external_identity)
        if _complete_external_setup:
            _transaction.complete()
    else:
        ensure_vault_git(vault_path, initialize_if_missing=initialize_git)

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

    return _plist_data_workspace(load_server_plist(launch_agents_dir))


def _plist_data_workspace(data: dict[str, Any] | None) -> Path | None:
    """The resolved ``CIAO_WORKSPACE`` a parsed server plist names, if any."""

    if data is None:
        return None
    environment = data.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        return None
    workspace = environment.get("CIAO_WORKSPACE")
    if not workspace:
        return None
    try:
        return Path(str(workspace)).expanduser().resolve()
    except OSError:
        return None


def _registered_service_workspace(definitions_dir: Path) -> Path | None:
    """Workspace the installed service definition serves, if it says one: the plist's ``CIAO_WORKSPACE``, resolved so a caller can compare it with a requested workspace."""

    return _plist_workspace(definitions_dir)


def _service_definition(launch_dir: Path | str | None) -> Path | None:
    """The server LaunchAgent this platform keeps in ``launch_dir``, or None."""

    if launch_dir is None:
        return None
    return Path(launch_dir).expanduser() / "com.ciao.server.plist"


def _setup_command(args: argparse.Namespace) -> int:
    root = Path(args.workspace).expanduser().resolve()
    if args.load_launchd and not _service_platform():
        print(
            "Error: --load-launchd requires macOS. "
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

    from ciao.setup_status import cloud_sync_warning

    cloud_warning = cloud_sync_warning(root)
    if cloud_warning:
        print(f"Warning: {cloud_warning}", file=sys.stderr)

    env_path = root / ".env"
    try:
        had_token = "PWA_AUTH_TOKEN=" in env_path.read_text(encoding="utf-8")
    except OSError:
        had_token = False
    sync_failures: list[str] = []
    transaction = None
    try:
        notes = validate_external_setup_vault(root, args.notes_folder) if args.notes_folder else None
        notes_stat = notes.stat() if notes else None
        token = args.auth_token
        if notes:
            from ciao.setup_transaction import SetupTransaction

            root.mkdir(parents=True, exist_ok=True)
            transaction = SetupTransaction(root, notes)
            token = transaction.begin(args.auth_token, (notes_stat.st_dev, notes_stat.st_ino) if notes_stat else None)
        written = setup_workspace(
            args.workspace,
            auth_token=token,
            vault_root=notes,
            vault_mode="existing" if notes else "scratch",
            _adopt_exact_vault=bool(notes),
            _external_identity=(notes_stat.st_dev, notes_stat.st_ino) if notes_stat else None,
            _transaction=transaction,
            workspace_name=args.workspace_name,
            python_path=args.python,
            port=args.port,
            launch_agents_dir=args.launch_agents_dir,
            app_dir=args.app_dir,
            confirm_repoint=args.yes,
            initialize_git=args.initialize_git,
            sync_failures=sync_failures,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if transaction is not None:
            transaction.close()
    for path in written:
        print(path)
    if notes:
        print(f"Ciaobot data: {root}\nNotes: {notes}")
    from ciao.setup_status import macos_access_note

    access_note = macos_access_note(root)
    if access_note:
        print(f"Note: {access_note}", file=sys.stderr)
    for failure in sync_failures:
        print(
            f"Warning: {failure}. Skills were synced; fix the error and "
            "re-run `ciao setup`.",
            file=sys.stderr,
        )
    setup_rc = SETUP_MEMORY_FAILED_RC if sync_failures else 0
    if not args.auth_token and not had_token:
        print(
            "\nPassword protection is on. No --auth-token was given, so a random "
            f"password was written to {root / '.env'} (PWA_AUTH_TOKEN).\n"
            "Ciaobot also shows it once on Home on this computer.\n"
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
        print(f"LaunchAgent not loaded. To load it: launchctl load -w {agent_definition}")
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
    try:
        if args.rotate:
            token = _rotate_setup_token(root)
        else:
            token = _ensure_setup_token(root)
    except FileExistsError:
        print(
            "Could not write the setup token: another process created it at the same time. Run `ciao setup-url` again.",
            file=sys.stderr,
        )
        return 1
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
        proc = subprocess.run(command, check=False, env=child_env())
    except OSError as exc:
        print(f"Error: failed to run {' '.join(command)}: {exc}", file=sys.stderr)
        return 1
    return int(proc.returncode)


def _resolve_vault_root(raw: Path | str | None = None) -> Path:
    """Locate the vault.

    With no explicit value the default is `vault_index.default_vault_root`:
    `CIAO_VAULT_ROOT`, else the active workspace's vault, else `memory-vault`.
    A relative value resolves against `CIAO_WORKSPACE`, not the current
    directory, because the bundled engine's launcher `cd`s into the app bundle
    before exec'ing Python.
    """
    if raw is None:
        from ciao.vault_index import default_vault_root

        return default_vault_root()
    root = Path(raw).expanduser()
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
    from ciao.fts_search import ConnectedIndexBusy, ConnectedWorkspaceChanged

    try:
        return _search_vault(args)
    except (ConnectedIndexBusy, ConnectedWorkspaceChanged) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _search_vault(args: argparse.Namespace) -> int:
    from ciao import fts_search

    from ciao.config import configured_runtime_root
    runtime_arg = getattr(args, "runtime_root", None)
    source_env = dict(os.environ)
    if runtime_arg:
        source_env["CIAO_RUNTIME_ROOT"] = str(_resolve_runtime_root(runtime_arg))
    auth_runtime = configured_runtime_root(source_env)
    registration = fts_search.ConnectedRegistration.capture(auth_runtime) if not args.logs else None
    vault_root = _resolve_vault_root(args.vault_root)
    # The re-rooting promotes Logs/ out of the vault, so the archive root cannot
    # be derived from the vault root on a migrated install.
    from ciao.config import logs_root_for, logs_roots_for

    # The install root defines BOTH the stored-key base and the database: an
    # explicit --runtime-root / CIAO_RUNTIME_ROOT names `<install>/.runtime`, so
    # its parent is the authoritative install root. Deriving the key base from
    # `--vault-root` instead (e.g. `/install/personal` from
    # `/install/personal/memory-vault`) opened the install's live database while
    # writing keys, and `_ensure_path_base` then cleared every workspace's rows
    # because the base did not match the server's `/install`.
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
    # Every archive root gets indexed, so transcripts written under a workspace's
    # own Logs/ are searchable as well as the shared Logs/. `--logs` answers from
    # this vault's workspace archive plus the shared one, never a sibling's.
    all_logs_roots = logs_roots_for(key_base, vault_root, runtime_root)
    shared_logs = logs_root_for(key_base, vault_root, runtime_root)
    own_logs = vault_root.parent / "Logs"
    search_logs_roots = [
        root for root in all_logs_roots if root in (own_logs, shared_logs)
    ] or [shared_logs]
    # Install-owned, exactly like the MCP tools and startup indexing: with the
    # legacy global `~/.ciao/vault-fts.db`, a `ciao vault-search` run from a dev
    # checkout cleared the production install's index (and vice versa) because
    # the key base differs between installs.
    from ciao.config import configured_connected_workspace

    connected = (configured_connected_workspace(source_env, vault_root, implicit_root=args.vault_root is None)
                 if not args.logs else None)
    if connected:
        vault_root = connected.root
        runtime_root = auth_runtime
        from ciao.config import installed_workspace_env

        install = installed_workspace_env(os.environ).get("CIAO_WORKSPACE")
        if install:
            key_base = Path(install).expanduser().resolve()
    if registration is not None:
        registration.verify()
        if connected:
            registration.authorize(vault_root, connected.name, key_base, connected.vault_base, allow_subfolders=True, link=connected.link)
    namespace = fts_search.adopted_vault_key_prefix(vault_root, connected.name, link=connected.link) if connected else None
    db_path = (fts_search.connected_db_path(runtime_root, vault_root, connected.source_roots)
               if connected else fts_search.get_db_path(runtime_root))

    if args.rebuild and not connected and db_path.exists():
        try:
            db_path.unlink()
            print("Dropped index database for rebuild.", file=sys.stderr)
        except OSError as exc:
            print(f"Error dropping index database: {exc}", file=sys.stderr)

    from contextlib import closing

    connection = (fts_search.connected_index_connection(db_path) if connected
                  else closing(sqlite3.connect(db_path)))
    with connection as conn:
        fts_search.init_db(conn)
        if connected and namespace:
            assert registration is not None
            try:
                fts_search.refresh_connected_index(conn, vault_root, connected.name, key_base, namespace, registration)
            except OSError as exc:
                if not args.query or args.rebuild or fts_search.connected_entries(conn, namespace) is None:
                    raise
                print(f"Searching the previous local index: {exc}", file=sys.stderr)
            if not args.query:
                print(f"Updated local index for {connected.name}.", file=sys.stderr)
                return 0
        if not args.query:
            vault_indexed, vault_removed = fts_search.index_vault(conn, vault_root, path_base=key_base, key_namespace=namespace)
            logs_indexed = logs_removed = 0
            for logs_root in all_logs_roots:
                indexed, removed = fts_search.index_logs(conn, vault_root, logs_root=logs_root, path_base=key_base)
                logs_indexed += indexed
                logs_removed += removed
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
                indexed = removed = 0
                for logs_root in all_logs_roots:
                    part_indexed, part_removed = fts_search.index_logs(conn, vault_root, logs_root=logs_root, path_base=key_base)
                    indexed += part_indexed
                    removed += part_removed
                if indexed or removed:
                    print(
                        f"Transcripts index: {indexed} indexed, {removed} removed.",
                        file=sys.stderr,
                    )
            elif not connected:
                indexed, removed = fts_search.index_vault(conn, vault_root, path_base=key_base, key_namespace=namespace)
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
                path_prefixes=tuple(
                    fts_search.logs_key_prefix(root, key_base) for root in search_logs_roots
                ),
            )
            if args.logs
            else fts_search.search_vault(
                conn,
                args.query,
                limit=args.limit,
                path_prefix=fts_search.vault_key_prefix(vault_root, key_base, key_namespace=namespace),
            )
        )

    if registration is not None:
        registration.verify()
    if not results:
        print(f"No matches found for: {args.query}")
        return 0

    print(f"### Search Results for: {args.query}\n")
    for result in results:
        # Keys are relative to `key_base`, not to the vault's parent: on a
        # re-rooted install those differ by the workspace segment, so joining
        # against the parent printed a `file://` link that does not exist.
        abs_path = (vault_root / result["path"].removeprefix(namespace) if namespace
                    else key_base / result["path"])
        # as_uri, not "file://" + the path: a space or non-ASCII name broke the markdown link.
        link = abs_path.as_uri()
        print(f"- **[{result['title']}]({link})** (rank: {result['rank']})")
        if result["snippet"]:
            snippet = result["snippet"].replace("<<<", "**`").replace(">>>", "`**")
            print(f"  *{snippet}*")
        print()
    return 0


def _registered_owner(config: Any, vault: Path) -> str | None:
    """The registered workspace whose vault is exactly ``vault``, else ``None``."""
    for owner in config.workspace_names():
        try:
            owned = Path(config.workspace_vault_root(owner)).resolve()
        except ValueError:
            # One workspace's registered vault being unusable — a symlinked
            # folder, an empty root — says nothing about the workspace the
            # operator named, so it must not fail their run.
            continue
        if owned == vault:
            return str(owner)
    return None


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
                "  Pass --force to ship a bundle whose links no consumer can follow.",
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
    from ciao.config import installed_workspace_env
    from ciao.os_audit import format_audit_markdown, run_os_audit

    explicit_workspace = args.workspace is not None
    # A bare shell has no CIAO_WORKSPACE, so the audit used to run over whatever
    # directory the operator happened to be standing in. The install the
    # LaunchAgent points at is the workspace they actually have, so discovery
    # seeds the default below; an explicit --workspace is the operator naming a
    # workspace and is left exactly as it was.
    env_source: dict[str, str] = dict(os.environ)
    if not explicit_workspace:
        discovered = installed_workspace_env(os.environ)
        if discovered.get("CIAO_WORKSPACE", "").strip():
            env_source = discovered

    workspace_raw = args.workspace or env_source.get("CIAO_WORKSPACE") or Path(".")
    workspace = Path(workspace_raw).expanduser().resolve()
    # An explicit --workspace scopes the whole audit. Consulting the ambient
    # environment for the runtime and vault roots then lets an absolute
    # CIAO_RUNTIME_ROOT from the surrounding install escape the directory the
    # caller named, so the audit silently reports on the wrong workspace: its
    # registry, its job runs, its migration receipts. Auditing a second
    # workspace from inside a running Ciaobot chat hits this every time, because
    # the chat exports CIAO_RUNTIME_ROOT for its own install.

    def resolve_under_workspace(
        explicit: Path | None,
        env_name: str,
        default: str,
    ) -> Path:
        env_raw = None if explicit_workspace else env_source.get(env_name)
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

    config_source = dict(env_source)
    config_source.update({
        "CIAO_WORKSPACE": str(workspace),
        "CIAO_VAULT_ROOT": str(vault),
        "CIAO_RUNTIME_ROOT": str(runtime),
    })
    if not config_source.get("PWA_AUTH_TOKEN", "").strip():
        # Loading config for a read-only audit must not create a session
        # secret merely because the CLI was invoked outside the server env.
        # Only when blank: forcing a stand-in over an empty value would hide
        # the bootstrap combination this resolution exists to avoid.
        config_source["PWA_AUTH_TOKEN"] = "os-audit"
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


def _workspace_move_command(args: argparse.Namespace) -> int:
    """Move the install workspace to another folder and repoint the service.

    Prints the plan by default and changes nothing. --apply hands the move to a
    one-shot job beside the engine's service (the same call Settings → Main
    workspace makes), then follows it until it finishes. There is no --undo:
    moving the folder back is the same command.
    """
    from ciao import workspace_move

    if args.workspace is not None:
        source = Path(args.workspace).expanduser().resolve()
    else:
        registered = workspace_move.registered_workspace()
        source = (registered or Path(os.environ.get("CIAO_WORKSPACE") or ".")).expanduser().resolve()
    move_plan = workspace_move.plan_for(source, args.target)
    if args.json and not args.apply:
        print(json.dumps(move_plan.as_dict(), indent=2))
        return 0 if move_plan.ok else 1
    print(f"Move {move_plan.source}")
    print(f"  to {move_plan.target}")
    for refusal in move_plan.refusals:
        print(f"Refused: {refusal}")
    for warning in move_plan.warnings:
        print(f"Warning: {warning}")
    if not move_plan.ok:
        return 1
    if not args.apply:
        print("Nothing changed. Re-run with --apply to move it.")
        return 0
    from ciao.package_version import running_from_app_bundle

    if running_from_app_bundle():
        # `workspace_move.start` would refuse the launchd job anyway; say the
        # actionable answer instead, before anything is recorded.
        print(
            "In Ciaobot for Mac, move the workspace from Settings → Ciaobot folder → Move…",
            file=sys.stderr,
        )
        return 1
    if sys.platform.startswith("linux"):
        print("Moving. Ciaobot stops once running chats finish, and starts again from the new folder.")
    try:
        op = workspace_move.start(move_plan, port=workspace_move.engine_port())
    except workspace_move.MoveError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    seen = ""
    while True:
        current = workspace_move.read_operation()
        if current is None or current.id != op.id:
            print("Error: the move's record was replaced or removed.", file=sys.stderr)
            return 1
        if current.phase != seen:
            seen = current.phase
            print(f"- {seen}")
        if current.phase in workspace_move.TERMINAL_PHASES:
            if current.error:
                print(current.error, file=sys.stderr)
            return 0 if current.phase == "done" else 1
        if workspace_move.abandoned(current):
            print(
                f"Error: the move job stopped during '{current.phase}' without finishing. "
                f"See {workspace_move.default_state_dir() / workspace_move.JOB_LOG_NAME}.",
                file=sys.stderr,
            )
            return 1
        time.sleep(1)


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
    from ciao.config import CiaoConfig, installed_workspace_env
    from ciao.memory_tool import DEFAULT_MEMORY_CHAR_LIMIT, DEFAULT_USER_CHAR_LIMIT
    from ciao.os_audit import (
        _aggregate_memory_guides,
        _memory_guide_specs,
        _scan_memory_guide,
        _scan_proposals,
        memory_actionable_count,
    )

    explicit_workspace = args.workspace is not None
    # A bare shell has no CIAO_WORKSPACE, so the audit used to read the guide
    # and state under whatever directory the operator was in. Discovery seeds
    # the installed root instead; an explicit --workspace is left as it was.
    env_source: dict[str, str] = dict(os.environ)
    if not explicit_workspace:
        discovered = installed_workspace_env(os.environ)
        if discovered.get("CIAO_WORKSPACE", "").strip():
            env_source = discovered

    workspace_raw = args.workspace or env_source.get("CIAO_WORKSPACE") or Path(".")
    workspace = Path(workspace_raw).expanduser().resolve()
    vault_raw = args.vault_root or env_source.get("CIAO_VAULT_ROOT") or "memory-vault"
    vault = Path(vault_raw).expanduser()
    if not vault.is_absolute():
        vault = workspace / vault
    vault = vault.resolve()

    config_source = dict(env_source)
    config_source.update({
        "CIAO_WORKSPACE": str(workspace),
        "CIAO_VAULT_ROOT": str(vault),
    })
    if not config_source.get("PWA_AUTH_TOKEN", "").strip():
        # Loading config for a read-only audit must not create a session
        # secret merely because the CLI was invoked outside the server env.
        # Only when blank: forcing a stand-in over an empty value would hide
        # the bootstrap combination this resolution exists to avoid.
        config_source["PWA_AUTH_TOKEN"] = "memory-audit"
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
    unsure_count, unsure_files, unsure_errors = _scan_proposals(
        vault if vault.exists() else None, None, ""
    )
    report = _aggregate_memory_guides(
        guides,
        pending_unsure_items=unsure_count,
        unsure_files=unsure_files,
        errors=unsure_errors,
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
            from ciao.config import configured_connected_workspace
            from ciao.fts_search import adopted_vault_key_prefix, connected_db_path

            connected = configured_connected_workspace(config_source, vault)
            runtime = Path(config.state_path).parent
            if connected:
                directory = connected_db_path(runtime, vault, connected.source_roots).parent
                key_prefix = adopted_vault_key_prefix(vault, connected.name, link=connected.link)
            else:
                directory = runtime
                key_prefix = vault_key_prefix(vault, Path(config.workspace_root))
            hit_paths = read_search_hit_paths(directory)
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
                "Notes not verified within the review horizon (informational): "
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


def _refuse_ambiguous_vault_write(args: argparse.Namespace) -> int | None:
    """Refuse a vault write when CIAO_VAULT_ROOT and the active workspace disagree.

    Called on the write/apply path only, by every command that resolves its vault
    implicitly. An explicit ``--workspace`` or ``--vault-root`` names the vault,
    so only the implicit resolution is checked.
    """
    if getattr(args, "vault_root", None) or getattr(args, "workspace", None):
        return None
    from ciao.vault_index import refuse_conflicting_vault_write

    return refuse_conflicting_vault_write()


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
    For an explicit ``--vault-root`` it is the registered workspace that owns that
    vault, and the directory's own name only when the registry does not know it.
    It is ``None`` when no name was resolved at all (no ``--vault-root`` and no
    registered ``CIAO_ACTIVE_WORKSPACE``). The entry pass is skipped and reported
    in that case rather than planned under a guess.
    """
    active = os.environ.get("CIAO_ACTIVE_WORKSPACE", "").strip()
    if not getattr(args, "vault_root", None) and not getattr(args, "workspace", None):
        if active:
            try:
                from ciao.config import CiaoConfig, installed_workspace_env

                # A read-only resolution must not mint a session secret just
                # because the CLI runs outside the server env (same rule as
                # the memory-audit command). Discovery first: with
                # `CIAO_ACTIVE_WORKSPACE` set and no `CIAO_WORKSPACE`, an
                # explicit env skipped the install and `from_env` took the
                # bootstrap branch, minting `~/.ciao/bootstrap/.runtime/...`.
                env_source = installed_workspace_env(os.environ)
                if not env_source.get("PWA_AUTH_TOKEN", "").strip():
                    env_source["PWA_AUTH_TOKEN"] = "memory-proposals"
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
    name: str | None = None
    discovered_workspace: str | None = None
    if getattr(args, "vault_root", None):
        from ciao.config import CiaoConfig, installed_workspace_env

        # The registry this install root holds is the authority, as it is
        # everywhere: an explicit vault the registry knows is named
        # by its owner, and only a vault it does not know falls back to the
        # directory's own name.
        if getattr(args, "workspace", None) or os.environ.get("CIAO_WORKSPACE", "").strip():
            env_source = dict(os.environ)
            env_source["CIAO_WORKSPACE"] = str(workspace)
        else:
            # Neither names an install — a bare shell, so `workspace` above is the
            # cwd — and its registry knows no vault. Read the installed one.
            env_source = installed_workspace_env(os.environ)
            if not env_source.get("CIAO_WORKSPACE", "").strip():
                # Not `setdefault`: an exported empty `CIAO_WORKSPACE=` survives the
                # helper (the process wins the merge), and `setdefault` would leave it
                # in place — `from_env` then sees no workspace with a token, enters
                # bootstrap mode and MINTS a bootstrap secret while resolving a name.
                env_source["CIAO_WORKSPACE"] = str(workspace)
            else:
                # Discovery found the install this bare shell is actually talking
                # to, and that install root — not the cwd — is the workspace the
                # vault is named after and its guide and state live under.
                discovered_workspace = env_source["CIAO_WORKSPACE"].strip()
        if not env_source.get("PWA_AUTH_TOKEN", "").strip():
            # Same reason as the workspace above, one level down: the install's
            # `.env` is overlaid into this mapping and keeps its empty values.
            env_source["PWA_AUTH_TOKEN"] = "memory-proposals"
        config = CiaoConfig.from_env(env_source)
        name = _registered_owner(config, resolved) or resolved.name
    if discovered_workspace:
        discovered_path = Path(discovered_workspace).expanduser().resolve()
        if discovered_path != workspace:
            workspace = discovered_path
    return workspace, resolved, resolved, name


def _decisions_digest_command(args: argparse.Namespace) -> int:
    """Print the operator's recent decisions grouped for the weekly rules pass.

    Read-only. Operator decisions only; agent-made ones are counted, never listed.
    """
    from datetime import UTC, datetime

    from ciao import decision_rules

    _workspace, vault = _resolve_workspace_and_vault(args)
    digest = decision_rules.build_digest(vault, today=datetime.now(UTC).date())
    if args.json:
        json.dump(digest, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    for kind in decision_rules.KINDS:
        section = digest[kind]
        print(
            f"{kind}: {section['rows']} operator decisions, {len(section['groups'])} groups, "
            f"rules file {'present' if section['file_present'] else 'absent'}"
        )
    return 0


def _task_rules_command(args: argparse.Namespace) -> int:
    """Print the operator's distilled task rules, fenced, or nothing when absent."""
    from ciao import decision_rules

    _workspace, vault = _resolve_workspace_and_vault(args)
    text = decision_rules.load_for_prompt(vault, decision_rules.TASK_RULES_NAME)
    if text:
        print(text)
    return 0


def _decision_rules_write_command(args: argparse.Namespace) -> int:
    """Write one rules note's managed section from a body file."""
    refused = _refuse_ambiguous_vault_write(args)
    if refused is not None:
        return refused
    from datetime import UTC, datetime

    from ciao import decision_rules

    body = _read_persisted_text_file(args.body_file, "--body-file")
    if body is None:
        return 2
    _workspace, vault = _resolve_workspace_and_vault(args)
    try:
        result = decision_rules.write_managed(vault, args.kind, body, today=datetime.now(UTC).date())
    except (decision_rules.RulesError, OSError) as exc:
        print(f"ciao decision-rules-write: {exc}", file=sys.stderr)
        return 2
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(f"{result['file']}: wrote {result['chars']} characters")
    return 0


def _proposal_check_command(args: argparse.Namespace) -> int:
    """Validate one proposal file and print every problem, so the agent can fix it."""
    from ciao import proposal_files
    from ciao.execution_modes import is_credential_adjacent

    target = Path(args.file).expanduser()
    try:
        resolved = target.resolve()
    except OSError as exc:
        print(f"ciao proposal-check: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    if is_credential_adjacent(resolved):
        print(f"ciao proposal-check: refuses credential-adjacent paths ({args.file})", file=sys.stderr)
        return 2
    try:
        with resolved.open(encoding="utf-8") as handle:
            text = handle.read(proposal_files.MAX_FILE_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        print(f"ciao proposal-check: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    problems = proposal_files.check(text, filename=resolved.name, parent=resolved.parent.name)
    if args.json:
        json.dump({"file": str(resolved), "ok": not problems, "problems": problems}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    elif problems:
        print(f"{resolved.name}: {len(problems)} problem(s) to fix")
        for problem in problems:
            print(f"- {problem}")
    else:
        print(f"{resolved.name}: ok")
    return 1 if problems else 0


def _read_persisted_text_file(path: str, flag: str) -> str | None:
    """The text of a file whose contents are persisted into the vault.

    ``None`` after printing why not. A reason file is written to the
    vault-tracked proposal queue or decision sidecar, so a path that names a
    credential store (``.env``, the runtime directory) is refused, the same
    check ``agent_cli._task_body`` applies. At most ``MAX_REASON * 4 + 1``
    characters are read so a huge file is never loaded whole; the caller's own
    length check then refuses an over-long reason.
    """
    from ciao.execution_modes import is_credential_adjacent
    from ciao.task_decisions import MAX_REASON

    target = Path(path).expanduser()
    try:
        resolved = target.resolve()
    except OSError as exc:
        print(f"{flag} cannot be read: {exc}", file=sys.stderr)
        return None
    if is_credential_adjacent(resolved):
        print(
            f"{flag} refuses credential-adjacent paths ({path}): "
            "pass the content in a workspace file instead.",
            file=sys.stderr,
        )
        return None
    try:
        with resolved.open(encoding="utf-8") as handle:
            return handle.read(MAX_REASON * 4 + 1)
    except (OSError, UnicodeError) as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return None


# The reason an automated dismissal records when its caller gives none. Fixed,
# so it never echoes proposal text, and History can say why the row was settled.
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


def _skills_list_command(args: argparse.Namespace) -> int:
    from ciao.skills_inventory import build_skill_inventory

    workspace_root = Path(args.workspace).expanduser().resolve()
    inventory = build_skill_inventory(workspace_root)
    json.dump(inventory, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


#: A draft decision an unattended run asked for. Distinct from 1 ("the command
#: could not do it") because nothing failed: the request was well-formed, the
#: draft exists, and the answer is that this run has no reviewer. 4 is EPERM,
#: and a scheduled agent that sees it can report the item as deferred rather than
#: retrying an action it is not allowed to take.


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
    from ciao.config import DOTENV_IGNORED_KEYS

    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
            continue
        key, value = cleaned.split("=", 1)
        key = key.strip()
        if key and key not in os.environ and key not in DOTENV_IGNORED_KEYS:
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

    # The bind address (Settings → Access & devices → Network access) is where the
    # server listens. For a loopback/wildcard bind we emit "localhost" so the
    # printed chat link matches the host the browser is authenticated on (the
    # menu bar, setup, and login URLs all use localhost). The session cookie is
    # host-only, so a "127.0.0.1" link would not carry the "localhost" cookie
    # and every /ws and authed /api request would be rejected.
    from ciao.app_settings import read_app_settings

    # The runtime root is `<workspace>/.runtime` unless the process
    # environment names another (a `.env` line is not read).
    runtime_env = os.environ.get("CIAO_RUNTIME_ROOT", "").strip()
    runtime_root = (
        Path(runtime_env).expanduser() if runtime_env else Path(RUNTIME_DIR_NAME)
    )
    if not runtime_root.is_absolute():
        runtime_root = workspace_root / runtime_root
    host = read_app_settings(runtime_root / "app_settings.json").pwa_host
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

    root = _validate_service_workspace(workspace)
    from ciao.setup_status import tcc_protected_location

    # Activation (E2): a verified host selects the hosted service, otherwise the
    # direct shape is written. The only caller registers when no definition is
    # installed, so there is no hosted definition here to preserve.
    host = _verified_host_if_installed()
    protected = tcc_protected_location(root)
    if protected and host is None:
        raise RuntimeError(
            f"{root} is inside ~/{protected}. macOS lets the background engine into that "
            "folder only when it runs as the Ciaobot Server app, and no verified Ciaobot "
            "Server is installed on this Mac. Re-run the Ciaobot installer, or move the "
            "workspace out of Desktop/Documents/Downloads first."
        )

    runtime_root = root / RUNTIME_DIR_NAME
    launch_dir = default_launch_agents_dir()
    resolved_engine = os.environ.get("CIAO_ENGINE_PATH", "").strip() or sys.executable
    host_python = _host_engine_interpreter(resolved_engine) if host is not None else None
    try:
        return _write_launchd_plist(
            workspace=root,
            launch_agents_dir=launch_dir,
            engine_path=resolved_engine,
            runtime_root=runtime_root,
            port=_pwa_port_from_env(root, macos_service.DEFAULT_PORT),
            path=os.environ.get("PATH", ""),
            host=host,
            host_python=host_python,
        )
    except ValueError as exc:
        # The render-time refusal (a host given without the python it must
        # serve) surfaces as the RuntimeError/ServiceResult message its callers
        # already handle, never a traceback.
        raise RuntimeError(str(exc)) from exc


def _mac_takeover_command(args: argparse.Namespace) -> int:
    from ciao import mac_takeover

    return mac_takeover.run_cli(args)


def _vault_rebind_command(args: argparse.Namespace) -> int:
    from ciao import vault_rebind

    return vault_rebind.run_cli(args)


def _macos_service_command(args: argparse.Namespace) -> int:
    from ciao import macos_service

    action = args.service_action
    as_json = bool(args.as_json)
    if getattr(args, "deprecated_alias", False) and not as_json:
        print("`ciao desktop-service` is deprecated; use `ciao service`.", file=sys.stderr)
    if sys.platform != "darwin":
        return macos_service.print_result(
            macos_service.ServiceResult(
                False,
                str(action),
                "`ciao service` manages the macOS LaunchAgent. On Linux use "
                "`ciao linux-service` and systemctl.",
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
        # The run that is there before the kickstart (if any) must not pass
        # for the one the kickstart starts, so its identity is read first.
        previous = macos_service.read_lock_owner(runtime)
        started_at = time.time()
        result = macos_service.start_service(runtime=runtime)
        if result.ok:
            # `start_service` submits the start; it does not confirm it. The
            # engine has to boot before it can fail, so a start reported
            # before the engine answered as itself reports a hope (#1654).
            # The wait ends when the new engine answers with the identity its
            # lock carries, when it records a bind failure or a denial, or at
            # the deadline, which is a failure, not a start (#1938).
            outcome = macos_service.wait_for_engine(
                runtime, previous=previous, started_at=started_at,
            )
            if outcome.state == "timeout":
                # The weaker bind-failure signal, launchd's last exit code,
                # is read once the deadline passed: an unsupervised engine
                # that exited on a taken port leaves no record of its own.
                reason = macos_service.port_in_use_reason(runtime)
                if reason is not None:
                    outcome = macos_service.StartOutcome("port_in_use", reason)
            if outcome.ok:
                result = macos_service.ServiceResult(
                    True,
                    "start",
                    outcome.message,
                    {
                        **result.details,
                        "verified": True,
                        "startup_id": outcome.startup_id,
                        "pid": outcome.pid,
                    },
                )
            else:
                result = macos_service.ServiceResult(
                    False,
                    "start",
                    outcome.message,
                    {
                        **result.details,
                        "verified": False,
                        "start_outcome": outcome.state,
                        "port_in_use": outcome.state == "port_in_use",
                    },
                )
    elif action == "restart":
        result = macos_service.restart_service(force=bool(args.force))
    elif action == "stop":
        result = macos_service.stop_service(force=bool(args.force))
    elif action == "login":
        result = macos_service.set_login_enabled(args.login_action == "enable")
    else:  # pragma: no cover - argparse constrains the action.
        parser_error = macos_service.ServiceResult(
            False,
            str(action),
            "Unknown service action.",
            {},
        )
        return macos_service.print_result(parser_error, as_json=as_json)
    return macos_service.print_result(result, as_json=as_json)


def _validate_service_workspace(workspace: Path) -> Path:
    """Resolve and check a workspace the engine service may serve.

    The two checks `_register_launchd_service` makes before writing a plist: a
    Ciaobot workspace has been set up, and the app's own source checkout never
    is one. The macOS TCC check is left out -- it lives in
    `_register_launchd_service` and applies only to a Mac with no verified
    Ciaobot Server host.
    """
    from ciao.setup_marker import is_set_up_workspace

    root = workspace.expanduser().resolve()
    if not is_set_up_workspace(root):
        raise RuntimeError(
            f"{root} is not a Ciaobot workspace (no .runtime/workspaces.json). "
            f"Run `ciao setup --workspace {root}` first."
        )
    if _looks_like_source_checkout(root):
        raise RuntimeError(
            f"{root} looks like the Ciaobot source checkout, not a workspace. "
            "Pass your workspace folder to --workspace."
        )
    return root


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


def _service_command(args: argparse.Namespace) -> int:
    if getattr(args, "deprecated_alias", False):
        print("`ciao desktop-service` is deprecated; use `ciao service`.", file=sys.stderr)
    if not sys.platform.startswith("linux"):
        print("Error: ciao service is Linux only.", file=sys.stderr)
        return 2
    if os.geteuid() == 0:
        print("Error: run as the account that will own Ciaobot, not root.", file=sys.stderr)
        return 2
    import pwd  # noqa: PLC0415  # POSIX-only module: lives in the Linux branch.

    from ciao import linux_service  # noqa: PLC0415

    # The account is who the unit will run as, not whoever holds $HOME (#3131 D2).
    identity = pwd.getpwuid(os.geteuid())
    # Do not resolve python (see render_service's comment).
    python = Path(sys.prefix) / "bin" / "python"
    workspace = args.workspace or linux_service.unit_workspace(linux_service.UNIT_PATH)
    if workspace is None:
        print(
            "Error: pass --workspace: no installed unit records a workspace for this account.",
            file=sys.stderr,
        )
        return 2
    try:
        plan = linux_service.plan_install(
            workspace=workspace, user=identity.pw_name, home=Path(identity.pw_dir),
            python=python, replace=args.replace, start=not args.no_start,
            unit_path=linux_service.UNIT_PATH,
        )
    except linux_service.UnitConflict as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(plan.unit, end="")
        for argv, stdin in plan.commands:
            print(f"# would run: {shlex.join(argv)}")
        return 0

    def run(argv: Sequence[str], stdin: str | None) -> None:
        subprocess.run(argv, input=stdin, text=True, encoding="utf-8", check=True)

    try:
        linux_service.run_plan(plan, run=run)
    except subprocess.CalledProcessError as exc:
        # Earlier steps ran and later ones did not, so the operator must be
        # told which step died and that the unit may be half installed.
        shown = shlex.join([str(part) for part in (exc.cmd or ())][:3])
        print(f"Error: {shown} failed with exit code {exc.returncode}.", file=sys.stderr)
        print(
            "The install may be half applied: inspect it with `systemctl status ciaobot`.",
            file=sys.stderr,
        )
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ciao", description="Ciaobot local assistant CLI.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the Ciaobot server.")
    run_parser.add_argument(
        "--supervised",
        action="store_true",
        help="Exit with the restart code instead of re-execing; for `ciao supervise`.",
    )
    # App-owned engine mode (#3129): the Mac app launches this exact flag and
    # supervises the process itself. Not operator surface: hidden from --help,
    # and refused outside the bundle at dispatch.
    run_parser.add_argument("--app-engine", action="store_true", help=argparse.SUPPRESS)
    # The app's DEBUG test launch picks the engine's port with this; it
    # replaces the default for this process and is refused without --app-engine.
    run_parser.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)
    run_parser.add_argument("--app-engine-ready-fd", type=int, help=argparse.SUPPRESS)
    run_parser.set_defaults(func=_run_command)

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
        for action in ("status", "start", "restart", "stop"):
            action_parser = service_sub.add_parser(action)
            action_parser.add_argument("--json", action="store_true", dest="as_json")
            if action in {"restart", "stop"}:
                action_parser.add_argument(
                    "--force",
                    action="store_true",
                    help="Proceed even when chats are active (after UI confirmation).",
                )
            if action == "start":
                action_parser.add_argument(
                    "--workspace",
                    type=Path,
                    default=None,
                    help="Register the LaunchAgent for this workspace first if it is not installed.",
                )
            action_parser.set_defaults(
                func=_macos_service_command,
                deprecated_alias=deprecated,
            )
        login_parser = service_sub.add_parser("login")
        login_parser.add_argument("login_action", choices=("enable", "disable"))
        login_parser.add_argument("--json", action="store_true", dest="as_json")
        login_parser.set_defaults(
            func=_macos_service_command,
            deprecated_alias=deprecated,
        )
        # #3131: the Linux system-service install shares this group so
        # `ciao service install` reads like the other service verbs. It is
        # registered only on `service`: `desktop-service` is the deprecated
        # macOS alias, and the handler refuses non-Linux and root invocations
        # itself.
        if not deprecated:
            install_parser = service_sub.add_parser(
                "install",
                help="Install the Linux systemd service for this account.",
            )
            install_parser.add_argument(
                "--workspace", type=Path,
                help="Workspace directory (default: the one the installed unit records).",
            )
            install_parser.add_argument(
                "--replace", action="store_true",
                help="Take over an existing unit that runs another interpreter (a source install).",
            )
            install_parser.add_argument(
                "--no-start", action="store_true",
                help="Install and enable the unit, but do not restart the service.",
            )
            install_parser.add_argument(
                "--dry-run", action="store_true",
                help="Print the unit and the commands without running anything.",
            )
            install_parser.set_defaults(
                func=_service_command,
                deprecated_alias=deprecated,
            )

    add_service_parser(
        "service",
        "Service control: start, stop and inspect on macOS (launchd), and install the Linux systemd unit.",
        deprecated=False,
    )
    add_service_parser(
        "desktop-service",
        "Deprecated alias of `ciao service` (used by Ciaobot.app).",
        deprecated=True,
    )

    takeover_parser = subparsers.add_parser(
        "mac-takeover",
        help="Ciaobot.app only: classify this Mac's 1.x engine for takeover "
        "(plan is read-only; apply/finish/rollback are the journalled transaction).",
    )
    takeover_sub = takeover_parser.add_subparsers(
        dest="mac_takeover_action", required=True
    )
    takeover_plan_parser = takeover_sub.add_parser(
        "plan", help="Print the takeover plan as JSON; changes nothing."
    )
    takeover_plan_parser.add_argument("--json", action="store_true", dest="as_json")
    takeover_plan_parser.add_argument(
        "--launch-agents-dir", type=Path, default=None
    )
    takeover_plan_parser.add_argument("--label", default="com.ciao.server")
    takeover_plan_parser.set_defaults(func=_mac_takeover_command)

    takeover_apply_parser = takeover_sub.add_parser(
        "apply",
        help="Ciaobot.app only: back up the old plist, stop the old engine and "
        "hand the workspace to the bundled engine (journalled).",
    )
    takeover_apply_parser.add_argument("--json", action="store_true", dest="as_json")
    takeover_apply_parser.add_argument(
        "--expect-sha256",
        required=True,
        help="The plist sha256 the plan reported, which must still match.",
    )
    takeover_apply_parser.add_argument(
        "--launch-agents-dir", type=Path, default=None
    )
    takeover_apply_parser.add_argument("--label", default="com.ciao.server")
    takeover_apply_parser.set_defaults(func=_mac_takeover_command)

    takeover_finish_parser = takeover_sub.add_parser(
        "finish",
        help="Ciaobot.app only: prove the bundled engine is healthy and list the "
        "leftovers it may trash; --done closes the takeover after trashing.",
    )
    takeover_finish_parser.add_argument("--json", action="store_true", dest="as_json")
    takeover_finish_parser.add_argument(
        "--id", required=True, dest="takeover_id", help="The takeover id that apply returned."
    )
    takeover_finish_parser.add_argument(
        "--done",
        action="store_true",
        dest="done",
        help="Close the takeover's journal (call after trashing).",
    )
    takeover_finish_parser.set_defaults(func=_mac_takeover_command)

    takeover_rollback_parser = takeover_sub.add_parser(
        "rollback",
        help="Ciaobot.app only: restore the 1.x engine after an unfinished takeover.",
    )
    takeover_rollback_parser.add_argument("--json", action="store_true", dest="as_json")
    takeover_rollback_parser.add_argument(
        "--id", required=True, dest="takeover_id", help="The takeover id that apply returned."
    )
    takeover_rollback_parser.set_defaults(func=_mac_takeover_command)

    rebind_parser = subparsers.add_parser(
        "vault-rebind",
        help="Point an existing workspace at an already prepared notes folder "
        "(preview is read-only; apply, reverse and recover need the engine stopped).",
    )
    rebind_sub = rebind_parser.add_subparsers(dest="vault_rebind_action", required=True)

    def add_rebind(action: str, help_text: str, *, destination: bool, operation: bool, token: bool) -> None:
        sub = rebind_sub.add_parser(action, help=help_text)
        sub.add_argument("name", help="The workspace name in the registry.")
        if destination:
            sub.add_argument("destination", help="Absolute path of the prepared notes folder.")
        if operation:
            sub.add_argument("--id", required=True, dest="operation", help="The id apply returned.")
        if token:
            sub.add_argument("--token", required=True, help="The token the matching preview printed.")
        sub.add_argument("--root", type=Path, default=Path("."), help="Ciaobot install directory.")
        sub.set_defaults(func=_vault_rebind_command)

    add_rebind("preview", "Print the exact change and a review token; changes nothing.", destination=True, operation=False, token=False)
    add_rebind("apply", "Rebind the workspace (engine stopped, reviewed token required).", destination=True, operation=False, token=True)
    add_rebind("reverse-preview", "Print the reversal of a completed rebind; changes nothing.", destination=False, operation=True, token=False)
    add_rebind("reverse", "Put the old folder back (engine stopped, reviewed token required).", destination=False, operation=True, token=True)
    recover_parser = rebind_sub.add_parser("recover", help="Resolve an interrupted rebind (engine stopped).")
    recover_parser.add_argument("--root", type=Path, default=Path("."), help="Ciaobot install directory.")
    recover_parser.set_defaults(func=_vault_rebind_command)

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
        "--notes-folder", type=Path, default=None,
        help="Use an existing separate notes directory for a new installation.",
    )
    setup_parser.add_argument(
        "--initialize-git",
        action="store_true",
        default=False,
        help=("Create local history and an initial commit of nonignored workspace "
              "and external-vault contents; local only, no remote or upload."),
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
    lint_parser.set_defaults(func=_vault_lint_command)

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

    move_parser = subparsers.add_parser(
        "workspace-move",
        help="Move the install workspace to another folder.",
        description=(
            "Move the whole install workspace (.env, .runtime, the vault, skills, "
            "every agent root) to TARGET on the same disk, repair what recorded "
            "the old path, repoint the engine's service and restart it. Prints "
            "the plan by default and changes nothing; --apply performs it. "
            "Settings -> Advanced -> Ciaobot folder runs the same move. To undo, move it back."
        ),
    )
    move_parser.add_argument("target", help="The new folder. It must not exist, or be empty.")
    move_parser.add_argument(
        "--workspace", type=Path, default=None,
        help="The workspace to move. Defaults to the one the service runs.",
    )
    move_parser.add_argument("--apply", action="store_true", help="Perform the move.")
    move_parser.add_argument("--json", action="store_true", help="Print the plan as JSON.")
    move_parser.set_defaults(func=_workspace_move_command)

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
            "review horizon. Read-only. Exit 0 when clean, 1 when there are "
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
            "against the review horizon) and report stale ones. Informational: "
            "they never change the exit code."
        ),
    )
    memory_audit_parser.set_defaults(func=_memory_audit_command)

    decisions_digest_parser = subparsers.add_parser(
        "decisions-digest",
        help="Group the operator's recent memory and task decisions for the weekly rules pass.",
        description=(
            "Read-only. Emits the operator's decisions of the last 180 days grouped by "
            "reason chip, kind and task state, with counts, sanitised excerpts and "
            "ready-made citations. Agent-made decisions are only counted."
        ),
    )
    decisions_digest_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
    )
    decisions_digest_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
    )
    decisions_digest_parser.add_argument(
        "--json", action="store_true", help="Emit the full digest as JSON."
    )
    decisions_digest_parser.set_defaults(func=_decisions_digest_command)

    task_rules_parser = subparsers.add_parser(
        "task-rules",
        help="Print the operator's distilled task preferences (Workspace/Task-Rules.md).",
        description=(
            "Read-only. Prints Workspace/Task-Rules.md fenced as evidence of the "
            "operator's preferences, never orders; prints nothing when the file is "
            "absent or fails its checks. Run it before filing a task."
        ),
    )
    task_rules_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
    )
    task_rules_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
    )
    task_rules_parser.set_defaults(func=_task_rules_command)

    decision_rules_write_parser = subparsers.add_parser(
        "decision-rules-write",
        help="Write the managed section of Memory-Review-Rules.md or Task-Rules.md.",
        description=(
            "The only way the weekly pass writes a rules note. Replaces only the span "
            "between the ciao:rules markers; refuses a body over 2500 characters, a "
            "malformed fence, an uncited or unknown rule, or an unapproved verdict flip."
        ),
    )
    decision_rules_write_parser.add_argument("kind", choices=["memory", "task"])
    decision_rules_write_parser.add_argument(
        "--body-file",
        required=True,
        help="File holding the managed rule lines.",
    )
    decision_rules_write_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace root. Defaults to CIAO_WORKSPACE or current directory.",
    )
    decision_rules_write_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or <workspace>/memory-vault.",
    )
    decision_rules_write_parser.add_argument(
        "--json", action="store_true", help="Emit the structured result as JSON."
    )
    decision_rules_write_parser.set_defaults(func=_decision_rules_write_command)

    proposal_check_parser = subparsers.add_parser(
        "proposal-check",
        help="Check a Workspace/Proposals/<slug>.md file for the required format.",
        description=(
            "Validates a proposal file (type skill-edit, upstream-issue, guide-edit or "
            "note-retire): front matter fields, values, evidence, body and credential-shaped "
            "text. Prints every problem so the file can be fixed; exit 0 valid, 1 problems, "
            "2 unreadable. Never edits the file."
        ),
    )
    proposal_check_parser.add_argument("file", help="The proposal file to check.")
    proposal_check_parser.add_argument(
        "--json", action="store_true", help="Emit the result as JSON."
    )
    proposal_check_parser.set_defaults(func=_proposal_check_command)

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
        help="Ciaobot server URL. Defaults to the bind address in Settings and PWA_PORT.",
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


# EX_NOPERM.
RUNTIME_STATE_UNREADABLE_EXIT = 77


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    os.environ.setdefault("CLAUDE_CODE_DISABLE_ARTIFACT", "1")
    argv_list = list(sys.argv[1:] if argv is None else argv)
    try:
        return _dispatch(argv_list)
    except Exception as exc:
        # The import lives in the handler so the agent fast path never pays for it.
        from ciao.workspace_reroot import RuntimeStateUnreadable  # noqa: PLC0415

        if not isinstance(exc, RuntimeStateUnreadable):
            raise
        print(f"ciao: {exc}", file=sys.stderr)
        return RUNTIME_STATE_UNREADABLE_EXIT


def _dispatch(argv_list: list[str]) -> int:
    if argv_list == ["--version"]:
        from ciao import __version__  # noqa: PLC0415

        print(f"ciao {__version__}")
        return 0
    # The agent surface (`ciao memory …`, `ciao vault search …`): dispatch
    # before building the operator parser so a call from a chat costs
    # interpreter start-up, not this module's import graph. ``ciao.agent_cli``
    # owns which words route there (``ciao run`` alone stays the server).
    from ciao.agent_cli import is_agent_invocation, main as agent_main

    if is_agent_invocation(argv_list):
        return agent_main(argv_list)
    if argv_list[:1] == ["update"]:
        from ciao.engine_update import main as update_main

        rest = argv_list[1:]
        # On Linux `ciao update` re-runs the latest installer. The receiving
        # verbs keep meaning the update record and the 1.x hop's detached job,
        # so they stay on engine_update, which the updater job also runs.
        receiver_verbs = {"status", "run-apply", "run-recover"}
        if sys.platform.startswith("linux") and not (rest and rest[0] in receiver_verbs):
            from ciao.package_version import detect_install_mode  # noqa: PLC0415

            # A source checkout is updated with git. The installer would replace
            # the venv the checkout runs from.
            if detect_install_mode() == "editable":
                print(
                    "This is a source checkout; update it with git, not ciao update.",
                    file=sys.stderr,
                )
                return 2
            from ciao.linux_service import run_update  # noqa: PLC0415

            return run_update(rest)
        return update_main(rest)
    parser = build_parser()
    args = parser.parse_args(argv_list)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
