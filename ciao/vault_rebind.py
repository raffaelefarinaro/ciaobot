"""Operator-initiated, reversible rebind of one workspace to another notes folder (#3606).

The comparison (`preview`, `authoritative_preview`) is read-only. `apply`,
`reverse` and `recover` are the only writers, and they run only for a STOPPED
engine: they hold the engine's `server.lock` and the interprocess registry
claim, recheck the reviewed token, write a verified private backup and a
durable journal, then change exactly one field (`vault_root`) of one registry
row. Neither notes tree is ever copied, moved, deleted or modified; a cloud
client controls their synchronisation, and only one engine may write them.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import make_private_dir, mkstemp_private
from ciao.workspaces import WorkspaceRegistryBusy, WorkspaceRegistryOwnership, acquire_registry_at

STATE_DIRNAME = "vault-rebind"
_PENDING = "pending.json"
_OPERATION = re.compile(r"[0-9a-f]{32}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

NOTICE = (
    "Neither notes folder is copied, moved, deleted or changed. Your cloud client controls "
    "synchronisation; keep it from rewriting either folder while the engine is stopped, and "
    "run only one Ciaobot engine against these notes. Only the workspace's registry entry "
    "changes, and `vault-rebind reverse` can put the old folder back."
)


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

    Roots resolve as the engine resolves them (see `_resolve_root`); a legacy
    single-segment root is refused. Not apply authorization by itself.
    """
    install = _directory(install)
    if not _NAME.fullmatch(workspace):
        raise ValueError("invalid workspace")
    if _registry_bytes(install) != expected_registry:
        raise ValueError("registry does not match expected before-image")
    rows = _parse_rows(expected_registry)
    roots: dict[str, Path] = {}
    folded_names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid registry row")
        name, root = row.get("name"), row.get("vault_root")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name.casefold() in folded_names or not isinstance(root, str):
            raise ValueError("ambiguous registry")
        folded_names.add(name.casefold())
        roots[name] = _resolve_root(install, root)
    if workspace not in roots:
        raise ValueError("workspace is not registered")
    # The install's runtime directory holds the registry, locks, journal and
    # backups. The rest of the install (a default layout keeps its vault there)
    # is not protected, only the other workspaces' roots and the runtime.
    if _overlaps(roots[workspace], install / ".runtime"):
        raise ValueError("the workspace's notes root overlaps the install's runtime state")
    protected = [install / ".runtime", *(root for name, root in roots.items() if name != workspace)]
    result = preview(roots[workspace], destination, expected_registry=expected_registry, protected_roots=protected)
    if _registry_bytes(install) != expected_registry:
        raise ValueError("registry changed during preview")
    token = hashlib.sha256(json.dumps([workspace, result.token], separators=(",", ":")).encode()).hexdigest()
    return RebindPreview(result.source, result.destination, result.files, token)


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _lexical_root(install: Path, raw: str) -> Path:
    """Where the engine puts a registered `vault_root`, without touching the disk.

    Mirrors `Config._resolve_vault_root`: absolute is kept, `.` is the install,
    several relative segments are joined to the install. `..` is refused, and so
    is a single relative segment (a legacy layout whose meaning depends on the
    engine's configured vault folder).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("vault_root must not be empty")
    path = Path(raw.strip())
    if path.is_absolute():
        return path
    parts = [part for part in path.parts if part not in {".", ""}]
    if ".." in parts:
        raise ValueError("relative vault_root must not contain '..'")
    if len(parts) == 1:
        raise ValueError("a single-segment relative vault_root is a legacy layout and is not supported")
    return install.joinpath(*parts)


def _resolve_root(install: Path, raw: str) -> Path:
    """The registered root as a verified absolute directory (no symlinks, no traversal)."""
    return _directory(_lexical_root(install, raw))


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("registry has duplicate keys")
    return dict(pairs)


def _parse_rows(raw: bytes) -> list[dict[str, Any]]:
    try:
        rows = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid registry") from exc
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError("invalid registry")
    return rows


def _checkpoint(name: str) -> None:
    """Named point between durable steps; tests raise here to simulate a crash."""


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private(path: Path, data: bytes) -> None:
    """Durably publish `data` as a private file: temp, fsync, rename, fsync the directory."""
    fd, name = mkstemp_private(dir=path.parent, prefix=".rebind-", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _engine_stopped(runtime: Path) -> Iterator[None]:
    """Hold the engine's instance lock, which proves no engine is running."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(runtime / "server.lock", flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("the engine lock is not a regular file")
        try:
            lock_exclusive(fd, blocking=False)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise ValueError("the engine is running (or another rebind is); stop the engine first") from None
        try:
            yield
        finally:
            unlock(fd)
    finally:
        os.close(fd)


@contextmanager
def _exclusive(install: Path, roots: Sequence[Path]) -> Iterator[tuple[Path, WorkspaceRegistryOwnership]]:
    """Instance lock, then the interprocess registry claim; both released on exit.

    `roots` are the notes folders involved. The private state must not lie inside
    one, and that is checked before any lock file is created, so no file is ever
    created under a notes folder.
    """
    install = _directory(install)
    runtime = _directory(install / ".runtime")
    _require_state_outside(runtime / STATE_DIRNAME, *roots)
    with _engine_stopped(runtime):
        try:
            owner = acquire_registry_at(runtime)
        except WorkspaceRegistryBusy:
            raise ValueError("the workspace registry is busy; try again when its writer finishes") from None
        with owner:
            yield runtime, owner


def _state_dir(runtime: Path, *, create: bool) -> Path:
    state = runtime / STATE_DIRNAME
    if create:
        state.mkdir(mode=0o700, exist_ok=True)
    if state.is_symlink() or not state.is_dir():
        raise ValueError("rebind state directory is unavailable")
    make_private_dir(state)
    return state


def _require_state_outside(state: Path, *roots: Path) -> None:
    for root in roots:
        if root == state or root in state.parents:
            raise ValueError("a notes folder contains the install's private rebind state")


def _load_journal(path: Path) -> dict[str, Any]:
    """Read and validate a journal or completed record; any inconsistency refuses."""
    try:
        if path.is_symlink():
            raise ValueError("journal is a symlink")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("kind") not in {"rebind", "reverse"}:
            raise ValueError("journal has an unknown kind")
        if not isinstance(data["operation"], str) or not _OPERATION.fullmatch(data["operation"]):
            raise ValueError("journal has an invalid operation id")
        for key in ("workspace", "old_root", "new_root"):
            if not isinstance(data[key], str) or not data[key]:
                raise ValueError("journal is incomplete")
        for side in ("before", "after"):
            raw = base64.b64decode(data[side + "_b64"], validate=True)
            if hashlib.sha256(raw).hexdigest() != data[side + "_sha256"]:
                raise ValueError("journal image does not match its digest")
        if data["before_sha256"] == data["after_sha256"]:
            raise ValueError("journal images are identical")
        # The roots that drive reversal come from the digest-verified images, not
        # from the unauthenticated fields beside them.
        for field, side in (("old_root", "before"), ("new_root", "after")):
            if _row_root(_image(data, side), data["workspace"]) != data[field]:
                raise ValueError("journal binding does not match its recorded images")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"rebind journal is invalid; evidence kept in {path.parent}: {exc}") from exc
    return data


def _row_root(raw: bytes, workspace: str) -> str:
    matches = [row for row in _parse_rows(raw) if row.get("name") == workspace]
    root = matches[0].get("vault_root") if len(matches) == 1 else None
    if not isinstance(root, str) or not root:
        raise ValueError("recorded registry has no unique binding for the workspace")
    return root


def _image(journal: dict[str, Any], side: str) -> bytes:
    return base64.b64decode(journal[side + "_b64"], validate=True)


def _finish(state: Path, journal: dict[str, Any]) -> None:
    """Record completion durably, then drop the pending journal (idempotent)."""
    _write_private(state / f"done-{journal['operation']}.json", json.dumps(dict(journal, completed=True), sort_keys=True).encode())
    _checkpoint("after_record")
    (state / _PENDING).unlink(missing_ok=True)
    _sync_dir(state)


def _reversal_record(runtime: Path, workspace: str, operation: str) -> dict[str, Any]:
    if not _OPERATION.fullmatch(operation):
        raise ValueError("invalid rebind id")
    state = runtime / STATE_DIRNAME
    if not state.is_dir():
        raise ValueError("no such rebind record")
    path = state / f"done-{operation}.json"
    if not path.is_file():
        raise ValueError("no such rebind record")
    record = _load_journal(path)
    if record["kind"] != "rebind" or record["workspace"] != workspace:
        raise ValueError("only a completed rebind of this workspace can be reversed")
    for other in state.glob("done-*.json"):
        if _load_journal(other).get("reverses") == operation:
            raise ValueError("this rebind was already reversed")
    return record


def review(install: Path, workspace: str, destination: Path) -> RebindPreview:
    """Read-only: the exact binding change and the token that authorises it."""
    install = _directory(install)
    return authoritative_preview(install, workspace, destination, expected_registry=_registry_bytes(install))


def review_reverse(install: Path, workspace: str, operation: str) -> RebindPreview:
    """Read-only preview of putting a completed rebind's old folder back."""
    install = _directory(install)
    record = _reversal_record(install / ".runtime", workspace, operation)
    plan = authoritative_preview(install, workspace, _resolve_root(install, record["old_root"]), expected_registry=_registry_bytes(install))
    if plan.source != Path(record["new_root"]):
        raise ValueError("the workspace no longer uses the folder this rebind set")
    return plan


def _current_root(install: Path, workspace: str) -> Path:
    """The workspace's registered root from an unlocked read (advisory; rechecked under the locks)."""
    install = _directory(install)
    return _lexical_root(install, _row_root(_registry_bytes(install), workspace))


def _transact(install: Path, kind: str, workspace: str, destination: Path | None, token: str, operation: str | None) -> dict[str, Any]:
    if operation:
        record = _reversal_record(_directory(install) / ".runtime", workspace, operation)
        roots = [_lexical_root(_directory(install), record["old_root"]), _lexical_root(_directory(install), record["new_root"])]
    else:
        assert destination is not None
        roots = [_current_root(install, workspace), destination]
    with _exclusive(install, roots) as (runtime, owner):
        pending = runtime / STATE_DIRNAME / _PENDING
        if pending.exists() or pending.is_symlink():
            raise ValueError("an interrupted rebind is pending; run `ciao vault-rebind recover` first")
        reversed_record = _reversal_record(runtime, workspace, operation) if operation else None
        if reversed_record is not None:
            destination = _resolve_root(install, reversed_record["old_root"])
        assert destination is not None
        before = _registry_bytes(install)

        def verify() -> RebindPreview:
            plan = authoritative_preview(install, workspace, destination, expected_registry=before)
            if reversed_record is not None and plan.source != Path(reversed_record["new_root"]):
                raise ValueError("the workspace no longer uses the folder this rebind set")
            return plan

        plan = verify()
        if not hmac.compare_digest(plan.token, token):
            raise ValueError("the reviewed token no longer matches the registry or notes; review again")
        state = runtime / STATE_DIRNAME
        _require_state_outside(state, plan.source, plan.destination)
        # The field is written absolute for a rebind; a reversal puts back the
        # exact value the rebind replaced (a default install stores it relative).
        old_value = _row_root(before, workspace)
        new_value = reversed_record["old_root"] if reversed_record is not None else str(plan.destination)
        rows = repoint_registry(_parse_rows(before), workspace, expected_root=old_value, new_root=new_value)
        after = (json.dumps(rows, indent=2) + "\n").encode()
        state = _state_dir(runtime, create=True)
        op = secrets.token_hex(16)
        backup = state / f"{op}.before.json"
        _write_private(backup, before)
        if backup.read_bytes() != before or stat.S_IMODE(backup.stat().st_mode) != 0o600:
            backup.unlink(missing_ok=True)
            raise ValueError("the registry backup could not be verified; nothing was changed")
        _checkpoint("after_backup")
        journal: dict[str, Any] = {
            "version": 1, "kind": kind, "operation": op, "workspace": workspace,
            "old_root": old_value, "new_root": new_value, "reverses": operation,
            "before_sha256": hashlib.sha256(before).hexdigest(), "after_sha256": hashlib.sha256(after).hexdigest(),
            "before_b64": base64.b64encode(before).decode(), "after_b64": base64.b64encode(after).decode(),
        }
        _write_private(state / _PENDING, json.dumps(journal, sort_keys=True).encode())
        _checkpoint("after_journal")
        try:
            if verify().token != plan.token:
                raise ValueError("notes changed while applying; nothing was changed")
        except ValueError:
            (state / _PENDING).unlink(missing_ok=True)
            _sync_dir(state)
            raise
        _checkpoint("before_replace")
        try:
            owner.replace(after, expected=before)
        except RuntimeError as exc:
            raise ValueError(f"the registry changed while applying; run `ciao vault-rebind recover`: {exc}") from exc
        _sync_dir(runtime)
        _checkpoint("after_replace")
        _finish(state, journal)
        return {"state": "applied", "operation": op, "workspace": workspace, "from": journal["old_root"], "to": journal["new_root"], "notice": NOTICE}


def apply(install: Path, workspace: str, destination: Path, *, token: str) -> dict[str, Any]:
    """Rebind `workspace` to `destination` for a stopped engine, only with the reviewed token."""
    return _transact(install, "rebind", workspace, destination, token, None)


def reverse(install: Path, workspace: str, operation: str, *, token: str) -> dict[str, Any]:
    """Point `workspace` back at a completed rebind's old folder, changing only that field."""
    return _transact(install, "reverse", workspace, None, token, operation)


def recover(install: Path) -> dict[str, Any]:
    """Resolve an interrupted operation from the recorded before and after images."""
    pending_path = _directory(install) / ".runtime" / STATE_DIRNAME / _PENDING
    roots: list[Path] = []
    if pending_path.exists() and not pending_path.is_symlink():
        pending_journal = _load_journal(pending_path)
        base = _directory(install)
        roots = [_lexical_root(base, pending_journal["old_root"]), _lexical_root(base, pending_journal["new_root"])]
    with _exclusive(install, roots) as (runtime, _owner):
        state = runtime / STATE_DIRNAME
        pending = state / _PENDING
        if not pending.exists() and not pending.is_symlink():
            return {"state": "none"}
        state = _state_dir(runtime, create=False)
        journal = _load_journal(pending)
        outcome = recovery_state(_registry_bytes(_directory(install)), before=_image(journal, "before"), after=_image(journal, "after"))
        if outcome == "before":
            pending.unlink()
            _sync_dir(state)
            return {"state": "not_applied", "operation": journal["operation"]}
        _finish(state, journal)
        return {"state": "applied", "operation": journal["operation"]}


def run_cli(args: argparse.Namespace) -> int:
    """`ciao vault-rebind`: JSON on stdout; a refusal prints the reason and exits 1."""
    install = Path(os.path.abspath(Path(args.root).expanduser()))
    action = args.vault_rebind_action
    try:
        if action in {"preview", "apply"}:
            destination = Path(os.path.abspath(Path(args.destination).expanduser()))
            if action == "apply":
                result = apply(install, args.name, destination, token=args.token)
            else:
                plan = review(install, args.name, destination)
                result = {"workspace": args.name, "from": str(plan.source), "to": str(plan.destination), "files": len(plan.files), "token": plan.token, "notice": NOTICE}
        elif action in {"reverse-preview", "reverse"}:
            if action == "reverse":
                result = reverse(install, args.name, args.operation, token=args.token)
            else:
                plan = review_reverse(install, args.name, args.operation)
                result = {"workspace": args.name, "from": str(plan.source), "to": str(plan.destination), "files": len(plan.files), "token": plan.token, "notice": NOTICE}
        else:
            result = recover(install)
    except (ValueError, OSError) as exc:
        print(f"vault-rebind refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0
