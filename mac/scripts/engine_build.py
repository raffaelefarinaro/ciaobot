#!/usr/bin/env python3
"""Build the bundled engine end to end from the committed pins.

Runs the merged chain (fetch, audit, install, export, wheel, assemble) into a
fresh destination that holds exactly the C2 layout (`engine/` plus
`assembly-receipt.json`). The engine is built from `HEAD`, so the tracked tree
must be clean. There is no option to skip a step or to reuse a prebuilt engine.
Stdlib only; each step is the existing helper run as a child process, except the
offline audit, which runs in-process against the hash-verified `packaging` wheel.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
GIT = "/usr/bin/git"
STEPS = ("fetch-bootstrap", "audit", "fetch-closure", "install", "export", "wheel", "assemble")
DEADLINES = {"fetch": 3600, "install": 1800, "export": 3600, "wheel": 1800, "assemble": 1800, "git": 120}
_KEY = re.compile(r"[0-9a-f]{64}")
_HEAD = re.compile(r"[0-9a-f]{40}")
ALLOWED_PYTHONS = ((3, 12), (3, 13))
STDERR_TAIL_BYTES = 4096
DIRTY_MESSAGE = "engine build failed: commit or stash tracked changes; the engine is built from HEAD"


class BuildError(Exception):
    """A deliberately non-descriptive refusal suitable for CLI output."""

    def __init__(self, *_args: object) -> None:
        super().__init__("invalid engine build")


class DirtyTree(BuildError):
    """The tracked tree differs from HEAD."""


class PythonVersion(Exception):
    """The host interpreter is not an allowed CPython."""


def _require(condition: bool) -> None:
    if not condition:
        raise BuildError()


def _load_sibling(name: str) -> Any:
    path = SCRIPT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("sibling helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Private-helper coupling: this module calls underscore-prefixed names of these siblings
# (engine_fetch._Inputs, engine_artifact._safe_parent, engine_install._platform_check);
# renaming them breaks the build and the tests here fail at call time. The in-process
# builders of the derived-wheel and project-wheel helpers are deliberately NOT used: their
# audit guard blocks subprocesses in the calling process.
engine_install = _load_sibling("engine_install")
engine_fetch = engine_install.engine_fetch
engine_artifact = engine_install.engine_artifact
engine_dependencies = engine_install.engine_dependencies


# ------------------------------------------------------------------- seams


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run(argv: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> tuple[int, bytes]:
    """The single process seam; tests patch it. Children inherit the caller's environment
    so the export step finds node and npm on PATH; every helper runs under -I.

    The child leads its own process group, so the deadline covers a grandchild too. A failing
    child's last STDERR_TAIL_BYTES of stderr are printed, so a late failure is diagnosable."""
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(argv, cwd=cwd, env=dict(os.environ) if env is None else env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except BaseException:
            _kill_group(proc)
            proc.communicate()
            raise
        if proc.returncode != 0:
            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell() - STDERR_TAIL_BYTES))
            tail = errors.read().decode("utf-8", "replace")
            if tail:
                print(f"engine build: {Path(argv[0]).name} exited {proc.returncode}; stderr tail:\n{tail}",
                      file=sys.stderr, flush=True)
        return proc.returncode, out


def _git_env() -> dict[str, str]:
    """The caller's environment without git's location and configuration overrides."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(args: list[str], repo: Path) -> bytes:
    code, out = _run([GIT, "-c", "core.fsmonitor=false", *args], repo, DEADLINES["git"], _git_env())
    _require(code == 0)
    return out


def check_python() -> None:
    found = sys.version_info[:2]
    if found not in ALLOWED_PYTHONS:
        raise PythonVersion(f"engine build needs Python 3.12 or 3.13 (found {found[0]}.{found[1]})")


def _platform_check() -> None:
    try:
        engine_install._platform_check()
    except Exception as exc:
        raise BuildError() from exc


# --------------------------------------------------------------------- git


def head_commit(repo: Path) -> str:
    commit = _git(["rev-parse", "--verify", "HEAD"], repo).decode("ascii", "strict").strip()
    _require(_HEAD.fullmatch(commit) is not None)
    return commit


def require_clean(repo: Path) -> None:
    """Refuse a tracked change, and any tracked file git has been told to ignore changes to."""
    if _git(["status", "--porcelain=v1", "-z", "--untracked-files=no"], repo) != b"":
        raise DirtyTree()
    for entry in _git(["ls-files", "-v", "-z"], repo).split(b"\0"):
        # Tags: S skip-worktree; lowercase (h, s, ...) assume-unchanged.
        if entry[:1] == b"S" or entry[:1].islower():
            raise DirtyTree()


def committed_version(repo: Path) -> str:
    """The project version in HEAD's pyproject.toml."""
    data = tomllib.loads(_git(["show", "HEAD:pyproject.toml"], repo).decode("utf-8", "strict"))
    version = data["project"]["version"]
    _require(isinstance(version, str) and re.fullmatch(r"[0-9][0-9A-Za-z.+!-]*", version) is not None)
    return version


def verify_engine(engine: Path, receipt: Path) -> str:
    """Prove a (moved) engine tree equals its assembly receipt; return the expected `--version` line."""
    try:
        data = json.loads(Path(receipt).read_bytes())
        _require(isinstance(data, dict) and data.get("kind") == "engine-assembly")
        _require(engine_install.inventory(Path(engine)) == data["inventory"])
        return f"ciao {committed_version(REPO_ROOT)}"
    except BuildError:
        raise
    except Exception as exc:
        raise BuildError() from exc


# ------------------------------------------------------------------- cache


def cache_key(hashes: dict[str, str]) -> str:
    raw = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _own_private_dir(path: Path) -> bool:
    info = os.lstat(path)
    return stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid()


def prepare_wheelhouse(cache: Path, key: str) -> Path:
    """The wheelhouse for one input key. Every other key is pruned; any odd entry is a refusal."""
    _require(_KEY.fullmatch(key) is not None)
    names = sorted(os.listdir(cache))
    for name in names:
        _require(_KEY.fullmatch(name) is not None and _own_private_dir(cache / name))
    for name in names:
        if name != key:
            shutil.rmtree(cache / name)
    house = cache / key
    if not os.path.lexists(house):
        house.mkdir(mode=0o700)
        os.chmod(house, 0o700)
    return house


def write_audit_report(house: Path, work: Path) -> Path:
    """The offline lock-wheel audit, run in-process with the lock-pinned `packaging` wheel."""
    engine_fetch.load_verified_packaging(house)
    for name in ("markers", "requirements", "specifiers", "tags", "utils", "version"):
        importlib.import_module(f"packaging.{name}")
    report = engine_dependencies.audit(REPO_ROOT / "pyproject.toml", REPO_ROOT / "uv.lock")
    _require(report["blockers"] == [])
    path = work / "audit-report.json"
    with open(path, "x", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
        handle.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return path


# ------------------------------------------------------------------ helpers


def helper_argv(step: str, **paths: Any) -> list[str]:
    py = sys.executable
    work: Path = paths["work"]
    house = paths.get("house")
    if step == "fetch-bootstrap":
        return [py, "-I", "-S", str(SCRIPT_DIR / "engine_fetch.py"), "bootstrap", "--wheelhouse", str(house)]
    if step == "fetch-closure":
        return [py, "-I", "-S", str(SCRIPT_DIR / "engine_fetch.py"), "closure", "--wheelhouse", str(house),
                "--audit-report", str(paths["report"])]
    if step == "install":
        return [py, "-I", "-S", str(SCRIPT_DIR / "engine_install.py"), "--wheelhouse", str(house),
                "--audit-report", str(paths["report"]), "--destination", str(work / "b3b")]
    if step == "export":
        return [py, "-I", str(SCRIPT_DIR / "engine_project_wheel.py"), "export", "--commit",
                str(paths["commit"]), "--destination", str(work / "export")]
    if step == "wheel":
        return [py, "-I", str(SCRIPT_DIR / "engine_project_wheel.py"), "build", "--source",
                str(work / "export"), "--backend-wheel", str(house / paths["setuptools"]),
                "--destination", str(work / "wheel")]
    if step == "assemble":
        return [py, "-I", "-S", str(SCRIPT_DIR / "engine_assemble.py"), "--wheelhouse", str(house),
                "--install", str(work / "b3b"), "--source", str(work / "export"), "--project-build",
                str(work / "wheel"), "--project-receipt", str(work / "wheel-receipt.json"),
                "--destination", str(work / "c2")]
    raise BuildError()


def _deadline(step: str) -> int:
    return DEADLINES[step.split("-")[0]]


def _step(step: str, **paths: Any) -> bytes:
    print(f"engine build: {step}", flush=True)
    code, out = _run(helper_argv(step, **paths), REPO_ROOT, _deadline(step))
    _require(code == 0)
    return out


def _destination(destination: Path) -> Path:
    raw = os.fspath(destination)
    _require(bool(raw) and not any(p in (".", "..") for p in raw.split(os.sep)))
    absolute = Path(os.path.abspath(destination))
    parent = engine_artifact._safe_parent(absolute.parent)
    target = parent / absolute.name
    _require(bool(target.name) and not os.path.lexists(target))
    return target


def build(cache: Path, destination: Path) -> None:
    _platform_check()
    try:
        target = _destination(destination)
        cache = engine_artifact._safe_parent(Path(cache))
    except BuildError:
        raise
    except Exception as exc:
        raise BuildError() from exc
    require_clean(REPO_ROOT)
    commit = head_commit(REPO_ROOT)
    work: Path | None = None
    try:
        inputs = engine_fetch._Inputs()
        house = prepare_wheelhouse(cache, cache_key(inputs.hashes))
        work = Path(tempfile.mkdtemp(prefix=".engine-build-", dir=target.parent))
        os.chmod(work, 0o700)
        tools = [t for t in inputs.build_inputs.data["build_tools"] if t["name"] == "setuptools"]
        _require(len(tools) == 1)
        common: dict[str, Any] = {"work": work, "house": house}
        _step("fetch-bootstrap", **common)
        print("engine build: audit", flush=True)
        report = write_audit_report(house, work)
        common["report"] = report
        _step("fetch-closure", **common)
        _step("install", **common)
        _step("export", commit=commit, **common)
        receipt = _step("wheel", setuptools=tools[0]["filename"], **common)
        with open(work / "wheel-receipt.json", "xb") as handle:
            handle.write(receipt)
        _step("assemble", **common)
        _require(sorted(os.listdir(work / "c2")) == ["assembly-receipt.json", "engine"])
        os.rename(work / "c2", target)
        leftover, work = work, None
        # The engine is already published; a cleanup failure must not turn that into a failure.
        try:
            shutil.rmtree(leftover)
        except OSError:
            print(f"engine build: warning: could not remove the scratch directory {leftover}", file=sys.stderr)
    except BuildError:
        raise
    except Exception as exc:
        raise BuildError() from exc
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print("engine build failed: usage", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(allow_abbrev=False, description="Build the bundled engine from HEAD.")
    parser.add_argument("--cache")
    parser.add_argument("--destination")
    parser.add_argument("--verify", nargs=2, metavar=("ENGINE", "RECEIPT"))
    args = parser.parse_args(argv)
    if args.verify is None and (args.cache is None or args.destination is None):
        parser.error("")
    if args.verify is not None and (args.cache is not None or args.destination is not None):
        parser.error("")
    try:
        check_python()
        if args.verify is not None:
            print(verify_engine(Path(args.verify[0]), Path(args.verify[1])))
        else:
            build(Path(args.cache), Path(args.destination))
    except PythonVersion as exc:
        print(exc, file=sys.stderr)
        return 1
    except DirtyTree:
        print(DIRTY_MESSAGE, file=sys.stderr)
        return 1
    except Exception:
        print("engine build failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
