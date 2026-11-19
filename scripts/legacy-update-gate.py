#!/usr/bin/env python3
"""Prove that a 1.x engine can stage a release's update (#3131 L5).

A 1.x engine finds an update by reading the release's signed engine manifest
and staging the one wheel it lists. This gate takes the released 1.x wheel,
installs it into a fresh venv, and runs that wheel's own ``stage_update``
against the release's manifest and signature. It only stages: the update is
never applied and launchd is never touched.

Stdlib only, and run with a fresh Python. Never import ``ciao`` from the
checkout: the point is to exercise the 1.x code the released clients run.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "raffaelefarinaro/ciaobot"
#: The 1.x releases that must be able to update to the release under test, with
#: the sha256 of each wheel. The digest is trust-on-first-use, taken from the
#: release's wheel; it matches the wheel sha256 in that release's signed
#: ``ciaobot-engine-manifest.json`` (checked with ``ciao.release_manifest verify``).
LEGACY_WHEEL_SHA256 = {
    "1.10.0": "7d9fd66814e57b32b464bd1f44d6fa31716c4088bb271a2474f331afb1e2133b",
}
LEGACY_VERSIONS = tuple(LEGACY_WHEEL_SHA256)
LEGACY_PYTHON = "3.13"
MANIFEST_NAME = "ciaobot-engine-manifest.json"
SIGNATURE_NAME = MANIFEST_NAME + ".sig"
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# Runs inside the legacy venv with -I, so nothing from this checkout is
# importable. Prints the operation record as JSON either way; a failed stage
# exits nonzero, and so does a stage that did not reach "staged".
RUNNER = """
import json, sys
from dataclasses import asdict
from pathlib import Path
from ciao.engine_update import UpdateError, read_operation, stage_update

target, base, uv, state_dir = sys.argv[1:5]
state = Path(state_dir)
try:
    op = stage_update(target, release_base=base, state_dir=state, uv=uv)
except UpdateError as exc:
    record = read_operation(state)
    print(json.dumps(asdict(record) if record else {"error": str(exc)}, indent=2, sort_keys=True))
    sys.exit(1)
print(json.dumps(asdict(op), indent=2, sort_keys=True))
sys.exit(0 if op.phase == "staged" else 2)
"""

#: Variables the child processes may inherit. Everything else, CIAO_* above all,
#: stays out of the 1.x updater and its uv.
_ALLOWED_ENV = ("PATH", "LANG", "TMPDIR", "SSL_CERT_FILE")


class GateError(RuntimeError):
    """The gate could not run, or the 1.x updater refused the release."""


def gh_download_argv(legacy: str, dest: Path) -> list[str]:
    return [
        "gh", "release", "download", f"v{legacy}",
        "--repo", REPO,
        "--pattern", f"ciaobot-{legacy}-py3-none-any.whl",
        "--dir", str(dest),
    ]


def uv_venv_argv(uv: str, venv: Path) -> list[str]:
    return [uv, "venv", "--python", LEGACY_PYTHON, str(venv)]


def uv_pip_install_argv(uv: str, py: Path, wheel: Path) -> list[str]:
    return [uv, "pip", "install", "--python", str(py), str(wheel)]


def runner_argv(py: Path, target: str, base: str, uv: str, state: Path) -> list[str]:
    # -I ignores PYTHON* variables and the working directory, so the 1.x code
    # is the only ciao the child can import.
    return [str(py), "-I", "-c", RUNNER, target, base, uv, str(state)]


def _allowed(environ: dict[str, str]) -> dict[str, str]:
    env = {k: environ[k] for k in _ALLOWED_ENV if k in environ}
    env.update({k: v for k, v in environ.items() if k.startswith("UV_")})
    return env


def gh_env(environ: dict[str, str]) -> dict[str, str]:
    """Env for the one gh call: the caller's HOME (gh's login) and GH_TOKEN."""
    env = _allowed(environ)
    if "HOME" in environ:
        env["HOME"] = environ["HOME"]
    if "GH_TOKEN" in environ:
        env["GH_TOKEN"] = environ["GH_TOKEN"]
    return env


def child_env(environ: dict[str, str], home: Path) -> dict[str, str]:
    """Env for uv and the 1.x updater: allowlisted, with a throwaway HOME."""
    return {**_allowed(environ), "HOME": str(home)}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_wheel_digest(legacy: str, wheel: Path) -> None:
    actual = sha256_file(wheel)
    if actual != LEGACY_WHEEL_SHA256[legacy]:
        raise GateError(
            f"{wheel.name} sha256 is {actual}, expected {LEGACY_WHEEL_SHA256[legacy]}; "
            "refusing to install it"
        )


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, env=env, check=False, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise GateError(f"cannot run {cmd[0]}: {exc}") from exc


def _gate_one(legacy: str, target: str, base: str, uv: str, environ: dict[str, str], tmp: Path) -> None:
    wheel_dir = tmp / "wheel"
    wheel_dir.mkdir()
    fetched = _run(gh_download_argv(legacy, wheel_dir), gh_env(environ))
    if fetched.returncode != 0:
        raise GateError(f"could not download the {legacy} wheel: {fetched.stderr.strip()}")
    wheels = sorted(wheel_dir.glob(f"ciaobot-{legacy}-py3-none-any.whl"))
    if len(wheels) != 1:
        raise GateError(f"expected one {legacy} wheel, found {len(wheels)}")
    verify_wheel_digest(legacy, wheels[0])

    home = tmp / "home"
    home.mkdir()
    env = child_env(environ, home)
    venv = tmp / "venv"
    created = _run(uv_venv_argv(uv, venv), env)
    if created.returncode != 0:
        raise GateError(f"uv venv failed: {created.stderr.strip()}")
    py = venv / "bin" / "python"
    installed = _run(uv_pip_install_argv(uv, py, wheels[0]), env)
    if installed.returncode != 0:
        raise GateError(f"installing the {legacy} wheel failed: {installed.stderr.strip()}")

    staged = _run(runner_argv(py, target, base, uv, tmp / "state"), env)
    print(staged.stdout, end="")
    if staged.returncode != 0:
        print(staged.stderr, end="", file=sys.stderr)
        raise GateError(f"the {legacy} updater did not stage {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="legacy-update-gate.py", description=__doc__)
    parser.add_argument("--target", required=True, help="release version under test, no leading v")
    parser.add_argument(
        "--legacy-version",
        action="append",
        choices=LEGACY_VERSIONS,
        help="1.x version to test (repeatable); defaults to every entry of LEGACY_WHEEL_SHA256",
    )
    parser.add_argument(
        "--release-root",
        type=Path,
        help="directory holding v<target>/ with the manifest, its sig and the wheel",
    )
    parser.add_argument(
        "--release-base",
        help="release URL base to fetch from, instead of --release-root",
    )
    parser.add_argument("--uv", help="uv binary (default: uv on PATH)")
    args = parser.parse_args(argv)

    target: str = args.target
    if not _VERSION_RE.fullmatch(target):
        parser.error(f"not a release version: {target!r}")
    if (args.release_root is None) == (args.release_base is None):
        parser.error("give exactly one of --release-root or --release-base")
    uv = args.uv or shutil.which("uv")
    if not uv:
        parser.error("no uv binary: pass --uv or put uv on PATH")

    if args.release_base is not None:
        base = args.release_base.rstrip("/")
    else:
        release_dir = (args.release_root / f"v{target}").resolve()
        for name in (MANIFEST_NAME, SIGNATURE_NAME):
            if not (release_dir / name).is_file():
                print(f"Error: {release_dir / name} is missing", file=sys.stderr)
                return 1
        base = args.release_root.resolve().as_uri()

    # CIAO_* settings from the caller's shell must not steer the 1.x updater;
    # the child environments are built from an allowlist (see _allowed).
    environ = dict(os.environ)
    legacy_versions = args.legacy_version or list(LEGACY_VERSIONS)
    for legacy in legacy_versions:
        with tempfile.TemporaryDirectory(prefix=f"legacy-gate-{legacy}-") as scratch:
            try:
                _gate_one(legacy, target, base, uv, environ, Path(scratch))
            except GateError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
        print(f"{legacy} updater stages {target} from {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
