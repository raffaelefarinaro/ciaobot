"""``ciao gws`` — profile-aware passthrough to the ``gws`` CLI.

Replaces the old ``scripts/gws-profile.sh`` bash wrapper so the same command
works on a dev checkout and inside an installed Ciaobot.app (where the repo
script does not ship). The bundled ``ciao`` executable ships with every
install, making it the single canonical terminal entry point for Google
Workspace calls.

It resolves the profile the same way the bash wrapper did, computes the
per-profile ``GOOGLE_WORKSPACE_CLI_CONFIG_DIR`` via ``ciao.gws_auth`` (the single
source of truth), and ``exec``s ``gws`` with the remaining arguments.

Security invariants: no OAuth material is printed or logged; credentials
reside only in the profile's credential directory under the workspace.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence

from ciao.gws_auth import GWS_SERVICE_NAMES, profile_config_dir
from ciao.tool_path import login_shell_path, resolve_tool


def _configured_workspace_root(config) -> Path | None:
    """Resolve the real workspace root when ``CiaoConfig.from_env`` cannot.

    When ``ciao gws`` is run from a plain terminal (no ``CIAO_WORKSPACE`` in the
    environment), ``CiaoConfig.from_env`` falls back to the bootstrap workspace
    rather than the configured install root. Recover the actual root from the
    LaunchAgent plist so the credential directory points at the workspace the
    Settings → Workspaces card shows, not ``~/.ciao/bootstrap``.
    """
    from ciao.config import _bootstrap_workspace
    from ciao.macos_service import default_launch_agents_dir

    configured = str(getattr(config, "workspace_root", "") or "")
    if not configured:
        return None
    resolved = Path(configured).expanduser().resolve()
    if resolved != _bootstrap_workspace(os.environ):
        # Real (server-spawned) or explicitly configured workspace: trust it.
        return resolved
    try:
        import plistlib

        plist = default_launch_agents_dir() / "com.ciao.server.plist"
        with plist.open("rb") as handle:
            data = plistlib.load(handle)
        workspace = (data.get("EnvironmentVariables") or {}).get("CIAO_WORKSPACE")
        if workspace:
            return Path(str(workspace)).expanduser().resolve()
    except (OSError, ValueError):
        pass
    return None


def _gws_environment(workspace_root: Path, profile: str) -> dict[str, str]:
    """Environment for a profile-aware ``gws`` invocation.

    Sets ``GOOGLE_WORKSPACE_CLI_CONFIG_DIR`` to the profile's credential dir and
    unsets ``GOOGLE_APPLICATION_CREDENTIALS`` (the workspace ``.env`` stores it
    as a base64 string meant for the BigQuery runner; ``gws`` expects a file
    path and must use its own OAuth token cache, not a service account).
    """
    from types import SimpleNamespace

    config = SimpleNamespace(workspace_root=str(workspace_root))
    config_dir = profile_config_dir(config, profile)
    if config_dir is None:
        raise ValueError(f"'{profile}' is not a usable profile name")
    env = dict(os.environ)
    env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(config_dir)
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    # Resolve ``gws``-spawned subprocesses against the login-shell PATH the same
    # way the server does.
    env["PATH"] = login_shell_path()
    return env


def _split_profile_and_args(argv: Sequence[str]) -> tuple[str | None, list[str]]:
    """Extract an explicit ``--profile`` and a leading positional profile.

    Returns ``(profile, remaining_args)``. ``--profile <name>`` wins; otherwise
    a leading positional that is not a ``gws`` service name (and does not start
    with ``-``) is treated as the profile, mirroring the bash wrapper's
    disambiguation. ``GWS_SERVICE_NAMES`` collisions must be passed via
    ``GWS_PROFILE`` or ``--profile``.
    """
    args = list(argv)
    profile: str | None = None
    rest: list[str] = []
    if args and args[0] == "--profile" and len(args) >= 2:
        profile = args[1]
        args = args[2:]
    if profile is None and args and not args[0].startswith("-") and args[0] not in GWS_SERVICE_NAMES:
        profile = args.pop(0)
    rest = args
    return profile, rest


def main(argv: list[str] | None = None) -> int:
    from ciao.config import CiaoConfig

    args = list(sys.argv[1:] if argv is None else argv)
    profile, rest = _split_profile_and_args(args)
    config = CiaoConfig.from_env()

    if not profile:
        profile = os.environ.get("GWS_PROFILE", "").strip()
    if not profile:
        profile = str(getattr(config, "gws_default_profile", "") or "").strip()
    if not profile:
        print(
            "ciao gws: no profile given and GWS_PROFILE is unset",
            file=sys.stderr,
        )
        return 2

    workspace_root = _configured_workspace_root(config) or Path(
        config.workspace_root
    )

    try:
        env = _gws_environment(workspace_root, profile)
    except ValueError as exc:
        print(f"ciao gws: {exc}", file=sys.stderr)
        return 2

    gws = resolve_tool("gws")
    if not gws:
        print(
            "ciao gws: the 'gws' CLI was not found on PATH. Install it from "
            "Settings → Workspaces (Google Workspace card).",
            file=sys.stderr,
        )
        return 1

    try:
        os.execve(gws, [gws, *rest], env)
    except OSError as exc:
        print(f"ciao gws: failed to run gws: {exc}", file=sys.stderr)
        return 1
    # unreachable
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
