#!/usr/bin/env python3
"""Offline, bounded verifier and extractor for the reviewed Python runtime tarball."""

from __future__ import annotations

import argparse
from collections import deque
import gzip
import hashlib
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import unicodedata
import zlib
from pathlib import Path

MAX_COMPRESSED = 512 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_MEMBERS = 100_000
MAX_RESOLUTION_OPS = 1_000_000
# Leave headroom below the stricter supported kernel symlink walk limit.
MAX_LINK_HOPS = 30
_FILE, _DIR, _LINK = "file", "dir", "link"


class ArtifactError(Exception):
    """A deliberately non-descriptive refusal suitable for CLI output."""


class _Node:
    def __init__(self, name: str, kind: str | None, explicit: bool = False):
        self.name = name
        self.kind = kind
        self.explicit = explicit
        self.children: dict[str, _Node] = {}
        self.target: str | None = None


def _require(condition: bool) -> None:
    if not condition:
        raise ArtifactError("invalid artifact")


def _platform_check() -> None:
    _require(platform.system() in ("Darwin", "Linux"))
    _require(sys.version_info >= (3, 12))
    _require(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_NONBLOCK"))


def _components(value: str) -> list[str]:
    _require(bool(value) and not value.startswith("/") and "\\" not in value)
    _require(not any(ord(c) < 32 or ord(c) == 127 for c in value))
    parts = value.split("/")
    _require(all(p not in ("", ".", "..") for p in parts))
    _require(len(value.encode("utf-8")) <= 256)
    _require(all(len(p.encode("utf-8")) <= 255 for p in parts))
    return parts


def _safe_parent(parent: Path) -> Path:
    absolute = Path(os.path.abspath(parent))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        _require(not current.is_symlink())
    info = absolute.stat()
    _require(
        stat.S_ISDIR(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o700
        and info.st_uid == os.getuid()
    )
    return absolute


def _snapshot(source: Path, target: Path, expected_hash: str, expected_size: int) -> None:
    _require(re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) is not None)
    _require(type(expected_size) is int and 0 <= expected_size <= MAX_COMPRESSED)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    count = 0
    try:
        _require(stat.S_ISREG(os.fstat(fd).st_mode))
        with os.fdopen(fd, "rb", closefd=False) as src, target.open("xb") as dst:
            while True:
                chunk = src.read(min(1024 * 1024, MAX_COMPRESSED - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                _require(count <= MAX_COMPRESSED and count <= expected_size)
                digest.update(chunk)
                dst.write(chunk)
        _require(count == expected_size and digest.hexdigest().lower() == expected_hash.lower())
    finally:
        os.close(fd)


def _decompress(source: Path, target: Path, limit: int | None = None) -> None:
    limit = MAX_EXPANDED if limit is None else limit
    _require(0 <= limit <= MAX_EXPANDED)
    total = 0
    with source.open("rb") as src, target.open("xb") as dst, gzip.GzipFile(fileobj=src, mode="rb") as stream:
        while True:
            out = stream.read(min(65536, limit - total + 1))
            if not out:
                break
            total += len(out)
            _require(total <= limit)
            dst.write(out)
        _require(total > 0)


def _octal(field: bytes, *, empty_zero: bool = False) -> int:
    _require(field and not (field[0] & 0x80))
    text = field.rstrip(b"\0 ").lstrip(b" ")
    if not text and empty_zero:
        return 0
    _require(bool(text) and all(48 <= ch <= 55 for ch in text))
    return int(text, 8)


def _read_tar_header(stream):
    return stream.read(512)


def _field(raw: bytes, limit: int) -> str:
    _require(len(raw) <= limit)
    if b"\0" in raw:
        value, padding = raw.split(b"\0", 1)
        _require(not any(padding))
    else:
        value = raw
    try:
        return value.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise ArtifactError("invalid artifact") from exc


def _preflight(path: Path, member_limit: int | None = None) -> None:
    member_limit = MAX_MEMBERS if member_limit is None else member_limit
    size = path.stat().st_size
    _require(size >= 1024 and size % 512 == 0)
    count = 0
    ended = False
    with path.open("rb") as stream:
        while True:
            block = _read_tar_header(stream)
            _require(len(block) == 512)
            if block == bytes(512):
                second = _read_tar_header(stream)
                _require(second == bytes(512))
                ended = True
                while True:
                    padding = stream.read(65536)
                    if not padding:
                        break
                    _require(not any(padding))
                break
            _require(not ended)
            count += 1
            _require(count <= member_limit)
            stored = _octal(block[148:156])
            _require(sum(block[:148]) + 8 * 32 + sum(block[156:]) == stored)
            _require(block[257:263] == b"ustar\0" and block[263:265] == b"00")
            typ = block[156:157]
            _require(typ in (b"\0", b"0", b"5", b"2"))
            name, prefix = _field(block[:100], 100), _field(block[345:500], 155)
            if typ == b"5" and name.endswith("/"):
                name = name[:-1]
            full = f"{prefix}/{name}" if prefix else name
            _components(full)
            if typ == b"2":
                link = _field(block[157:257], 100)
                _require(bool(link) and len(link.encode("utf-8")) <= 100)
            mode = _octal(block[100:108])
            _octal(block[108:116], empty_zero=True)
            _octal(block[116:124], empty_zero=True)
            _octal(block[136:148], empty_zero=True)
            _octal(block[329:337], empty_zero=True)
            _octal(block[337:345], empty_zero=True)
            length = _octal(block[124:136])
            _require(typ not in (b"5", b"2") or length == 0)
            padded = (length + 511) // 512 * 512
            _require(stream.tell() + padded <= size)
            stream.seek(padded, os.SEEK_CUR)
        _require(ended)


def _manifest(path: Path, member_limit: int = MAX_MEMBERS) -> tuple[_Node, list[tuple[list[str], _Node, tarfile.TarInfo]]]:
    root = _Node("", _DIR)
    records = []
    with tarfile.open(path, "r:", format=tarfile.USTAR_FORMAT) as tf:
        for index, info in enumerate(tf):
            _require(index < member_limit)
            parts = _components(info.name.rstrip("/") if info.isdir() else info.name)
            _require(parts[0] == "python")
            _require(not (info.mode & 0o6000))
            kind = _DIR if info.isdir() else _LINK if info.issym() else _FILE if info.isfile() else None
            _require(kind is not None and not info.islnk())
            node = root
            for i, part in enumerate(parts):
                canonical = unicodedata.normalize("NFC", part).casefold()
                last = i == len(parts) - 1
                requested = kind if last else _DIR
                child = node.children.get(canonical)
                if child is None:
                    child = _Node(part, requested, last)
                    node.children[canonical] = child
                else:
                    _require(child.name == part)
                    if last:
                        _require(not child.explicit)
                        _require(child.kind in (None, kind))
                        child.kind, child.explicit = kind, True
                    else:
                        _require(child.kind in (None, _DIR))
                        child.kind = _DIR
                node = child
            _require(node is not root)
            if kind == _LINK:
                node.target = info.linkname
                _require(not node.children)
            records.append((parts, node, info))
    _require(bool(records))
    prefix = root.children["python"]
    _require(prefix.kind == _DIR)
    return prefix, records


def _resolve_links(
    root: _Node,
    records: list[tuple[list[str], _Node, tarfile.TarInfo]],
    operation_limit: int | None = None,
) -> None:
    operation_limit = MAX_RESOLUTION_OPS if operation_limit is None else operation_limit
    links = [(p, n) for p, n, _ in records if n.kind == _LINK]
    ops = 0
    for original, node in links:
        # Resolve the link target relative to its parent; expand each link immediately.
        target = node.target
        _require(target is not None and not target.startswith("/") and "\\" not in target)
        _require(len(target.encode("utf-8")) <= 256)
        _require(not any(ord(c) < 32 or ord(c) == 127 for c in target))
        raw = target.split("/")
        _require(all(raw))
        work: deque[str | tuple[str, int]] = deque(list(original[1:-1]) + raw)
        remaining_components = len(work)
        # Start at the root containing python; original[0] is the archive prefix.
        current: list[str] = []
        nodes = [root]
        active: set[int] = set()
        hops = 1
        while work:
            comp = work.popleft()
            if isinstance(comp, tuple):
                _require(comp[0] == "end")
                active.remove(comp[1])
                continue
            remaining_components -= 1
            ops += 1
            _require(ops <= operation_limit)
            if comp == ".":
                continue
            if comp == "..":
                _require(current)
                current.pop()
                nodes.pop()
                continue
            key = unicodedata.normalize("NFC", comp).casefold()
            child = nodes[-1].children.get(key)
            _require(child is not None and child.name == comp)
            current.append(comp)
            if child.kind == _LINK:
                _require(id(child) not in active and len(active) < len(links))
                active.add(id(child))
                hops += 1
                _require(hops <= MAX_LINK_HOPS)
                linktarget = child.target
                _require(linktarget is not None and not linktarget.startswith("/") and "\\" not in linktarget)
                _require(len(linktarget.encode("utf-8")) <= 256)
                _require(not any(ord(c) < 32 or ord(c) == 127 for c in linktarget))
                current.pop()
                expanded = linktarget.split("/")
                remaining_components += len(expanded)
                work.extendleft(reversed([*expanded, ("end", id(child))]))
            else:
                nodes.append(child)
                if remaining_components:
                    _require(child.kind == _DIR)
        _require(not active and nodes[-1].kind in (_FILE, _DIR))
    for parts, node, _ in records:
        if node.kind == _LINK:
            continue
        cur = root
        for part in parts[1:-1]:
            cur = cur.children[unicodedata.normalize("NFC", part).casefold()]
            _require(cur.kind == _DIR)


def _write_tree(snapshot: Path, records: list[tuple[list[str], _Node, tarfile.TarInfo]], destination: Path) -> None:
    root = destination
    _require(root.is_dir() and not any(root.iterdir()))
    root.chmod(0o755)
    dirs: set[tuple[str, ...]] = {()}
    for parts, _, info in records:
        relative = parts[1:]
        for i in range(1, len(relative)):
            dirs.add(tuple(relative[:i]))
        if info.isdir():
            dirs.add(tuple(relative))
    for components in sorted(dirs, key=lambda p: (len(p), p)):
        if components:
            (root.joinpath(*components)).mkdir(mode=0o755, exist_ok=True)
    with tarfile.open(snapshot, "r:") as tf:
        for parts, node, info in records:
            if node.kind != _FILE:
                continue
            relative = parts[1:]
            target = root.joinpath(*relative)
            source = tf.extractfile(info)
            _require(source is not None)
            with source, target.open("xb") as output:
                _copy_member(source, output, info.size)
            target.chmod(0o755 if info.mode & 0o111 else 0o644)
    for parts, node, _ in records:
        if node.kind == _LINK:
            target = root.joinpath(*parts[1:])
            target.symlink_to(node.target)


def _copy_member(source, output, size: int) -> None:
    remaining = size
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        _require(bool(chunk))
        output.write(chunk)
        remaining -= len(chunk)
    _require(not source.read(1))


def _validate_destination_path(destination: Path | str) -> None:
    raw = os.fspath(destination)
    _require(isinstance(raw, str))
    _require(not any(part in (".", "..") for part in raw.split(os.sep)))


def _check_no_overlap(source: Path, destination: Path) -> None:
    source_path = Path(os.path.abspath(source))
    destination_path = Path(os.path.abspath(destination))
    _require(source_path != destination_path)
    _require(
        not source_path.is_relative_to(destination_path)
        and not destination_path.is_relative_to(source_path)
    )


def verify_and_extract(archive: Path, destination: Path, *, sha256: str, size: int) -> None:
    _platform_check()
    _validate_destination_path(destination)
    source = Path(archive)
    destination = Path(os.path.abspath(destination))
    _check_no_overlap(source, destination)
    parent = _safe_parent(destination.parent)
    destination = parent / destination.name
    _require(not os.path.lexists(destination))
    scratch = Path(tempfile.mkdtemp(prefix=".engine-artifact-", dir=parent))
    staging: Path | None = None
    published_identity: tuple[int, int] | None = None
    try:
        os.chmod(scratch, 0o700)
        staging = Path(tempfile.mkdtemp(prefix=".engine-artifact-stage-", dir=parent))
        compressed, expanded = scratch / "source.gz", scratch / "expanded.tar"
        _snapshot(source, compressed, sha256, size)
        _decompress(compressed, expanded)
        _preflight(expanded)
        root, records = _manifest(expanded)
        _resolve_links(root, records)
        _write_tree(expanded, records, staging)
        shutil.rmtree(scratch)
        scratch = None
        _require(not os.path.lexists(destination))
        staging_info = os.stat(staging)
        _require(staging_info.st_dev == os.stat(parent).st_dev)
        published_identity = staging_info.st_dev, staging_info.st_ino
        try:
            os.rename(staging, destination)
        except OSError:
            if os.path.lexists(destination):
                existing = os.stat(destination, follow_symlinks=False)
                if (existing.st_dev, existing.st_ino) == published_identity:
                    shutil.rmtree(destination)
            raise
        staging = None
    except ArtifactError:
        raise
    except (OSError, EOFError, tarfile.TarError, UnicodeError, ValueError, zlib.error) as exc:
        raise ArtifactError("invalid artifact") from exc
    finally:
        cleanup_failed = False
        for owned in (staging, scratch):
            if owned is not None and os.path.lexists(owned):
                try:
                    shutil.rmtree(owned)
                except OSError:
                    cleanup_failed = True
        if cleanup_failed:
            raise ArtifactError("invalid artifact")


def main() -> int:
    class Parser(argparse.ArgumentParser):
        def error(self, _message: str) -> None:
            print("artifact verification failed", file=sys.stderr)
            raise SystemExit(2)

    parser = Parser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", required=True, type=int)
    args = parser.parse_args()
    try:
        _validate_destination_path(args.destination)
        verify_and_extract(Path(args.archive), Path(args.destination), sha256=args.sha256, size=args.size)
    except (ArtifactError, OSError, ValueError):
        print("artifact verification failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
