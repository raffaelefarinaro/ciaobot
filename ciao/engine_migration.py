"""Classify a Mac for the Ciaobot.app → terminal-engine migration (#562 Phase 3, #576).

Run from the *verified* wheel by scripts/install-engine.sh before anything is
installed. Reads state raw and never writes: NodeStateManager would create a
host state when node_state.json is absent.
"""

from __future__ import annotations

import argparse
import json
import plistlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ciao.macos_service import DEFAULT_PORT, default_launch_agents_dir, discover_runtime

# The engine LaunchAgent this classifies from: the same file
# `macos_service.SERVER_LABEL` names, spelled out because this module also runs
# from a wheel that has to be importable before anything else is set up.
SERVER_PLIST = "com.ciao.server.plist"
KINDS = (
    "none",
    "engine",
    "desktop_host",
    "desktop_client",
    "desktop_invalid",
    "desktop_stale",
)

# The node roles Ciaobot has used, mapped onto the two outcomes a migration
# cares about. Anything outside this table is unknown, and an unknown role is
# never guessed at: a wrong guess here either double-writes a host's runtime
# root or leaves a client believing it is still serving.
_ROLE_MAP = {
    "active": "host",
    "host": "host",
    "standby": "client",
    "client": "client",
}


@dataclass(frozen=True)
class Classification:
    kind: str
    workspace: str = ""
    runtime_root: str = ""
    port: int = DEFAULT_PORT
    node_role: str = ""  # "host" | "client" | "" (absent) | "invalid"
    host_url: str = ""
    app_bundle: str = ""
    plist_program: str = ""


def _read_plist(path: Path) -> dict[str, Any]:
    """The engine plist, or an empty dict when it is absent or unreadable.

    Absent and unreadable are the same answer here (nothing owns the engine
    through this file), and neither may raise: this runs on the install path
    where the worst outcome is a confusing `kind`, never a traceback.
    """
    try:
        with path.open("rb") as handle:
            loaded = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _app_bundle(program: str) -> str:
    """The `.app` path `program` lives in, or "" when it lives outside one."""
    marker = ".app/"
    index = program.find(marker)
    if index < 0:
        return ""
    return program[: index + len(".app")]


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _classify_node_state(runtime_root: str) -> tuple[str, str]:
    """(node_role, host_url) from `node_state.json`, read raw.

    Missing file means "no state yet", which the desktop shell treats as a
    host that has not finished onboarding, so the role stays empty and the
    caller maps that to the host path. A file that cannot be read is a
    different thing entirely: nobody may know what this Mac writes, so it is
    reported as invalid rather than guessed.
    """
    node_path = Path(runtime_root) / "node_state.json"
    try:
        if not node_path.exists():
            return "", ""
        raw = node_path.read_text(encoding="utf-8")
    except OSError:
        return "invalid", ""
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        return "invalid", ""
    if not isinstance(state, dict):
        return "invalid", ""
    role = str(state.get("role", "") or "").strip().lower()
    mapped = _ROLE_MAP.get(role, "invalid")
    if mapped == "invalid":
        return "invalid", ""
    host_url = str(state.get("host_url", "") or "").strip()
    if mapped == "client" and not _is_http_url(host_url):
        # A client with no reachable host is not a client this migration can
        # hand the user over to, so it fails closed with everything else.
        return "invalid", ""
    return mapped, host_url


def classify(launch_agents_dir: Path | None = None) -> Classification:
    """Read the engine LaunchAgent and say what a `--migrate` install may do.

    Never writes and never raises: a Mac whose state cannot be understood is
    `desktop_invalid` (or `none`, when there is no plist at all), which the
    installer turns into a refusal with an explicit-choice message.
    """
    if launch_agents_dir is None:
        agents = default_launch_agents_dir()
    else:
        agents = Path(launch_agents_dir).expanduser()
    try:
        return _classify(agents)
    except Exception:  # noqa: BLE001 - a classifier that raises is unusable
        # The one thing not negotiable is the guarantee that this never
        # touches state, so a surprise is reported as "undecidable" and the
        # caller asks the user, rather than guessed at.
        return Classification(kind="none")


def _classify(agents: Path) -> Classification:
    plist = _read_plist(agents / SERVER_PLIST)
    if not plist:
        return Classification(kind="none")

    arguments = plist.get("ProgramArguments")
    program = ""
    if isinstance(arguments, (list, tuple)) and arguments:
        program = str(arguments[0] or "")
    if not program:
        return Classification(kind="none")

    bundle = _app_bundle(program)
    if not bundle:
        # An installer-managed or hand-rolled engine: this script is already
        # the tool that owns it.
        return Classification(kind="engine", plist_program=program)

    # The same rules the engine itself uses for workspace, .env and runtime
    # root, so the migration points the new engine at the state that is
    # actually live.
    runtime = discover_runtime(launch_agents_dir=agents)
    # Ciaobot.app is gone but its plist is not: the stale case, which the
    # installer replaces like any other leftover, and the only one that does not
    # need a node state to decide.
    stale = not Path(program).exists()
    node_role, host_url = (
        ("", "") if stale else _classify_node_state(runtime.runtime_root)
    )

    if stale:
        kind = "desktop_stale"
    elif node_role == "invalid":
        kind = "desktop_invalid"
    elif node_role == "client":
        kind = "desktop_client"
    else:
        # An empty role means the node file is absent, which the desktop shell
        # treats as a host: the workspace is the host's own.
        kind = "desktop_host"
    return Classification(
        kind=kind,
        workspace=runtime.workspace,
        runtime_root=runtime.runtime_root,
        port=runtime.port,
        node_role=node_role,
        host_url=host_url,
        app_bundle=bundle,
        plist_program=program,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ciao.engine_migration",
        description="Classify this Mac for the Ciaobot.app → engine migration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    classify_parser = sub.add_parser("classify", help="classify this install")
    classify_parser.add_argument(
        "--json", action="store_true", help="print the full result as JSON"
    )
    classify_parser.add_argument(
        "--launch-agents-dir",
        default=None,
        help="LaunchAgents directory (default: the user's own)",
    )
    args = parser.parse_args(argv)

    result = classify(
        Path(args.launch_agents_dir) if args.launch_agents_dir else None
    )
    if getattr(args, "json", False):
        print(json.dumps(asdict(result)))
    else:
        print(f"kind={result.kind} workspace={result.workspace}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a module
    raise SystemExit(main())
