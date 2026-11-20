#!/usr/bin/env python3
"""Offline, binary-only, hash-locked install into a private runtime prefix.

Input: a wheelhouse produced by `engine_fetch.py` and the B2a audit report.
Output: a destination directory holding `prefix/` (the pinned CPython runtime
plus every audited third-party distribution, and no `ciaobot`),
`requirements.txt` and `install-receipt.json`.

Every byte that reaches the prefix was size- and SHA-256-checked against a
committed pin. The wheels are installed by the runtime's own pip with the
network denied, from exactly the selected distributions, and the result is
re-derived independently from the installed METADATA (this is the closure check
that replaces a separate frozen-export comparison).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import importlib.util
import json
import os
import platform
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from email.parser import BytesHeaderParser
from pathlib import Path
from typing import Any

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SANDBOX_DEVICES = ("/dev/null", "/dev/dtracehelper")
CHILD_PATH = "/usr/bin:/bin"
RUNTIME_PIP = ("pip", "26.3")
RECEIPT_NAME = "install-receipt.json"
MAX_OUTPUT = 8 * 1024 * 1024
MAX_METADATA = 1024 * 1024
MAX_RECORD = 4 * 1024 * 1024
PIP_DEADLINE = 900
CHECK_DEADLINE = 300
SMOKE_DEADLINE = 300
DERIVE_DEADLINE = 600
SMOKE_MODULES = (
    "ssl", "pyexpat", "sqlite3", "_ctypes", "cryptography.hazmat.bindings._rust", "cffi",
    "pydantic_core", "uvloop", "httptools", "websockets", "yarl", "multidict", "frozenlist",
    "propcache", "aiohttp", "rpds", "yaml", "watchfiles", "charset_normalizer", "http_ece",
    "pywebpush", "py_vapid", "acp", "anydoc", "mcp", "starlette",
    "uvicorn", "certifi",
)
SCRIPT_DIR = Path(__file__).resolve().parent
DERIVED_WHEEL_SCRIPT = SCRIPT_DIR / "engine_derived_wheel.py"
_FORBIDDEN_ADDED = ("pyvenv.cfg", "direct_url.json", "EXTERNALLY-MANAGED")


class InstallError(Exception):
    """A deliberately non-descriptive refusal suitable for CLI output."""


def _require(condition: bool) -> None:
    if not condition:
        raise InstallError("invalid install")


def _load_sibling(name: str) -> Any:
    path = SCRIPT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("sibling helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Private-helper coupling: this module calls underscore-prefixed names of these
# siblings (engine_fetch._Inputs/_bootstrap_plan/_closure_plan/_verify_file/_wheelhouse,
# engine_artifact._snapshot/_safe_parent, engine_dependencies.Target/_evaluate);
# renaming them breaks the install and the tests here fail at call time. The D1
# derived-wheel builder is deliberately NOT imported: its build guard blocks
# subprocesses in the calling process, so it only ever runs as a child process.
engine_fetch = _load_sibling("engine_fetch")
engine_artifact = engine_fetch.engine_artifact
engine_runtime = engine_fetch.engine_runtime
engine_dependencies = _load_sibling("engine_dependencies")


def _platform_check() -> None:
    _require(sys.platform == "darwin" and platform.machine() == "arm64")
    engine_artifact._platform_check()


# ------------------------------------------------------------- process seam


def _child_env(home: Path) -> dict[str, str]:
    return {"PATH": CHILD_PATH, "HOME": os.fspath(home), "TMPDIR": os.fspath(home), "LANG": "C"}


def _sbpl_path(path: Path) -> str:
    """A resolved absolute path as an SBPL string literal; anything needing escapes is refused."""
    resolved = os.path.realpath(path)
    _require(os.path.isabs(resolved) and all(c.isprintable() and c not in '"\\' for c in resolved))
    return resolved


def sandbox_profile(writable: list[Path]) -> str:
    """Network denied; writes denied except under the given private directories and a few devices."""
    allowed = "".join(f'(subpath "{_sbpl_path(path)}")' for path in writable)
    allowed += "".join(f'(literal "{device}")' for device in SANDBOX_DEVICES)
    return f"(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* {allowed})"


def _sandboxed(argv: list[str], writable: list[Path]) -> list[str]:
    return [SANDBOX_EXEC, "-p", sandbox_profile(writable), *argv]


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run(argv: list[str], env: dict[str, str], cwd: Path, timeout: int) -> tuple[int, bytes]:
    """The single process seam: no shell, no inherited stdin or stderr.

    The child leads its own process group. The deadline covers the whole group (a grandchild that
    keeps the pipe open cannot outlive it) and output is capped while it is read, not afterwards.
    """
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(
            argv, env=env, cwd=cwd, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError("invalid install") from exc
    chunks: list[bytes] = []
    total = 0
    assert proc.stdout is not None
    try:
        fd = proc.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                _require(remaining > 0)
                if not selector.select(remaining):
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                _require(total <= MAX_OUTPUT)
                chunks.append(chunk)
        remaining = deadline - time.monotonic()
        _require(remaining > 0)
        proc.wait(timeout=remaining)
    except (OSError, subprocess.SubprocessError) as exc:
        _kill_group(proc)
        proc.wait()
        raise InstallError("invalid install") from exc
    except BaseException:
        _kill_group(proc)
        proc.wait()
        raise
    finally:
        proc.stdout.close()
    return proc.returncode, b"".join(chunks)


# ------------------------------------------------------------------ step 1


class _Selection:
    """The audited selection, bound to the committed pins and the wheelhouse files."""

    def __init__(self, inputs: Any, bootstrap: list[dict[str, Any]], closure: list[dict[str, Any]],
                 rows: list[dict[str, Any]]) -> None:
        self.inputs = inputs
        self.bootstrap = bootstrap
        self.closure = closure
        self.rows = rows


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    return engine_fetch._json_object(pairs)


def _load_packaging(house: Path) -> Any:
    """The hash-verified `packaging`, with every submodule this module and B2a's marker code use imported."""
    packaging = engine_fetch.load_verified_packaging(house)
    try:
        for name in ("markers", "requirements", "specifiers", "tags", "utils", "version"):
            importlib.import_module(f"packaging.{name}")
    except ImportError as exc:
        raise InstallError("invalid install") from exc
    return packaging


def _canonical(packaging: Any, name: str) -> str:
    return str(packaging.utils.canonicalize_name(name))


def load_selection(packaging: Any, inputs: Any, report_bytes: bytes, scratch: Path) -> _Selection:
    """Validate the report exactly as B3a does, then bind derived rows to the D1 pin."""
    staged = scratch / "report.json"
    with staged.open("xb") as handle:
        handle.write(report_bytes)
    closure = engine_fetch._closure_plan(inputs, staged)
    try:
        report = json.loads(report_bytes.decode("utf-8"), object_pairs_hook=_json_object,
                            parse_constant=engine_fetch._reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InstallError("invalid install") from exc
    derived_pins = {_canonical(packaging, d["name"]): d for d in inputs.build_inputs.data["derived_wheels"]}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in report["selected"]:
        name = _canonical(packaging, entry["name"])
        _require(name not in seen and entry["name"] == name)
        seen.add(name)
        if entry["origin"] == "derived":
            pin = derived_pins.get(name)
            _require(pin is not None)
            wheel = pin["wheel"]
            _require(entry["version"] == pin["version"] and entry["url"] is None
                     and entry["filename"] == wheel["filename"] and entry["size"] == wheel["size"]
                     and entry["sha256"] == wheel["sha256"])
        rows.append({"name": name, "version": entry["version"], "origin": entry["origin"],
                     "filename": entry["filename"], "size": entry["size"], "sha256": entry["sha256"]})
    rows.sort(key=lambda row: row["name"])
    return _Selection(inputs, engine_fetch._bootstrap_plan(inputs), closure, rows)


def _expected_receipt(plan: list[dict[str, Any]], hashes: dict[str, str]) -> dict[str, Any]:
    return {"schema": 1, "inputs": {key: hashes[key] for key in sorted(hashes)},
            "artifacts": sorted(plan, key=lambda record: record["filename"])}


def verify_wheelhouse(house: Path, selection: _Selection) -> None:
    """Rehash every wheelhouse file against its receipt and the pins."""
    hashes = selection.inputs.hashes
    for receipt_name, plan in (("bootstrap-receipt.json", selection.bootstrap),
                               ("closure-receipt.json", selection.closure)):
        try:
            raw = engine_fetch._read_bounded(house / receipt_name)
            recorded = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object,
                                  parse_constant=engine_fetch._reject_constant)
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise InstallError("invalid install") from exc
        _require(recorded == _expected_receipt(plan, hashes))
    expected = {record["filename"]: record for record in selection.bootstrap + selection.closure}
    _require(len(expected) == len(selection.bootstrap) + len(selection.closure))
    try:
        present = {entry.name for entry in house.iterdir()}
    except OSError as exc:
        raise InstallError("invalid install") from exc
    _require(present == set(expected) | {"bootstrap-receipt.json", "closure-receipt.json"})
    for filename, record in expected.items():
        try:
            _require(engine_fetch._verify_file(house / filename, record["size"], record["sha256"]))
        except OSError as exc:
            raise InstallError("invalid install") from exc


# ------------------------------------------------------------------ step 4


def requirements_text(rows: list[dict[str, Any]]) -> str:
    lines = [f"{row['name']}=={row['version']} --hash=sha256:{row['sha256']}"
             for row in sorted(rows, key=lambda row: row["name"])]
    return "\n".join(lines) + "\n"


def pip_argv(python: Path, find_links: Path, requirements: Path) -> list[str]:
    return [os.fspath(python), "-I", "-B", "-m", "pip", "install", "--isolated", "--no-index",
            "--find-links", os.fspath(find_links), "--only-binary", ":all:", "--require-hashes",
            "--no-compile", "--no-warn-script-location", "--disable-pip-version-check",
            "--no-input", "-r", os.fspath(requirements)]


# ------------------------------------------------------------ tree scanning


def _scan(root: Path) -> list[tuple[str, str, Path]]:
    """Sorted (relative path, kind, absolute path) for everything under root; links are not followed."""
    found: list[tuple[str, str, Path]] = []

    def walk(directory: Path, prefix: str) -> None:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            rel = prefix + entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                found.append((rel, "link", Path(entry.path)))
            elif stat.S_ISDIR(info.st_mode):
                found.append((rel, "dir", Path(entry.path)))
                walk(Path(entry.path), rel + "/")
            elif stat.S_ISREG(info.st_mode):
                found.append((rel, "file", Path(entry.path)))
            else:
                raise InstallError("invalid install")

    walk(root, "")
    return found


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _require(stat.S_ISREG(os.fstat(fd).st_mode))
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            count += len(chunk)
            digest.update(chunk)
    finally:
        os.close(fd)
    return count, digest.hexdigest()


def inventory(prefix: Path) -> list[dict[str, Any]]:
    """The sorted path/size/sha256 inventory; a symlink records its target instead."""
    rows: list[dict[str, Any]] = []
    try:
        for rel, kind, path in _scan(prefix):
            if kind == "file":
                size, sha = _file_digest(path)
                rows.append({"path": rel, "size": size, "sha256": sha})
            elif kind == "link":
                rows.append({"path": rel, "symlink": os.readlink(path)})
    except OSError as exc:
        raise InstallError("invalid install") from exc
    return rows


def _paths(prefix: Path) -> set[str]:
    try:
        return {rel for rel, _kind, _path in _scan(prefix)}
    except OSError as exc:
        raise InstallError("invalid install") from exc


def check_added(prefix: Path, baseline: set[str]) -> None:
    """Nothing the install added may be a venv marker, direct-url record, bytecode or an externally-managed marker."""
    for rel in _paths(prefix) - baseline:
        name = rel.rsplit("/", 1)[-1]
        _require(name not in _FORBIDDEN_ADDED and not name.endswith(".pyc"))
    for rel in baseline | _paths(prefix):
        _require(rel.rsplit("/", 1)[-1] != "pyvenv.cfg")


# ------------------------------------------------------------------ step 6


class _Dist:
    def __init__(self, name: str, version: str, requires: list[str], directory: Path) -> None:
        self.name, self.version, self.requires, self.directory = name, version, requires, directory


def _read_dists(site: Path, packaging: Any) -> dict[str, _Dist]:
    installed: dict[str, _Dist] = {}
    try:
        names = sorted(entry for entry in os.listdir(site) if entry.endswith(".dist-info"))
        for entry in names:
            directory = site / entry
            _require(directory.is_dir() and not directory.is_symlink())
            raw = engine_fetch._read_bounded(directory / "METADATA", MAX_METADATA)
            message = BytesHeaderParser().parsebytes(raw)
            name, version = message.get("Name"), message.get("Version")
            _require(type(name) is str and type(version) is str)
            canonical = _canonical(packaging, name)
            stem_name, _, stem_version = entry[: -len(".dist-info")].rpartition("-")
            _require(_canonical(packaging, stem_name) == canonical and stem_version == version)
            _require(canonical not in installed)
            installed[canonical] = _Dist(canonical, version, [str(r) for r in message.get_all("Requires-Dist", [])], directory)
    except (OSError, ValueError) as exc:
        raise InstallError("invalid install") from exc
    return installed


def _roots(packaging: Any, pyproject_bytes: bytes) -> list[Any]:
    try:
        project = tomllib.loads(pyproject_bytes.decode("utf-8"))["project"]
        values = project["dependencies"]
        _require(type(values) is list and all(type(v) is str for v in values))
        return [packaging.requirements.Requirement(v) for v in values]
    except (UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as exc:
        raise InstallError("invalid install") from exc


def validate_installed(site: Path, rows: list[dict[str, Any]], packaging: Any, target: Any,
                       pyproject_bytes: bytes) -> dict[str, _Dist]:
    """Check the installed dist-info set against the report and re-derive the dependency closure.

    Returns the installed distributions by canonical name. Raises InstallError on any mismatch.
    """
    installed = _read_dists(site, packaging)
    expected = {row["name"]: row["version"] for row in rows}
    pip_name, pip_version = RUNTIME_PIP
    _require(pip_name not in expected)
    _require(set(installed) == set(expected) | {pip_name})
    Version = packaging.version.Version
    for name, version in expected.items():
        _require(Version(installed[name].version) == Version(version))
    _require(installed[pip_name].version == pip_version)
    reached: set[str] = set()
    reached_states: set[tuple[str, str]] = set()
    queue: list[tuple[str, str]] = []

    def need(requirement: Any) -> None:
        _require(requirement.url is None)
        dist = installed.get(_canonical(packaging, requirement.name))
        _require(dist is not None and requirement.specifier.contains(dist.version, prereleases=True))
        extras = {""} | {_canonical(packaging, extra) for extra in requirement.extras}
        for extra in extras:
            if (dist.name, extra) not in reached_states:
                reached_states.add((dist.name, extra))
                queue.append((dist.name, extra))
        reached.add(dist.name)

    for root in _roots(packaging, pyproject_bytes):
        if engine_dependencies._evaluate(str(root.marker) if root.marker else None, target, ""):
            need(root)
    while queue:
        name, extra = queue.pop()
        for text in installed[name].requires:
            try:
                requirement = packaging.requirements.Requirement(text)
            except ValueError as exc:
                raise InstallError("invalid install") from exc
            marker = str(requirement.marker) if requirement.marker else None
            try:
                applies = engine_dependencies._evaluate(marker, target, extra)
            except engine_dependencies.AuditError as exc:
                raise InstallError("invalid install") from exc
            if applies:
                need(requirement)
    _require(set(installed) - {pip_name} == reached)
    return installed


# ------------------------------------------------------------------ step 7


def _contains(path: Path, needle: bytes) -> bool:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        tail = b""
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return False
            if needle in tail + chunk:
                return True
            tail = (tail + chunk)[-(len(needle) - 1):] if len(needle) > 1 else b""
    finally:
        os.close(fd)


def scrub_scripts(prefix: Path, site: Path, installed: dict[str, _Dist], rows: list[dict[str, Any]],
                  baseline: set[str]) -> list[str]:
    """Delete every pip-generated console script and its RECORD row; return the removed names."""
    bin_dir = prefix / "bin"
    removed: list[str] = []
    try:
        for row in rows:
            record = installed[row["name"]].directory / "RECORD"
            raw = engine_fetch._read_bounded(record, MAX_RECORD).decode("utf-8")
            kept: list[str] = []
            changed = False
            for line in raw.splitlines(keepends=True):
                fields = next(csv.reader([line]), [])
                first = fields[0] if fields else ""
                resolved = Path(os.path.normpath(os.path.join(site, first)))
                if resolved.parent == bin_dir:
                    rel = f"bin/{resolved.name}"
                    _require(rel not in baseline and resolved.is_file() and not resolved.is_symlink())
                    resolved.unlink()
                    removed.append(resolved.name)
                    changed = True
                else:
                    kept.append(line)
            if changed:
                record.write_text("".join(kept), encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InstallError("invalid install") from exc
    return sorted(removed)


def refuse_build_path(prefix: Path, site: Path, baseline: set[str], build_path: Path) -> None:
    """After the scrub, bin/ is exactly what the runtime shipped and nothing names the build path."""
    needle = os.fsencode(os.fspath(build_path))
    bin_paths = {rel for rel in _paths(prefix) if rel.startswith("bin/") or rel == "bin"}
    _require(bin_paths == {rel for rel in baseline if rel.startswith("bin/") or rel == "bin"})
    try:
        for rel, kind, path in _scan(prefix / "bin"):
            if kind == "link":
                _require(needle not in os.fsencode(os.readlink(path)))
            elif kind == "file":
                _require(not _contains(path, needle))
        for directory in sorted(site.glob("*.dist-info")):
            for _rel, kind, path in _scan(directory):
                if kind == "link":
                    _require(needle not in os.fsencode(os.readlink(path)))
                elif kind == "file":
                    _require(not _contains(path, needle))
    except OSError as exc:
        raise InstallError("invalid install") from exc


# ----------------------------------------------------------------- derive


def _derived_receipt(output: bytes, pin: dict[str, Any], tool: dict[str, Any]) -> dict[str, Any]:
    """Find D1's receipt in its stdout and rebuild it from validated fields only (identical on every host)."""
    # The setuptools backend writes warnings to stdout before D1 prints its receipt, which is
    # the one pretty-printed object that starts at column 0 and runs to the end. The last such
    # object wins; the pin comparison below, not the position, is the authority.
    found = output.rfind(b"\n{\n")
    start = found + 1 if found >= 0 else 0
    try:
        _require(output[start:].startswith(b"{\n"))
        receipt = json.loads(output[start:].decode("utf-8"), object_pairs_hook=_json_object,
                             parse_constant=engine_fetch._reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InstallError("invalid install") from exc
    wheel = pin["wheel"]
    _require(type(receipt) is dict and receipt.get("name") == pin["name"]
             and receipt.get("version") == pin["version"]
             and receipt.get("sdist_sha256") == pin["sdist"]["sha256"]
             and receipt.get("backend_sha256") == tool["sha256"])
    built = receipt.get("wheel")
    _require(type(built) is dict and built.get("filename") == wheel["filename"]
             and built.get("size") == wheel["size"] and built.get("sha256") == wheel["sha256"])
    return {"name": pin["name"], "version": pin["version"],
            "wheel": {"filename": wheel["filename"], "size": wheel["size"], "sha256": wheel["sha256"]}}


def derive_wheels(selection: _Selection, house: Path, work: Path, home: Path) -> list[dict[str, Any]]:
    """Build and verify each derived wheel in sandboxed D1 child processes; return the validated receipts."""
    receipts: list[dict[str, Any]] = []
    data = selection.inputs.build_inputs.data
    tools = [tool for tool in data["build_tools"] if tool["name"] == "setuptools"]
    _require(len(tools) == 1)
    env = _child_env(home)
    derived = work / "derived"
    derived.mkdir(mode=0o700)
    for row in selection.rows:
        if row["origin"] != "derived":
            continue
        pin = next((d for d in data["derived_wheels"] if d["name"] == row["name"]), None)
        _require(pin is not None)
        destination = derived / row["name"]
        build = [sys.executable, "-I", os.fspath(DERIVED_WHEEL_SCRIPT), "build", "--name", row["name"],
                 "--sdist", os.fspath(house / pin["sdist"]["filename"]),
                 "--backend-wheel", os.fspath(house / tools[0]["filename"]),
                 "--destination", os.fspath(destination)]
        status, output = _run(_sandboxed(build, [derived, home]), env, home, DERIVE_DEADLINE)
        _require(status == 0)
        receipt = _derived_receipt(output, pin, tools[0])
        wheel = pin["wheel"]
        wheel_path = destination / wheel["filename"]
        verify = [sys.executable, "-I", os.fspath(DERIVED_WHEEL_SCRIPT), "verify", "--name", row["name"],
                  "--wheel", os.fspath(wheel_path)]
        status, _ = _run(_sandboxed(verify, [home]), env, home, CHECK_DEADLINE)
        _require(status == 0)
        try:
            _require(sorted(entry.name for entry in destination.iterdir()) == [wheel["filename"]])
            _require(engine_fetch._verify_file(wheel_path, wheel["size"], wheel["sha256"]))
        except OSError as exc:
            raise InstallError("invalid install") from exc
        receipts.append(receipt)
    return receipts


def stage_links(selection: _Selection, house: Path, work: Path, links: Path) -> None:
    """Copy exactly the selected wheels (hash-checked while copying) into the pip find-links directory."""
    links.mkdir(mode=0o700)
    lock_files = {record["filename"]: record for record in selection.closure}
    for row in selection.rows:
        try:
            if row["origin"] == "derived":
                source = work / "derived" / row["name"] / row["filename"]
            else:
                _require(row["filename"] in lock_files)
                source = house / row["filename"]
            engine_artifact._snapshot(source, links / row["filename"], row["sha256"], row["size"])
        except (engine_artifact.ArtifactError, OSError) as exc:
            raise InstallError("invalid install") from exc


# ---------------------------------------------------------------- install


def _pip_version(site: Path) -> str:
    entries = sorted(p.name for p in site.glob("pip-*.dist-info"))
    _require(entries == [f"pip-{RUNTIME_PIP[1]}.dist-info"])
    return RUNTIME_PIP[1]


def install(wheelhouse: Path, audit_report: Path, destination: Path) -> dict[str, Any]:
    _platform_check()
    raw_destination = os.fspath(destination)
    _require(bool(raw_destination) and not any(p in (".", "..") for p in raw_destination.split(os.sep)))
    destination = Path(os.path.abspath(destination))
    parent = engine_artifact._safe_parent(destination.parent)
    destination = parent / destination.name
    _require(bool(destination.name) and not os.path.lexists(destination))
    house = engine_fetch._wheelhouse(wheelhouse)
    work: Path | None = None
    try:
        report_bytes = engine_fetch._read_bounded(Path(audit_report), engine_fetch.MAX_INPUT_BYTES)
        work = Path(tempfile.mkdtemp(prefix=".engine-install-", dir=parent))
        os.chmod(work, 0o700)
        home = work / "home"
        home.mkdir(mode=0o700)
        # Step 1: packaging from the verified wheel, report-to-lock agreement, wheelhouse rehash.
        packaging = _load_packaging(house)
        inputs = engine_fetch._Inputs()
        selection = load_selection(packaging, inputs, report_bytes, home)
        verify_wheelhouse(house, selection)
        pin = inputs.pin
        major, minor = pin["python_version"].split(".")[:2]
        # Step 2: the runtime. Everything the install will read is hash-checked first.
        prefix = work / "prefix"
        engine_runtime.extract_pinned_runtime(house / pin["filename"], prefix)
        python = prefix / "bin" / f"python{major}.{minor}"
        site = prefix / "lib" / f"python{major}.{minor}" / "site-packages"
        _require(python.is_file() and site.is_dir())
        baseline = _paths(prefix)
        # Step 3: derive http-ece in the D1 child process.
        derived_receipts = derive_wheels(selection, house, work, home)
        stage_links(selection, house, work, work / "links")
        shutil.rmtree(work / "derived", ignore_errors=True)
        # Step 4.
        requirements = work / "requirements.txt"
        requirements.write_text(requirements_text(selection.rows), encoding="utf-8")
        # Step 5: offline, sandboxed install into the prefix's own site-packages.
        env = _child_env(home)
        status, _ = _run(_sandboxed(pip_argv(python, work / "links", requirements), [prefix, home]), env, home,
                         PIP_DEADLINE)
        _require(status == 0)
        # Step 6.
        target = engine_dependencies.Target(pin)
        installed = validate_installed(site, selection.rows, packaging, target, inputs.pyproject_bytes)
        status, _ = _run(_sandboxed([os.fspath(python), "-I", "-B", "-m", "pip", "check"], [home]), env, home,
                         CHECK_DEADLINE)
        _require(status == 0)
        check_added(prefix, baseline)
        pip_version = _pip_version(site)
        # Step 7.
        removed = scrub_scripts(prefix, site, installed, selection.rows, baseline)
        refuse_build_path(prefix, site, baseline, work)
        # Step 8.
        code = ("import importlib, ssl\nfor name in %r:\n    importlib.import_module(name)\n"
                "print(ssl.OPENSSL_VERSION)\n" % (SMOKE_MODULES,))
        status, output = _run(_sandboxed([os.fspath(python), "-I", "-B", "-c", code], [home]), env, home,
                              SMOKE_DEADLINE)
        _require(status == 0)
        lines = output.decode("utf-8", "replace").splitlines()
        _require(len(lines) == 1 and 0 < len(lines[0]) <= 200 and lines[0].startswith(("OpenSSL", "LibreSSL")))
        check_added(prefix, baseline)
        # Step 9.
        receipt = {
            "schema": 1,
            "inputs": {key: inputs.hashes[key] for key in sorted(inputs.hashes)},
            "selected": [{key: row[key] for key in ("name", "version", "origin", "filename", "size", "sha256")}
                         for row in selection.rows],
            "derived": derived_receipts,
            "removed_scripts": removed,
            "pip_version": pip_version,
            "openssl_version": lines[0],
            "inventory": inventory(prefix),
        }
        with (work / RECEIPT_NAME).open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
        shutil.rmtree(home)
        shutil.rmtree(work / "links")
        # One rename, re-checked just before. A rename onto an existing empty directory succeeds
        # on macOS, so the exclusively owned 0700 parent (nobody else can create the name) is the
        # no-replace guarantee; this check only catches a mistaken caller.
        _require(not os.path.lexists(destination))
        os.rename(work, destination)
        work = None
        return receipt
    except InstallError:
        raise
    except (OSError, engine_fetch.FetchError, engine_artifact.ArtifactError,
            engine_runtime.RuntimePinError, KeyError, TypeError, ValueError) as exc:
        raise InstallError("invalid install") from exc
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


# -------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print("engine install failed: usage", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(allow_abbrev=False, description="Install the audited engine prefix offline.")
    parser.add_argument("--wheelhouse", required=True)
    parser.add_argument("--audit-report", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    try:
        install(Path(args.wheelhouse), Path(args.audit_report), Path(args.destination))
    except Exception:
        print("engine install failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
