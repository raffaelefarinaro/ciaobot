"""Receipt-backed batch drain of legacy ``[learnings]`` queue rows."""
from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ciao.memory_proposals import (
    dismissed_log_path,
    learnings_path,
    record_promotion,
    render_learning_append,
)
from ciao.file_kernel import content_revision, queue_lock
from ciao.os_support.private import is_private_descriptor_strict, mkstemp_private

SCHEMA = 2
RECEIPT_PREFIX = "learnings-queue-drain-"
DISMISS_PREFIX = "learnings-row-dismiss-"
RECEIPT_DIRNAME = "learnings-queue-drain"
MAX_RECEIPT_BYTES = 12 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_ROWS = 1000
_PINNED_WORKSPACE: ContextVar[tuple[Path, int] | None] = ContextVar("drain_workspace_fd", default=None)

# Independent copy of the proposal bullet grammar. Group order matches the
# historical queue grammar: kind payload, text, source, request, decider, reason.
ROW_RE = re.compile(
    r"^\s*-\s*\[learnings(?:[ \t]+([^\]]*))?\]\s+(.+?)"
    r"(?:\s+_\(from:\s*(.+?)\)_)?"
    r"(?:\s+_\(request:\s*(.+?)\)_)?"
    r"(?:\s+_\(decide:\s*(nightly|you)(?:;\s*(.+?))?\)_)?\s*$",
    re.IGNORECASE,
)

_BASE_FIELDS = {
    "schema", "status", "workspace", "vault_identity", "run_id", "prepared_at",
    "today", "queue_before_exists", "queue_before", "queue_after",
    "queue_revision_before", "queue_revision_after", "learning_before_exists",
    "learning_before", "learning_after", "learning_revision_before",
    "learning_revision_after", "rows",
}
_REVERT_FIELDS = {"revert_at", "revert_operation_ids"}


def _queue(vault: Path) -> Path:
    return vault / "Workspace" / "Memory-Proposals.md"


def pending_rows(
    vault_root: Path, *, workspace: str, runtime_root: Path
) -> dict[str, Any]:
    """List legacy rows with an identity tied to the exact queue revision."""
    vault = Path(vault_root).resolve()
    path = _queue(vault)
    try:
        with queue_lock(path):
            receipts, errors = _read_receipt_set(runtime_root, workspace=workspace, vault=vault)
            if errors:
                raise ValueError("could not safely inspect migration receipts: " + "; ".join(errors))
            reverted_raw = {
                row["raw"]
                for _receipt_path, receipt in receipts
                if receipt.get("workspace") == workspace
                and receipt.get("vault_identity") == str(vault)
                and receipt.get("status") == "reverted"
                for row in receipt["rows"]
            }
            exists, text = _safe_file(path)
            if not exists:
                return {"revision": content_revision(""), "rows": [], "suppressed_rows": []}
            revision = content_revision(text)
            rows: list[dict[str, Any]] = []
            suppressed: list[dict[str, Any]] = []
            for index, raw in enumerate(_queue_lines(text)):
                parsed = _parse_row(raw)
                if parsed is None:
                    continue
                identity = hashlib.sha256(
                    f"{workspace}\0{revision}\0{index}\0{raw}".encode("utf-8")
                ).hexdigest()[:24]
                item = {"id": identity, "revision": revision, **parsed}
                (suppressed if raw in reverted_raw else rows).append(item)
            visible_ids = {row["id"] for row in rows}
            dismissal_receipts, dismissal_errors = _dismissal_receipts(
                runtime_root, workspace=workspace, vault=vault
            )
            if dismissal_errors:
                raise ValueError("could not safely inspect explicit removal receipts: " + "; ".join(dismissal_errors))
            for _receipt_path, receipt in dismissal_receipts:
                if receipt["status"] != "prepared" or receipt["target"] != "queue":
                    continue
                if receipt["row_id"] in visible_ids:
                    for item in rows:
                        if item["id"] == receipt["row_id"]:
                            item["removal_pending"] = True
                    continue
                else:
                    rows.append({
                        "id": receipt["row_id"],
                        "revision": revision,
                        **receipt["source_row"],
                        "removal_pending": True,
                    })
            return {"revision": revision, "rows": rows, "suppressed_rows": suppressed}
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        return {"revision": "", "rows": [], "failed": [str(exc)]}


def dismiss_pending_row(
    vault_root: Path,
    *,
    workspace: str,
    runtime_root: Path,
    row_id: str,
    expected_revision: str,
) -> dict[str, Any]:
    """Dismiss one legacy row only from the exact queue version the user saw."""
    vault = Path(vault_root).resolve()
    queue = _queue(vault)
    pin = _pin_workspace(vault)
    pinned = False
    try:
        pin.__enter__()
        pinned = True
        workspace_dir = vault / "Workspace"
        if workspace_dir.is_symlink() or not workspace_dir.is_dir():
            raise ValueError("Workspace must be a real directory inside the vault")
        with queue_lock(queue):
            receipts, receipt_errors = _read_receipt_set(
                runtime_root, workspace=workspace, vault=vault
            )
            unresolved = list(receipt_errors)
            dismissals, dismissal_errors = _dismissal_receipts(
                runtime_root, workspace=workspace, vault=vault
            )
            unresolved.extend(dismissal_errors)
            unresolved.extend(
                f"{path.name}: queued-row removal needs finishing"
                for path, receipt in dismissals
                if receipt["status"] == "prepared"
                and not (receipt.get("target") == "queue" and receipt.get("row_id") == row_id)
            )
            unresolved.extend(
                f"{path.name}: transaction is {receipt['status']}"
                for path, receipt in receipts
                if receipt.get("workspace") == workspace
                and receipt.get("vault_identity") == str(vault)
                and receipt.get("status") in {"prepared", "reverting"}
            )
            if unresolved:
                return {"dismissed": False, "conflict": False, "failed": unresolved}
            exists, before = _safe_file(queue, required=True)
            revision = content_revision(before)
            if revision != expected_revision:
                return {"dismissed": False, "conflict": True, "revision": revision}
            pending = next(
                (
                    (path, receipt)
                    for path, receipt in dismissals
                    if receipt["status"] == "prepared"
                    and receipt.get("target") == "queue"
                    and receipt.get("row_id") == row_id
                ),
                None,
            )
            if pending is not None:
                pending_path, pending_receipt = pending
                _finish_row_dismissal(
                    pending_path, pending_receipt, vault=vault
                )
                return {"dismissed": True, "conflict": False, "revision": pending_receipt["after_revision"]}
            lines = _queue_lines(before)
            found: int | None = None
            selected: dict[str, Any] | None = None
            for index, raw in enumerate(lines):
                parsed = _parse_row(raw)
                if parsed is None:
                    continue
                identity = hashlib.sha256(
                    f"{workspace}\0{revision}\0{index}\0{raw}".encode("utf-8")
                ).hexdigest()[:24]
                if identity == row_id:
                    if found is not None:
                        raise ValueError("legacy row identity is ambiguous")
                    found, selected = index, parsed
            if found is None or selected is None:
                return {"dismissed": False, "conflict": True, "revision": revision}
            after = "".join(lines[:found] + lines[found + 1 :])
            _safe_file(dismissed_log_path(queue))
            history_at = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            dismissal = {
                "schema": 1,
                "status": "prepared",
                "workspace": workspace,
                "vault_identity": str(vault),
                "operation_id": str(uuid.uuid4()),
                "row_id": row_id,
                "target": "queue",
                "before": before,
                "after": after,
                "before_revision": revision,
                "after_revision": content_revision(after),
                "history_at": history_at,
                "source_row": selected,
            }
            receipt_path = _receipt_dir(runtime_root) / f"{DISMISS_PREFIX}{uuid.uuid4()}.json"
            _write_receipt(receipt_path, dismissal, create=True)
            _finish_row_dismissal(receipt_path, dismissal, vault=vault)
            return {"dismissed": True, "conflict": False, "revision": content_revision(after)}
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        return {"dismissed": False, "conflict": False, "failed": [str(exc)]}
    finally:
        if pinned:
            pin.__exit__(None, None, None)


@contextmanager
def _pin_workspace(vault: Path):
    """Pin Workspace for a transaction; path replacement aborts publication."""
    workspace = vault / "Workspace"
    fd = os.open(workspace, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise ValueError("Workspace must be a real directory")
    token = _PINNED_WORKSPACE.set((workspace, fd))
    try:
        yield
    finally:
        _PINNED_WORKSPACE.reset(token)
        os.close(fd)


def _workspace_dir_fd(parent: Path) -> tuple[int, bool]:
    pinned = _PINNED_WORKSPACE.get()
    if pinned is not None and pinned[0] == parent:
        return pinned[1], False
    fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    return fd, True


def _verify_pinned_workspace(parent: Path, fd: int) -> None:
    pinned = _PINNED_WORKSPACE.get()
    if pinned is None or pinned != (parent, fd):
        return
    try:
        current = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        raise ValueError("Workspace was replaced during the transaction") from exc
    pinned_info = os.fstat(fd)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (pinned_info.st_dev, pinned_info.st_ino):
        raise ValueError("Workspace was replaced during the transaction")


def _queue_lines(text: str) -> list[str]:
    """Split only on LF, preserving CRLF and Unicode separators in proposal text."""
    parts = text.split("\n")
    return [*(part + "\n" for part in parts[:-1]), parts[-1]]


def _receipt_dir(runtime_root: Path) -> Path:
    return Path(runtime_root).resolve() / "migration" / RECEIPT_DIRNAME


def _private_dir(path: Path, *, create: bool) -> bool:
    """Validate the dedicated receipt directory without following a symlink."""
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except FileNotFoundError:
        if not create:
            return False
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_info = parent.lstat()
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise ValueError("migration receipt parent is not a real directory")
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return False
        path.mkdir(mode=0o700)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("drain receipt path is not a real directory")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if create:
            os.fchmod(fd, 0o700)
        if not is_private_descriptor_strict(fd):
            raise ValueError("drain receipt directory must be owner-only")
    finally:
        os.close(fd)
    return True


def _safe_file(path: Path, *, required: bool = False) -> tuple[bool, str]:
    try:
        parent_fd, owned_fd = _workspace_dir_fd(path.parent)
    except FileNotFoundError:
        if required:
            raise ValueError(f"{path.name} is missing")
        return False, ""
    try:
        _verify_pinned_workspace(path.parent, parent_fd)
        try:
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if required:
                raise ValueError(f"{path.name} is missing")
            return False, ""
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path.name} must be a regular file, not a symlink")
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
        try:
            current = os.fstat(fd)
            if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError(f"{path.name} changed while it was being read")
            with os.fdopen(os.dup(fd), "rb") as handle:
                return True, handle.read().decode("utf-8")
        finally:
            os.close(fd)
    finally:
        if owned_fd:
            os.close(parent_fd)


def _assert_workspace_paths(vault: Path) -> None:
    workspace_dir = vault / "Workspace"
    try:
        info = workspace_dir.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("Workspace must be a real directory inside the vault")
    for path in (_queue(vault), learnings_path(vault), dismissed_log_path(_queue(vault))):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{path.name} must be a regular file")


def _atomic_file(path: Path, text: str) -> None:
    """Replace one destination through a unique private sibling temp file."""
    parent_fd, owned_fd = _workspace_dir_fd(path.parent)
    try:
        destination = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        destination_mode = 0o600
    else:
        if not stat.S_ISREG(destination.st_mode):
            if owned_fd:
                os.close(parent_fd)
            raise ValueError(f"{path.name} must be a regular file")
        destination_mode = stat.S_IMODE(destination.st_mode)
    temp_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
    except BaseException:
        if owned_fd:
            os.close(parent_fd)
        raise
    try:
        os.fchmod(fd, destination_mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        _verify_pinned_workspace(path.parent, parent_fd)
        os.replace(temp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        if owned_fd:
            os.close(parent_fd)


def _write_receipt(path: Path, value: dict[str, Any], *, create: bool = False) -> None:
    folder = path.parent
    if not _private_dir(folder, create=create):
        raise ValueError("drain receipt directory is unavailable")
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_RECEIPT_BYTES:
        raise ValueError("drain receipt exceeds its size limit")
    if create:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                view = view[count:]
            os.fsync(fd)
            if not is_private_descriptor_strict(fd):
                raise ValueError("drain receipt file must be owner-only")
        finally:
            os.close(fd)
        dir_fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    else:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("drain receipt must be a regular file")
        fd, name = mkstemp_private(dir=folder, prefix=".receipt-", suffix=".tmp")
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            dir_fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise


def _dismissal_receipts(
    runtime_root: Path, *, workspace: str, vault: Path
) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    """Read exact-scope queued-row removal intents; prepared ones need finishing."""
    folder = _receipt_dir(runtime_root)
    try:
        if not _private_dir(folder, create=False):
            return [], []
    except (OSError, ValueError) as exc:
        return [], [str(exc)]
    receipts: list[tuple[Path, dict[str, Any]]] = []
    errors: list[str] = []
    requested_vault = vault.resolve()
    for path in sorted(folder.glob(f"{DISMISS_PREFIX}*.json")):
        try:
            row = _load_receipt(path, runtime_root=runtime_root)
            receipt_workspace = row.get("workspace")
            vault_identity = row.get("vault_identity")
            if not isinstance(receipt_workspace, str) or not receipt_workspace:
                raise ValueError("queued-row removal workspace identity is invalid")
            if not isinstance(vault_identity, str):
                raise ValueError("queued-row removal vault identity is invalid")
            receipt_vault = Path(vault_identity)
            if (
                not receipt_vault.is_absolute()
                or str(receipt_vault.resolve()) != vault_identity
            ):
                raise ValueError("queued-row removal vault identity is not canonical")
            if receipt_workspace != workspace or vault_identity != str(requested_vault):
                continue
            if (
                set(row) != {
                    "schema", "status", "workspace", "vault_identity", "operation_id",
                    "row_id", "target", "before", "after", "before_revision",
                    "after_revision", "history_at", "source_row",
                }
                or type(row.get("schema")) is not int
                or row.get("schema") != 1
                or row.get("status") not in {"prepared", "applied"}
                or row.get("target") != "queue"
                or not isinstance(row.get("operation_id"), str)
                or not isinstance(row.get("row_id"), str)
                or not isinstance(row.get("before"), str)
                or not isinstance(row.get("after"), str)
                or not isinstance(row.get("history_at"), str)
                or not isinstance(row.get("source_row"), dict)
                or content_revision(row["before"]) != row.get("before_revision")
                or content_revision(row["after"]) != row.get("after_revision")
            ):
                raise ValueError("queued-row removal receipt is malformed")
            try:
                uuid.UUID(row["operation_id"])
                datetime.fromisoformat(row["history_at"].replace("Z", "+00:00"))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("queued-row removal identity or timestamp is invalid") from exc
            lines = _queue_lines(row["before"])
            indices = [
                index for index, raw in enumerate(lines)
                if hashlib.sha256(
                    f"{workspace}\0{row['before_revision']}\0{index}\0{raw}".encode("utf-8")
                ).hexdigest()[:24] == row["row_id"]
            ]
            if len(indices) != 1 or _parse_row(lines[indices[0]]) != row["source_row"]:
                raise ValueError("queued-row removal identity does not match its before image")
            expected_after = "".join(lines[:indices[0]] + lines[indices[0] + 1 :])
            if expected_after != row["after"]:
                raise ValueError("queued-row removal after image is invalid")
            receipts.append((path, row))
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {exc}")
    return receipts, errors


def _finish_row_dismissal(
    path: Path, receipt: dict[str, Any], *, vault: Path
) -> None:
    """Finish one exact queued-row removal from its saved file images."""
    queue = _queue(vault)
    before = (True, receipt["before"])
    after = (True, receipt["after"])
    current = _safe_file(queue, required=True)
    if current == before:
        _atomic_file(queue, receipt["after"])
    elif current != after:
        raise ValueError("Memory Proposals changed after removal was prepared; refresh and review the row")
    row = receipt["source_row"]
    from ciao.memory_proposals import record_dismissal

    record_dismissal(
        queue,
        text=row["text"],
        kind="learnings",
        via="pwa",
        source=row["source"],
        outcome="dismissed",
        reason="removed this exact queued learning row",
        operation_id=receipt["operation_id"],
        decision_at=receipt["history_at"],
        workspace_dir_fd=(
            pinned_workspace[1] if (pinned_workspace := _PINNED_WORKSPACE.get()) else None
        ),
    )
    receipt["status"] = "applied"
    _write_receipt(path, receipt)


def _load_receipt(path: Path, *, runtime_root: Path) -> dict[str, Any]:
    folder = _receipt_dir(runtime_root)
    if (
        path.parent.resolve() != folder.resolve()
        or not path.name.startswith((RECEIPT_PREFIX, DISMISS_PREFIX))
        or not _private_dir(folder, create=False)
    ):
        raise ValueError("receipt is outside the private drain receipt directory")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not is_private_descriptor_strict(fd):
            raise ValueError("drain receipt must be an owner-only file")
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(MAX_RECEIPT_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_RECEIPT_BYTES:
        raise ValueError("drain receipt exceeds its size limit")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("drain receipt is not an object")
    return value


def _parse_row(raw: str) -> dict[str, Any] | None:
    match = ROW_RE.fullmatch(raw.rstrip("\r\n"))
    if match is None:
        return None
    return {
        "target": (match.group(1) or "").strip(),
        "text": match.group(2).strip(),
        "source": (match.group(3) or "").strip(),
        "request": (match.group(4) or "").strip(),
        "decider": (match.group(5) or "").lower(),
        "decide_reason": (match.group(6) or "").strip(),
    }


def _after_images(receipt: dict[str, Any], *, workspace: str) -> tuple[str, str]:
    queue_before = receipt["queue_before"]
    lines = _queue_lines(queue_before)
    removed: set[int] = set()
    rows = receipt["rows"]
    if not 0 < len(rows) <= MAX_ROWS:
        raise ValueError("receipt has an invalid row count")
    learning = receipt["learning_before"]
    today = date.fromisoformat(receipt["today"]).isoformat()
    for ordinal, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {
            "ordinal", "queue_index", "raw", "target", "text", "source", "request",
            "decider", "decide_reason", "operation_id", "history_at",
        }:
            raise ValueError("receipt row has an invalid shape")
        index = row["queue_index"]
        if type(index) is not int or index < 0 or index >= len(lines) or index in removed:
            raise ValueError("receipt row index is invalid")
        if type(row["ordinal"]) is not int or row["ordinal"] != ordinal:
            raise ValueError("receipt row ordinal is invalid")
        if row["raw"] != lines[index]:
            raise ValueError("receipt row no longer matches its queue image")
        parsed = _parse_row(row["raw"])
        if parsed is None or parsed != {key: row[key] for key in parsed}:
            raise ValueError("receipt row fields do not match its raw queue line")
        if row["operation_id"] != f"{receipt['run_id']}:{ordinal}":
            raise ValueError("receipt row operation id is invalid")
        if row["history_at"] != receipt["prepared_at"]:
            raise ValueError("receipt row history timestamp is invalid")
        removed.add(index)
        learning, _operation = render_learning_append(
            learning,
            row["text"],
            workspace=workspace,
            source=row["source"],
            request=row["request"],
            today=today,
        )
    queue_after = "".join(line for index, line in enumerate(lines) if index not in removed)
    return queue_after, learning


def _validate_receipt(
    receipt: dict[str, Any], *, vault: Path, workspace: str, allow_reverting: bool = True
) -> None:
    status = receipt.get("status")
    # Check container/value types before membership, len(), indexing or string
    # methods. Receipts are untrusted private files and malformed JSON must be
    # reported as an unresolved transaction, never escape as TypeError.
    if not isinstance(status, str):
        raise ValueError("drain receipt status is invalid")
    expected_fields = _BASE_FIELDS | (_REVERT_FIELDS if status in {"reverting", "reverted"} else set())
    if set(receipt) != expected_fields:
        raise ValueError("drain receipt fields do not match the schema")
    if type(receipt.get("schema")) is not int or receipt["schema"] != SCHEMA or status not in {"prepared", "applied", "reverting", "reverted"}:
        raise ValueError("drain receipt schema or status is unsupported")
    if status == "reverting" and not allow_reverting:
        raise ValueError("drain receipt is already reverting")
    if receipt.get("workspace") != workspace or receipt.get("vault_identity") != str(vault):
        raise ValueError("drain receipt belongs to another workspace or vault")
    for name in ("run_id", "today", "prepared_at", "queue_revision_before", "queue_revision_after",
                 "learning_revision_before", "learning_revision_after"):
        if not isinstance(receipt.get(name), str):
            raise ValueError(f"drain receipt field is invalid: {name}")
    if not isinstance(receipt.get("rows"), list) or not 0 < len(receipt["rows"]) <= MAX_ROWS:
        raise ValueError("receipt has an invalid row count")
    if status in {"reverting", "reverted"}:
        if not isinstance(receipt.get("revert_at"), str) or not isinstance(receipt.get("revert_operation_ids"), list):
            raise ValueError("receipt revert metadata is invalid")
    try:
        uuid.UUID(receipt["run_id"])
        date.fromisoformat(receipt["today"])
        datetime.fromisoformat(receipt["prepared_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("drain receipt identity or timestamp is invalid") from exc
    if type(receipt["queue_before_exists"]) is not bool or not receipt["queue_before_exists"]:
        raise ValueError("receipt queue existence is invalid")
    if type(receipt["learning_before_exists"]) is not bool:
        raise ValueError("receipt Learnings existence is invalid")
    for name in ("queue_before", "queue_after", "learning_before", "learning_after"):
        if not isinstance(receipt[name], str) or len(receipt[name].encode("utf-8")) > MAX_IMAGE_BYTES:
            raise ValueError("receipt image is invalid or too large")
    queue_after, learning_after = _after_images(receipt, workspace=workspace)
    if queue_after != receipt["queue_after"] or learning_after != receipt["learning_after"]:
        raise ValueError("receipt after-images do not match the deterministic plan")
    for key, image in (
        ("queue_revision_before", receipt["queue_before"]),
        ("queue_revision_after", receipt["queue_after"]),
        ("learning_revision_before", receipt["learning_before"]),
        ("learning_revision_after", receipt["learning_after"]),
    ):
        if receipt[key] != content_revision(image):
            raise ValueError(f"receipt revision is invalid: {key}")
    if receipt["learning_before_exists"] and receipt["learning_before"] == "":
        # Existing empty and absent files are distinct states; the bytes alone
        # cannot establish which one a receipt means unless existence is saved.
        pass
    if not receipt["learning_before_exists"] and receipt["learning_before"]:
        raise ValueError("an absent Learnings file cannot have a before image")
    if status in {"reverting", "reverted"}:
        try:
            datetime.fromisoformat(receipt["revert_at"].replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("receipt revert timestamp is invalid") from exc
        expected = [f"{receipt['run_id']}:revert:{i}" for i in range(len(receipt["rows"]))]
        if receipt["revert_operation_ids"] != expected:
            raise ValueError("receipt revert operation ids are invalid")


def _expected_history(row: dict[str, Any], *, operation_id: str, at: str, outcome: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "promoted_at": at,
        "kind": "learnings",
        "text": row["text"],
        "via": "migration",
    }
    if row["source"]:
        entry["source"] = row["source"]
    if row["request"]:
        entry["request"] = row["request"]
    if row["decider"]:
        entry["decider"] = row["decider"]
    if row["decide_reason"]:
        entry["decide_reason"] = row["decide_reason"]
    entry["destination"] = "Workspace/Learnings.md"
    entry["outcome"] = outcome
    entry["operation_id"] = operation_id
    if outcome == "reverted":
        entry["reverses_operation_id"] = row["operation_id"]
    entry["history_only"] = True
    return entry


def _history_state(path: Path, expected: dict[str, Any]) -> str:
    exists, text = _safe_file(path)
    if not exists:
        return "missing"
    raw = text.encode("utf-8")
    wanted = (json.dumps(expected, ensure_ascii=False) + "\n").encode("utf-8")
    for line in raw.split(b"\n"):
        try:
            item = json.loads(line)
        except (UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("operation_id") == expected["operation_id"]:
            return "same" if item == expected else "conflict"
    if raw and not raw.endswith(b"\n") and wanted.startswith(raw.rsplit(b"\n", 1)[-1]):
        return "partial"
    return "missing"


def _history_event(vault: Path, row: dict[str, Any], *, operation_id: str, at: str, outcome: str) -> None:
    pinned = _PINNED_WORKSPACE.get()
    if pinned is None or pinned[0] != vault / "Workspace":
        raise ValueError("History publication requires a pinned Workspace")
    _verify_pinned_workspace(pinned[0], pinned[1])
    record_promotion(
        _queue(vault),
        text=row["text"],
        kind="learnings",
        via="migration",
        source=row["source"],
        request=row["request"],
        decider=row["decider"],
        decide_reason=row["decide_reason"],
        destination="Workspace/Learnings.md",
        outcome=outcome,
        operation_id=operation_id,
        reverses_operation_id=row["operation_id"] if outcome == "reverted" else "",
        workspace_dir_fd=pinned[1],
        history_only=True,
        decision_at=at,
    )


def _preflight_files(vault: Path, receipt: dict[str, Any], *, forward: bool) -> None:
    queue_path = _queue(vault)
    learning_path = learnings_path(vault)
    queue_before = (True, receipt["queue_before"])
    queue_after = (True, receipt["queue_after"])
    learning_before = (receipt["learning_before_exists"], receipt["learning_before"])
    learning_after = (True, receipt["learning_after"])
    allowed = ((queue_before, queue_after), (learning_before, learning_after))
    for path, (before, after) in zip((queue_path, learning_path), allowed):
        current = _safe_file(path)
        if current not in (before, after):
            phase = "apply" if forward else "revert"
            raise ValueError(f"{path.name} changed; refusing to {phase} the receipt")


def _preflight_forward(vault: Path, receipt: dict[str, Any], *, workspace: str) -> None:
    _assert_workspace_paths(vault)
    _validate_receipt(receipt, vault=vault, workspace=workspace)
    _preflight_files(vault, receipt, forward=True)
    history_path = dismissed_log_path(_queue(vault))
    for row in receipt["rows"]:
        expected = _expected_history(
            row, operation_id=row["operation_id"], at=row["history_at"], outcome="accepted"
        )
        if _history_state(history_path, expected) == "conflict":
            raise ValueError("History contains a conflicting drain operation id")


def _preflight_revert(vault: Path, receipt: dict[str, Any], *, workspace: str) -> None:
    _assert_workspace_paths(vault)
    _validate_receipt(receipt, vault=vault, workspace=workspace)
    _preflight_files(vault, receipt, forward=False)
    queue_path = _queue(vault)
    history_path = dismissed_log_path(queue_path)
    for row in receipt["rows"]:
        accepted = _expected_history(
            row, operation_id=row["operation_id"], at=row["history_at"], outcome="accepted"
        )
        if _history_state(history_path, accepted) not in {"same"}:
            raise ValueError("History is missing or conflicts with a batch decision")
    if receipt["status"] in {"reverting", "reverted"}:
        for row, opid in zip(receipt["rows"], receipt["revert_operation_ids"]):
            reversal = _expected_history(
                row, operation_id=opid, at=receipt["revert_at"], outcome="reverted"
            )
            if _history_state(history_path, reversal) == "conflict":
                raise ValueError("History contains a conflicting revert operation id")


def _write_image(path: Path, before: tuple[bool, str], after: tuple[bool, str]) -> None:
    current = _safe_file(path)
    if current == after:
        return
    if current != before:
        raise ValueError(f"{path.name} changed after transaction preflight")
    if after[0]:
        _atomic_file(path, after[1])
    else:
        parent_fd, owned_fd = _workspace_dir_fd(path.parent)
        try:
            _verify_pinned_workspace(path.parent, parent_fd)
            try:
                os.unlink(path.name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.fsync(parent_fd)
        finally:
            if owned_fd:
                os.close(parent_fd)


def _resume_forward(vault: Path, receipt: dict[str, Any], receipt_path: Path, *, workspace: str, runtime_root: Path) -> None:
    _preflight_forward(vault, receipt, workspace=workspace)
    queue_path, learning_path = _queue(vault), learnings_path(vault)
    _write_image(
        learning_path,
        (receipt["learning_before_exists"], receipt["learning_before"]),
        (True, receipt["learning_after"]),
    )
    _write_image(queue_path, (True, receipt["queue_before"]), (True, receipt["queue_after"]))
    history_path = dismissed_log_path(queue_path)
    for row in receipt["rows"]:
        expected = _expected_history(
            row, operation_id=row["operation_id"], at=row["history_at"], outcome="accepted"
        )
        state = _history_state(history_path, expected)
        if state == "conflict":
            raise ValueError("History contains a conflicting drain operation id")
        if state != "same":
            _history_event(
                vault, row, operation_id=row["operation_id"], at=row["history_at"], outcome="accepted"
            )
    receipt["status"] = "applied"
    _write_receipt(receipt_path, receipt)


def _resume_revert(vault: Path, receipt: dict[str, Any], receipt_path: Path, *, workspace: str) -> None:
    _preflight_revert(vault, receipt, workspace=workspace)
    queue_path, learning_path = _queue(vault), learnings_path(vault)
    _write_image(learning_path, (True, receipt["learning_after"]), (receipt["learning_before_exists"], receipt["learning_before"]))
    _write_image(queue_path, (True, receipt["queue_after"]), (True, receipt["queue_before"]))
    for row, opid in zip(receipt["rows"], receipt["revert_operation_ids"]):
        expected = _expected_history(row, operation_id=opid, at=receipt["revert_at"], outcome="reverted")
        state = _history_state(dismissed_log_path(queue_path), expected)
        if state == "conflict":
            raise ValueError("History contains a conflicting revert operation id")
        if state != "same":
            _history_event(vault, row, operation_id=opid, at=receipt["revert_at"], outcome="reverted")
    receipt["status"] = "reverted"
    _write_receipt(receipt_path, receipt)


def _read_receipt_set(
    runtime_root: Path, *, workspace: str, vault: Path
) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    requested_workspace = workspace
    requested_vault = vault.resolve()
    folder = _receipt_dir(runtime_root)
    try:
        if not _private_dir(folder, create=False):
            return [], []
    except (OSError, ValueError) as exc:
        return [], [str(exc)]
    receipts: list[tuple[Path, dict[str, Any]]] = []
    errors: list[str] = []
    for path in sorted(folder.glob(f"{RECEIPT_PREFIX}*.json")):
        try:
            receipt = _load_receipt(path, runtime_root=runtime_root)
            receipt_workspace = receipt.get("workspace")
            vault_identity = receipt.get("vault_identity")
            if not isinstance(receipt_workspace, str) or not receipt_workspace or not isinstance(vault_identity, str):
                raise ValueError("receipt workspace identity is invalid")
            receipt_vault = Path(vault_identity)
            if not receipt_vault.is_absolute() or str(receipt_vault.resolve()) != vault_identity:
                raise ValueError("receipt vault identity is not canonical")
            _validate_receipt(receipt, vault=receipt_vault, workspace=receipt_workspace)
            if receipt_workspace == requested_workspace and vault_identity == str(requested_vault):
                receipts.append((path, receipt))
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            # Preserve an identifiable damaged receipt's scope. An unreadable
            # receipt with no recoverable identity remains globally unknown.
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    if not is_private_descriptor_strict(fd):
                        raise ValueError("receipt must be owner-only")
                    with os.fdopen(os.dup(fd), "rb") as handle:
                        data = handle.read(MAX_RECEIPT_BYTES + 1)
                finally:
                    os.close(fd)
                partial = json.loads(data.decode("utf-8")) if len(data) <= MAX_RECEIPT_BYTES else None
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                partial = None
            if isinstance(partial, dict):
                named_workspace = partial.get("workspace")
                named_vault = partial.get("vault_identity")
                if isinstance(named_workspace, str) and isinstance(named_vault, str):
                    try:
                        identity = Path(named_vault)
                        if (
                            named_workspace != requested_workspace
                            or identity.resolve() != requested_vault
                        ):
                            continue
                    except (OSError, RuntimeError):
                        pass
            errors.append(f"{path.name}: {exc}")
    return receipts, errors


def inspect(vault_root: Path, *, workspace: str, runtime_root: Path) -> dict[str, Any]:
    """Read-only preview, including pending/corrupt receipts and an empty queue."""
    vault = Path(vault_root).resolve()
    summary: dict[str, Any] = {
        "workspace": workspace,
        "rows": [],
        "receipts": [],
        "applied_receipts": [],
        "reverted_receipts": [],
        "automatic_rows": 0,
        "suppressed_rows": [],
        "failed": [],
        "unresolved": [],
    }
    try:
        _assert_workspace_paths(vault)
        queue_exists, queued = _safe_file(_queue(vault))
        queue_raw_rows = _queue_lines(queued) if queue_exists else []
        reverted_raw: set[str] = set()
        if queue_exists:
            for raw in queue_raw_rows:
                row = _parse_row(raw)
                if row is not None:
                    summary["rows"].append(row)
        receipts, errors = _read_receipt_set(runtime_root, workspace=workspace, vault=vault)
        summary["unresolved"].extend(errors)
        dismissals, dismissal_errors = _dismissal_receipts(
            runtime_root, workspace=workspace, vault=vault
        )
        summary["unresolved"].extend(dismissal_errors)
        summary["unresolved"].extend(
            f"{path.name}: queued-row removal needs finishing"
            for path, receipt in dismissals
            if receipt["status"] == "prepared"
        )
        for path, receipt in receipts:
            if receipt.get("workspace") != workspace or receipt.get("vault_identity") != str(vault):
                continue
            try:
                _validate_receipt(receipt, vault=vault, workspace=workspace)
            except (ValueError, KeyError, TypeError) as exc:
                summary["unresolved"].append(f"{path.name}: {exc}")
                continue
            if receipt["status"] in {"prepared", "reverting"}:
                summary["unresolved"].append(f"{path.name}: transaction is {receipt['status']}")
            elif receipt["status"] == "applied":
                summary["applied_receipts"].append(str(path))
            elif receipt["status"] == "reverted":
                reverted_raw.update(row["raw"] for row in receipt["rows"])
                summary["reverted_receipts"].append(str(path))
        summary["automatic_rows"] = sum(
            1 for raw in queue_raw_rows if _parse_row(raw) is not None and raw not in reverted_raw
        )
        summary["suppressed_rows"] = [
            parsed
            for raw in queue_raw_rows
            if raw in reverted_raw
            if (parsed := _parse_row(raw)) is not None
        ]
        summary["not_applicable"] = not summary["automatic_rows"] and not summary["unresolved"]
    except (OSError, UnicodeError, ValueError) as exc:
        summary["failed"].append(str(exc))
    return summary


def _prepare_batch(
    vault: Path,
    *,
    workspace: str,
    queue_text: str,
    learning_exists: bool,
    learning_text: str,
    excluded_raw: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    lines = _queue_lines(queue_text)
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(lines):
        parsed = _parse_row(raw)
        if parsed is None or raw in excluded_raw:
            continue
        rows.append({"ordinal": len(rows), "queue_index": index, "raw": raw, **parsed})
    if not rows:
        return None
    if len(rows) > MAX_ROWS:
        raise ValueError(f"queue has more than {MAX_ROWS} pending learnings rows")
    run_id = str(uuid.uuid4())
    at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    today = date.today().isoformat()
    for row in rows:
        row["operation_id"] = f"{run_id}:{row['ordinal']}"
        row["history_at"] = at
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "prepared",
        "workspace": workspace,
        "vault_identity": str(vault),
        "run_id": run_id,
        "prepared_at": at,
        "today": today,
        "queue_before_exists": True,
        "queue_before": queue_text,
        "queue_after": "",
        "queue_revision_before": content_revision(queue_text),
        "queue_revision_after": "",
        "learning_before_exists": learning_exists,
        "learning_before": learning_text,
        "learning_after": "",
        "learning_revision_before": content_revision(learning_text),
        "learning_revision_after": "",
        "rows": rows,
    }
    queue_after, learning_after = _after_images(receipt, workspace=workspace)
    receipt["queue_after"] = queue_after
    receipt["learning_after"] = learning_after
    receipt["queue_revision_after"] = content_revision(queue_after)
    receipt["learning_revision_after"] = content_revision(learning_after)
    return receipt


def _recover_locked(
    vault: Path, *, workspace: str, runtime_root: Path
) -> tuple[list[str], list[str]]:
    """Recover while the caller holds queue then Learnings locks."""
    dismissals, dismissal_errors = _dismissal_receipts(
        runtime_root, workspace=workspace, vault=vault
    )
    if dismissal_errors:
        return dismissal_errors, []
    pending_dismissals = [
        path.name for path, receipt in dismissals if receipt["status"] == "prepared"
    ]
    if pending_dismissals:
        return [
            f"{name}: queued-row removal needs finishing before migration recovery"
            for name in pending_dismissals
        ], []
    receipts, errors = _read_receipt_set(runtime_root, workspace=workspace, vault=vault)
    if errors:
        return errors, []
    completed: list[str] = []
    matching: list[tuple[Path, dict[str, Any]]] = []
    for path, _snapshot in receipts:
        try:
            receipt = _load_receipt(path, runtime_root=runtime_root)
            if receipt.get("workspace") != workspace or receipt.get("vault_identity") != str(vault):
                continue
            _validate_receipt(receipt, vault=vault, workspace=workspace)
            if receipt["status"] == "prepared":
                _preflight_forward(vault, receipt, workspace=workspace)
            elif receipt["status"] == "reverting":
                _preflight_revert(vault, receipt, workspace=workspace)
            matching.append((path, receipt))
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return [f"{path.name}: {exc}"], []
    for path, receipt in matching:
        try:
            if receipt["status"] == "prepared":
                _resume_forward(vault, receipt, path, workspace=workspace, runtime_root=runtime_root)
                completed.append(str(path))
            elif receipt["status"] == "reverting":
                _resume_revert(vault, receipt, path, workspace=workspace)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return [f"{path.name}: {exc}"], completed
    return [], completed


def recover(vault_root: Path, *, workspace: str, runtime_root: Path) -> list[str]:
    """Apply may resume only valid, matching prepared transactions."""
    vault = Path(vault_root).resolve()
    pin = _pin_workspace(vault)
    pinned = False
    try:
        assert pin is not None
        assert pin is not None
        assert pin is not None
        pin.__enter__()
        pinned = True
        with queue_lock(_queue(vault)):
            with queue_lock(learnings_path(vault)):
                errors, _completed = _recover_locked(
                    vault, workspace=workspace, runtime_root=runtime_root
                )
                return errors
    except (OSError, ValueError) as exc:
        return [str(exc)]
    finally:
        if pinned:
            pin.__exit__(None, None, None)


def drain(
    vault_root: Path, *, workspace: str, runtime_root: Path, apply: bool = False,
    fresh_only: bool = False,
) -> dict[str, Any]:
    """Preview, apply one batch, or apply only when no receipt needs recovery."""
    vault = Path(vault_root).resolve()
    if not apply:
        result = inspect(vault, workspace=workspace, runtime_root=runtime_root)
        if result["unresolved"]:
            result["failed"].extend(result["unresolved"])
        return result
    workspace_dir = vault / "Workspace"
    try:
        workspace_info = workspace_dir.lstat()
    except FileNotFoundError:
        inspected = inspect(vault, workspace=workspace, runtime_root=runtime_root)
        if inspected["unresolved"] or inspected["failed"]:
            errors = [*inspected["unresolved"], *inspected["failed"]]
            return {"workspace": workspace, "rows": [], "receipts": [], "failed": errors, "unresolved": errors}
        return {"workspace": workspace, "rows": [], "receipts": [], "failed": [], "not_applicable": True}
    if stat.S_ISLNK(workspace_info.st_mode) or not stat.S_ISDIR(workspace_info.st_mode):
        return {"workspace": workspace, "rows": [], "receipts": [], "failed": ["Workspace must be a real directory inside the vault"]}
    queue_path, learning_path = _queue(vault), learnings_path(vault)
    pin = _pin_workspace(vault)
    pinned = False
    try:
        pin.__enter__()
        pinned = True
        with queue_lock(queue_path):
            with queue_lock(learning_path):
                if fresh_only:
                    receipts, receipt_errors = _read_receipt_set(
                        runtime_root, workspace=workspace, vault=vault
                    )
                    dismissals, dismissal_errors = _dismissal_receipts(
                        runtime_root, workspace=workspace, vault=vault
                    )
                    unresolved = [*receipt_errors, *dismissal_errors]
                    unresolved.extend(
                        f"{path.name}: queued-row removal needs finishing"
                        for path, receipt in dismissals
                        if receipt["status"] == "prepared"
                    )
                    for path, existing in receipts:
                        if (
                            existing.get("workspace") == workspace
                            and existing.get("vault_identity") == str(vault)
                            and existing.get("status") in {"prepared", "reverting"}
                        ):
                            unresolved.append(
                                f"{path.name}: transaction is {existing['status']}; "
                                "fresh-only mode leaves recovery untouched"
                            )
                    if unresolved:
                        return {
                            "workspace": workspace,
                            "rows": [],
                            "receipts": [],
                            "failed": unresolved,
                            "unresolved": unresolved,
                        }
                    # Undo is an operator decision. Keep those exact legacy
                    # lines out of automatic startup/care runs even if an
                    # unrelated queue edit changes the file revision later.
                    # The manual --apply path remains an explicit retry.
                    excluded_raw = frozenset(
                        row["raw"]
                        for _path, receipt in receipts
                        if receipt.get("status") == "reverted"
                        for row in receipt["rows"]
                    )
                else:
                    excluded_raw = frozenset()
                errors, recovered = _recover_locked(
                    vault, workspace=workspace, runtime_root=runtime_root
                )
                if errors:
                    return {"workspace": workspace, "rows": [], "receipts": recovered, "failed": errors, "unresolved": errors}
                _assert_workspace_paths(vault)
                queue_exists, queue_text = _safe_file(queue_path)
                if not queue_exists:
                    return {"workspace": workspace, "rows": [], "receipts": recovered, "failed": [], "not_applicable": True}
                learning_exists, learning_text = _safe_file(learning_path)
                receipt = _prepare_batch(
                    vault,
                    workspace=workspace,
                    queue_text=queue_text,
                    learning_exists=learning_exists,
                    learning_text=learning_text,
                    excluded_raw=excluded_raw,
                )
                rows = [_parse_row(row["raw"]) for row in receipt["rows"]] if receipt else []
                if receipt is None:
                    return {"workspace": workspace, "rows": [], "receipts": recovered, "failed": [], "not_applicable": True}
                folder = _receipt_dir(runtime_root)
                _private_dir(folder, create=True)
                path = folder / f"{RECEIPT_PREFIX}{receipt['run_id']}.json"
                _write_receipt(path, receipt, create=True)
                try:
                    _resume_forward(vault, receipt, path, workspace=workspace, runtime_root=runtime_root)
                except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
                    return {"workspace": workspace, "rows": rows, "receipts": [*recovered, str(path)], "failed": [str(exc)]}
                return {"workspace": workspace, "rows": rows, "receipts": [*recovered, str(path)], "failed": [], "applied": True}
    except (OSError, UnicodeError, ValueError) as exc:
        return {"workspace": workspace, "rows": [], "receipts": [], "failed": [str(exc)]}
    finally:
        if pinned:
            pin.__exit__(None, None, None)


def drain_registered_workspaces(config: Any) -> dict[str, dict[str, Any]]:
    """Run one fresh-only migration pass for every registered workspace."""
    runtime_root = Path(config.state_path).parent
    results: dict[str, dict[str, Any]] = {}
    for workspace in config.workspace_names():
        try:
            results[workspace] = drain(
                Path(config.workspace_vault_root(workspace)),
                workspace=workspace,
                runtime_root=runtime_root,
                apply=True,
                fresh_only=True,
            )
        except Exception as exc:
            results[workspace] = {
                "workspace": workspace,
                "rows": [],
                "receipts": [],
                "failed": [str(exc)],
            }
    return results


def revert(vault_root: Path, receipt_path: Path, *, workspace: str, runtime_root: Path, apply: bool = False) -> dict[str, Any]:
    """Revert one whole batch after checking both destination revisions."""
    vault = Path(vault_root).resolve()
    folder = _receipt_dir(runtime_root)
    pin = _pin_workspace(vault) if apply else None
    pinned = False
    try:
        selected = Path(receipt_path).absolute()
        if selected.parent.resolve() != folder.resolve() or not selected.name.startswith(RECEIPT_PREFIX):
            raise ValueError("receipt is outside this workspace's private drain directory")
        if not apply:
            receipt = _load_receipt(selected, runtime_root=runtime_root)
            _validate_receipt(receipt, vault=vault, workspace=workspace)
            _check_revert_dismissals(receipt, vault=vault, workspace=workspace, runtime_root=runtime_root)
            if receipt["status"] != "applied":
                raise ValueError("receipt is not in the applied state")
            _preflight_files(vault, receipt, forward=False)
            for row in receipt["rows"]:
                expected = _expected_history(row, operation_id=row["operation_id"], at=row["history_at"], outcome="accepted")
                if _history_state(dismissed_log_path(_queue(vault)), expected) != "same":
                    raise ValueError("History is missing or conflicts with a batch decision")
            return {"reverted": False, "would_revert": True, "rows": len(receipt["rows"])}
        assert pin is not None
        pin.__enter__()
        pinned = True
        with queue_lock(_queue(vault)):
            with queue_lock(learnings_path(vault)):
                errors, _completed = _recover_locked(vault, workspace=workspace, runtime_root=runtime_root)
                if errors:
                    return {"failed": errors, "reverted": False}
                receipt = _load_receipt(selected, runtime_root=runtime_root)
                _validate_receipt(receipt, vault=vault, workspace=workspace)
                _check_revert_dismissals(receipt, vault=vault, workspace=workspace, runtime_root=runtime_root)
                if receipt["status"] == "reverted":
                    return {"reverted": True, "already_reverted": True, "rows": len(receipt["rows"]), "failed": []}
                if receipt["status"] != "applied":
                    raise ValueError("receipt is not in the applied state")
                _preflight_files(vault, receipt, forward=False)
                for row in receipt["rows"]:
                    expected = _expected_history(row, operation_id=row["operation_id"], at=row["history_at"], outcome="accepted")
                    if _history_state(dismissed_log_path(_queue(vault)), expected) != "same":
                        raise ValueError("History is missing or conflicts with a batch decision")
                receipt["status"] = "reverting"
                receipt["revert_at"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
                receipt["revert_operation_ids"] = [f"{receipt['run_id']}:revert:{i}" for i in range(len(receipt["rows"]))]
                _write_receipt(selected, receipt)
                _resume_revert(vault, receipt, selected, workspace=workspace)
        return {"reverted": True, "rows": len(receipt["rows"]), "failed": []}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {"failed": [str(exc)], "reverted": False}
    finally:
        if pin is not None and pinned:
            pin.__exit__(None, None, None)


def _check_revert_dismissals(
    receipt: dict[str, Any], *, vault: Path, workspace: str, runtime_root: Path
) -> None:
    dismissals, errors = _dismissal_receipts(
        runtime_root, workspace=workspace, vault=vault
    )
    if errors:
        raise ValueError("queued-row removal history needs review before revert")
    if any(dismissal["status"] == "prepared" for _path, dismissal in dismissals):
        raise ValueError("a queued-row removal needs finishing before reverting a batch")
