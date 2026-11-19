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
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: The 1.x releases that must be able to update to the release under test.
LEGACY_VERSIONS = ("1.10.0",)
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


class GateError(RuntimeError):
    """The gate could not run, or the 1.x updater refused the release."""


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env=env, check=False, text=True, capture_output=True)


def _gate_one(
    legacy: str,
    target: str,
    base: str,
    uv: str,
    env: dict[str, str],
    stage_env: dict[str, str],
    tmp: Path,
) -> None:
    wheel_dir = tmp / "wheel"
    wheel_dir.mkdir()
    fetched = _run(
        [
            "gh", "release", "download", f"v{legacy}",
            "--pattern", f"ciaobot-{legacy}-py3-none-any.whl",
            "--dir", str(wheel_dir),
        ],
        env,
    )
    if fetched.returncode != 0:
        raise GateError(f"could not download the {legacy} wheel: {fetched.stderr.strip()}")
    wheels = sorted(wheel_dir.glob(f"ciaobot-{legacy}-py3-none-any.whl"))
    if len(wheels) != 1:
        raise GateError(f"expected one {legacy} wheel, found {len(wheels)}")

    venv = tmp / "venv"
    created = _run([uv, "venv", "--python", LEGACY_PYTHON, str(venv)], env)
    if created.returncode != 0:
        raise GateError(f"uv venv failed: {created.stderr.strip()}")
    py = venv / "bin" / "python"
    installed = _run([uv, "pip", "install", "--python", str(py), str(wheels[0])], env)
    if installed.returncode != 0:
        raise GateError(f"installing the {legacy} wheel failed: {installed.stderr.strip()}")

    state = tmp / "state"
    staged = _run(
        [str(py), "-I", "-c", RUNNER, target, base, uv, str(state)],
        stage_env,
    )
    if staged.returncode != 0:
        print(staged.stdout, end="")
        raise GateError(f"the {legacy} updater did not stage {target}")
    print(staged.stdout, end="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="legacy-update-gate.py", description=__doc__)
    parser.add_argument("--target", required=True, help="release version under test, no leading v")
    parser.add_argument(
        "--legacy-version",
        action="append",
        choices=LEGACY_VERSIONS,
        help="1.x version to test (repeatable); defaults to every entry of LEGACY_VERSIONS",
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

    # CIAO_* settings from the caller's shell must not steer the 1.x updater.
    # gh and uv keep the caller's HOME (their logins live there); only the
    # 1.x updater runs with a throwaway HOME and state directory.
    env = {k: v for k, v in os.environ.items() if not k.startswith("CIAO_")}
    legacy_versions = args.legacy_version or list(LEGACY_VERSIONS)
    for legacy in legacy_versions:
        with tempfile.TemporaryDirectory(prefix=f"legacy-gate-{legacy}-") as scratch:
            tmp = Path(scratch)
            home = tmp / "home"
            home.mkdir()
            stage_env = {**env, "HOME": str(home)}
            try:
                _gate_one(legacy, target, base, uv, env, stage_env, tmp)
            except GateError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
        print(f"{legacy} updater stages {target} from {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
