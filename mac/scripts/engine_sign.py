#!/usr/bin/env python3
"""Sign and check the Mach-O files of the bundled engine inside a Ciaobot app (#3128 F, G).

    python3 -I -S scripts/engine_sign.py sign --adhoc ENGINE
    python3 -I -S scripts/engine_sign.py sign --distribution TEAM IDENTITY ENGINE
    python3 -I -S scripts/engine_sign.py check ENGINE
    python3 -I -S scripts/engine_sign.py check --distribution TEAM ENGINE
    python3 -I -S scripts/engine_sign.py inventory ENGINE

ENGINE is ``<app>/Contents/Resources/engine``. ``sign`` signs every engine Mach-O (found by magic,
never by extension) except the SDK's own CLI, deepest path first, one file at a time: never
``--deep``, no hardened runtime and no entitlements (an ad-hoc hardened binary cannot load an
ad-hoc dylib, and ad-hoc code has no Team ID). ``--distribution`` signs with a Developer ID
Application IDENTITY of team TEAM, hardened runtime and a secure timestamp, still with no
entitlements and never ``--deep``; ``check --distribution`` is the gate that makes an engine-bearing
app eligible for notarization, an image or an appcast. The SDK CLI keeps Anthropic's Developer ID
signature, which is verified (pinned to Apple's CA and Anthropic's team with a codesign
requirement) and never replaced. ``check`` writes nothing. An outer ad-hoc seal
passes ``codesign --verify --deep --strict`` even over unsigned engine files, so only this check
proves the engine itself is signed. ``inventory`` writes nothing and prints the engine's full
path/size/sha256 inventory as one JSON array on stdout, so a caller can compare two reads.

Stdlib only. Exit 0 silent on success (``inventory`` prints the JSON), 1 with ``engine sign failed``
on a refusal, 2 on usage.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

CODESIGN = "/usr/bin/codesign"
REQUIRED_FILES = ("bin/ciao", "bin/python3.12", "provenance.json")
CODESIGN_DEADLINE = 600
MAX_PROVENANCE = 1024 * 1024
SCRIPT_DIR = Path(__file__).resolve().parent
FAILED = "engine sign failed"


class SignError(Exception):
    """A refusal; the CLI prints only a fixed line."""


def _require(condition: bool) -> None:
    if not condition:
        raise SignError()


def _load_sibling(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError("sibling helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Private-helper coupling: this module calls engine_install._scan/inventory and
# engine_assemble._slice_bounds (the Mach-O magic test, which raises AssembleError for a
# non-arm64 file). Renaming them breaks signing and the tests here fail at call time.
engine_install = _load_sibling("engine_install")
engine_assemble = _load_sibling("engine_assemble")


def _platform_check() -> None:
    _require(sys.platform == "darwin")


def _run(argv: list[str]) -> tuple[int, str]:
    """The process seam. Tests patch this; stderr is merged because codesign -d writes there."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=CODESIGN_DEADLINE, check=False,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SignError() from exc
    return proc.returncode, proc.stdout + proc.stderr


def _applications_roots() -> tuple[str, ...]:
    return ("/Applications", os.path.join(os.path.expanduser("~"), "Applications"))


def _is_macho(path: Path) -> bool:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            return engine_assemble._slice_bounds(handle, size) is not None
    except (OSError, engine_assemble.AssembleError) as exc:
        raise SignError() from exc


def check_path(engine_arg: str) -> Path:
    """The engine must be ``<app>/Contents/Resources/engine`` made of real directories, outside Applications."""
    engine = Path(os.path.abspath(engine_arg))
    _require(engine.name == "engine" and engine.parent.name == "Resources" and engine.parent.parent.name == "Contents")
    app = engine.parent.parent.parent
    _require(app.name != "")
    for directory in (app, app / "Contents", engine.parent, engine):
        _require(directory.is_dir() and not directory.is_symlink())
    real = os.path.realpath(engine)
    for root in _applications_roots():
        root_real = os.path.realpath(root)
        _require(not (real == root_real or real.startswith(root_real + os.sep)))
        _require(not (os.fspath(engine) + os.sep).startswith(root + os.sep))
    return engine


def check_layout(engine: Path) -> str:
    """Validate the launcher, interpreter and provenance; returns the engine version."""
    ciao = engine / "bin/ciao"
    _require(ciao.is_file() and not ciao.is_symlink() and stat.S_IMODE(ciao.stat().st_mode) == 0o755)
    python = engine / "bin/python3.12"
    _require(python.is_file() and not python.is_symlink() and _is_macho(python))
    provenance = engine / "provenance.json"
    _require(provenance.is_file() and not provenance.is_symlink())
    _require(provenance.stat().st_size <= MAX_PROVENANCE)
    try:
        data = json.loads(provenance.read_text(encoding="utf-8"))
        kind = data["kind"]
        version = data["engine"]["version"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SignError() from exc
    _require(kind == "ciaobot-engine-provenance")
    _require(isinstance(version, str) and re.fullmatch(r"[0-9]+(\.[0-9]+)*", version) is not None)
    return str(version)


def macho_files(engine: Path) -> list[str]:
    """Relative paths of the regular files that are Mach-O by magic; links and directories are skipped."""
    try:
        rows = engine_install._scan(engine)
    except (OSError, engine_install.InstallError) as exc:
        raise SignError() from exc
    return [rel for rel, kind, path in rows if kind == "file" and _is_macho(path)]


def signing_order(paths: list[str]) -> list[str]:
    return sorted(paths, key=lambda p: (-p.count("/"), p))


def _team_requirement(team: str) -> str:
    """A codesign requirement pinning the signer to Apple's CA chain, the Developer ID leaf and the team.

    Passed as ``-R=<requirement>`` so codesign itself validates the certificate chain; text-matching
    the ``-d`` display alone would accept a locally forged Authority line.
    """
    return ('anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6]'
            ' and certificate leaf[field.1.2.840.113635.100.6.1.13]'
            f' and certificate leaf[subject.OU] = "{team}"')


def _inventory(engine: Path) -> list[dict[str, Any]]:
    try:
        return list(engine_install.inventory(engine))
    except engine_install.InstallError as exc:
        raise SignError() from exc


def _check_distribution_file(path: str, team: str) -> None:
    """A distribution-signed file: this team's Developer ID, hardened runtime and a secure timestamp."""
    code, _out = _run([CODESIGN, "--verify", "--strict", f"-R={_team_requirement(team)}", path])
    _require(code == 0)
    code, out = _run([CODESIGN, "-d", "--verbose=4", path])
    _require(code == 0)
    lines = out.splitlines()
    _require("Signature=adhoc" not in lines)
    authorities = [line for line in lines if line.startswith("Authority=")]
    _require(bool(authorities) and authorities[0].startswith("Authority=Developer ID Application:"))
    _require(f"TeamIdentifier={team}" in lines)
    flags = [line for line in lines if line.startswith("CodeDirectory ") and "flags=" in line]
    _require(any("runtime" in line.split("flags=", 1)[1].split(" ", 1)[0] for line in flags))
    _require(any(line.startswith("Timestamp=") for line in lines))


def check(engine: Path, team: str | None = None) -> None:
    """Every Mach-O is signed: ad-hoc, or with ``team``'s distribution identity."""
    check_layout(engine)
    for rel in macho_files(engine):
        path = os.fspath(engine / rel)
        if team is not None:
            _check_distribution_file(path, team)
            continue
        code, _out = _run([CODESIGN, "--verify", "--strict", path])
        _require(code == 0)
        code, out = _run([CODESIGN, "-d", "--verbose=4", path])
        _require(code == 0 and "Signature=adhoc" in out.splitlines())


def _team_of(team: str, identity: str) -> None:
    """The identity must be a Developer ID Application identity of exactly this team."""
    _require(re.fullmatch(r"[A-Z0-9]{10}", team) is not None)
    _require(identity.startswith("Developer ID Application: ") and identity.endswith(f" ({team})"))


def sign_engine(engine: Path, distribution: tuple[str, str] | None = None) -> list[str]:
    """Sign every Mach-O: ad-hoc, or ``distribution = (team, identity)`` with runtime and timestamp."""
    if distribution is not None:
        _team_of(*distribution)
        sign_args = ["--force", "--sign", distribution[1], "--options", "runtime", "--timestamp"]
    else:
        sign_args = ["--force", "--sign", "-", "--timestamp=none"]
    check_layout(engine)
    before = _inventory(engine)
    targets = signing_order(macho_files(engine))
    for rel in targets:
        code, _out = _run([CODESIGN, *sign_args, os.fspath(engine / rel)])
        _require(code == 0)
    for rel in targets:
        code, _out = _run([CODESIGN, "--verify", "--strict", "--verbose=2", os.fspath(engine / rel)])
        _require(code == 0)
    after = _inventory(engine)
    old = {row["path"]: row for row in before}
    new = {row["path"]: row for row in after}
    _require(old.keys() == new.keys())
    # A target that is no longer a regular file after signing (a symlink row has a target, not a
    # sha256) is not the file the codesign calls verified, so the whole run is refused.
    _require(all("sha256" in new[path] for path in targets))
    changed = {path for path in old if old[path] != new[path]}
    _require(changed <= set(targets))
    check(engine, distribution[0] if distribution is not None else None)
    return targets


def sign_adhoc(engine: Path) -> list[str]:
    return sign_engine(engine)


def inventory(engine: Path) -> list[dict[str, Any]]:
    """The engine's full path/size/sha256 inventory, read-only (symlinks record their target)."""
    return _inventory(engine)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    distribution: tuple[str, str] | None = None
    check_team: str | None = None
    inventory_only = False
    if len(args) == 3 and args[0] == "sign" and args[1] == "--adhoc":
        mode, engine_arg = "sign", args[2]
    elif len(args) == 5 and args[0] == "sign" and args[1] == "--distribution":
        mode, engine_arg = "sign", args[4]
        distribution = (args[2], args[3])
    elif len(args) == 2 and args[0] == "check":
        mode, engine_arg = "check", args[1]
    elif len(args) == 4 and args[0] == "check" and args[1] == "--distribution":
        mode, engine_arg = "check", args[3]
        check_team = args[2]
    elif len(args) == 2 and args[0] == "inventory":
        mode, engine_arg = "inventory", args[1]
        inventory_only = True
    else:
        print(f"{FAILED}: usage", file=sys.stderr)
        return 2
    try:
        _platform_check()
        engine = check_path(engine_arg)
        if check_team is not None:
            _require(re.fullmatch(r"[A-Z0-9]{10}", check_team) is not None)
        if mode == "sign":
            sign_engine(engine, distribution)
        elif inventory_only:
            print(json.dumps(inventory(engine)))
        else:
            check(engine, check_team)
    except SignError:
        print(FAILED, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
