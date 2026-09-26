"""Classify a Mac for the Ciaobot.app → terminal-engine migration (#562 Phase 3, #576).

Run from the *verified* wheel by scripts/install-engine.sh before anything is
installed. Reads state raw and never writes: NodeStateManager would create a
host state when node_state.json is absent.

The one rule every decision here obeys is *fail closed*. A Mac nobody can
account for is reported as `desktop_invalid` so the installer stops and asks,
because the two available guesses are both expensive: guessing "host" puts a
second writer on a runtime root that may already have one, and guessing
"client" strands a user who has no engine anywhere. So an unreadable plist, a
runtime root that is not there, an unknown role and a host URL that is not an
address all end in the same answer.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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


def _read_plist(path: Path) -> tuple[bool, dict[str, Any]]:
    """`(exists, plist)` for the engine LaunchAgent.

    The two are different answers and the difference is the whole point of this
    module. `exists` false means nothing owns the engine through this file, and
    the install is an ordinary one. `exists` true with an empty dict means the
    file is there and cannot be parsed or is not a plist at all: that is a
    service definition nobody can account for, and the caller must not install
    over it as though it were nothing.
    """
    try:
        with path.open("rb") as handle:
            loaded = plistlib.load(handle)
    except FileNotFoundError:
        return False, {}
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return True, {}
    if not isinstance(loaded, dict):
        return True, {}
    return True, loaded


def _app_bundle(program: str) -> str:
    """The `.app` path `program` lives in, or "" when it lives outside one."""
    marker = ".app/"
    index = program.find(marker)
    if index < 0:
        return ""
    return program[: index + len(".app")]


def _client_host_url(value: Any) -> str:
    """`value` as a host URL a client can be handed over to, or "".

    A prefix check accepts `https://`, `https:///app` and `https://:8443`
    alike, and a state file can hold any of them. None of those is an address
    the user could open, and handing one out would leave a client with no engine
    and no way to find the host, so the URL is parsed instead of prefixed: the
    scheme has to be http(s), there has to be a hostname that is not whitespace
    or a path, and credentials in the URL are refused rather than printed back
    at the user.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate:
        return ""
    try:
        parts = urlsplit(candidate)
        host = parts.hostname or ""
        parts.port  # raises ValueError on a port that is not a number
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host:
        return ""
    if any(char.isspace() for char in host) or any(
        char in host for char in "/?#@\\"
    ):
        return ""
    if parts.username or parts.password:
        return ""
    return candidate


def _runtime_is_readable(root: str) -> bool:
    """Whether `root` is a directory this process can list and enter.

    A runtime root that is missing is not the same thing as an absent
    `node_state.json` inside it: the first means nothing on this Mac can be
    accounted for, while the second is what a host looks like before it has
    written one. So the root is checked before its state is read, or "no state
    file" would be read as "this Mac is a host" on a Mac with no state at all.
    """
    return Path(root).is_dir() and os.access(root, os.R_OK | os.X_OK)


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
    host_url = _client_host_url(state.get("host_url"))
    if mapped == "client" and not host_url:
        # A client with no reachable host is not a client this migration can
        # hand the user over to, so it fails closed with everything else.
        return "invalid", ""
    return mapped, host_url


def classify(launch_agents_dir: Path | None = None) -> Classification:
    """Read the engine LaunchAgent and say what a `--migrate` install may do.

    Never writes and never raises: a Mac whose state cannot be understood is
    `desktop_invalid`, which the installer turns into a refusal with an
    explicit-choice message. `none` is reserved for the one case that really is
    "nothing to migrate" - no engine plist at all - and a surprise inside an
    existing plist never reads as it.
    """
    if launch_agents_dir is None:
        agents = default_launch_agents_dir()
    else:
        agents = Path(launch_agents_dir).expanduser()
    try:
        return _classify(agents)
    except Exception:  # noqa: BLE001 - a classifier that raises is unusable
        # The one thing not negotiable is that a surprise must not read as
        # "nothing to migrate": that answer sends the installer straight on to
        # ordinary setup over the top of a service definition it could not
        # parse. The plist is what decides, and if it is there the answer is
        # "undecidable", so the caller asks the user.
        if (agents / SERVER_PLIST).exists():
            return Classification(kind="desktop_invalid", node_role="invalid")
        return Classification(kind="none")


def _unreadable(workspace: str = "", program: str = "") -> Classification:
    """`desktop_invalid` for a plist that is there but cannot be trusted."""
    return Classification(
        kind="desktop_invalid",
        workspace=workspace,
        node_role="invalid",
        app_bundle=_app_bundle(program),
        plist_program=program,
    )


def _classify(agents: Path) -> Classification:
    exists, plist = _read_plist(agents / SERVER_PLIST)
    if not exists:
        return Classification(kind="none")
    if not plist:
        return _unreadable()

    arguments = plist.get("ProgramArguments")
    program = ""
    if isinstance(arguments, (list, tuple)) and arguments:
        program = str(arguments[0] or "")
    if not program:
        # A service definition that names no program is still a service
        # definition somebody's launchd may be running.
        return _unreadable()

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
    # need a runtime root or a node state to decide.
    stale = not Path(program).exists()
    if stale:
        return Classification(
            kind="desktop_stale",
            workspace=runtime.workspace,
            runtime_root=runtime.runtime_root,
            port=runtime.port,
            app_bundle=bundle,
            plist_program=program,
        )
    if not runtime.workspace or not _runtime_is_readable(runtime.runtime_root):
        # The engine is live and the state it is supposed to own cannot be
        # found. Reading that as "no node file, therefore a host" would hand a
        # second writer to a runtime root nobody has looked at.
        return _unreadable(runtime.workspace, program)

    node_role, host_url = _classify_node_state(runtime.runtime_root)
    if node_role == "invalid":
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
