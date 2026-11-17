#!/usr/bin/env python3
"""Pinned artifact acquisition: the only network boundary of the engine build.

Every byte fetched here is requested from a URL that a committed pin names
(`uv.lock`, `mac/engine-runtime.json`, `mac/engine-build-inputs.json`) and is
size- and SHA-256-checked against that same pin before it is published. The
B2a audit report is only a selection: each entry must equal a `uv.lock` wheel
record exactly, so a tampered report can ask for nothing the lock does not pin.
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
import stat
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / "uv.lock"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
MAX_INPUT_BYTES = 16 * 1024 * 1024
RECEIPTS = {"bootstrap": "bootstrap-receipt.json", "closure": "closure-receipt.json"}
CURL = "/usr/bin/curl"
ARTIFACT_HOST = "files.pythonhosted.org"
RUNTIME_PREFIX = "https://github.com/astral-sh/python-build-standalone/releases/download/"
_HEX = re.compile(r"[0-9a-f]{64}")
_URL_FORBIDDEN = re.compile(r"[\x00-\x20\x7f\\;]")
_ENTRY_KEYS = {"name", "version", "origin", "filename", "url", "size", "sha256", "tags"}
_INPUT_KEYS = {"pyproject_sha256", "lock_sha256", "runtime_pin_sha256", "build_inputs_sha256"}
_REPORT_KEYS = {"schema", "kind", "installable", "target", "inputs", "selected", "blockers"}


class FetchError(Exception):
    """A deliberately non-descriptive refusal suitable for CLI output."""


def _require(condition: bool) -> None:
    if not condition:
        raise FetchError("invalid fetch")


def _load_sibling(name: str) -> Any:
    path = Path(__file__).resolve().with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("sibling helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Private-helper coupling: this module calls underscore-prefixed names of these
# siblings (engine_artifact._snapshot/_safe_parent/_platform_check, ...); renaming
# them breaks the fetch step and the tests here will fail at import or call time.
engine_artifact = _load_sibling("engine_artifact")
engine_runtime = _load_sibling("engine_runtime")
engine_derived_wheel = _load_sibling("engine_derived_wheel")


# ------------------------------------------------------------------ inputs


def _read_bounded(path: Path, limit: int = MAX_INPUT_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        _require(stat.S_ISREG(os.fstat(fd).st_mode))
        raw = b""
        while len(raw) <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(fd)
    _require(len(raw) <= limit)
    return raw


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_hex(value: Any) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


def _is_size(value: Any) -> bool:
    return type(value) is int and 0 < value <= engine_artifact.MAX_COMPRESSED


def _canonical_pythonhosted(url: Any) -> str:
    """Return the URL's file name if it is a canonical pythonhosted packages URL."""
    _require(type(url) is str and _URL_FORBIDDEN.search(url) is None)
    parsed = urllib.parse.urlsplit(url)
    _require(parsed.geturl() == url and parsed.scheme == "https" and parsed.netloc == ARTIFACT_HOST)
    _require(not (parsed.query or parsed.fragment))
    _require(parsed.path.startswith("/packages/"))
    parts = parsed.path.split("/")
    _require(all(part not in ("", ".", "..") for part in parts[1:]))
    name = urllib.parse.unquote(parts[-1])
    _require(bool(name) and "/" not in name and "\\" not in name)
    return name


def _canonical_name(url: str) -> str:
    """The percent-decoded basename of a URL (the runtime URL encodes `+` as %2B)."""
    return urllib.parse.unquote(url.rsplit("/", 1)[-1])


def _record(filename: Any, url: Any, size: Any, sha256: Any, origin: str, source: str) -> dict[str, Any]:
    _require(type(filename) is str and _is_size(size) and _is_hex(sha256))
    _require(_canonical_pythonhosted(url) == filename)
    return {"filename": filename, "url": url, "size": size, "sha256": sha256,
            "origin": origin, "source": source}


class _Inputs:
    """The committed pins as they are on disk now, with their hashes."""

    def __init__(self) -> None:
        try:
            self.lock_bytes = _read_bounded(Path(LOCK_PATH))
            self.pyproject_bytes = _read_bounded(Path(PYPROJECT_PATH))
            pin_bytes = _read_bounded(Path(engine_runtime.MANIFEST), engine_runtime.MAX_PIN_BYTES)
            self.pin = engine_runtime.load_runtime_pin()
            self.build_inputs = engine_derived_wheel.load_build_inputs()
            self.lock = tomllib.loads(self.lock_bytes.decode("utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, RecursionError,
                engine_runtime.RuntimePinError, engine_derived_wheel.DerivedWheelError) as exc:
            raise FetchError("invalid fetch") from exc
        self.hashes = {
            "pyproject_sha256": _sha(self.pyproject_bytes),
            "lock_sha256": _sha(self.lock_bytes),
            "runtime_pin_sha256": _sha(pin_bytes),
            "build_inputs_sha256": self.build_inputs.sha256,
        }
        packages = self.lock.get("package")
        _require(type(packages) is list)
        self.packages: dict[str, dict[str, Any]] = {}
        for package in packages:
            _require(type(package) is dict and type(package.get("name")) is str)
            _require(package["name"] not in self.packages)
            self.packages[package["name"]] = package

    def lock_wheel(self, name: str, version: Any, filename: Any) -> dict[str, Any]:
        """The one lock wheel record for (name, version, filename), as a fetch record."""
        _require(type(name) is str and type(version) is str and type(filename) is str)
        package = self.packages.get(name)
        _require(package is not None and package.get("version") == version)
        wheels = package.get("wheels", [])
        _require(type(wheels) is list)
        found = []
        for wheel in wheels:
            _require(type(wheel) is dict and type(wheel.get("url")) is str)
            if _canonical_pythonhosted(wheel["url"]) == filename:
                found.append(wheel)
        _require(len(found) == 1)
        wheel = found[0]
        digest = wheel.get("hash")
        _require(type(digest) is str and digest.startswith("sha256:"))
        return _record(filename, wheel["url"], wheel.get("size"), digest[len("sha256:"):], "lock", "lock")

    def packaging_record(self) -> dict[str, Any]:
        package = self.packages.get("packaging")
        _require(package is not None and type(package.get("version")) is str)
        version = package["version"]
        return self.lock_wheel("packaging", version, f"packaging-{version}-py3-none-any.whl")


# ------------------------------------------------------------------ plans


def _bootstrap_plan(inputs: _Inputs) -> list[dict[str, Any]]:
    pin = inputs.pin
    _require(pin["url"].startswith(RUNTIME_PREFIX) and "?" not in pin["url"] and "#" not in pin["url"])
    plan = [{"filename": pin["filename"], "url": pin["url"], "size": pin["size"],
             "sha256": pin["sha256"], "origin": "pin", "source": "runtime-pin"}]
    # The destination name is the pin's filename; it must be the URL's own basename.
    _require(_canonical_name(pin["url"]) == pin["filename"])
    data = inputs.build_inputs.data
    tools = [tool for tool in data["build_tools"] if tool["name"] == "setuptools"]
    _require(len(tools) == 1)
    tool = tools[0]
    plan.append(_record(tool["filename"], tool["url"], tool["size"], tool["sha256"],
                        "pin", "build-inputs"))
    for derived in data["derived_wheels"]:
        sdist = derived["sdist"]
        plan.append(_record(sdist["filename"], sdist["url"], sdist["size"], sdist["sha256"],
                            "pin", "build-inputs"))
    plan.append(inputs.packaging_record())
    return plan


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise FetchError("invalid fetch")


def _closure_plan(inputs: _Inputs, report_path: Path) -> list[dict[str, Any]]:
    try:
        report = json.loads(_read_bounded(report_path).decode("utf-8"),
                            object_pairs_hook=_json_object, parse_constant=_reject_constant)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise FetchError("invalid fetch") from exc
    _require(type(report) is dict and set(report) == _REPORT_KEYS)
    _require(type(report["schema"]) is int and report["schema"] == 1)
    _require(report["kind"] == "lock-wheel-audit")
    _require(type(report["blockers"]) is list and report["blockers"] == [])
    recorded = report["inputs"]
    _require(type(recorded) is dict and set(recorded) == _INPUT_KEYS)
    _require(all(recorded[key] == inputs.hashes[key] for key in _INPUT_KEYS))
    selected = report["selected"]
    _require(type(selected) is list and bool(selected))
    plan: list[dict[str, Any]] = []
    for entry in selected:
        _require(type(entry) is dict and set(entry) == _ENTRY_KEYS)
        _require(entry["origin"] in ("lock", "derived"))
        if entry["origin"] == "derived":
            continue
        expected = inputs.lock_wheel(entry["name"], entry["version"], entry["filename"])
        _require(all(entry[key] == expected[key] for key in ("filename", "url", "size", "sha256")))
        plan.append(expected)
    _require(len({item["filename"] for item in plan}) == len(plan))
    return plan


# -------------------------------------------------------------- transport


def _run(argv: list[str], env: dict[str, str], timeout: int) -> int:
    """The single process seam: runs curl with no shell and no inherited input or output."""
    completed = subprocess.run(
        argv, env=env, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=timeout)
    return completed.returncode


# --max-filesize only bounds a response that declares a Content-Length; a chunked
# response is bounded by --max-time until _snapshot rejects the oversize file.
def _curl_argv(url: str, size: int, output: Path) -> list[str]:
    return [CURL, "-q", "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2", "-fsS",
            "--max-redirs", "5", "--max-filesize", str(size), "--connect-timeout", "30",
            "--max-time", "900", "-o", os.fspath(output), url]


def _download(url: str, size: int, output: Path, home: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(home), "LANG": "C"}
    try:
        status = _run(_curl_argv(url, size, output), env, 960)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FetchError("invalid fetch") from exc
    _require(status == 0)


# ------------------------------------------------------------ publication


def _verify_file(path: Path, size: int, sha256: str) -> bool:
    """Rehash an existing file without following links or modifying it."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return False
        digest = hashlib.sha256()
        count = 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            count += len(chunk)
            if count > size:
                return False
            digest.update(chunk)
        return count == size and digest.hexdigest() == sha256
    finally:
        os.close(fd)


def _publish(wheelhouse: Path, record: dict[str, Any]) -> bool:
    """Make one pinned artifact present; True if it was downloaded, False on a cache hit."""
    destination = wheelhouse / record["filename"]
    _require(destination.parent == wheelhouse)
    if os.path.lexists(destination):
        try:
            _require(_verify_file(destination, record["size"], record["sha256"]))
        except OSError as exc:
            raise FetchError("invalid fetch") from exc
        return False
    scratch = Path(tempfile.mkdtemp(prefix=".engine-fetch-", dir=wheelhouse))
    try:
        os.chmod(scratch, 0o700)
        download, verified = scratch / "download", scratch / "verified"
        _download(record["url"], record["size"], download, scratch)
        engine_artifact._snapshot(download, verified, record["sha256"], record["size"])
        os.link(verified, destination)
    except (engine_artifact.ArtifactError, OSError) as exc:
        raise FetchError("invalid fetch") from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return True


def _write_receipt(wheelhouse: Path, name: str, plan: list[dict[str, Any]], hashes: dict[str, str]) -> None:
    body = {
        "schema": 1,
        "inputs": {key: hashes[key] for key in sorted(hashes)},
        "artifacts": sorted(plan, key=lambda r: r["filename"]),
    }
    scratch = Path(tempfile.mkdtemp(prefix=".engine-fetch-", dir=wheelhouse))
    try:
        os.chmod(scratch, 0o700)
        staged = scratch / name
        with staged.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(body, sort_keys=True, indent=2) + "\n")
        os.replace(staged, wheelhouse / name)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _wheelhouse(path: Path) -> Path:
    try:
        engine_artifact._platform_check()
        return engine_artifact._safe_parent(Path(path))
    except (engine_artifact.ArtifactError, OSError) as exc:
        raise FetchError("invalid fetch") from exc


def _acquire(wheelhouse: Path, command: str, plan: list[dict[str, Any]], inputs: _Inputs) -> None:
    house = _wheelhouse(wheelhouse)
    for record in plan:
        _publish(house, record)
    _write_receipt(house, RECEIPTS[command], plan, inputs.hashes)


def bootstrap(wheelhouse: Path) -> None:
    inputs = _Inputs()
    _acquire(wheelhouse, "bootstrap", _bootstrap_plan(inputs), inputs)


def closure(wheelhouse: Path, audit_report: Path) -> None:
    inputs = _Inputs()
    _acquire(wheelhouse, "closure", _closure_plan(inputs, Path(audit_report)), inputs)


# ------------------------------------------------------- packaging helper


def _inside(file: Any, wheel: Path) -> bool:
    return type(file) is str and file.startswith(os.fspath(wheel) + os.sep)


def load_verified_packaging(wheelhouse: Path) -> ModuleType:
    """Import `packaging` only from the lock-pinned wheel in the wheelhouse."""
    try:
        inputs = _Inputs()
        record = inputs.packaging_record()
        version = inputs.packages["packaging"]["version"]
        wheel = _wheelhouse(wheelhouse) / record["filename"]
        _require(_verify_file(wheel, record["size"], record["sha256"]))
        loaded = [n for n in sys.modules if n == "packaging" or n.startswith("packaging.")]
        if loaded:
            # Already imported: the checks below accept it only if it came from this exact wheel.
            module = sys.modules.get("packaging")
        else:
            sys.path.insert(0, os.fspath(wheel))
            try:
                module = importlib.import_module("packaging")
            except BaseException:
                for name in [n for n in sys.modules if n == "packaging" or n.startswith("packaging.")]:
                    del sys.modules[name]
                raise
            finally:
                sys.path.remove(os.fspath(wheel))
        _require(_inside(getattr(module, "__file__", None), wheel))
        _require(getattr(module, "__version__", None) == version)
        return module
    except FetchError:
        raise
    except (OSError, ImportError, zipfile.BadZipFile, ValueError) as exc:
        raise FetchError("invalid fetch") from exc


# -------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print("artifact fetch failed: usage", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(allow_abbrev=False, description="Fetch the pinned engine build artifacts.")
    commands = parser.add_subparsers(dest="command", required=True)
    boot = commands.add_parser("bootstrap", allow_abbrev=False)
    boot.add_argument("--wheelhouse", required=True)
    clos = commands.add_parser("closure", allow_abbrev=False)
    clos.add_argument("--wheelhouse", required=True)
    clos.add_argument("--audit-report", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "bootstrap":
            bootstrap(Path(args.wheelhouse))
        else:
            closure(Path(args.wheelhouse), Path(args.audit_report))
    except (FetchError, OSError, ValueError, engine_artifact.ArtifactError):
        print("artifact fetch failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
