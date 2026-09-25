"""Installer-owned receipt describing a terminal-installed engine (#562).

The shell installer replaces a uv tool environment on every upgrade, so nothing
inside the engine can answer "which release installed me, which executables did
it place, and which service owns me" after the fact. The installer writes this
receipt instead: a small owner-only JSON document at
``~/.local/state/ciaobot/install-receipt.json``, deliberately outside any tool
environment so a runtime swap never deletes it and outside any workspace because
one engine serves several logical workspaces.

Two rules keep it trustworthy. Reading is fail-safe — a missing, corrupt,
wrong-schema or unreadable file reads as "no receipt" and never raises, because
the reader runs on the startup path. And the receipt only counts when it
describes *this* process: its ``python`` must sit in the running interpreter's
own environment, so a receipt left behind by an uninstall (or a developer
checkout sharing a machine with an installed engine) never turns the wrong
process into ``installer``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.jsonio import write_private_text

SCHEMA_VERSION = 1
SERVICE_BACKENDS = ("launchd", "systemd-user", "none")

# Fields every receipt must carry as a string. `previous_*` and `schema` are
# optional; the rest describe the install itself and a receipt missing any of
# them cannot answer "what is running", so it is rejected outright.
_REQUIRED_FIELDS = (
    "version",
    "executable",
    "python",
    "service_backend",
    "service_label",
    "installed_at",
)

# Optional, but a string when present: a `previous_version` of `[1]` is a
# corrupt receipt, not a release to coerce into "['1']".
_OPTIONAL_STRING_FIELDS = ("previous_version", "previous_executable")


def default_receipt_path() -> Path:
    return Path.home() / ".local" / "state" / "ciaobot" / "install-receipt.json"


@dataclass(frozen=True, slots=True)
class InstallReceipt:
    version: str                 # engine version installed, e.g. "0.9.2"
    executable: str              # absolute path of the installed `ciao` entry point
    python: str                  # absolute path of the tool environment's interpreter
    service_backend: str         # one of SERVICE_BACKENDS
    service_label: str           # e.g. "com.ciao.server"; "" when backend is "none"
    installed_at: str            # ISO-8601 UTC, e.g. "2026-09-25T16:00:00+00:00"
    previous_version: str = ""   # release this install replaced, "" for a fresh install
    previous_executable: str = ""
    schema: int = SCHEMA_VERSION


def read_receipt(path: Path | None = None) -> InstallReceipt | None:
    """Return the receipt at ``path``, or None when there is nothing to read.

    Fail-safe by design: every way the file can be absent or unusable — not
    there, not readable, not JSON, not an object, a future schema, a missing or
    mistyped field, an unknown service backend — answers None rather than
    raising, so a corrupt receipt degrades the install mode to "unknown" instead
    of breaking startup. Unknown extra keys are ignored, which keeps an older
    engine able to read a receipt written by a newer installer.
    """
    target = path or default_receipt_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        parsed: Any = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    data: dict[str, Any] = parsed
    if data.get("schema") != SCHEMA_VERSION:
        return None
    if any(not isinstance(data.get(field), str) for field in _REQUIRED_FIELDS):
        return None
    if any(
        field in data and not isinstance(data[field], str)
        for field in _OPTIONAL_STRING_FIELDS
    ):
        return None
    if data["service_backend"] not in SERVICE_BACKENDS:
        return None
    if not (data["version"] and data["executable"] and data["python"]):
        return None
    # Optional fields are strings when present, so these need no coercion.
    previous_version: str = data.get("previous_version", "")
    previous_executable: str = data.get("previous_executable", "")
    # Explicit keywords, never `InstallReceipt(**data)`: a file written by a
    # future installer carrying an extra key must still parse today.
    return InstallReceipt(
        version=data["version"],
        executable=data["executable"],
        python=data["python"],
        service_backend=data["service_backend"],
        service_label=data["service_label"],
        installed_at=data["installed_at"],
        previous_version=previous_version,
        previous_executable=previous_executable,
        schema=SCHEMA_VERSION,
    )


def write_receipt(receipt: InstallReceipt, path: Path | None = None) -> Path:
    """Write ``receipt`` atomically, owner-only, and return the path written.

    Validated first, so a bad receipt fails before it creates a directory or
    replaces a good one. The write goes to a sibling temp file and is renamed
    into place, so a reader never observes a half-written document. A write that
    fails part way — including a rename that never happens — leaves no temp file
    behind, so a later write never trips over a stale one.
    """
    if receipt.service_backend not in SERVICE_BACKENDS:
        raise ValueError(f"Unknown service backend: {receipt.service_backend!r}")
    for field in ("version", "executable", "python"):
        if not getattr(receipt, field):
            raise ValueError(f"Receipt {field} must not be empty")

    target = path or default_receipt_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    tmp = target.with_name(target.name + ".tmp")
    try:
        write_private_text(tmp, json.dumps(asdict(receipt), indent=2, sort_keys=True) + "\n")
        os.replace(tmp, target)
    finally:
        # A no-op once the rename succeeded, a cleanup when it did not.
        tmp.unlink(missing_ok=True)
    return target


def _interpreter_dir(path: str) -> Path:
    """The ``bin`` directory an interpreter path belongs to.

    The directory is resolved, not the interpreter: a venv's ``bin/python`` is a
    symlink to a shared base Python, so following it would make every
    environment built on that base compare equal.
    """
    return Path(os.path.abspath(path)).parent.resolve()


def receipt_matches_running(
    receipt: InstallReceipt, *, executable: str | None = None
) -> bool:
    """True when ``receipt`` describes the environment running this process.

    What is compared is the interpreter's ``bin`` directory, not the
    interpreter file. A venv or uv tool env links ``bin/python`` to the shared
    base interpreter, so resolving the file would make any other environment on
    that same base look like this one — including a developer's clean smoke-test
    venv, which would then be reported as an installer engine. Comparing the
    directory keeps the identity of the *environment* while still tolerating the
    same interpreter reached through a symlink, a relative path, or a different
    entry name (``python`` vs ``python3``).
    """
    try:
        return _interpreter_dir(receipt.python) == _interpreter_dir(
            executable or sys.executable
        )
    except (OSError, RuntimeError, ValueError):
        return False


def running_receipt(path: Path | None = None) -> InstallReceipt | None:
    """The receipt for this process, or None if there is none or it is stale."""
    receipt = read_receipt(path)
    if receipt is not None and receipt_matches_running(receipt):
        return receipt
    return None


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m ciao.install_receipt``.

    The shell installer calls this instead of writing the JSON itself, so the
    schema lives in one place and bash cannot drift from it.
    """
    parser = argparse.ArgumentParser(
        prog="python -m ciao.install_receipt", description=__doc__
    )
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write", help="Write the installer receipt")
    write.add_argument("--version", required=True, help="engine version installed")
    write.add_argument("--executable", required=True, help="path of the ciao entry point")
    write.add_argument(
        "--python", default=sys.executable, help="path of the tool environment's python"
    )
    write.add_argument(
        "--service-backend", required=True, choices=SERVICE_BACKENDS, help="service owner"
    )
    write.add_argument("--service-label", default="", help="launchd/systemd unit label")
    write.add_argument("--previous-version", default="", help="release being replaced")
    write.add_argument(
        "--previous-executable", default="", help="executable being replaced"
    )
    write.add_argument(
        "--path", type=Path, default=None, help="receipt path (defaults to the standard one)"
    )
    args = parser.parse_args(argv)

    receipt = InstallReceipt(
        version=args.version,
        executable=args.executable,
        python=args.python,
        service_backend=args.service_backend,
        service_label=args.service_label,
        installed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        previous_version=args.previous_version,
        previous_executable=args.previous_executable,
    )
    try:
        written = write_receipt(receipt, args.path)
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
