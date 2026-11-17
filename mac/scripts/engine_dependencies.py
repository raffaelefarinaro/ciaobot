#!/usr/bin/env python3
"""Offline audit: does every locked production dependency have a usable wheel?

Reads a project file and its ``uv.lock`` (never the network, never a
subprocess), follows the already-resolved production graph for one fixed
target (CPython 3.12.16, macOS 13.0+, arm64) and reports which wheel each
distribution would use. A locked wheel wins; a distribution with no
compatible locked wheel may use the derived wheel pinned in
``mac/engine-build-inputs.json``. The report is a claim made from lock
records, never an installable manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tomllib
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_PIN_BYTES = 65_536
MAX_PACKAGES = 10_000
MAX_EDGES = 100_000
MAX_EXTRAS_PER_PACKAGE = 128
MAX_STATES = 100_000
MAX_OPERATIONS = 1_000_000
MAX_FILENAME_BYTES = 4096
MAX_TAG_COMPONENTS = 64
MAX_TAGS_PER_WHEEL = 4096
MAX_TOTAL_TAGS = 1_000_000
MAX_TARGET_TAGS = 10_000
LOCK_VERSION = 1
LOCK_REVISION = 5
REGISTRY = "https://pypi.org/simple"
ARTIFACT_HOST = "files.pythonhosted.org"
_HEX = re.compile(r"[0-9a-f]{64}")
_UNSUPPORTED_MARKER = re.compile(r"platform_release|platform_version")
_EXTRA_MARKER = re.compile(r"\bextra\b")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_CHAIN_SEPARATOR = " → "


class AuditError(Exception):
    """A refusal carrying a fixed, non-descriptive code (CLI exit 2)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str = "invalid-input") -> None:
    if not condition:
        raise AuditError(code)


def _load_sibling(name: str) -> Any:
    path = Path(__file__).resolve().with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("sibling helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine_derived_wheel = _load_sibling("engine_derived_wheel")
engine_runtime = _load_sibling("engine_runtime")


def _packaging() -> Any:
    """Import the packaging helpers, or refuse with `parser-prerequisite`."""
    try:
        import packaging
        import packaging.markers
        import packaging.requirements
        import packaging.specifiers
        import packaging.tags
        import packaging.utils
        import packaging.version
    except ImportError as exc:
        raise AuditError("parser-prerequisite") from exc
    return packaging


# ----------------------------------------------------------------- inputs


def _read_bounded(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AuditError("invalid-input") from exc
    try:
        _require(stat.S_ISREG(os.fstat(fd).st_mode))
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError as exc:
        raise AuditError("invalid-input") from exc
    finally:
        os.close(fd)
    _require(total <= limit, "resource-limit")
    return b"".join(chunks)


def _toml(raw: bytes) -> dict[str, Any]:
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        raise AuditError("invalid-input") from exc


def _is_hex(value: Any) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


def _is_pos_int(value: Any) -> bool:
    return type(value) is int and value > 0


# ----------------------------------------------------------------- target


class Target:
    """The fixed deployment target, derived from the validated runtime pin."""

    def __init__(self, pin: dict[str, Any]) -> None:
        python = pin["python_version"]
        _require(type(python) is str and _VERSION.fullmatch(python) is not None)
        parts = python.split(".")
        _require(len(parts) == 3)
        _require(pin["target"] == "aarch64-apple-darwin")
        macos = pin["minimum_macos"]
        _require(type(macos) is str and re.fullmatch(r"[0-9]+\.[0-9]+", macos) is not None)
        self.python_full = python
        self.python_tuple = (int(parts[0]), int(parts[1]))
        self.macos = (int(macos.split(".")[0]), int(macos.split(".")[1]))
        self.interpreter = f"cp{parts[0]}{parts[1]}"
        self.arch = "arm64"

    def environment(self) -> dict[str, str]:
        major, minor = self.python_tuple
        return {
            "python_version": f"{major}.{minor}",
            "python_full_version": self.python_full,
            "implementation_name": "cpython",
            "implementation_version": self.python_full,
            "platform_python_implementation": "CPython",
            "os_name": "posix",
            "sys_platform": "darwin",
            "platform_system": "Darwin",
            "platform_machine": "arm64",
            "extra": "",
        }

    def description(self) -> dict[str, Any]:
        return {
            "python": self.python_full,
            "implementation": "cpython",
            "platform": f"macosx_{self.macos[0]}_{self.macos[1]}_{self.arch}",
            "os": "darwin",
            "arch": self.arch,
            "minimum_macos": f"{self.macos[0]}.{self.macos[1]}",
        }

    def tag_priority(self) -> dict[Any, int]:
        pk = _packaging()
        platforms = list(pk.tags.mac_platforms(version=self.macos, arch=self.arch))
        ordered = list(pk.tags.cpython_tags(
            python_version=self.python_tuple, abis=[self.interpreter], platforms=platforms))
        ordered += list(pk.tags.compatible_tags(
            python_version=self.python_tuple, interpreter=self.interpreter, platforms=platforms))
        priority: dict[Any, int] = {}
        for tag in ordered:
            priority.setdefault(tag, len(priority))
        _require(len(priority) <= MAX_TARGET_TAGS, "resource-limit")
        return priority


# --------------------------------------------------------------- markers


class _Budget:
    def __init__(self) -> None:
        self.operations = 0

    def spend(self, count: int = 1) -> None:
        self.operations += count
        _require(self.operations <= MAX_OPERATIONS, "resource-limit")


def _evaluate(marker: Any, target: Target, extra: str = "") -> bool:
    if marker is None:
        return True
    _require(type(marker) is str)
    _require(_UNSUPPORTED_MARKER.search(marker) is None)
    pk = _packaging()
    try:
        parsed = pk.markers.Marker(marker)
        env = target.environment()
        env["extra"] = extra
        return bool(parsed.evaluate(env))
    except Exception as exc:
        raise AuditError("invalid-input") from exc


# ------------------------------------------------------------ lock model


def _norm(name: Any) -> str:
    _require(type(name) is str and bool(name))
    return str(_packaging().utils.canonicalize_name(name))


def _edge_list(value: Any) -> list[dict[str, Any]]:
    _require(type(value) is list)
    for entry in value:
        _require(type(entry) is dict and "name" in entry and set(entry) <= {"name", "extra", "marker"})
        if "extra" in entry:
            extras = entry["extra"]
            _require(type(extras) is list and all(type(e) is str for e in extras))
            _require(len(extras) <= MAX_EXTRAS_PER_PACKAGE, "resource-limit")
        if "marker" in entry:
            _require(type(entry["marker"]) is str)
    return value


class _Lock:
    def __init__(self, data: dict[str, Any], target: Target) -> None:
        pk = _packaging()
        _require(data.get("version") == LOCK_VERSION and data.get("revision") == LOCK_REVISION)
        requires_python = data.get("requires-python")
        _require(type(requires_python) is str)
        try:
            ok = pk.specifiers.SpecifierSet(requires_python).contains(target.python_full)
        except Exception as exc:
            raise AuditError("invalid-input") from exc
        _require(ok)
        markers = data.get("resolution-markers", [])
        _require(type(markers) is list)
        matching = [m for m in markers if _evaluate(m, target)]
        _require(len(matching) == 1 or not markers)
        packages = data.get("package")
        _require(type(packages) is list)
        _require(len(packages) <= MAX_PACKAGES, "resource-limit")
        self.packages: dict[str, dict[str, Any]] = {}
        edges = 0
        for package in packages:
            _require(type(package) is dict and type(package.get("version")) is str)
            _require(type(package.get("source")) is dict)
            name = _norm(package.get("name"))
            _require(name not in self.packages)
            edges += len(_edge_list(package.get("dependencies", [])))
            optional = package.get("optional-dependencies", {})
            _require(type(optional) is dict)
            _require(len(optional) <= MAX_EXTRAS_PER_PACKAGE, "resource-limit")
            for key, value in optional.items():
                _require(type(key) is str)
                edges += len(_edge_list(value))
            _require(edges <= MAX_EDGES, "resource-limit")
            self.packages[name] = package

    def roots(self) -> str:
        found = [n for n, p in self.packages.items() if p["source"] == {"editable": "."}]
        _require(len(found) == 1)
        return found[0]


# ------------------------------------------------------- project binding


def _requirement_key(name: Any, extras: Any, specifier: Any, marker: Any) -> tuple[Any, ...]:
    pk = _packaging()
    try:
        spec = str(pk.specifiers.SpecifierSet(specifier)) if specifier else ""
        mark = str(pk.markers.Marker(marker)) if marker else ""
    except Exception as exc:
        raise AuditError("invalid-input") from exc
    return (_norm(name), tuple(sorted(_norm(e) for e in extras)), spec, mark)


def _project_requirements(project: dict[str, Any]) -> tuple[str, str, set[tuple[Any, ...]], str]:
    pk = _packaging()
    meta = project.get("project")
    _require(type(meta) is dict)
    name, version = meta.get("name"), meta.get("version")
    _require(type(name) is str and type(version) is str)
    deps = meta.get("dependencies", [])
    _require(type(deps) is list)
    keys: set[tuple[Any, ...]] = set()
    for line in deps:
        _require(type(line) is str)
        try:
            req = pk.requirements.Requirement(line)
        except Exception as exc:
            raise AuditError("invalid-input") from exc
        _require(req.url is None)
        keys.add(_requirement_key(req.name, req.extras, str(req.specifier), str(req.marker) if req.marker else None))
    optional = meta.get("optional-dependencies", {})
    _require(type(optional) is dict)
    pins = []
    for line in optional.get("test", []):
        _require(type(line) is str)
        try:
            req = pk.requirements.Requirement(line)
        except Exception as exc:
            raise AuditError("invalid-input") from exc
        if _norm(req.name) == "packaging":
            pins.append(str(req.specifier))
    _require(len(pins) == 1 and pins[0].startswith("=="))
    return _norm(name), version, keys, pins[0][2:]


def _check_packaging(lock: _Lock, test_pin: str) -> None:
    pk = _packaging()
    package = lock.packages.get("packaging")
    _require(package is not None, "parser-prerequisite")
    assert package is not None
    version = str(getattr(pk, "__version__", ""))
    _require(version == package["version"] == test_pin, "parser-prerequisite")


def _bind_root(
    lock: _Lock, root_name: str, project_name: str, project_version: str,
    project_keys: set[tuple[Any, ...]], target: Target,
) -> dict[str, Any]:
    pk = _packaging()
    root = lock.packages[root_name]
    _require(root_name == project_name and root["version"] == project_version)
    metadata = root.get("metadata", {})
    _require(type(metadata) is dict and type(metadata.get("requires-dist")) is list)
    locked_keys: set[tuple[Any, ...]] = set()
    for entry in metadata["requires-dist"]:
        _require(type(entry) is dict and "name" in entry)
        marker = entry.get("marker")
        if type(marker) is str and _EXTRA_MARKER.search(marker):
            continue
        locked_keys.add(_requirement_key(
            entry["name"], entry.get("extras", []), entry.get("specifier", ""), marker))
    _require(locked_keys == project_keys)
    edge_names = set()
    for edge in _edge_list(root.get("dependencies", [])):
        edge_names.add((_norm(edge["name"]), tuple(sorted(_norm(e) for e in edge.get("extra", [])))))
    _require(edge_names == {(k[0], k[1]) for k in locked_keys})
    for name, extras, spec, marker in sorted(locked_keys):
        if not _evaluate(marker or None, target):
            continue
        node = lock.packages.get(name)
        _require(node is not None)
        assert node is not None
        try:
            fits = pk.specifiers.SpecifierSet(spec).contains(node["version"], prereleases=True)
        except Exception as exc:
            raise AuditError("invalid-input") from exc
        _require(fits)
    return root


# ------------------------------------------------------------- traversal

State = tuple[str, str | None]


def _label(lock: _Lock, state: State, root_name: str) -> str:
    name, extra = state
    if name == root_name:
        return name
    shown = f"{name}[{extra}]" if extra else name
    return f"{shown}=={lock.packages[name]['version']}"


def _state_edges(lock: _Lock, state: State, root_name: str, target: Target, budget: _Budget) -> list[State]:
    name, extra = state
    package = lock.packages[name]
    if extra is None:
        raw = package.get("dependencies", [])
    else:
        optional = package.get("optional-dependencies", {})
        _require(extra in optional)
        raw = optional[extra]
    found: set[State] = set()
    for entry in raw:
        budget.spend()
        if not _evaluate(entry.get("marker"), target):
            continue
        dep = _norm(entry["name"])
        _require(dep in lock.packages and dep != root_name)
        found.add((dep, None))
        for extra_name in entry.get("extra", []):
            found.add((dep, _norm(extra_name)))
    def key(item: State) -> tuple[str, str, str, str]:
        package = lock.packages[item[0]]
        return (item[0], package["version"], json.dumps(package["source"], sort_keys=True), item[1] or "")
    return sorted(found, key=key)


def _walk(lock: _Lock, root_name: str, target: Target) -> tuple[dict[str, State], dict[State, State | None]]:
    """Breadth-first over (package, extra) states from the project root.

    Returns the first state that reached each package (the shortest witness
    end) and the parent links used to rebuild chains.
    """
    budget = _Budget()
    start: State = (root_name, None)
    parent: dict[State, State | None] = {start: None}
    first: dict[str, State] = {}
    extras_seen: dict[str, int] = {}
    queue: deque[State] = deque([start])
    while queue:
        state = queue.popleft()
        for nxt in _state_edges(lock, state, root_name, target, budget):
            if nxt in parent:
                continue
            _require(len(parent) < MAX_STATES, "resource-limit")
            if nxt[1] is not None:
                extras_seen[nxt[0]] = extras_seen.get(nxt[0], 0) + 1
                _require(extras_seen[nxt[0]] <= MAX_EXTRAS_PER_PACKAGE, "resource-limit")
            parent[nxt] = state
            first.setdefault(nxt[0], nxt)
            queue.append(nxt)
    return first, parent


def _chain(lock: _Lock, state: State, parent: dict[State, State | None], root_name: str) -> str:
    labels: list[str] = []
    current: State | None = state
    while current is not None:
        labels.append(_label(lock, current, root_name))
        current = parent[current]
    return _CHAIN_SEPARATOR.join(reversed(labels))


# ----------------------------------------------------------------- wheels


def _preflight_wheel_filename(filename: str, budget_total: list[int]) -> None:
    _require(type(filename) is str and filename.endswith(".whl"))
    _require(len(filename.encode("utf-8", "replace")) <= MAX_FILENAME_BYTES, "resource-limit")
    fields = filename[: -len(".whl")].split("-")
    _require(len(fields) in (5, 6))
    product = 1
    for field in fields[-3:]:
        count = field.count(".") + 1
        _require(count <= MAX_TAG_COMPONENTS, "resource-limit")
        product *= count
    _require(product <= MAX_TAGS_PER_WHEEL, "resource-limit")
    budget_total[0] += product
    _require(budget_total[0] <= MAX_TOTAL_TAGS, "resource-limit")


def _wheel_matches(
    name: str, version: str, filename: str, priority: dict[Any, int], budget_total: list[int],
) -> tuple[int, list[str]] | None:
    """(best priority, sorted matching tags) when the wheel fits, else None."""
    pk = _packaging()
    _preflight_wheel_filename(filename, budget_total)
    try:
        wheel_name, wheel_version, _build, tags = pk.utils.parse_wheel_filename(filename)
        expected = pk.version.Version(version)
    except Exception as exc:
        raise AuditError("invalid-input") from exc
    _require(wheel_name == name and wheel_version == expected)
    matched = [t for t in tags if t in priority]
    if not matched:
        return None
    return min(priority[t] for t in matched), sorted(str(t) for t in matched)


def _locked_candidate(
    name: str, version: str, record: Any, priority: dict[Any, int], budget_total: list[int],
) -> tuple[Any, dict[str, Any]] | None:
    _require(type(record) is dict and {"url", "hash", "size"} <= set(record))
    url, digest, size = record["url"], record["hash"], record["size"]
    _require(type(url) is str and type(digest) is str and digest.startswith("sha256:"))
    _require(_is_hex(digest[len("sha256:"):]) and _is_pos_int(size))
    parsed = urllib.parse.urlsplit(url)
    _require(parsed.scheme == "https" and parsed.hostname == ARTIFACT_HOST)
    _require(not (parsed.username or parsed.password or parsed.query or parsed.fragment))
    _require(parsed.port is None and parsed.path.startswith("/packages/"))
    filename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
    match = _wheel_matches(name, version, filename, priority, budget_total)
    if match is None:
        return None
    rank, tags = match
    entry = {
        "filename": filename, "url": url, "size": size,
        "sha256": digest[len("sha256:"):], "tags": tags,
    }
    return (rank, filename), entry


def _select_locked(
    name: str, package: dict[str, Any], priority: dict[Any, int], budget_total: list[int],
) -> dict[str, Any] | None:
    _require(package["source"] == {"registry": REGISTRY})
    best: tuple[Any, dict[str, Any]] | None = None
    wheels = package.get("wheels", [])
    _require(type(wheels) is list)
    for record in wheels:
        found = _locked_candidate(name, package["version"], record, priority, budget_total)
        if found is not None and (best is None or found[0] < best[0]):
            best = found
    return None if best is None else best[1]


def _sdist_matches(package: dict[str, Any], sdist: dict[str, Any]) -> bool:
    record = package.get("sdist")
    if type(record) is not dict:
        return False
    return (
        record.get("url") == sdist["url"]
        and record.get("hash") == f"sha256:{sdist['sha256']}"
        and record.get("size") == sdist["size"]
    )


def _derived_candidates(
    lock: _Lock, reachable: set[str], locked: dict[str, dict[str, Any]],
    priority: dict[Any, int], budget_total: list[int], build_inputs: Any,
) -> dict[str, dict[str, Any]]:
    entries = build_inputs.data["derived_wheels"]
    _require(type(entries) is list)
    result: dict[str, dict[str, Any]] = {}
    for entry in sorted(entries, key=lambda e: e["name"]):
        name = _norm(entry["name"])
        package = lock.packages.get(name)
        _require(name in reachable and package is not None, "derived-wheel-mismatch")
        assert package is not None
        _require(package["version"] == entry["version"], "derived-wheel-mismatch")
        _require(_sdist_matches(package, entry["sdist"]), "derived-wheel-mismatch")
        _require(name not in locked, "derived-wheel-unneeded")
        wheel = entry["wheel"]
        match = _wheel_matches(name, entry["version"], wheel["filename"], priority, budget_total)
        _require(match is not None, "derived-wheel-mismatch")
        assert match is not None
        result[name] = {
            "filename": wheel["filename"], "url": None, "size": wheel["size"],
            "sha256": wheel["sha256"], "tags": match[1],
        }
    return result


# ------------------------------------------------------------------ audit


def audit(project: Path, lock: Path) -> dict[str, Any]:
    """Return the audit report; raise AuditError for exit-2 conditions."""
    _packaging()
    try:
        pin = engine_runtime.load_runtime_pin()
        pin_bytes = _read_bounded(Path(engine_runtime.MANIFEST), MAX_PIN_BYTES)
        build_inputs = engine_derived_wheel.load_build_inputs()
    except AuditError:
        raise
    except Exception as exc:
        raise AuditError("invalid-input") from exc
    target = Target(pin)
    project_bytes = _read_bounded(Path(project), MAX_INPUT_BYTES)
    lock_bytes = _read_bounded(Path(lock), MAX_INPUT_BYTES)
    project_data, lock_data = _toml(project_bytes), _toml(lock_bytes)
    model = _Lock(lock_data, target)
    project_name, project_version, project_keys, test_pin = _project_requirements(project_data)
    _check_packaging(model, test_pin)
    root_name = model.roots()
    _bind_root(model, root_name, project_name, project_version, project_keys, target)

    first, parent = _walk(model, root_name, target)
    priority = target.tag_priority()
    budget_total = [0]
    locked: dict[str, dict[str, Any]] = {}
    for name in sorted(first):
        selected = _select_locked(name, model.packages[name], priority, budget_total)
        if selected is not None:
            locked[name] = selected
    derived = _derived_candidates(
        model, set(first), locked, priority, budget_total, build_inputs)

    selected_rows: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    for name in sorted(first):
        version = model.packages[name]["version"]
        if name in locked:
            selected_rows.append({"name": name, "version": version, "origin": "lock", **locked[name]})
        elif name in derived:
            selected_rows.append({"name": name, "version": version, "origin": "derived", **derived[name]})
        else:
            blockers.append({
                "code": "missing-compatible-wheel",
                "chain": _chain(model, first[name], parent, root_name),
            })
    blockers.sort(key=lambda b: (b["code"], b["chain"]))
    return {
        "schema": 1,
        "kind": "lock-wheel-audit",
        "installable": False,
        "target": target.description(),
        "inputs": {
            "pyproject_sha256": hashlib.sha256(project_bytes).hexdigest(),
            "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
            "runtime_pin_sha256": hashlib.sha256(pin_bytes).hexdigest(),
            "build_inputs_sha256": build_inputs.sha256,
        },
        "selected": selected_rows,
        "blockers": blockers,
    }


# -------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print("lock wheel audit failed: usage", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(allow_abbrev=False, description="Audit locked wheel availability offline.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--lock", required=True)
    args = parser.parse_args(argv)
    try:
        report = audit(Path(args.project), Path(args.lock))
    except AuditError as exc:
        print(f"lock wheel audit failed: {exc.code}", file=sys.stderr)
        return 2
    except Exception:
        print("lock wheel audit failed: invalid-input", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, indent=2))
    return 1 if report["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
