#!/usr/bin/env python3
"""Exact-source export, PWA build and the ``ciaobot`` wheel with the pinned setuptools.

Two subcommands, run as two separate processes: ``export`` runs git and npm, while
``build`` runs the pinned setuptools in-process under the audit guard that blocks
subprocesses and network, so it must not share a process with anything that needs them.
Stdlib only; nothing here reads the network itself. The setuptools wheel is the one pinned in
``mac/engine-build-inputs.json`` and is size- and SHA-256-checked before it is parsed.

The build driver is the D1 helper (``engine_derived_wheel.py``), loaded by file path. Its private
seams (backend extraction, guarded in-process backend run, input loader) are reused as they are.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email import message_from_bytes
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_EPOCH = 315_532_800
MAX_EPOCH = 4_102_444_800
MAX_TAR_BYTES = 512 * 1024 * 1024
MAX_TREE_MEMBERS = 20_000
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_STATIC_FILES = 5_000
MAX_STATIC_FILE_BYTES = 64 * 1024 * 1024
MAX_STATIC_BYTES = 128 * 1024 * 1024
MAX_PROJECT_FILE_BYTES = 8 * 1024 * 1024
MAX_PROJECT_FILES = 20_000
MAX_WHEEL_BYTES = 192 * 1024 * 1024
MAX_WHEEL_MEMBERS = 25_000
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_PYPROJECT_BYTES = 262_144
STATIC_REL = "ciao/web/static"
LOCK_REL = "web/package-lock.json"
PROJECT_FILES = ("pyproject.toml", "README.md", "LICENSE")
PROJECT_NAME = "ciaobot"
TOOL_TIMEOUT = 1800
# Mirrors `engines.node` in web/package.json and SUPPORTED_RANGE in web/scripts/check-node.mjs.
NODE_RANGE = "^22.22.2 || ^24.15.0 || >=26.0.0"
NPM_ARGVS = (("ci", "--no-audit", "--no-fund"), ("run", "build"))
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_NODE_VERSION = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")
_REQUIREMENT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*\(?([^()]*)\)?")
_GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
}


class ProjectWheelError(Exception):
    """A deliberately non-descriptive refusal suitable for CLI output."""


def _require(condition: bool) -> None:
    if not condition:
        raise ProjectWheelError()


def _load_derived():
    path = Path(__file__).resolve().with_name("engine_derived_wheel.py")
    spec = importlib.util.spec_from_file_location("engine_derived_wheel", path)
    if spec is None or spec.loader is None:
        raise ImportError("derived wheel helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


derived = _load_derived()
engine_artifact = derived.engine_artifact


def _hex40(value: Any) -> bool:
    return type(value) is str and _HEX40.fullmatch(value) is not None


def _hex64(value: Any) -> bool:
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _int(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _fresh_destination(destination: Path) -> tuple[Path, Path]:
    _require(isinstance(destination, Path) and bool(destination.name))
    _require(destination.name not in (".", ".."))
    parent = engine_artifact._safe_parent(Path(os.path.abspath(destination)).parent)
    target = parent / destination.name
    _require(not os.path.lexists(target))
    return parent, target


def _private_work(parent: Path, prefix: str) -> Path:
    work = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    os.chmod(work, 0o700)
    return work


def _write_file(path: Path, data: bytes, mode: int) -> None:
    derived._write_file(path, data, mode)


# ---------------------------------------------------------------- inventory


def _inventory(static: Path) -> list[dict[str, Any]]:
    """Every file under ``static`` (regular files only, no symlinks), sorted by path."""
    _require(stat.S_ISDIR(os.lstat(static).st_mode))
    records: list[dict[str, Any]] = []
    total = 0
    seen: set[str] = set()
    stack: list[tuple[Path, str]] = [(static, "")]
    while stack:
        directory, prefix = stack.pop()
        with os.scandir(directory) as entries:
            listing = sorted(entries, key=lambda e: e.name)
        for entry in listing:
            rel = prefix + entry.name
            info = entry.stat(follow_symlinks=False)
            derived._components(rel)
            _require(rel.casefold() not in seen)
            seen.add(rel.casefold())
            if stat.S_ISDIR(info.st_mode):
                stack.append((Path(entry.path), rel + "/"))
            else:
                _require(stat.S_ISREG(info.st_mode) and len(records) < MAX_STATIC_FILES)
                data = derived._read_regular(Path(entry.path), MAX_STATIC_FILE_BYTES)
                total += len(data)
                _require(total <= MAX_STATIC_BYTES)
                records.append({"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    records.sort(key=lambda r: r["path"])
    _require(any(r["path"] == "index.html" for r in records))
    return records


# ------------------------------------------------------------------- export


def _git(repo: Path, *args: str, stdout: Any = subprocess.PIPE) -> bytes:
    proc = subprocess.run(
        ["/usr/bin/git", "-c", "core.fsmonitor=false", *args],
        cwd=repo, env=_GIT_ENV, stdin=subprocess.DEVNULL, stdout=stdout,
        stderr=subprocess.DEVNULL, check=False, timeout=600,
    )
    _require(proc.returncode == 0)
    return proc.stdout or b""


def _tree_entries(repo: Path, commit: str) -> dict[str, tuple[bool, str]]:
    raw = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries: dict[str, tuple[bool, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, path_bytes = record.partition(b"\t")
        mode, kind, sha = meta.decode("ascii").split(" ")
        # Symlinks, submodules and anything else are refused outright.
        _require(kind == "blob" and mode in ("100644", "100755") and _hex40(sha))
        path = path_bytes.decode("utf-8")
        derived._components(path)
        _require(path not in entries and len(entries) < MAX_TREE_MEMBERS)
        entries[path] = (mode == "100755", sha)
    _require(bool(entries))
    folded = {p.casefold() for p in entries}
    _require(len(folded) == len(entries))
    return entries


def _scan_archive(archive: Path, entries: dict[str, tuple[bool, str]]) -> list[str]:
    """Pre-scan the whole tar before anything is written: only listed regular files."""
    names: list[str] = []
    total = 0
    with tarfile.open(archive, "r:") as tar:
        for count, member in enumerate(tar, 1):
            _require(count <= MAX_TREE_MEMBERS * 2 and (member.isreg() or member.isdir()))
            derived._components(member.name)
            if member.isdir():
                continue
            entry = entries.get(member.name)
            _require(entry is not None and member.name not in names)
            _require((member.mode & 0o111 != 0) == entry[0])
            total += member.size
            _require(total <= MAX_TREE_BYTES)
            names.append(member.name)
    _require(sorted(names) == sorted(entries))
    return names


def _extract_archive(archive: Path, entries: dict[str, tuple[bool, str]], epoch: int, target: Path) -> None:
    target.mkdir(mode=0o700)
    with tarfile.open(archive, "r:") as tar:
        for member in tar:
            if member.isdir():
                continue
            executable, blob_sha = entries[member.name]
            handle = tar.extractfile(member)
            _require(handle is not None)
            data = handle.read(member.size + 1)
            _require(len(data) == member.size)
            # git's own object id: the archive bytes must be the committed blob, byte for byte
            # (no export-subst or attribute conversion).
            digest = hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()
            _require(digest == blob_sha)
            path = target / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_file(path, data, 0o755 if executable else 0o644)
            os.utime(path, (epoch, epoch))
    for directory in [target, *(p for p in target.rglob("*") if p.is_dir())]:
        os.chmod(directory, 0o755)
        os.utime(directory, (epoch, epoch))


def _node_supported(version: str) -> bool:
    match = _NODE_VERSION.fullmatch(version.strip())
    if match is None:
        return False
    major, minor, patch = (int(g) for g in match.groups())
    if major == 22:
        return (minor, patch) >= (22, 2)
    if major == 24:
        return minor >= 15
    return major >= 26


def _node_dir() -> Path:
    found = shutil.which("node")
    _require(found is not None)
    directory = Path(os.path.abspath(found)).parent
    _require((directory / "npm").exists())
    return directory


def _run_tool(argv: list[str], cwd: Path, env: dict[str, str]) -> bytes:
    proc = subprocess.run(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
        check=False, timeout=TOOL_TIMEOUT,
    )
    _require(proc.returncode == 0)
    return proc.stdout


def _npm_env(node_dir: Path, scratch: Path) -> dict[str, str]:
    home, tmp = scratch / "home", scratch / "tmp"
    home.mkdir(mode=0o700)
    tmp.mkdir(mode=0o700)
    return {
        "PATH": f"{node_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LC_ALL": "C",
        "TZ": "UTC",
    }


def _build_pwa(source: Path, scratch: Path) -> None:
    node_dir = _node_dir()
    env = _npm_env(node_dir, scratch)
    version = _run_tool([str(node_dir / "node"), "--version"], source, env).decode("ascii", "replace")
    _require(_node_supported(version))
    for args in NPM_ARGVS:
        _run_tool([str(node_dir / "npm"), *args], source / "web", env)


def export_source(commit: str, destination: Path, repo: Path | None = None) -> dict[str, Any]:
    work: Path | None = None
    try:
        derived._platform_check()
        repo = REPO_ROOT if repo is None else Path(repo)
        _require(_hex40(commit))
        parent, target = _fresh_destination(destination)
        _require(_git(repo, "rev-parse", "--verify", "HEAD").decode().strip() == commit)
        _require(_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=no") == b"")
        tree = _git(repo, "rev-parse", "--verify", f"{commit}^{{tree}}").decode().strip()
        _require(_hex40(tree))
        epoch = int(_git(repo, "show", "-s", "--format=%ct", commit).decode().strip())
        _require(_int(epoch, MIN_EPOCH, MAX_EPOCH))
        entries = _tree_entries(repo, commit)
        work = _private_work(parent, ".project-export-")
        scratch = work / "scratch"
        scratch.mkdir(mode=0o700)
        archive = work / "source.tar"
        with archive.open("xb") as handle:
            _git(repo, "archive", "--format=tar", commit, stdout=handle)
        _require(0 < archive.stat().st_size <= MAX_TAR_BYTES)
        _scan_archive(archive, entries)
        out = work / "out"
        out.mkdir(mode=0o700)
        source = out / "source"
        _extract_archive(archive, entries, epoch, source)
        lock = derived._read_regular(source / LOCK_REL, MAX_STATIC_FILE_BYTES)
        # The tree's own ciao/web/static holds only the committed brand files; the build output
        # replaces it wholesale, so nothing but the PWA build can end up in the inventory.
        shutil.rmtree(source / STATIC_REL, ignore_errors=True)
        _build_pwa(source, scratch)
        _require(hashlib.sha256(derived._read_regular(source / LOCK_REL, MAX_STATIC_FILE_BYTES)).digest()
                 == hashlib.sha256(lock).digest())
        shutil.rmtree(source / "web" / "node_modules", ignore_errors=True)
        receipt = {
            "schema_version": 1,
            "commit": commit,
            "tree": tree,
            "source_date_epoch": epoch,
            "package_lock_sha256": hashlib.sha256(lock).hexdigest(),
            "static_inventory": _inventory(source / STATIC_REL),
        }
        _write_file(out / "receipt.json", (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode(), 0o600)
        os.rename(out, target)
    except ProjectWheelError:
        raise
    except Exception as exc:
        raise ProjectWheelError() from exc
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    return receipt


# -------------------------------------------------------------------- build


def _load_receipt(root: Path) -> dict[str, Any]:
    _require(sorted(p.name for p in root.iterdir()) == ["receipt.json", "source"])
    raw = derived._read_regular(root / "receipt.json", MAX_RECEIPT_BYTES)
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=derived._object,
                      parse_constant=derived._reject_constant)
    derived._keys(data, {"schema_version", "commit", "tree", "source_date_epoch",
                         "package_lock_sha256", "static_inventory"})
    _require(data["schema_version"] == 1 and type(data["schema_version"]) is int)
    _require(_hex40(data["commit"]) and _hex40(data["tree"]) and _hex64(data["package_lock_sha256"]))
    _require(_int(data["source_date_epoch"], MIN_EPOCH, MAX_EPOCH))
    inventory = data["static_inventory"]
    _require(type(inventory) is list and 0 < len(inventory) <= MAX_STATIC_FILES)
    previous = None
    for record in inventory:
        derived._keys(record, {"path", "size", "sha256"})
        derived._components(record["path"])
        _require(_int(record["size"], 0, MAX_STATIC_FILE_BYTES) and _hex64(record["sha256"]))
        _require(previous is None or record["path"] > previous)
        previous = record["path"]
    _require(any(r["path"] == "index.html" for r in inventory))
    return data


def _copy_tree(source: Path, target: Path, epoch: int, budget: list[int]) -> None:
    target.mkdir(mode=0o700)
    with os.scandir(source) as entries:
        listing = sorted(entries, key=lambda e: e.name)
    for entry in listing:
        info = entry.stat(follow_symlinks=False)
        derived._components(entry.name)
        path = target / entry.name
        if stat.S_ISDIR(info.st_mode):
            _copy_tree(Path(entry.path), path, epoch, budget)
            continue
        _require(stat.S_ISREG(info.st_mode))
        budget[0] += 1
        _require(budget[0] <= MAX_PROJECT_FILES)
        data = derived._read_regular(Path(entry.path), MAX_STATIC_FILE_BYTES)
        budget[1] += len(data)
        _require(budget[1] <= MAX_TREE_BYTES)
        _write_file(path, data, 0o755 if info.st_mode & 0o100 else 0o644)
        os.utime(path, (epoch, epoch))


def _copy_project(tree: Path, project: Path, epoch: int) -> None:
    """Only what the wheel is built from: pyproject, readme, licence and the ``ciao`` package."""
    _require(stat.S_ISDIR(os.lstat(tree).st_mode))
    project.mkdir(mode=0o700)
    for name in PROJECT_FILES:
        data = derived._read_regular(tree / name, MAX_PROJECT_FILE_BYTES)
        _write_file(project / name, data, 0o644)
        os.utime(project / name, (epoch, epoch))
    _require(stat.S_ISDIR(os.lstat(tree / "ciao").st_mode))
    _copy_tree(tree / "ciao", project / "ciao", epoch, [0, 0])
    for directory in [project, *(p for p in project.rglob("*") if p.is_dir())]:
        os.chmod(directory, 0o755)
        os.utime(directory, (epoch, epoch))


def _read_project(project: Path, tool: dict[str, Any]) -> tuple[str, list[str]]:
    _require(not (project / "setup.py").exists() and not (project / "setup.cfg").exists())
    declaration = tomllib.loads(derived._read_regular(project / "pyproject.toml", MAX_PYPROJECT_BYTES).decode("utf-8"))
    build_system = declaration.get("build-system")
    _require(type(build_system) is dict and build_system.get("build-backend") == "setuptools.build_meta")
    # `wheel` is declared but unused: setuptools 84 vendors bdist_wheel.
    requires = build_system.get("requires")
    _require(type(requires) is list and bool(requires)
             and all(type(r) is str and derived._requirement_ok(r, tool) for r in requires))
    section = declaration.get("project")
    _require(type(section) is dict and section.get("name") == PROJECT_NAME and "dynamic" not in section)
    version = section.get("version")
    _require(type(version) is str and _VERSION.fullmatch(version) is not None)
    dependencies = section.get("dependencies")
    _require(type(dependencies) is list and bool(dependencies))
    _require(all(type(d) is str and ";" not in d and _REQUIREMENT.fullmatch(d.strip()) for d in dependencies))
    return version, dependencies


def _normalise_requirement(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    match = _REQUIREMENT.fullmatch(text.strip())
    _require(match is not None)
    name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
    extras = tuple(sorted(e.strip().lower() for e in (match.group(2) or "").split(",") if e.strip()))
    specifiers = tuple(sorted(s.replace(" ", "") for s in match.group(3).split(",") if s.strip()))
    return name, extras, specifiers


def _check_wheel(
    data: bytes, version: str, dependencies: list[str], inventory: list[dict[str, Any]], epoch: int
) -> None:
    dist_info = f"{PROJECT_NAME}-{version}.dist-info"
    expected_time = derived._zip_time(epoch)
    contents: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        _require(0 < len(infos) <= MAX_WHEEL_MEMBERS)
        _require(sum(i.file_size for i in infos) <= MAX_TREE_BYTES)
        for info in infos:
            path = info.filename
            derived._relpath(path)
            _require(path not in contents and not info.is_dir())
            _require(not derived._is_symlink_or_special(info) and info.date_time == expected_time)
            # Nothing but the package and its own metadata: in particular no `mac/` member.
            _require(path.startswith(("ciao/", dist_info + "/")))
            contents[path] = derived._read_zip_member(archive, info)
    _require(len({p.casefold() for p in contents}) == len(contents))
    prefix = STATIC_REL + "/"
    static = {p[len(prefix):]: blob for p, blob in contents.items() if p.startswith(prefix)}
    _require(f"{prefix}index.html" in contents)
    _require(sorted(static) == [r["path"] for r in inventory])
    for record in inventory:
        blob = static[record["path"]]
        _require(len(blob) == record["size"] and hashlib.sha256(blob).hexdigest() == record["sha256"])
    meta = message_from_bytes(contents[f"{dist_info}/METADATA"])
    _require(meta.get("Name") is not None and meta["Name"].lower() == PROJECT_NAME and meta.get("Version") == version)
    lines = meta.get_all("Requires-Dist") or []
    _require(all(("extra ==" in line) if ";" in line else True for line in lines))
    production = [line for line in lines if ";" not in line]
    _require(sorted(_normalise_requirement(r) for r in production)
             == sorted(_normalise_requirement(d) for d in dependencies))
    wheel_meta = message_from_bytes(contents[f"{dist_info}/WHEEL"])
    _require(wheel_meta.get("Root-Is-Purelib") == "true" and wheel_meta.get_all("Tag") == ["py3-none-any"])
    rows = list(csv.reader(contents[f"{dist_info}/RECORD"].decode("utf-8").splitlines()))
    _require(all(len(r) == 3 for r in rows) and len({r[0] for r in rows}) == len(rows))
    _require({r[0] for r in rows} == set(contents))
    for path_, digest, size in rows:
        if path_ == f"{dist_info}/RECORD":
            _require(digest == "" and size == "")
        else:
            blob = contents[path_]
            encoded = base64.urlsafe_b64encode(hashlib.sha256(blob).digest()).rstrip(b"=").decode()
            _require(digest == f"sha256={encoded}" and size == str(len(blob)))


def build_project_wheel(source: Path, backend_wheel: Path, destination: Path) -> dict[str, Any]:
    work: Path | None = None
    try:
        derived._platform_check()
        parent, target = _fresh_destination(destination)
        root = engine_artifact._safe_parent(Path(os.path.abspath(source)))
        receipt = _load_receipt(root)
        epoch = receipt["source_date_epoch"]
        tree = root / "source"
        inputs = derived.load_build_inputs()
        tool = next((t for t in inputs.data["build_tools"] if t["name"] == "setuptools"), None)
        _require(tool is not None)
        work = _private_work(parent, ".project-build-")
        backend_copy = work / tool["filename"]
        engine_artifact._snapshot(Path(backend_wheel), backend_copy, tool["sha256"], tool["size"])
        derived._extract_backend(backend_copy, tool, work / "backend")
        lock = derived._read_regular(tree / LOCK_REL, MAX_STATIC_FILE_BYTES)
        _require(hashlib.sha256(lock).hexdigest() == receipt["package_lock_sha256"])
        project = work / "project"
        _copy_project(tree, project, epoch)
        _require(_inventory(project / STATIC_REL) == receipt["static_inventory"])
        version, dependencies = _read_project(project, tool)
        out_dir, scratch = work / "out", work / "scratch"
        out_dir.mkdir(mode=0o700)
        scratch.mkdir(mode=0o700)
        # setuptools logs progress and warnings on stdout/stderr; the receipt must be all the CLI prints.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            derived._run_backend("setuptools.build_meta", work / "backend", project, out_dir, epoch, tool, scratch)
        produced = sorted(out_dir.iterdir())
        _require(len(produced) == 1 and produced[0].name == f"{PROJECT_NAME}-{version}-py3-none-any.whl")
        data = derived._read_regular(produced[0], MAX_WHEEL_BYTES)
        _check_wheel(data, version, dependencies, receipt["static_inventory"], epoch)
        os.chmod(produced[0], 0o644)
        # Defence in depth: the bytes validated above are the bytes that get published.
        _require(hashlib.sha256(derived._read_regular(produced[0], MAX_WHEEL_BYTES)).digest()
                 == hashlib.sha256(data).digest())
        os.rename(out_dir, target)
        result = {
            "wheel": {
                "filename": produced[0].name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
            "commit": receipt["commit"],
            "tree": receipt["tree"],
            "backend_sha256": tool["sha256"],
            "source_date_epoch": epoch,
        }
    except ProjectWheelError:
        raise
    except Exception as exc:
        raise ProjectWheelError() from exc
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    return result


# ---------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print("project wheel failed", file=sys.stderr)
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(allow_abbrev=False, description="Export the exact source and build the ciaobot wheel.")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", allow_abbrev=False)
    for flag in ("--commit", "--destination"):
        export.add_argument(flag, required=True)
    build = commands.add_parser("build", allow_abbrev=False)
    for flag in ("--source", "--backend-wheel", "--destination"):
        build.add_argument(flag, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        derived._platform_check()
        if not args.destination or any(p in (".", "..") for p in args.destination.split("/")):
            raise ProjectWheelError()
        if args.command == "export":
            receipt = export_source(args.commit, Path(args.destination))
        else:
            receipt = build_project_wheel(Path(args.source), Path(args.backend_wheel), Path(args.destination))
        print(json.dumps(receipt, sort_keys=True, indent=2))
    except Exception:
        print("project wheel failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
