#!/usr/bin/env python3
"""Sign (ad-hoc) and check the Mach-O files of the bundled engine inside a Ciaobot app (#3128 F).

    python3 -I -S scripts/engine_sign.py sign --adhoc ENGINE
    python3 -I -S scripts/engine_sign.py check ENGINE

ENGINE is ``<app>/Contents/Resources/engine``. ``sign`` signs every engine Mach-O (found by magic,
never by extension) except the SDK's own CLI, deepest path first, one file at a time: never
``--deep``, no hardened runtime and no entitlements (an ad-hoc hardened binary cannot load an
ad-hoc dylib, and ad-hoc code has no Team ID). The SDK CLI keeps Anthropic's Developer ID
signature, which is verified and never replaced. ``check`` writes nothing. An outer ad-hoc seal
passes ``codesign --verify --deep --strict`` even over unsigned engine files, so only this check
proves the engine itself is signed.

Stdlib only. Exit 0 silent on success, 1 with ``engine sign failed`` on a refusal, 2 on usage.
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
VENDOR_SIGNED = {"lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude": "Q6L2SF6YDW"}
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


def check_vendor(engine: Path, rel: str, team: str) -> None:
    """The vendor-signed file must verify and still carry the vendor's Developer ID, team and runtime flag."""
    path = os.fspath(engine / rel)
    code, _out = _run([CODESIGN, "--verify", "--strict", path])
    _require(code == 0)
    code, out = _run([CODESIGN, "-d", "--verbose=4", path])
    _require(code == 0)
    lines = out.splitlines()
    authorities = [line for line in lines if line.startswith("Authority=")]
    _require(bool(authorities) and authorities[0].startswith("Authority=Developer ID Application:"))
    _require(f"TeamIdentifier={team}" in lines)
    flags = [line for line in lines if line.startswith("CodeDirectory ") and "flags=" in line]
    _require(any("runtime" in line.split("flags=", 1)[1].split(" ", 1)[0] for line in flags))


def _inventory(engine: Path) -> list[dict[str, Any]]:
    try:
        return list(engine_install.inventory(engine))
    except engine_install.InstallError as exc:
        raise SignError() from exc


def check(engine: Path) -> None:
    check_layout(engine)
    for rel in macho_files(engine):
        if rel in VENDOR_SIGNED:
            continue
        path = os.fspath(engine / rel)
        code, _out = _run([CODESIGN, "--verify", "--strict", path])
        _require(code == 0)
        code, out = _run([CODESIGN, "-d", "--verbose=4", path])
        _require(code == 0 and "Signature=adhoc" in out.splitlines())
    for rel, team in VENDOR_SIGNED.items():
        if (engine / rel).is_file():
            check_vendor(engine, rel, team)


def sign_adhoc(engine: Path) -> list[str]:
    check_layout(engine)
    before = _inventory(engine)
    targets = signing_order([p for p in macho_files(engine) if p not in VENDOR_SIGNED])
    for rel, team in VENDOR_SIGNED.items():
        if (engine / rel).is_file():
            check_vendor(engine, rel, team)
    for rel in targets:
        code, _out = _run([CODESIGN, "--force", "--sign", "-", "--timestamp=none", os.fspath(engine / rel)])
        _require(code == 0)
    for rel in targets:
        code, _out = _run([CODESIGN, "--verify", "--strict", "--verbose=2", os.fspath(engine / rel)])
        _require(code == 0)
    after = _inventory(engine)
    old = {row["path"]: row for row in before}
    new = {row["path"]: row for row in after}
    _require(old.keys() == new.keys())
    changed = {path for path in old if old[path] != new[path]}
    _require(changed <= set(targets))
    check(engine)
    return targets


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "sign" and args[1] == "--adhoc":
        mode, engine_arg = "sign", args[2]
    elif len(args) == 2 and args[0] == "check":
        mode, engine_arg = "check", args[1]
    else:
        print(f"{FAILED}: usage", file=sys.stderr)
        return 2
    try:
        _platform_check()
        engine = check_path(engine_arg)
        if mode == "sign":
            sign_adhoc(engine)
        else:
            check(engine)
    except SignError:
        print(FAILED, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
