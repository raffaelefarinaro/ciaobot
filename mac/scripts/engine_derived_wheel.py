#!/usr/bin/env python3
"""Offline, hash-pinned derivation and verification of the http-ece wheel.

Stdlib only. Nothing here reads the network, runs pip/uv, or trusts anything
that is not pinned in ``mac/engine-build-inputs.json``.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib
import importlib.util
import io
import json
import os
import platform
import re
import shutil
import stat
import sys
import sysconfig
import tarfile
import tempfile
import time
import tomllib
import zipfile
from email import message_from_bytes
from pathlib import Path
from typing import Any, NamedTuple

MAX_INPUT_BYTES = 65_536
REPO_ROOT = Path(__file__).resolve().parents[2]
INPUTS = REPO_ROOT / "mac" / "engine-build-inputs.json"
MAX_LICENCE_BYTES = 16_384
LICENCE_DIR = "mac/licenses"
MIN_EPOCH = 315_532_800  # 1980-01-01, the zip floor
MAX_SDIST_MEMBERS = 500
MAX_SDIST_BYTES = 4 * 1024 * 1024
MAX_BACKEND_MEMBERS = 5_000
MAX_BACKEND_BYTES = 32 * 1024 * 1024
MAX_RATIO = 200
MAX_WHEEL_BYTES = 8 * 1024 * 1024
MAX_WHEEL_MEMBERS = 500
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
WHEEL_LICENCE_NAME = "LICENSE"
_BACKENDS = {"setuptools.build_meta", "setuptools.build_meta:__legacy__"}
_HEX = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_URL = re.compile(r"https://files\.pythonhosted\.org/packages/[A-Za-z0-9._/+-]+")
_REQUIREMENT = re.compile(r"(setuptools|wheel)(?:\s*>=\s*([0-9]+(?:\.[0-9]+)*))?")
_BLOCKED_EVENTS = frozenset({
    "socket.connect", "socket.getaddrinfo", "socket.bind", "socket.gethostbyname",
    "socket.gethostbyaddr", "socket.getnameinfo", "urllib.Request", "subprocess.Popen",
    "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
    "socket.sendto", "socket.sendmsg",
})
_BLOCKED_PREFIXES = ("ctypes.",)
_GUARD = {"on": False, "violation": False, "installed": False}


class DerivedWheelError(Exception):
    """A deliberately non-descriptive refusal suitable for CLI output."""


def _require(condition: bool) -> None:
    if not condition:
        raise DerivedWheelError()


def _load_artifact_helper():
    path = Path(__file__).resolve().with_name("engine_artifact.py")
    spec = importlib.util.spec_from_file_location("engine_artifact", path)
    if spec is None or spec.loader is None:
        raise ImportError("engine artifact helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine_artifact = _load_artifact_helper()


def _platform_check() -> None:
    _require(sys.platform != "win32")
    engine_artifact._platform_check()


# ---------------------------------------------------------------- loader


class BuildInputs(NamedTuple):
    data: dict[str, Any]
    sha256: str


def _read_inputs_bytes() -> bytes:
    with INPUTS.open("rb") as handle:
        return handle.read(MAX_INPUT_BYTES + 1)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise DerivedWheelError()


def _is_int(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _hex(value: Any) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


def _text(value: Any) -> bool:
    return type(value) is str and bool(value) and all(32 <= ord(c) < 127 for c in value)


def _keys(value: Any, expected: set[str]) -> dict[str, Any]:
    _require(type(value) is dict and set(value) == expected)
    return value


def _relpath(value: Any) -> list[str]:
    _require(type(value) is str and bool(value) and not value.startswith("/") and "\\" not in value)
    _require(not any(ord(c) < 32 or ord(c) == 127 for c in value))
    parts = value.split("/")
    _require(all(p not in ("", ".", "..") for p in parts))
    _require(len(value.encode("utf-8")) <= 256)
    return parts


def _url(value: Any, filename: str) -> None:
    _require(type(value) is str and _URL.fullmatch(value) is not None)
    segments = value.split("/")[3:]
    _require(all(s not in ("", ".", "..") for s in segments) and segments[-1] == filename)


def _dist_stem(name: str, version: str) -> str:
    return f"{name.replace('-', '_')}-{version}"


def _wheel_filenames(name: str, version: str) -> set[str]:
    stem = _dist_stem(name, version)
    return {f"{stem}-py3-none-any.whl", f"{stem}-py2.py3-none-any.whl"}


def _validate_tool(tool: Any) -> None:
    _keys(tool, {"name", "version", "filename", "url", "size", "sha256"})
    _require(type(tool["name"]) is str and _NAME.fullmatch(tool["name"]) is not None)
    _require(type(tool["version"]) is str and _VERSION.fullmatch(tool["version"]) is not None)
    _require(tool["filename"] == f"{_dist_stem(tool['name'], tool['version'])}-py3-none-any.whl")
    _url(tool["url"], tool["filename"])
    _require(_is_int(tool["size"], 1, MAX_ARCHIVE_BYTES) and _hex(tool["sha256"]))


def _validate_derived(entry: Any) -> None:
    _keys(entry, {"name", "version", "reason", "sdist", "license", "build", "wheel"})
    name, version = entry["name"], entry["version"]
    _require(type(name) is str and _NAME.fullmatch(name) is not None)
    _require(type(version) is str and _VERSION.fullmatch(version) is not None)
    _require(_text(entry["reason"]))
    sdist = _keys(entry["sdist"], {"filename", "url", "size", "sha256"})
    _require(sdist["filename"] == f"{_dist_stem(name, version)}.tar.gz")
    _url(sdist["url"], sdist["filename"])
    _require(_is_int(sdist["size"], 1, MAX_ARCHIVE_BYTES) and _hex(sdist["sha256"]))
    licence = _keys(entry["license"], {"spdx", "declared_in", "text_file", "source_url", "note"})
    _require(_text(licence["spdx"]) and _text(licence["note"]))
    _require(_text(licence["source_url"]) and licence["source_url"].startswith("https://"))
    declared = _keys(licence["declared_in"], {"sdist_member", "sha256"})
    parts = _relpath(declared["sdist_member"])
    _require(len(parts) == 2 and parts[0] == _dist_stem(name, version) and parts[1] == "PKG-INFO")
    _require(_hex(declared["sha256"]))
    text_file = _keys(licence["text_file"], {"path", "size", "sha256"})
    text_parts = _relpath(text_file["path"])
    _require(len(text_parts) == 3 and "/".join(text_parts[:2]) == LICENCE_DIR)
    _require(_is_int(text_file["size"], 1, MAX_LICENCE_BYTES) and _hex(text_file["sha256"]))
    build = _keys(entry["build"], {"tool", "backend", "source_date_epoch", "umask"})
    _require(build["tool"] == "setuptools" and build["backend"] in _BACKENDS)
    _require(_is_int(build["source_date_epoch"], MIN_EPOCH, 4_102_444_800))
    _require(build["umask"] == "022")
    wheel = _keys(entry["wheel"], {"filename", "size", "sha256", "requires_dist", "members"})
    _require(wheel["filename"] in _wheel_filenames(name, version))
    _require(_is_int(wheel["size"], 1, MAX_WHEEL_BYTES) and _hex(wheel["sha256"]))
    requires = wheel["requires_dist"]
    _require(type(requires) is list and all(_text(r) for r in requires))
    members = wheel["members"]
    _require(type(members) is list and 0 < len(members) <= MAX_WHEEL_MEMBERS)
    previous = None
    for member in members:
        _keys(member, {"path", "size", "sha256"})
        _relpath(member["path"])
        _require(_is_int(member["size"], 0, MAX_WHEEL_BYTES) and _hex(member["sha256"]))
        _require(previous is None or member["path"] > previous)
        previous = member["path"]


def _validate_inputs(data: Any) -> dict[str, Any]:
    _keys(data, {"schema_version", "build_tools", "derived_wheels"})
    _require(type(data["schema_version"]) is int and data["schema_version"] == 1)
    tools, derived = data["build_tools"], data["derived_wheels"]
    _require(type(tools) is list and len(tools) >= 1 and type(derived) is list and len(derived) >= 1)
    for tool in tools:
        _validate_tool(tool)
    for entry in derived:
        _validate_derived(entry)
    _require(len({t["name"] for t in tools}) == len(tools))
    _require(len({d["name"] for d in derived}) == len(derived))
    _require(any(t["name"] == "setuptools" for t in tools))
    return data


def load_build_inputs() -> BuildInputs:
    try:
        raw = _read_inputs_bytes()
        _require(len(raw) <= MAX_INPUT_BYTES)
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant)
        return BuildInputs(_validate_inputs(data), hashlib.sha256(raw).hexdigest())
    except DerivedWheelError:
        raise
    except Exception as exc:
        raise DerivedWheelError() from exc


def _select(inputs: BuildInputs, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    _require(type(name) is str)
    entry = next((d for d in inputs.data["derived_wheels"] if d["name"] == name), None)
    _require(entry is not None)
    tool = next((t for t in inputs.data["build_tools"] if t["name"] == entry["build"]["tool"]), None)
    _require(tool is not None)
    return entry, tool


# ----------------------------------------------------------- wheel checks


def _zip_time(epoch: int) -> tuple[int, int, int, int, int, int]:
    moment = time.gmtime(epoch)
    return (moment.tm_year, moment.tm_mon, moment.tm_mday, moment.tm_hour, moment.tm_min,
            moment.tm_sec - moment.tm_sec % 2)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _read_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    with archive.open(info) as handle:
        data = handle.read(info.file_size + 1)
    _require(len(data) == info.file_size)
    return data


def _is_symlink_or_special(info: zipfile.ZipInfo) -> bool:
    kind = stat.S_IFMT(info.external_attr >> 16)
    return kind not in (0, stat.S_IFREG, stat.S_IFDIR)


def _check_wheel(
    data: bytes, entry: dict[str, Any], sdist_files: dict[str, bytes] | None, filename: str
) -> list[dict[str, Any]]:
    """Validate wheel bytes against the pin's rules; return sorted member records."""
    name, version = entry["name"], entry["version"]
    package, stem = name.replace("-", "_"), _dist_stem(name, version)
    dist_info = f"{stem}.dist-info"
    tag_part = filename[len(stem) + 1:-len("-none-any.whl")]
    expected_tags = {f"{part}-none-any" for part in tag_part.split(".")}
    expected_time = _zip_time(entry["build"]["source_date_epoch"])
    licence_path = f"{dist_info}/licenses/{WHEEL_LICENCE_NAME}"
    fixed = {f"{dist_info}/{n}" for n in ("METADATA", "WHEEL", "RECORD", "top_level.txt")} | {licence_path}
    contents: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        _require(0 < len(infos) <= MAX_WHEEL_MEMBERS)
        _require(sum(i.file_size for i in infos) <= MAX_WHEEL_BYTES)
        for info in infos:
            path = info.filename
            _relpath(path)
            _require(path not in contents and not info.is_dir())
            _require(not _is_symlink_or_special(info) and info.date_time == expected_time)
            blob = _read_zip_member(archive, info)
            if path.startswith(package + "/") and path.endswith(".py"):
                _require(sdist_files is None or sdist_files.get(f"{package}/{path[len(package) + 1:]}") == blob)
            elif path == licence_path:
                _require(hashlib.sha256(blob).hexdigest() == entry["license"]["text_file"]["sha256"])
            else:
                _require(path in fixed)
            contents[path] = blob
    _require(len({p.casefold() for p in contents}) == len(contents))
    _require(all(p in contents for p in fixed))
    meta = message_from_bytes(contents[f"{dist_info}/METADATA"])
    _require(meta.get("Name") is not None and meta["Name"].replace("_", "-").lower() == name)
    _require(meta.get("Version") == version)
    _require(sorted(meta.get_all("Requires-Dist") or []) == sorted(entry["wheel"]["requires_dist"]))
    wheel_meta = message_from_bytes(contents[f"{dist_info}/WHEEL"])
    tags = wheel_meta.get_all("Tag") or []
    _require(wheel_meta.get("Root-Is-Purelib") == "true" and set(tags) == expected_tags and len(tags) == len(expected_tags))
    rows = list(csv.reader(contents[f"{dist_info}/RECORD"].decode("utf-8").splitlines()))
    _require(all(len(r) == 3 for r in rows) and len({r[0] for r in rows}) == len(rows))
    _require({r[0] for r in rows} == set(contents))
    for path_, digest, size in rows:
        if path_ == f"{dist_info}/RECORD":
            _require(digest == "" and size == "")
        else:
            _require(digest == f"sha256={_b64(contents[path_])}" and size == str(len(contents[path_])))
    return [
        {"path": p, "size": len(contents[p]), "sha256": hashlib.sha256(contents[p]).hexdigest()}
        for p in sorted(contents)
    ]


def _read_regular(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _require(stat.S_ISREG(os.fstat(fd).st_mode))
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
    finally:
        os.close(fd)
    _require(len(data) <= limit)
    return data


def verify_derived_wheel(name: str, wheel: Path) -> None:
    try:
        _platform_check()
        inputs = load_build_inputs()
        entry, _tool = _select(inputs, name)
        pin = entry["wheel"]
        wheel = Path(wheel)
        _require(wheel.name == pin["filename"])
        data = _read_regular(wheel, pin["size"])
        _require(len(data) == pin["size"] and hashlib.sha256(data).hexdigest() == pin["sha256"])
        _require(_check_wheel(data, entry, None, pin["filename"]) == pin["members"])
    except DerivedWheelError:
        raise
    except Exception as exc:
        raise DerivedWheelError() from exc


# ------------------------------------------------------------- extraction


def _components(value: str) -> list[str]:
    parts = _relpath(value)
    _require(all(len(p.encode("utf-8")) <= 255 for p in parts))
    return parts


def _write_file(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)


def _extract_sdist(archive: Path, root_name: str, epoch: int, target: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0
    with tarfile.open(archive, "r:gz") as tar:
        infos: list[tarfile.TarInfo] = []
        for member in tar:
            infos.append(member)
            _require(len(infos) <= MAX_SDIST_MEMBERS)
            _require(member.isreg() or member.isdir())
            name = member.name
            _components(name)
            _require(name == root_name or name.startswith(root_name + "/"))
            _require(name.casefold() not in seen)
            seen.add(name.casefold())
            if member.isreg():
                total += member.size
                _require(total <= MAX_SDIST_BYTES)
        target.mkdir(mode=0o700)
        for member in infos:
            rel = member.name[len(root_name) + 1:]
            path = target / rel if rel else target
            if member.isdir():
                if rel:
                    path.mkdir(parents=True, exist_ok=True)
                continue
            _require(bool(rel))
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = tar.extractfile(member)
            _require(handle is not None)
            data = handle.read(member.size + 1)
            _require(len(data) == member.size)
            _write_file(path, data, 0o755 if member.mode & 0o100 else 0o644)
            os.utime(path, (epoch, epoch))
            files[rel] = data
    for directory in [target, *(p for p in target.rglob("*") if p.is_dir())]:
        os.chmod(directory, 0o755)
        os.utime(directory, (epoch, epoch))
    return files


def _extract_backend(wheel: Path, tool: dict[str, Any], target: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        infos = archive.infolist()
        _require(0 < len(infos) <= MAX_BACKEND_MEMBERS)
        uncompressed = sum(i.file_size for i in infos)
        _require(uncompressed <= MAX_BACKEND_BYTES)
        _require(uncompressed <= MAX_RATIO * max(1, sum(i.compress_size for i in infos)))
        target.mkdir(mode=0o700)
        seen: set[str] = set()
        metadata: list[bytes] = []
        for info in infos:
            name = info.filename.rstrip("/") if info.is_dir() else info.filename
            _components(name)
            _require(name.casefold() not in seen and not _is_symlink_or_special(info))
            seen.add(name.casefold())
            path = target / name
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            blob = _read_zip_member(archive, info)
            _write_file(path, blob, 0o644)
            if re.fullmatch(r"[^/]+\.dist-info/METADATA", name):
                metadata.append(blob)
    _require(len(metadata) == 1)
    meta = message_from_bytes(metadata[0])
    _require(meta.get("Name") is not None and meta["Name"].replace("_", "-").lower() == tool["name"])
    _require(meta.get("Version") == tool["version"])


def _requirement_ok(requirement: str, tool: dict[str, Any]) -> bool:
    match = _REQUIREMENT.fullmatch(requirement.strip().lower())
    if match is None:
        return False
    if match.group(1) == "setuptools" and match.group(2) is not None:
        wanted = tuple(int(p) for p in match.group(2).split("."))
        return tuple(int(p) for p in tool["version"].split(".")) >= wanted
    return True


def _detect_backend(sdist_root: Path, tool: dict[str, Any]) -> str:
    path = sdist_root / "pyproject.toml"
    if not path.is_file():
        return "setuptools.build_meta:__legacy__"
    declaration = tomllib.loads(_read_regular(path, 65_536).decode("utf-8"))
    section = declaration.get("build-system")
    if section is None:
        return "setuptools.build_meta:__legacy__"
    _require(type(section) is dict)
    requires = section.get("requires", [])
    _require(type(requires) is list and all(type(r) is str and _requirement_ok(r, tool) for r in requires))
    backend = section.get("build-backend")
    if backend is None:
        return "setuptools.build_meta:__legacy__"
    _require(backend == "setuptools.build_meta")
    return backend


# ---------------------------------------------------------------- build


def _audit(event: str, _args: tuple) -> None:
    # A tripwire, not a sandbox: it catches a build that strays, it does not contain a
    # hostile one. Containment rests on the hash-pinned inputs, on verify_derived_wheel
    # against the pinned wheel, and on sandbox-exec (network and write deny) in the real build.
    if _GUARD["on"] and (event in _BLOCKED_EVENTS or event.startswith(_BLOCKED_PREFIXES)):
        _GUARD["violation"] = True
        raise RuntimeError("blocked")


def _install_guard() -> None:
    if not _GUARD["installed"]:
        sys.addaudithook(_audit)
        _GUARD["installed"] = True


def _stdlib_roots() -> list[str]:
    roots = {sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib")}
    paths = [p for p in sys.path if p and any(p == r or p.startswith(r + os.sep) for r in roots)
             and "site-packages" not in p and "dist-packages" not in p]
    return list(dict.fromkeys([*paths, *roots]))


_PURGED_PREFIXES = ("setuptools", "pkg_resources", "_distutils_hack", "distutils", "wheel")


def _is_purged(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in _PURGED_PREFIXES)


def _private_dirs(scratch: Path) -> dict[str, str]:
    """Private HOME, TMPDIR and XDG dirs, so no per-user config (~/.pydistutils.cfg) is read."""
    dirs = {"HOME": "home", "TMPDIR": "tmp", "XDG_CONFIG_HOME": "xdg-config",
            "XDG_CACHE_HOME": "xdg-cache", "XDG_DATA_HOME": "xdg-data"}
    for leaf in dirs.values():
        (scratch / leaf).mkdir(mode=0o700)
    return {key: str(scratch / leaf) for key, leaf in dirs.items()}


def _run_backend(
    backend: str, backend_dir: Path, sdist_root: Path, out_dir: Path, epoch: int, tool: dict[str, Any],
    scratch: Path,
) -> None:
    private = _private_dirs(scratch)
    stdlib = _stdlib_roots()
    roots = [str(backend_dir), str(sdist_root), *stdlib, str(Path(__file__).resolve().parent)]
    saved_path, saved_cwd = list(sys.path), os.getcwd()
    saved_env, saved_umask = dict(os.environ), os.umask(0o022)
    saved_bytecode, saved_tempdir = sys.dont_write_bytecode, tempfile.tempdir
    saved_modules = {m: sys.modules.pop(m) for m in [m for m in sys.modules if _is_purged(m)]}
    before = set(sys.modules)
    _install_guard()
    _GUARD["violation"] = False
    try:
        os.environ.clear()
        os.environ.update({"SOURCE_DATE_EPOCH": str(epoch), "LC_ALL": "C", "TZ": "UTC", **private})
        tempfile.tempdir = private["TMPDIR"]
        time.tzset()
        sys.dont_write_bytecode = True
        sys.path[:] = [str(backend_dir), str(sdist_root), *stdlib]
        os.chdir(sdist_root)
        _GUARD["on"] = True
        try:
            module_name, _, attribute = backend.partition(":")
            module = importlib.import_module(module_name)
            driver = getattr(module, attribute) if attribute else module
            getter = getattr(driver, "get_requires_for_build_wheel", None)
            if getter is not None:
                requirements = getter()
                _require(type(requirements) is list and all(
                    type(r) is str and _requirement_ok(r, tool) for r in requirements))
            driver.build_wheel(str(out_dir))
        finally:
            _GUARD["on"] = False
        _require(not _GUARD["violation"])
        for loaded in set(sys.modules) - before:
            origin = getattr(sys.modules[loaded], "__file__", None)
            _require(origin is None or any(
                os.path.abspath(origin) == r or os.path.abspath(origin).startswith(r + os.sep) for r in roots))
    finally:
        for loaded in set(sys.modules) - before:
            sys.modules.pop(loaded, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_bytecode
        tempfile.tempdir = saved_tempdir
        os.chdir(saved_cwd)
        os.umask(saved_umask)
        os.environ.clear()
        os.environ.update(saved_env)
        time.tzset()


def _committed_licence(entry: dict[str, Any]) -> bytes:
    pin = entry["license"]["text_file"]
    data = _read_regular(REPO_ROOT / pin["path"], MAX_LICENCE_BYTES)
    _require(len(data) == pin["size"] and hashlib.sha256(data).hexdigest() == pin["sha256"])
    return data


def _check_declared_licence(entry: dict[str, Any], files: dict[str, bytes], root_name: str) -> None:
    """The sdist ships no licence file; its PKG-INFO must declare the pinned SPDX id."""
    declared = entry["license"]["declared_in"]
    blob = files.get(declared["sdist_member"][len(root_name) + 1:])
    _require(blob is not None and hashlib.sha256(blob).hexdigest() == declared["sha256"])
    meta = message_from_bytes(blob)
    values = meta.get_all("License") or []
    _require(values == [entry["license"]["spdx"]])


def build_derived_wheel(name: str, sdist: Path, backend_wheel: Path, destination: Path) -> dict[str, Any]:
    work: Path | None = None
    try:
        _platform_check()
        _require(isinstance(destination, Path) and bool(destination.name))
        parent = engine_artifact._safe_parent(Path(os.path.abspath(destination)).parent)
        target = parent / destination.name
        _require(not os.path.lexists(target))
        inputs = load_build_inputs()
        entry, tool = _select(inputs, name)
        work = Path(tempfile.mkdtemp(prefix=".derived-build-", dir=parent))
        os.chmod(work, 0o700)
        sdist_pin, epoch = entry["sdist"], entry["build"]["source_date_epoch"]
        sdist_copy, backend_copy = work / sdist_pin["filename"], work / tool["filename"]
        engine_artifact._snapshot(Path(sdist), sdist_copy, sdist_pin["sha256"], sdist_pin["size"])
        engine_artifact._snapshot(Path(backend_wheel), backend_copy, tool["sha256"], tool["size"])
        root_name = _dist_stem(entry["name"], entry["version"])
        files = _extract_sdist(sdist_copy, root_name, epoch, work / "sdist")
        _check_declared_licence(entry, files, root_name)
        licence = _committed_licence(entry)
        sdist_root = work / "sdist"
        _require(not any(p.name.upper().startswith(("LICENSE", "LICENCE", "COPYING")) for p in sdist_root.iterdir()))
        _write_file(sdist_root / WHEEL_LICENCE_NAME, licence, 0o644)
        os.utime(sdist_root / WHEEL_LICENCE_NAME, (epoch, epoch))
        os.utime(sdist_root, (epoch, epoch))
        _extract_backend(backend_copy, tool, work / "backend")
        _require(_detect_backend(sdist_root, tool) == entry["build"]["backend"])
        out_dir = work / "out"
        out_dir.mkdir(mode=0o700)
        scratch = work / "scratch"
        scratch.mkdir(mode=0o700)
        _run_backend(entry["build"]["backend"], work / "backend", sdist_root, out_dir, epoch, tool, scratch)
        produced = sorted(out_dir.iterdir())
        _require(len(produced) == 1 and produced[0].name in _wheel_filenames(entry["name"], entry["version"]))
        data = _read_regular(produced[0], MAX_WHEEL_BYTES)
        members = _check_wheel(data, entry, files, produced[0].name)
        os.chmod(produced[0], 0o644)
        # Defence in depth: the bytes validated above are the bytes that get published.
        _require(hashlib.sha256(_read_regular(produced[0], MAX_WHEEL_BYTES)).digest()
                 == hashlib.sha256(data).digest())
        os.rename(out_dir, target)
        receipt = {
            "name": entry["name"],
            "version": entry["version"],
            "wheel": {
                "filename": produced[0].name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "members": members,
            },
            "sdist_sha256": sdist_pin["sha256"],
            "backend_sha256": tool["sha256"],
            "source_date_epoch": epoch,
            "python": platform.python_version(),
            "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
    except DerivedWheelError:
        raise
    except Exception as exc:
        raise DerivedWheelError() from exc
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    return receipt


# ------------------------------------------------------------------ CLI


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print("derived wheel failed", file=sys.stderr)
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(allow_abbrev=False, description="Build or verify the pinned derived wheel offline.")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", allow_abbrev=False)
    for flag in ("--name", "--sdist", "--backend-wheel", "--destination"):
        build.add_argument(flag, required=True)
    verify = commands.add_parser("verify", allow_abbrev=False)
    for flag in ("--name", "--wheel"):
        verify.add_argument(flag, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _platform_check()
        if args.command == "build":
            if not args.destination or any(p in (".", "..") for p in args.destination.split("/")):
                raise DerivedWheelError()
            receipt = build_derived_wheel(
                args.name, Path(args.sdist), Path(args.backend_wheel), Path(args.destination))
            print(json.dumps(receipt, sort_keys=True, indent=2))
        else:
            verify_derived_wheel(args.name, Path(args.wheel))
    except Exception:
        print("derived wheel failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
