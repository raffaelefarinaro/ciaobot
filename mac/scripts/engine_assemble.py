#!/usr/bin/env python3
"""Assemble the relocatable engine prefix: B3b runtime + the ``ciaobot`` wheel + launcher.

Inputs are the B3b install destination (the pinned runtime plus every audited third-party
distribution), the C1 exact-source export, the C1 ``ciaobot`` wheel and its receipt. Every input is
bound to the committed pins before anything runs. The wheel is installed offline, hash-locked and
sandboxed, the dependency closure is re-proved with ``ciaobot`` as the root, runtime bytecode is
removed, the launcher is installed, and pip is removed as the very last mutation. The result is
verified read-only, every Mach-O file is audited and a deterministic ``provenance.json`` is written.

Stdlib only. The destination is published by one rename; a refusal leaves nothing behind.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import stat
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
LAUNCHER_TEMPLATE = SCRIPT_DIR / "engine-launcher.sh"
PY_MM = "3.12"  # must equal the runtime pin's major.minor; the launcher hardcodes it
LAUNCHER_REL = "bin/ciao"
PYTHON_REL = f"bin/python{PY_MM}"
SITE_REL = f"lib/python{PY_MM}/site-packages"
CAFILE_REL = f"{SITE_REL}/certifi/cacert.pem"
STDLIB_REL = f"lib/python{PY_MM}"
PIP_VERSION = "26.3"  # checked against engine_install.RUNTIME_PIP at import
PIP_TREES = (f"{SITE_REL}/pip", f"{SITE_REL}/pip-{PIP_VERSION}.dist-info", f"{STDLIB_REL}/ensurepip")
PIP_SCRIPTS = ("bin/pip", "bin/pip3", f"bin/pip{PY_MM}")
PIP_BUNDLED_WHEEL = f"{STDLIB_REL}/ensurepip/_bundled/pip-{PIP_VERSION}-py3-none-any.whl"
REQUIRED_MACHO = (PYTHON_REL, f"{SITE_REL}/claude_agent_sdk/_bundled/claude")
MAX_MINOS = (13, 0)
PROVENANCE_NAME = "provenance.json"
RECEIPT_NAME = "assembly-receipt.json"
PROJECT_SMOKE_MODULES = ("ciao", "ciao.cli", "ciao.main", "ciao.web.app", "ciao.release_manifest",
                         "ciao.supervise")
RUNTIME_STATEMENT = (
    "Every file is byte-identical to the pinned runtime archive or an installed pinned wheel "
    "(audited, derived http-ece, or ciaobot), except: the removals listed here; console scripts "
    "pip generated (never shipped, their RECORD rows dropped); dist-info INSTALLER/RECORD files "
    "written by pip; bin/ciao; provenance.json.")
MAX_RECEIPT_BYTES = 64 * 1024 * 1024
MAX_LAUNCHER_BYTES = 64 * 1024
MAX_MACHO_COMMANDS = 4096
MAX_SIZEOFCMDS = 1 << 20
MAX_FAT_ARCHES = 4096
THIN_MAGIC_64 = b"\xcf\xfa\xed\xfe"
FAT_MAGIC = b"\xca\xfe\xba\xbe"
FAT_MAGIC_64 = b"\xca\xfe\xba\xbf"
OTHER_MACHO_MAGICS = (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")
JAVA_CLASS_FAT_COUNT = 43
CPU_TYPE_ARM64 = 0x0100000C
FILETYPES = {2: "execute", 6: "dylib", 8: "bundle"}
LC_BUILD_VERSION = 0x32
LC_VERSION_MIN_MACOSX = 0x24
LC_RPATH = 0x8000001C
LC_DYLD_ENVIRONMENT = 0x27
LC_LOAD_DYLIBS = (0xC, 0x80000018, 0x8000001F, 0x20, 0x80000023)
SYSTEM_PREFIXES = ("/usr/lib/", "/System/Library/")
SYSTEM_PREFIXES_BARE = ("/usr/lib", "/System/Library")


class AssembleError(Exception):
    """A deliberately non-descriptive refusal suitable for CLI output."""

    def __init__(self, *_args: object) -> None:
        super().__init__("invalid assembly")


def _require(condition: bool) -> None:
    if not condition:
        raise AssembleError()


def _load_sibling(name: str) -> Any:
    path = SCRIPT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("sibling helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Private-helper coupling: this module calls underscore-prefixed names of these siblings
# (engine_install._run/_sandboxed/_child_env/_scan/_file_digest/_paths/_read_dists/_contains/
# _load_packaging/_platform_check, engine_project_wheel._load_receipt/_read_project/_check_wheel,
# engine_fetch._Inputs/_wheelhouse/_read_bounded/_json_object/_reject_constant,
# engine_artifact._safe_parent/_snapshot); renaming them breaks assembly and the tests here fail
# at call time. The in-process builders of the D1 and C1 helpers are deliberately NOT used: their
# audit guard blocks subprocesses in the calling process.
engine_install = _load_sibling("engine_install")
engine_project_wheel = _load_sibling("engine_project_wheel")
engine_fetch = engine_install.engine_fetch
engine_artifact = engine_install.engine_artifact
engine_runtime = engine_install.engine_runtime
engine_dependencies = engine_install.engine_dependencies

if engine_install.RUNTIME_PIP[1] != PIP_VERSION:  # pragma: no cover - guards a re-pin
    raise ImportError("pip removal list is out of date")


# ------------------------------------------------------------------- seams


def _run(argv: list[str], env: dict[str, str], cwd: Path, timeout: int) -> tuple[int, bytes]:
    """The single process seam; tests patch it."""
    return engine_install._run(argv, env, cwd, timeout)


def _inputs() -> Any:
    return engine_fetch._Inputs()


def _platform_check() -> None:
    try:
        engine_install._platform_check()
    except Exception as exc:
        raise AssembleError() from exc


def _json_load(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=engine_fetch._json_object,
                          parse_constant=engine_fetch._reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise AssembleError() from exc


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path, limit: int = MAX_RECEIPT_BYTES) -> bytes:
    return engine_fetch._read_bounded(path, limit)


def _is_hex64(value: Any) -> bool:
    return bool(engine_project_wheel._hex64(value))


# ------------------------------------------------------------ input binding


def load_install(install: Path, inputs: Any) -> dict[str, Any]:
    """The B3b destination: exactly prefix/, requirements.txt and install-receipt.json, bound to the pins."""
    root = engine_artifact._safe_parent(install)
    _require(sorted(entry.name for entry in root.iterdir())
             == ["install-receipt.json", "prefix", "requirements.txt"])
    prefix = root / "prefix"
    _require(prefix.is_dir() and not prefix.is_symlink())
    receipt = _json_load(_read(root / engine_install.RECEIPT_NAME))
    keys = {"schema", "inputs", "selected", "derived", "removed_scripts", "pip_version", "openssl_version",
            "inventory"}
    _require(type(receipt) is dict and set(receipt) == keys)
    _require(type(receipt["schema"]) is int and receipt["schema"] == 1)
    _require(receipt["inputs"] == inputs.hashes)
    _require(receipt["pip_version"] == engine_install.RUNTIME_PIP[1])
    selected = receipt["selected"]
    _require(type(selected) is list and bool(selected))
    row_keys = {"name", "version", "origin", "filename", "size", "sha256"}
    for row in selected:
        _require(type(row) is dict and set(row) == row_keys and row["origin"] in {"lock", "derived"})
        _require(all(type(row[key]) is str for key in ("name", "version", "filename", "sha256")))
        _require(type(row["size"]) is int)
    _require(type(receipt["derived"]) is list)
    _require(type(receipt["removed_scripts"]) is list and all(type(n) is str for n in receipt["removed_scripts"]))
    _require(type(receipt["openssl_version"]) is str and bool(receipt["openssl_version"]))
    _require(type(receipt["inventory"]) is list)
    requirements = _read(root / "requirements.txt").decode("utf-8", "replace")
    _require(requirements == engine_install.requirements_text(selected))
    receipt["prefix"] = prefix
    return receipt


def bind_selected(receipt: dict[str, Any], inputs: Any) -> None:
    """Each receipt row must equal the lock wheel (origin lock) or the D1 derived pin (origin derived)."""
    derived_pins = {d["name"]: d for d in inputs.build_inputs.data["derived_wheels"]}
    names: set[str] = set()
    for row in receipt["selected"]:
        _require(row["name"] not in names)
        names.add(row["name"])
        if row["origin"] == "derived":
            pin = derived_pins.get(row["name"])
            _require(pin is not None and row["version"] == pin["version"])
            wheel = pin["wheel"]
            expected = {"filename": wheel["filename"], "size": wheel["size"], "sha256": wheel["sha256"]}
        else:
            lock = inputs.lock_wheel(row["name"], row["version"], row["filename"])
            expected = {key: lock[key] for key in ("filename", "size", "sha256")}
        _require(all(row[key] == value for key, value in expected.items()))
    _require(names >= set(derived_pins))


def _extract_runtime(archive: Path, destination: Path) -> None:
    """The seam that unpacks the pinned runtime archive; tests patch it."""
    engine_runtime.extract_pinned_runtime(archive, destination)


def check_interpreter(python: Path, house: Path, pin: dict[str, Any], scratch: Path) -> None:
    """The prefix's interpreter must be byte-identical to the one in the pinned runtime archive."""
    try:
        _extract_runtime(house / pin["filename"], scratch)
        pristine = scratch / PYTHON_REL
        _require(pristine.is_file() and not pristine.is_symlink())
        _require(engine_install._file_digest(pristine) == engine_install._file_digest(python))
    except (engine_artifact.ArtifactError, engine_runtime.RuntimePinError, OSError) as exc:
        raise AssembleError() from exc
    shutil.rmtree(scratch)


def load_project(source_root: Path, build_dir: Path, receipt_path: Path, inputs: Any,
                 links: Path) -> dict[str, Any]:
    """The C1 export and build, bound to the pins, to each other and to the wheel bytes."""
    try:
        root = engine_artifact._safe_parent(source_root)
        build = engine_artifact._safe_parent(build_dir)
        export = engine_project_wheel._load_receipt(root)
        tree = root / "source"
        for rel, key in (("pyproject.toml", "pyproject_sha256"), ("uv.lock", "lock_sha256"),
                         ("mac/engine-runtime.json", "runtime_pin_sha256"),
                         ("mac/engine-build-inputs.json", "build_inputs_sha256")):
            _require(_sha(_read(tree / rel, engine_fetch.MAX_INPUT_BYTES)) == inputs.hashes[key])
        receipt = _json_load(_read(receipt_path, engine_project_wheel.MAX_RECEIPT_BYTES))
        _require(type(receipt) is dict and set(receipt) == {"wheel", "commit", "tree", "backend_sha256",
                                                            "source_date_epoch"})
        wheel = receipt["wheel"]
        _require(type(wheel) is dict and set(wheel) == {"filename", "size", "sha256"})
        _require(receipt["commit"] == export["commit"] and receipt["tree"] == export["tree"]
                 and type(receipt["source_date_epoch"]) is int
                 and receipt["source_date_epoch"] == export["source_date_epoch"])
        tools = [t for t in inputs.build_inputs.data["build_tools"] if t["name"] == "setuptools"]
        _require(len(tools) == 1)
        tool = tools[0]
        _require(receipt["backend_sha256"] == tool["sha256"])
        _require(type(wheel["filename"]) is str and type(wheel["size"]) is int and _is_hex64(wheel["sha256"]))
        _require(sorted(entry.name for entry in build.iterdir()) == [wheel["filename"]])
        version, dependencies = engine_project_wheel._read_project(tree, tool)
        _require(wheel["filename"] == f"ciaobot-{version}-py3-none-any.whl")
        snapshot = links / wheel["filename"]
        engine_artifact._snapshot(build / wheel["filename"], snapshot, wheel["sha256"], wheel["size"])
        data = _read(snapshot, engine_project_wheel.MAX_WHEEL_BYTES)
        engine_project_wheel._check_wheel(data, version, dependencies, export["static_inventory"],
                                          export["source_date_epoch"])
    except (engine_project_wheel.ProjectWheelError, engine_artifact.ArtifactError) as exc:
        raise AssembleError() from exc
    row = {"name": "ciaobot", "version": version, "origin": "project", "filename": wheel["filename"],
           "size": wheel["size"], "sha256": wheel["sha256"]}
    return {"version": version, "dependencies": dependencies, "export": export, "build": receipt, "row": row,
            "tool": tool, "license": _read(tree / "LICENSE", engine_project_wheel.MAX_PROJECT_FILE_BYTES)}


def copy_prefix(source: Path, target: Path, expected: list[dict[str, Any]]) -> None:
    """Copy the B3b prefix (the B3b output stays reusable) and prove the copy equals its receipt."""
    shutil.copytree(source, target, symlinks=True)
    _require(engine_install.inventory(target) == expected)
    real_root = os.path.realpath(target)
    for rel, kind, path in engine_install._scan(target):
        if kind != "link":
            continue
        link = os.readlink(path)
        _require(not os.path.isabs(link))
        resolved = os.path.realpath(path)
        _require(resolved == real_root or resolved.startswith(real_root + os.sep))


def project_pip_argv(python: Path, links: Path, requirements: Path) -> list[str]:
    return [os.fspath(python), "-I", "-B", "-m", "pip", "install", "--isolated", "--no-index", "--no-deps",
            "--find-links", os.fspath(links), "--only-binary", ":all:", "--require-hashes", "--no-compile",
            "--no-warn-script-location", "--disable-pip-version-check", "--no-input", "-r",
            os.fspath(requirements)]


def closure_roots(version: str) -> bytes:
    return f'[project]\ndependencies = ["ciaobot=={version}"]\n'.encode()


def _record_hash(site: Path, dist_dir: str, rel_to_site: str) -> tuple[str, str]:
    """The (urlsafe-b64 sha256, size) a dist-info RECORD lists for a file under site-packages."""
    raw = _read(site / dist_dir / "RECORD", engine_install.MAX_RECORD).decode("utf-8")
    for line in raw.splitlines():
        fields = next(csv.reader([line]), [])
        if len(fields) == 3 and fields[0] == rel_to_site:
            prefix = "sha256="
            _require(fields[1].startswith(prefix))
            return fields[1][len(prefix):], fields[2]
    raise AssembleError()


def _file_matches(path: Path, size: int, sha256: str) -> bool:
    try:
        return engine_install._file_digest(path) == (size, sha256)
    except OSError:
        return False


def check_licences_and_cafile(engine: Path, inputs: Any) -> dict[str, Any]:
    """The CPython and http-ece licence texts and the CA file, each bound to its pin or RECORD row."""
    import base64

    pin_license = inputs.pin["license"]
    runtime = engine / STDLIB_REL / "LICENSE.txt"
    _require(_file_matches(runtime, pin_license["size"], pin_license["sha256"]))
    derived = inputs.build_inputs.data["derived_wheels"]
    _require(len(derived) == 1 and derived[0]["name"] == "http-ece")
    text = derived[0]["license"]["text_file"]
    http_rel = f"{SITE_REL}/http_ece-{derived[0]['version']}.dist-info/licenses/LICENSE"
    _require(_file_matches(engine / http_rel, text["size"], text["sha256"]))
    site = engine / SITE_REL
    certifi = sorted(entry for entry in os.listdir(site) if entry.startswith("certifi-")
                     and entry.endswith(".dist-info"))
    _require(len(certifi) == 1)
    cafile = engine / CAFILE_REL
    size, sha = engine_install._file_digest(cafile)
    digest, recorded_size = _record_hash(site, certifi[0], "certifi/cacert.pem")
    encoded = base64.urlsafe_b64encode(bytes.fromhex(sha)).rstrip(b"=").decode()
    _require(digest == encoded and recorded_size == str(size))
    version = certifi[0][len("certifi-"):-len(".dist-info")]
    return {
        "licenses": [
            {"component": "cpython-runtime", "path": f"{STDLIB_REL}/LICENSE.txt", "size": pin_license["size"],
             "sha256": pin_license["sha256"]},
            {"component": "http-ece", "path": http_rel, "size": text["size"], "sha256": text["sha256"]},
        ],
        "tls": {"policy": "certifi-only", "cafile": CAFILE_REL, "size": size, "sha256": sha,
                "certifi_version": version},
    }


def ciaobot_licence(engine: Path, version: str, expected: bytes) -> dict[str, Any]:
    rel = f"{SITE_REL}/ciaobot-{version}.dist-info/licenses/LICENSE"
    size, sha = engine_install._file_digest(engine / rel)
    _require(size == len(expected) and sha == _sha(expected))
    return {"component": "ciaobot", "path": rel, "size": size, "sha256": sha}


def console_scripts(site: Path, version: str) -> list[str]:
    raw = _read(site / f"ciaobot-{version}.dist-info" / "entry_points.txt", engine_install.MAX_METADATA)
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(raw.decode("utf-8"))
        names = sorted(parser["console_scripts"])
    except (UnicodeError, configparser.Error, KeyError) as exc:
        raise AssembleError() from exc
    _require(bool(names))
    return names


# ---------------------------------------------------------------- tree rules


def _bytecode_entry(rel: str) -> bool:
    return rel.endswith(".pyc") or "__pycache__" in rel.split("/")


def _inventory_by_path(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["path"]: row for row in rows}


def _removal_row(row: dict[str, Any], reason: str) -> dict[str, Any]:
    if "symlink" in row:
        return {"path": row["path"], "symlink": row["symlink"], "reason": reason}
    return {"path": row["path"], "size": row["size"], "sha256": row["sha256"], "reason": reason}


def remove_bytecode(engine: Path, baseline: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Delete every runtime .pyc / __pycache__ (M2); bytecode anywhere else refuses."""
    by_path = _inventory_by_path(baseline)
    removed: list[dict[str, Any]] = []
    directories: list[str] = []
    entries = [(rel, kind, path) for rel, kind, path in engine_install._scan(engine) if _bytecode_entry(rel)]
    for rel, kind, _path in entries:
        _require(rel.startswith(STDLIB_REL + "/") and not (rel + "/").startswith(SITE_REL + "/"))
        if kind == "dir":
            _require(rel.rsplit("/", 1)[-1] == "__pycache__")
    for rel, kind, path in entries:
        if kind == "dir":
            directories.append(rel)
            continue
        _require(rel in by_path)
        removed.append(_removal_row(by_path[rel], "bytecode"))
        path.unlink()
    for rel in sorted(directories, key=lambda value: (-value.count("/"), value)):
        (engine / rel).rmdir()
    removed.sort(key=lambda row: row["path"])
    return removed, sorted(directories)


def install_launcher(engine: Path) -> dict[str, Any]:
    data = _read(LAUNCHER_TEMPLATE, MAX_LAUNCHER_BYTES)
    target = engine / LAUNCHER_REL
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
    try:
        os.fchmod(fd, 0o755)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
    finally:
        os.close(fd)
    return {"path": LAUNCHER_REL, "size": len(data), "sha256": _sha(data)}


def remove_pip(engine: Path, baseline: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Delete pip, its dist-info, its console scripts and ensurepip (M3). The last mutation."""
    by_path = _inventory_by_path(baseline)
    for tree in PIP_TREES:
        info = os.lstat(engine / tree)
        _require(stat.S_ISDIR(info.st_mode))
    _require(PIP_BUNDLED_WHEEL in by_path and "sha256" in by_path[PIP_BUNDLED_WHEEL])
    site = engine / SITE_REL
    record_rows = {os.path.normpath(os.path.join(site, next(csv.reader([line]), [""])[0]))
                   for line in _read(site / f"pip-{PIP_VERSION}.dist-info" / "RECORD",
                                     engine_install.MAX_RECORD).decode("utf-8").splitlines()}
    for script in PIP_SCRIPTS:
        _require(script in by_path and "sha256" in by_path[script])
        _require(os.path.normpath(os.fspath(engine / script)) in record_rows)
        _require(stat.S_ISREG(os.lstat(engine / script).st_mode))
    removed: list[dict[str, Any]] = []
    directories: list[str] = []
    for rel, kind, _path in engine_install._scan(engine):
        if not any(rel == tree or rel.startswith(tree + "/") for tree in PIP_TREES):
            continue
        if kind == "dir":
            directories.append(rel)
        else:
            _require(rel in by_path)
            removed.append(_removal_row(by_path[rel], "pip"))
    for script in PIP_SCRIPTS:
        removed.append(_removal_row(by_path[script], "pip"))
    for tree in PIP_TREES:
        shutil.rmtree(engine / tree)
    for script in PIP_SCRIPTS:
        (engine / script).unlink()
    removed.sort(key=lambda row: row["path"])
    return removed, sorted(directories)


def assert_no_bytecode(engine: Path) -> None:
    for rel, _kind, _path in engine_install._scan(engine):
        _require(not _bytecode_entry(rel))


def assert_no_installer(engine: Path) -> None:
    """No wheel, no pip entry point, no pip distribution and no ensurepip; exact components only."""
    for rel, _kind, _path in engine_install._scan(engine):
        parts = rel.split("/")
        _require(not rel.endswith(".whl"))
        if parts[0] == "bin" and len(parts) == 2:
            _require(parts[1] != "pip" and not parts[1].startswith("pip3"))
        _require(rel != f"{SITE_REL}/pip" and not rel.startswith(f"{SITE_REL}/pip/"))
        if rel.startswith(SITE_REL + "/") and len(parts) == len(SITE_REL.split("/")) + 1:
            name = parts[-1]
            _require(not (name.startswith("pip-") and name.endswith(".dist-info")))
        _require(rel != f"{STDLIB_REL}/ensurepip" and not rel.startswith(f"{STDLIB_REL}/ensurepip/"))


def build_needles(*paths: Path) -> list[Path]:
    """Every input and build path, as given and fully resolved, deduplicated in order."""
    found: dict[str, Path] = {}
    for path in paths:
        for variant in (Path(os.path.abspath(path)), Path(os.path.realpath(path))):
            found.setdefault(os.fspath(variant), variant)
    return list(found.values())


def refuse_needles(engine: Path, needles: list[Path]) -> None:
    """No file content and no symlink target in the whole tree names a build or input path."""
    encoded = [os.fsencode(os.fspath(needle)) for needle in needles]
    for _rel, kind, path in engine_install._scan(engine):
        if kind == "link":
            target = os.fsencode(os.readlink(path))
            _require(all(needle not in target for needle in encoded))
        elif kind == "file":
            for needle in encoded:
                _require(not engine_install._contains(path, needle))


# ---------------------------------------------------------------------- Mach-O


def _cstring(body: bytes, offset: int) -> str:
    raw = body[offset:]
    terminator = raw.find(b"\0")
    _require(terminator > 0)
    try:
        return raw[:terminator].decode("utf-8")
    except UnicodeError as exc:
        raise AssembleError() from exc


def _format_minos(value: int) -> str:
    major, minor, patch = value >> 16, (value >> 8) & 0xFF, value & 0xFF
    return f"{major}.{minor}" + (f".{patch}" if patch else "")


def _plausible_cputype(cputype: int) -> bool:
    """A Mach-O cputype: a small id with at most the 64-bit ABI flags; a Java constant pool is not."""
    return (cputype & 0x00FFFF00) == 0 and (cputype >> 24) in (0, 1, 2)


def _slice_bounds(handle: Any, size: int) -> tuple[int, int] | None:
    """(offset, length) of the arm64 slice, or None when the file is not Mach-O at all.

    A Mach-O of any other architecture raises AssembleError, including a thin 64-bit file whose
    cputype is read from the header and compared directly.
    """
    handle.seek(0)
    magic = handle.read(8)
    if len(magic) < 8:
        return None
    head = magic[:4]
    if head == THIN_MAGIC_64:
        if struct.unpack("<i", magic[4:8])[0] != CPU_TYPE_ARM64:
            raise AssembleError()
        return 0, size
    if head in OTHER_MACHO_MAGICS:
        raise AssembleError()
    if head not in (FAT_MAGIC, FAT_MAGIC_64):
        return None
    count = struct.unpack(">I", magic[4:8])[0]
    if count >= JAVA_CLASS_FAT_COUNT:
        # A Java class file shares the fat magic and reads as a huge arch count. Only a first entry that
        # is not a plausible cputype proves that; a plausible one is audited like any other fat file.
        first = handle.read(4)
        if len(first) < 4 or not _plausible_cputype(struct.unpack(">I", first)[0]):
            return None
        _require(count <= MAX_FAT_ARCHES)
        handle.seek(8)
    _require(count > 0)
    wide = head == FAT_MAGIC_64
    width = 32 if wide else 20
    table = handle.read(count * width)
    _require(len(table) == count * width)
    found: tuple[int, int] | None = None
    for index in range(count):
        chunk = table[index * width:(index + 1) * width]
        if wide:
            cputype, _sub, offset, length, _align, _res = struct.unpack(">iiQQII", chunk)
        else:
            cputype, _sub, offset, length, _align = struct.unpack(">iiIII", chunk)
        if cputype == CPU_TYPE_ARM64:
            _require(found is None and offset >= 8 + count * width and length >= 32 and offset + length <= size)
            found = (offset, length)
    _require(found is not None)
    return found


def _under(engine_real: str, candidate: str) -> bool:
    real = os.path.realpath(candidate)
    return (real == engine_real or real.startswith(engine_real + os.sep)) and os.path.isfile(real)


def _system(name: str) -> bool:
    return (name.startswith(SYSTEM_PREFIXES) or name.rstrip("/") in SYSTEM_PREFIXES_BARE) and ".." not in name.split("/")


def _resolve_base(name: str, loader_dir: str, executable_dir: str) -> str | None:
    for variable, base in (("@loader_path", loader_dir), ("@executable_path", executable_dir)):
        if name == variable or name.startswith(variable + "/"):
            return base + name[len(variable):]
    return None


def _audit_slice(engine_real: str, path: Path, filetype: str, blob: bytes) -> dict[str, Any]:
    magic, cputype, _sub, kind, ncmds, sizeofcmds, _flags, _reserved = struct.unpack("<IiiIIIII", blob[:32])
    _require(magic == 0xFEEDFACF and cputype == CPU_TYPE_ARM64 and kind in FILETYPES)
    _require(FILETYPES[kind] == filetype)
    _require(ncmds <= MAX_MACHO_COMMANDS and 32 + sizeofcmds <= len(blob))
    commands = blob[32:32 + sizeofcmds]
    cursor = 0
    versions: list[int] = []
    dylibs: list[str] = []
    rpaths: list[str] = []
    for _ in range(ncmds):
        _require(cursor + 8 <= sizeofcmds)
        cmd, cmdsize = struct.unpack("<II", commands[cursor:cursor + 8])
        _require(cmdsize >= 8 and cmdsize % 8 == 0 and cursor + cmdsize <= sizeofcmds)
        body = commands[cursor:cursor + cmdsize]
        if cmd == LC_BUILD_VERSION:
            _require(cmdsize >= 24)
            platform_id, minos = struct.unpack("<II", body[8:16])
            _require(platform_id == 1)
            versions.append(minos)
        elif cmd == LC_VERSION_MIN_MACOSX:
            _require(cmdsize >= 16)
            versions.append(struct.unpack("<I", body[8:12])[0])
        elif cmd in LC_LOAD_DYLIBS:
            _require(cmdsize >= 24)
            offset = struct.unpack("<I", body[8:12])[0]
            _require(24 <= offset < cmdsize)
            dylibs.append(_cstring(body, offset))
        elif cmd == LC_DYLD_ENVIRONMENT:
            raise AssembleError()  # dyld would apply an environment variable the launcher cannot sanitise
        elif cmd == LC_RPATH:
            _require(cmdsize >= 12)
            offset = struct.unpack("<I", body[8:12])[0]
            _require(12 <= offset < cmdsize)
            rpaths.append(_cstring(body, offset))
        cursor += cmdsize
    _require(bool(versions))
    for minos in versions:
        _require((minos >> 16, (minos >> 8) & 0xFF) <= MAX_MINOS)
    loader_dir = os.fspath(path.parent)
    executable_dir = loader_dir if filetype == "execute" else os.path.join(engine_real, "bin")
    for rpath in rpaths:
        _require(_system(rpath) or _resolve_base(rpath, loader_dir, executable_dir) is not None)
    for name in dylibs:
        if _system(name):
            continue
        based = _resolve_base(name, loader_dir, executable_dir)
        if based is not None:
            _require(_under(engine_real, based))
            continue
        _require(name.startswith("@rpath/"))
        rest = name[len("@rpath/"):]
        # dyld takes the first rpath whose expansion exists, so that match must be inside the prefix.
        resolved = False
        for rpath in rpaths:
            if _system(rpath):
                resolved = True
                break
            base = _resolve_base(rpath, loader_dir, executable_dir)
            if base is not None and os.path.isfile(os.path.realpath(f"{base}/{rest}")):
                _require(_under(engine_real, f"{base}/{rest}"))
                resolved = True
                break
        _require(resolved)
    return {"filetype": filetype, "arch": "arm64", "minos": _format_minos(max(versions)), "dylibs": dylibs,
            "rpaths": rpaths}


def macho_audit(engine: Path) -> list[dict[str, Any]]:
    """Every Mach-O file by magic number (never extension): arm64, minos <= 13.0, loads inside the prefix."""
    engine_real = os.path.realpath(engine)
    found: list[dict[str, Any]] = []
    for rel, kind, path in engine_install._scan(engine):
        if kind != "file":
            continue
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            bounds = _slice_bounds(handle, size)
            if bounds is None:
                continue
            offset, length = bounds
            handle.seek(offset)
            header = handle.read(32)
            _require(len(header) == 32 and header[:4] == THIN_MAGIC_64)
            _magic, _cpu, _sub, kind_id, _ncmds, sizeofcmds, _flags, _res = struct.unpack("<IiiIIIII", header)
            _require(sizeofcmds <= MAX_SIZEOFCMDS and 32 + sizeofcmds <= length and kind_id in FILETYPES)
            blob = header + handle.read(sizeofcmds)
            _require(len(blob) == 32 + sizeofcmds)
        entry = _audit_slice(engine_real, path, FILETYPES[kind_id], blob)
        found.append({"path": rel, **entry})
    found.sort(key=lambda row: row["path"])
    _require(all(any(row["path"] == required for row in found) for required in REQUIRED_MACHO))
    return found


# ------------------------------------------------------------------ provenance


def build_provenance(inputs: Any, receipt: dict[str, Any], project: dict[str, Any], licences: dict[str, Any],
                     launcher: dict[str, Any], removed: list[dict[str, Any]], removed_dirs: list[dict[str, str]],
                     scripts: list[str]) -> dict[str, Any]:
    pin = inputs.pin
    export, build, tool = project["export"], project["build"], project["tool"]
    rows = sorted([*receipt["selected"], project["row"]], key=lambda row: row["name"])
    return {
        "schema": 1,
        "kind": "ciaobot-engine-provenance",
        "engine": {"name": "ciaobot", "version": project["version"]},
        "source": {"commit": export["commit"], "tree": export["tree"],
                   "source_date_epoch": export["source_date_epoch"],
                   "package_lock_sha256": export["package_lock_sha256"]},
        "inputs": {key: inputs.hashes[key] for key in sorted(inputs.hashes)},
        "runtime": {"upstream": pin["upstream"], "release": pin["release"], "python_version": pin["python_version"],
                    "target": pin["target"], "flavor": pin["flavor"], "filename": pin["filename"],
                    "size": pin["size"], "sha256": pin["sha256"], "minimum_macos": pin["minimum_macos"],
                    "openssl_version": receipt["openssl_version"]},
        "project_wheel": {"filename": build["wheel"]["filename"], "size": build["wheel"]["size"],
                          "sha256": build["wheel"]["sha256"],
                          "backend": {"name": "setuptools", "version": tool["version"], "sha256": tool["sha256"]}},
        "distributions": [{key: row[key] for key in ("name", "version", "origin", "filename", "size", "sha256")}
                          for row in rows],
        "derived": receipt["derived"],
        "pwa": {"static_inventory": export["static_inventory"]},
        "licenses": sorted(licences["licenses"], key=lambda entry: entry["component"]),
        "tls": licences["tls"],
        "bytecode": {"policy": "none"},
        "launcher": launcher,
        "runtime_modifications": {
            "statement": RUNTIME_STATEMENT,
            "removed": sorted(removed, key=lambda row: row["path"]),
            "removed_directories": sorted(removed_dirs, key=lambda row: row["path"]),
        },
        "generated_scripts_removed": {"third_party": list(receipt["removed_scripts"]), "ciaobot": scripts},
        "build_tools": {"pip": engine_install.RUNTIME_PIP[1], "setuptools": tool["version"]},
    }


def _write_new(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
    finally:
        os.close(fd)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


# -------------------------------------------------------------------- assemble


def _child(argv: list[str], writable: list[Path], home: Path, timeout: int) -> tuple[int, bytes]:
    return _run(engine_install._sandboxed(argv, writable), engine_install._child_env(home), home, timeout)


def assemble(wheelhouse: Path, install: Path, source: Path, project_build: Path, project_receipt: Path,
             destination: Path) -> dict[str, Any]:
    work: Path | None = None
    try:
        _platform_check()
        raw_destination = os.fspath(destination)
        _require(bool(raw_destination) and not any(p in (".", "..") for p in raw_destination.split(os.sep)))
        destination = Path(os.path.abspath(destination))
        parent = engine_artifact._safe_parent(destination.parent)
        destination = parent / destination.name
        _require(bool(destination.name) and not os.path.lexists(destination))
        house = engine_fetch._wheelhouse(wheelhouse)
        work = Path(tempfile.mkdtemp(prefix=".engine-assemble-", dir=parent))
        os.chmod(work, 0o700)
        home, links = work / "home", work / "links"
        home.mkdir(mode=0o700)
        links.mkdir(mode=0o700)
        packaging = engine_install._load_packaging(house)
        inputs = _inputs()
        pin = inputs.pin
        _require(".".join(pin["python_version"].split(".")[:2]) == PY_MM)
        receipt = load_install(install, inputs)
        bind_selected(receipt, inputs)
        project = load_project(source, project_build, project_receipt, inputs, links)
        version = project["version"]
        engine = work / "engine"
        copy_prefix(receipt["prefix"], engine, receipt["inventory"])
        python, site = engine / PYTHON_REL, engine / SITE_REL
        _require(python.is_file() and site.is_dir())
        check_interpreter(python, house, pin, work / "pristine")
        licences = check_licences_and_cafile(engine, inputs)
        baseline = engine_install._paths(engine)
        bin_baseline = {rel for rel in baseline if rel.startswith("bin/")}
        # Step 5: offline, hash-locked, dependency-free install of the ciaobot wheel.
        requirements = work / "requirements.txt"
        requirements.write_text(f"ciaobot=={version} --hash=sha256:{project['build']['wheel']['sha256']}\n",
                                encoding="utf-8")
        status, _ = _child(project_pip_argv(python, links, requirements), [engine, home], home,
                           engine_install.PIP_DEADLINE)
        _require(status == 0)
        # Step 6: the closure rooted at ciaobot, then pip check (pip is still installed).
        rows = [*receipt["selected"], project["row"]]
        installed = engine_install.validate_installed(
            site, rows, packaging, engine_dependencies.Target(pin), closure_roots(version))
        status, _ = _child([os.fspath(python), "-I", "-B", "-m", "pip", "check"], [home], home,
                           engine_install.CHECK_DEADLINE)
        _require(status == 0)
        engine_install.check_added(engine, baseline)
        licences["licenses"].append(ciaobot_licence(engine, version, project["license"]))
        # Step 7: console scripts never ship.
        declared = console_scripts(site, version)
        scripts = engine_install.scrub_scripts(engine, site, installed, [project["row"]], baseline)
        _require(scripts == declared)
        engine_install.refuse_build_path(engine, site, baseline, work)
        # Step 8: smoke, including the proof that a re-exec repeats the isolation flags.
        code = ("import importlib, json, ssl\nfor name in %r:\n    importlib.import_module(name)\n"
                "import ciao.supervise\nprint(ssl.OPENSSL_VERSION)\n"
                "print(json.dumps(ciao.supervise.default_child_argv()[1:3]))\n"
                % (engine_install.SMOKE_MODULES + PROJECT_SMOKE_MODULES,))
        status, output = _child([os.fspath(python), "-I", "-B", "-c", code], [home], home,
                                engine_install.SMOKE_DEADLINE)
        _require(status == 0)
        lines = output.decode("utf-8", "replace").splitlines()
        _require(len(lines) == 2 and lines[0] == receipt["openssl_version"] and lines[1] == '["-I", "-B"]')
        status, _ = _child([os.fspath(python), "-I", "-B", "-m", "ciao.release_manifest", "check-static"],
                           [home], home, engine_install.SMOKE_DEADLINE)
        _require(status == 0)
        engine_install.check_added(engine, baseline)
        # Steps 9-11: bytecode, launcher, and pip last.
        bytecode_rows, bytecode_dirs = remove_bytecode(engine, receipt["inventory"])
        launcher = install_launcher(engine)
        pip_rows, pip_dirs = remove_pip(engine, receipt["inventory"])
        removed = bytecode_rows + pip_rows
        removed_dirs = ([{"path": rel, "reason": "bytecode"} for rel in bytecode_dirs]
                        + [{"path": rel, "reason": "pip"} for rel in pip_dirs])
        # Step 12: read-only verification.
        before = engine_install.inventory(engine)
        status, output = _child([os.fspath(engine / LAUNCHER_REL), "--version"], [home], home,
                                engine_install.SMOKE_DEADLINE)
        _require(status == 0 and output == f"ciao {version}\n".encode())
        probe = ("import importlib.util, sys\nsys.exit(0 if importlib.util.find_spec('pip') is None "
                 "and importlib.util.find_spec('ensurepip') is None else 3)")
        status, _ = _child([os.fspath(python), "-I", "-B", "-c", probe], [home], home,
                           engine_install.SMOKE_DEADLINE)
        _require(status == 0)
        _require(engine_install.inventory(engine) == before)
        assert_no_bytecode(engine)
        assert_no_installer(engine)
        now_bin = {rel for rel in engine_install._paths(engine) if rel.startswith("bin/")}
        _require(now_bin == (bin_baseline - set(PIP_SCRIPTS)) | {LAUNCHER_REL})
        _require(set(engine_install._read_dists(site, packaging)) == {row["name"] for row in rows})
        refuse_needles(engine, build_needles(work, install, source, project_build, wheelhouse))
        # Step 13-14.
        macho = macho_audit(engine)
        provenance = build_provenance(inputs, receipt, project, licences, launcher, removed, removed_dirs, scripts)
        provenance_bytes = _json_bytes(provenance)
        _write_new(engine / PROVENANCE_NAME, provenance_bytes, 0o644)
        final = engine_install.inventory(engine)
        files = [row for row in final if "sha256" in row]
        result = {
            "schema": 1,
            "kind": "engine-assembly",
            "provenance_sha256": _sha(provenance_bytes),
            "launcher": launcher,
            "macho": macho,
            "totals": {"files": len(files), "bytes": sum(row["size"] for row in files),
                       "symlinks": len(final) - len(files), "macho_files": len(macho)},
            "largest": [{"path": row["path"], "size": row["size"]}
                        for row in sorted(files, key=lambda row: (-row["size"], row["path"]))[:15]],
            "inventory": final,
            "host_python": platform.python_version(),
        }
        _write_new(work / RECEIPT_NAME, _json_bytes(result), 0o644)
        shutil.rmtree(home)
        shutil.rmtree(links)
        os.unlink(requirements)
        # One rename; the exclusively owned 0700 parent is the no-replace guarantee (see engine_install).
        _require(not os.path.lexists(destination))
        os.rename(work, destination)
        work = None
        return result
    except AssembleError:
        raise
    except Exception as exc:
        raise AssembleError() from exc
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


# -------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print("engine assemble failed: usage", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(allow_abbrev=False, description="Assemble the relocatable engine prefix.")
    for flag in ("--wheelhouse", "--install", "--source", "--project-build", "--project-receipt", "--destination"):
        parser.add_argument(flag, required=True)
    args = parser.parse_args(argv)
    try:
        assemble(Path(args.wheelhouse), Path(args.install), Path(args.source), Path(args.project_build),
                 Path(args.project_receipt), Path(args.destination))
    except Exception:
        print("engine assemble failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
