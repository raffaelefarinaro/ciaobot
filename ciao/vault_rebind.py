"""Read-only evidence for a future stopped-engine vault rebind.

Callers must derive the source, protected roots and expected registry from the
validated installation; this module deliberately provides no registry writer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence


@dataclass(frozen=True)
class RebindPreview:
    source: Path
    destination: Path
    files: tuple[str, ...]
    token: str


def _directory(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("directory must be an absolute path without traversal")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("symlink in directory path")
    if not path.is_dir():
        raise ValueError("directory is unavailable")
    return path


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_directory(path: Path) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ValueError("safe directory traversal is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _snapshot(root: Path) -> tuple[dict[str, str], dict[str, tuple[int, ...]]]:
    entries: dict[str, str] = {}
    identities: dict[str, tuple[int, ...]] = {}
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

    def walk(fd: int, prefix: str) -> None:
        start = os.fstat(fd)
        identities[prefix] = _identity(start)
        names = sorted(os.listdir(fd))
        for name in names:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            relative = prefix + name
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, flags | os.O_DIRECTORY, dir_fd=fd)
                try:
                    if _identity(info) != _identity(os.fstat(child)):
                        raise ValueError("directory changed during preview")
                    entries[relative + "/"] = "directory"
                    walk(child, relative + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                child = os.open(name, flags, dir_fd=fd)
                with os.fdopen(child, "rb") as stream:
                    if _identity(info) != _identity(os.fstat(stream.fileno())):
                        raise ValueError("file changed during preview")
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    if _identity(info) != _identity(os.fstat(stream.fileno())):
                        raise ValueError("file changed during preview")
                entries[relative] = digest
                identities[relative] = _identity(info)
            else:
                raise ValueError("non-regular file or symlink in notes tree")
            if _identity(info) != _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                raise ValueError("entry changed during preview")
        if _identity(start) != _identity(os.fstat(fd)) or names != sorted(os.listdir(fd)):
            raise ValueError("directory changed during preview")

    root_fd = _open_directory(root)
    try:
        walk(root_fd, "")
        check_fd = _open_directory(root)
        try:
            if _identity(os.fstat(root_fd)) != _identity(os.fstat(check_fd)):
                raise ValueError("root changed during preview")
        finally:
            os.close(check_fd)
    finally:
        os.close(root_fd)
    return entries, identities


def preview(source: Path, destination: Path, *, expected_registry: bytes, protected_roots: Sequence[Path]) -> RebindPreview:
    """Compare complete local trees and bind evidence to an explicit registry image."""
    source, destination = _directory(source), _directory(destination)
    def overlaps(a: Path, b: Path) -> bool:
        return a == b or a in b.parents or b in a.parents
    if overlaps(source, destination):
        raise ValueError("notes roots overlap")
    if any(overlaps(destination, _directory(root)) for root in protected_roots):
        raise ValueError("destination overlaps a protected root")
    before, source_identity = _snapshot(source)
    after, destination_identity = _snapshot(destination)
    if not any(not key.endswith("/") for key in before):
        raise ValueError("notes tree contains no regular files")
    if before != after:
        raise ValueError("notes trees do not match")
    evidence = json.dumps([str(source), str(destination), hashlib.sha256(expected_registry).hexdigest(), sorted(before.items()), sorted(source_identity.items()), sorted(destination_identity.items())], ensure_ascii=True, separators=(",", ":"))
    return RebindPreview(source, destination, tuple(k for k in sorted(before) if not k.endswith("/")), hashlib.sha256(evidence.encode()).hexdigest())


def repoint_registry(entries: list[dict[str, object]], workspace: str, *, expected_root: str, new_root: str) -> list[dict[str, object]]:
    """Return a changed copy only if exactly one row has the expected root."""
    if not workspace or not expected_root or not new_root or expected_root == new_root:
        raise ValueError("invalid registry transition")
    matches = [index for index, row in enumerate(entries) if row.get("name") == workspace]
    if len(matches) != 1 or entries[matches[0]].get("vault_root") != expected_root:
        raise ValueError("registry does not match expected binding")
    from copy import deepcopy

    result = deepcopy(entries)
    result[matches[0]]["vault_root"] = new_root
    return result


def recovery_state(current: bytes, *, before: bytes, after: bytes) -> str:
    """Classify a recorded transaction without overwriting a later edit."""
    if before == after:
        raise ValueError("ambiguous recovery record")
    if current == before:
        return "before"
    if current == after:
        return "after"
    raise ValueError("registry differs from both recorded images")


def _registry_bytes(install: Path) -> bytes:
    runtime_fd = _open_directory(install / ".runtime")
    try:
        fd = os.open("workspaces.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=runtime_fd)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("registry is not a regular file")
            data = stream.read()
            if _identity(before) != _identity(os.fstat(stream.fileno())):
                raise ValueError("registry changed while reading")
            if _identity(before) != _identity(os.stat("workspaces.json", dir_fd=runtime_fd, follow_symlinks=False)):
                raise ValueError("registry was replaced")
            return data
    finally:
        os.close(runtime_fd)


def authoritative_preview(install: Path, workspace: str, destination: Path, *, expected_registry: bytes) -> RebindPreview:
    """Derive absolute vault roots from a strict registry, without writing anything.

    Legacy and relative registry roots are refused until their authoritative
    resolution is integrated with setup; this is not apply authorization.
    """
    install = _directory(install)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", workspace):
        raise ValueError("invalid workspace")
    if _registry_bytes(install) != expected_registry:
        raise ValueError("registry does not match expected before-image")
    try:
        rows = json.loads(expected_registry)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid registry") from exc
    if not isinstance(rows, list) or not rows:
        raise ValueError("invalid registry")
    roots: dict[str, Path] = {}
    folded_names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid registry row")
        name, root = row.get("name"), row.get("vault_root")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) or name.casefold() in folded_names or not isinstance(root, str):
            raise ValueError("ambiguous registry")
        folded_names.add(name.casefold())
        roots[name] = _directory(Path(root))
    if workspace not in roots:
        raise ValueError("workspace is not registered")
    result = preview(roots[workspace], destination, expected_registry=expected_registry, protected_roots=[install, *(root for name, root in roots.items() if name != workspace)])
    if _registry_bytes(install) != expected_registry:
        raise ValueError("registry changed during preview")
    token = hashlib.sha256(json.dumps([workspace, result.token], separators=(",", ":")).encode()).hexdigest()
    return RebindPreview(result.source, result.destination, result.files, token)
