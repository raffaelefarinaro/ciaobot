"""Shadow verdicts for region proposals and the handover gate.

The nightly Workspace care run records what it would do with a queued
``[memory]``/``[profile]`` row without doing it. The verdicts live in an
append-only sidecar beside the queue, keyed by ``(kind, one-line text)`` rather
than ``stable_proposal_id``: the id mixes in the queue path, the source and the
duplicate ordinal, so removing an earlier duplicate renumbers a later row, and
the CLI decision writers record no id at all. The text is what every decision
writer already records.

This module joins a verdict onto the History row the operator's decision made,
measures how often the two agree, and says whether a workspace's handover
setting lets the run act on a verdict. Nothing here changes the setting.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.config import MEMORY_HANDOVERS
from ciao.os_support.files import same_file_identity

REGION_KINDS: frozenset[str] = frozenset({"memory", "profile"})
VERDICTS: tuple[str, ...] = ("accept", "dismiss")
PREDICTIONS_SUFFIX = ".predictions.jsonl"
REASON_MAX_CHARS = 200
AGREEMENT_WINDOW = 50
SUGGEST_MIN_DECISIONS = 20
SUGGEST_MIN_RATE = 0.9
#: Most facts the agent may accept into the always-loaded regions in one curation run.
HANDOVER_ACCEPT_CAP = 5


_CAP_SIDECARS = (".dismissed.jsonl", ".dismissed.log")
_CAP_ROW_BYTES = 2 * 1024 * 1024
_CAP_OPERATION_SLOT = "op-" + "0" * 32


class CapSlotNeedsInspection(ValueError):
    """An ambiguous runtime slot must be inspected before another index."""


def _cap_check_backup_scope(config: Any) -> None:
    """Refuse a system-temp location inside any configured backup root."""
    from ciao import backup_scope
    from ciao.config import CiaoConfig
    from ciao.os_support.users import user_key

    if not isinstance(config, CiaoConfig):
        raise ValueError("checkpoint backup configuration is unavailable")
    path = (Path(tempfile.gettempdir()).resolve() / f"ciao-cap-index-{user_key()}").resolve()
    roots = [backup_scope.data_root(config), Path(config.vault_root)]
    roots.extend(Path(config.workspace_vault_root(name)) for name in config.workspaces)
    if any(path.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("checkpoint runtime directory overlaps a configured backup root")


def _cap_runtime(queue: Path, config: Any) -> Path:
    from ciao.os_support.private import is_private_strict, make_private_dir
    from ciao.os_support.users import user_key

    _cap_check_backup_scope(config)
    vault = queue.parent.parent.resolve()
    if not any(vault == Path(config.workspace_vault_root(name)).resolve() for name in config.workspaces):
        raise ValueError("checkpoint queue is not registered in the backup configuration")
    path = Path(tempfile.gettempdir()).resolve() / f"ciao-cap-index-{user_key()}"
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        if os.name == "nt":
            make_private_dir(path)
    info = path.lstat()
    if (_cap_link(info) or not stat.S_ISDIR(info.st_mode) or not is_private_strict(path)):
        raise ValueError("unsafe checkpoint runtime directory")
    if path.resolve().is_relative_to(queue.parent.parent.resolve()):
        raise ValueError("checkpoint runtime directory is inside the vault")
    return path


def _cap_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _cap_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


@contextmanager
def _cap_runtime_lock(directory: Path):
    from ciao.os_support.locks import lock_exclusive, unlock
    from ciao.os_support.private import is_private_descriptor_strict, open_private

    path = directory / "guard.lock"
    fd = open_private(path, os.O_RDWR, follow_symlinks=False)
    try:
        info, named = os.fstat(fd), path.lstat()
        if (_cap_link(named) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or _cap_identity(info) != _cap_identity(named) or not is_private_descriptor_strict(fd)):
            raise ValueError("unsafe checkpoint runtime lock")
        lock_exclusive(fd)
        try:
            yield
        finally:
            unlock(fd)
    finally:
        os.close(fd)


def _cap_cleanup_operation(path: Path) -> None:
    """Remove only a fully identified operation; leave ambiguous files untouched."""
    from ciao.os_support.private import is_private_descriptor_strict, is_private_strict

    try:
        info = path.lstat()
        if _cap_link(info) or not stat.S_ISDIR(info.st_mode) or not is_private_strict(path):
            return
        entries = {entry.name for entry in path.iterdir()}
        required = {"owner.json"}
        sidecars = {"index.sqlite-journal", "index.sqlite-wal", "index.sqlite-shm"}
        if not required <= entries or entries - required - sidecars - {"index.sqlite"}:
            return
        marker, index = path / "owner.json", path / "index.sqlite"
        named = marker.lstat()
        if _cap_link(named) or not stat.S_ISREG(named.st_mode) or named.st_nlink != 1:
            return
        fd = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _cap_identity(opened) != _cap_identity(named) or not is_private_descriptor_strict(handle.fileno()):
                return
            data = handle.read(1025)
        record = json.loads(data)
        if not isinstance(record, dict) or type(record.get("version")) is not int:
            return
        if record["version"] == 1:
            if set(record) != {"version", "dev", "ino"}:
                return
        elif record["version"] == 2:
            if (set(record) != {"version", "dev", "ino", "dir_dev", "dir_ino"}
                    or type(record["dir_dev"]) is not int or type(record["dir_ino"]) is not int
                    or (record["dir_dev"], record["dir_ino"]) != _cap_identity(info)):
                return
        else:
            return
        if type(record["dev"]) is not int or type(record["ino"]) is not int:
            return
        if "index.sqlite" not in entries:
            if record["version"] != 2 or entries != {"owner.json"}:
                return
            if _cap_identity(path.lstat()) != _cap_identity(info) or _cap_identity(marker.lstat()) != _cap_identity(opened):
                return
            marker.unlink()
            path.rmdir()
            return
        named = index.lstat()
        if (_cap_link(named) or not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
                or _cap_identity(named) != (record["dev"], record["ino"]) or not is_private_strict(index)):
            return
        checked = [(index, named)]
        for name in sorted(entries & sidecars):
            child = path / name
            child_info = child.lstat()
            if (_cap_link(child_info) or not stat.S_ISREG(child_info.st_mode) or child_info.st_nlink != 1
                    or not is_private_strict(child)):
                return
            checked.insert(0, (child, child_info))
        if _cap_identity(path.lstat()) != _cap_identity(info):
            return
        for child, expected in checked:
            current = child.lstat()
            if _cap_link(current) or _cap_identity(current) != _cap_identity(expected):
                return
            child.unlink()
        marker.unlink()
        path.rmdir()
    except (OSError, ValueError, UnicodeError):
        return


def _cap_cleanup(directory: Path) -> None:
    for path in directory.iterdir():
        if re.fullmatch(r"op-[0-9a-f]{32}", path.name):
            _cap_cleanup_operation(path)


class UnprovenCapReceipt(ValueError):
    """A decision cannot be proved by its retained receipt."""


def _cap_unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate sidecar key")
        result[key] = value
    return result


def _cap_invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _verify_cap_event_receipt(queue: Path, event: dict[str, Any], receipt: dict[str, Any]) -> bool:
    from ciao import memory_receipts as mr

    after = receipt.get("after_text")
    admission = receipt.get("admission_started_at")
    if admission:
        try:
            floor = datetime.fromisoformat(str(admission))
            when = datetime.fromisoformat(str(event["timestamp"]))
            if floor.tzinfo is None or when.tzinfo is None or when < floor:
                return False
        except (KeyError, ValueError):
            return False
    return bool(
        receipt.get("kind") == "queue_resolve" and receipt.get("status") == mr.APPLIED
        and receipt.get("owner_landed") is True and receipt.get("actor") == "agent"
        and receipt.get("source") == "cli" and receipt.get("promoted") is True
        and mr._queue_receipt_bound(receipt, mr.journal_path(queue.parent.parent, None))
        and isinstance(after, str) and receipt.get("after_revision") == mr.content_revision(after)
        and mr._selected_queue_effect(receipt, after, pending=False)
        and receipt.get("proposal_id") == event["proposal_id"]
        and receipt.get("proposal_kind") == event["kind"]
        and receipt.get("proposal_source") == event["source"]
        and receipt.get("proposal_text") == event["identity_text"]
        and receipt.get("decision_via") == "agent"
        and receipt.get("decision_receipt_id", "") == event["destination_receipt_id"]
        and receipt.get("decision_outcome", "") == event["outcome"]
    )


def stream_checkpoint_events(
    queue: Path, checkpoint: dict[str, Any], receipts: list[dict[str, Any]],
    *, config: Any, capture: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, tuple[int, int, int, int, int] | None]]:
    """Authenticate and reduce with a private disposable encrypted identity index."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from ciao.os_support.private import is_private_descriptor_strict, is_private_strict, make_private_dir, open_private

    directory = _cap_runtime(queue, config)
    with _cap_runtime_lock(directory):
        _cap_cleanup(directory)
        operation = directory / _CAP_OPERATION_SLOT
        try:
            operation.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise CapSlotNeedsInspection(f"checkpoint runtime slot needs manual inspection: {operation}") from exc
        if os.name == "nt":
            make_private_dir(operation)
        if not is_private_strict(operation):
            raise ValueError("temporary checkpoint directory is not private")
        filename = operation / "index.sqlite"
        fd = open_private(filename, os.O_RDWR | os.O_EXCL, follow_symlinks=False)
        database: sqlite3.Connection | None = None
        try:
            if not is_private_descriptor_strict(fd):
                raise ValueError("temporary checkpoint index is not private")
            info = os.fstat(fd)
            marker_fd = open_private(operation / "owner.json", os.O_WRONLY | os.O_EXCL, follow_symlinks=False)
            with os.fdopen(marker_fd, "w", encoding="utf-8", newline="") as marker:
                if not is_private_descriptor_strict(marker.fileno()):
                    raise ValueError("temporary checkpoint marker is not private")
                operation_info = operation.lstat()
                json.dump({"version": 2, "dev": info.st_dev, "ino": info.st_ino,
                           "dir_dev": operation_info.st_dev, "dir_ino": operation_info.st_ino}, marker)
            database = sqlite3.connect(filename.as_uri() + "?mode=rw", uri=True)
            named = os.lstat(filename)
            if _cap_identity(info) != _cap_identity(named):
                raise ValueError("temporary checkpoint index changed")
            if database.execute("PRAGMA journal_mode=OFF").fetchone()[0] != "off":
                raise ValueError("temporary checkpoint index journaling could not be disabled")
            database.execute("PRAGMA temp_store=MEMORY")
            database.execute("PRAGMA synchronous=OFF")
            database.execute("PRAGMA cache_size=-2048")
            database.execute(
                "CREATE TABLE identities (id BLOB PRIMARY KEY, nonce BLOB NOT NULL, data BLOB NOT NULL, "
                "first_side INTEGER NOT NULL, first_ordinal INTEGER NOT NULL, "
                "prefix_side INTEGER, prefix_ordinal INTEGER) WITHOUT ROWID"
            )
            key = ChaCha20Poly1305.generate_key()
            return _stream_checkpoint_events(queue, checkpoint, receipts, database, key, capture=capture)
        except sqlite3.Error as exc:
            raise ValueError("temporary checkpoint index failed") from exc
        finally:
            try:
                if database is not None:
                    database.close()
            finally:
                os.close(fd)
                _cap_cleanup_operation(operation)


def _stream_checkpoint_events(
    queue: Path, checkpoint: dict[str, Any], receipts: list[dict[str, Any]],
    database: sqlite3.Connection, key: bytes, *, capture: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, tuple[int, int, int, int, int] | None]]:
    from ciao import memory_proposals, memory_receipts
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    cipher = ChaCha20Poly1305(key)
    lookup_key = hmac.digest(key, b"ciao checkpoint identity index", "sha256")

    def index_key(rid: str) -> bytes:
        return hmac.digest(lookup_key, rid.encode("utf-8"), "sha256")

    def store_identity(rid: str, raw: bytes, position: tuple[int, int], in_prefix: bool) -> None:
        token = index_key(rid)
        identifier = rid.encode("utf-8")
        payload = len(identifier).to_bytes(8, "big") + identifier + raw
        prior = database.execute(
            "SELECT nonce, data, prefix_side FROM identities WHERE id = ?", (token,),
        ).fetchone()
        if prior is None:
            nonce = secrets.token_bytes(12)
            encrypted = cipher.encrypt(nonce, payload, token)
            database.execute("INSERT INTO identities VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (token, nonce, encrypted, *position,
                              position[0] if in_prefix else None, position[1] if in_prefix else None))
            return
        try:
            original = cipher.decrypt(prior[0], prior[1], token)
        except InvalidTag as exc:
            raise ValueError("temporary checkpoint index authentication failed") from exc
        if original != payload:
            raise ValueError("inconsistent duplicate receipt decision")
        if in_prefix and prior[2] is None:
            database.execute("UPDATE identities SET prefix_side = ?, prefix_ordinal = ? WHERE id = ?",
                             (*position, token))

    bindings = checkpoint.get("sidecars")
    if not isinstance(bindings, dict) or set(bindings) != set(_CAP_SIDECARS):
        raise ValueError("invalid checkpoint sidecars")
    paths = {suffix: queue.with_suffix(suffix) for suffix in _CAP_SIDECARS}
    identities: dict[str, tuple[int, int, int, int, int] | None] = {}
    successor_bindings: dict[str, dict[str, Any] | None] = {}
    matched: set[str] = set()
    limits: dict[str, int] = {}
    for suffix, path in paths.items():
        binding = bindings[suffix]
        if binding is not None:
            if (not isinstance(binding, dict) or set(binding) != {"length", "sha256"}
                    or type(binding["length"]) is not int or binding["length"] < 0
                    or not isinstance(binding["sha256"], str)):
                raise ValueError("invalid checkpoint binding")
        limits[suffix] = 0 if binding is None else binding["length"]
        try:
            path.lstat()
        except FileNotFoundError:
            if binding is not None:
                raise ValueError("checkpoint sidecar missing")
            identities[suffix] = None
            successor_bindings[suffix] = None
            continue
        stream = memory_proposals.DecisionSidecarStream(path, prefix_length=limits[suffix], fence=capture is not None)
        for ordinal, row, _digest in stream:
            rid = row.get("queue_receipt_id", "")
            if rid and row.get("via") == "agent" and "promoted_at" in row:
                store_identity(rid, stream.raw_line, (_CAP_SIDECARS.index(suffix), ordinal),
                               binding is not None and stream.offset <= limits[suffix])
        if stream.identity is None or (binding is not None and stream.prefix_binding != binding):
            raise ValueError("checkpoint prefix changed")
        identities[suffix] = stream.identity
        successor_bindings[suffix] = stream.binding
    if capture is not None and memory_proposals.os.name != "nt":
        directory = memory_proposals.os.open(queue.parent, memory_proposals.os.O_RDONLY)
        try:
            memory_proposals.os.fsync(directory)
        finally:
            memory_proposals.os.close(directory)

    by_id: dict[str, dict[str, Any]] = {}
    for row in receipts:
        rid = row.get("id")
        if isinstance(rid, str) and rid:
            if rid in by_id:
                raise ValueError("duplicate receipt identity")
            by_id[rid] = row
    prefix_latest: list[dict[str, Any]] = []
    latest: list[dict[str, Any]] = []

    def add_latest(target: list[dict[str, Any]], event: dict[str, Any]) -> None:
        target.append(event)
        target.sort(key=lambda item: (datetime.fromisoformat(item["timestamp"]), item["identity"]))
        if len(target) > HANDOVER_ACCEPT_CAP:
            del target[0]

    def scan(suffix: str) -> memory_proposals.DecisionSidecarStream | None:
        identity = identities[suffix]
        if identity is None:
            return None
        return memory_proposals.DecisionSidecarStream(paths[suffix], expected_identity=identity)

    for suffix in _CAP_SIDECARS:
        outer = scan(suffix)
        if outer is None:
            continue
        for ordinal, row, row_digest in outer:
            if row.get("via") != "agent" or "promoted_at" not in row:
                continue
            rid = row.get("queue_receipt_id", "")
            position = (_CAP_SIDECARS.index(suffix), ordinal)
            in_prefix = bindings[suffix] is not None and outer.offset <= limits[suffix]
            if rid:
                reference = database.execute(
                    "SELECT first_side, first_ordinal, prefix_side, prefix_ordinal FROM identities WHERE id = ?",
                    (index_key(rid),),
                ).fetchone()
                if reference is None:
                    raise ValueError("temporary checkpoint index is incomplete")
                first_all = (reference[0], reference[1]) == position
                first_prefix = in_prefix and (reference[2], reference[3]) == position
                found_prefix = reference[2] is not None
                identity = f"receipt:{rid}"
            else:
                first_all, first_prefix, found_prefix = True, in_prefix, in_prefix
                identity = f"sidecar:{suffix}:{ordinal}:{row_digest}"
            event = {
                "identity": identity, "timestamp": row["promoted_at"], "sidecar": "" if rid else suffix,
                "ordinal": -1 if rid else ordinal, "row_sha256": row_digest, "receipt_id": rid,
                "proposal_id": row.get("proposal_id", ""), "kind": row.get("kind", ""),
                "source": row.get("source", ""), "identity_text": row.get("identity_text", row["text"]),
                "destination_receipt_id": row.get("receipt_id", ""), "outcome": row.get("outcome", ""),
            }
            if first_prefix:
                add_latest(prefix_latest, event)
            if first_all:
                if rid and rid in by_id and _verify_cap_event_receipt(queue, event, by_id[rid]):
                    matched.add(rid)
                if rid and not found_prefix:
                    receipt = by_id.get(rid)
                    if receipt is None or not _verify_cap_event_receipt(queue, event, receipt):
                        raise UnprovenCapReceipt("decision receipt does not prove the event")
                add_latest(latest, event)
    expected = {
        "v": memory_receipts.RECEIPT_VERSION, "id": memory_receipts.CAP_CHECKPOINT_ID,
        "kind": "cap_event_checkpoint", "status": "applied", "schema": 1,
        "queue": str(queue), "sidecars": bindings, "events": prefix_latest,
    }
    if checkpoint != expected:
        raise ValueError("invalid checkpoint evidence")
    for suffix, path in paths.items():
        expected_identity = identities[suffix]
        if expected_identity is None:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            raise ValueError("decision sidecar appeared during scan")
        info = path.lstat()
        if not same_file_identity(info, expected_identity):
            raise ValueError("decision sidecar changed during scan")
    if capture is not None:
        capture["bindings"] = successor_bindings
        capture["matched"] = matched
    return latest, identities


def _one_line(value: str) -> str:
    return " ".join(value.split())


def predictions_path(proposals_path: Path) -> Path:
    """Sidecar holding the shadow verdicts, next to the queue it describes."""
    return proposals_path.with_suffix(PREDICTIONS_SUFFIX)


def prediction_key(kind: str, text: str) -> str:
    return f"{kind.lower()}\x00{_one_line(text)}"


def read_predictions(proposals_path: Path) -> list[dict[str, str]]:
    """Every stored prediction, oldest first. Unreadable lines are skipped."""
    path = predictions_path(proposals_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows: list[dict[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        rows.append({str(k): str(v) for k, v in entry.items()})
    return rows


def _row_key(row: dict[str, str]) -> str:
    return prediction_key(row.get("kind", ""), row.get("text", ""))


def _decision_times(proposals_path: Path) -> dict[str, list[str]]:
    """Every dated region decision's timestamp, per ``(kind, text)`` key."""
    from ciao import memory_proposals  # noqa: PLC0415

    times: dict[str, list[str]] = {}
    for decision in memory_proposals.read_decisions(proposals_path):
        kind = str(decision.get("kind", "")).lower()
        ts = str(decision.get("ts", ""))
        if kind in REGION_KINDS and ts:
            times.setdefault(prediction_key(kind, str(decision["text"])), []).append(ts)
    return times


def latest_predictions(proposals_path: Path) -> dict[str, dict[str, str]]:
    """The newest *live* prediction per ``(kind, text)`` key.

    A prediction is void once the operator (or anyone) has decided the same fact
    after it was made: a fact dismissed and later re-queued is a new question,
    and the verdict recorded for the old one must not authorise acting on it.
    The sidecar keeps the row; it just stops counting.
    """
    latest: dict[str, dict[str, str]] = {}
    for row in read_predictions(proposals_path):
        latest[_row_key(row)] = row
    if not latest:
        return latest
    decided = _decision_times(proposals_path)
    return {
        key: row
        for key, row in latest.items()
        if not any(ts > row.get("predicted_at", "") for ts in decided.get(key, ()))
    }


def latest_accept_prediction(proposals_path: Path, kind: str, text: str) -> dict[str, str] | None:
    """Find one prediction without materializing unrelated decision history."""
    from ciao import memory_proposals

    key = prediction_key(kind, text)
    latest = None
    for row in read_predictions(proposals_path):
        if _row_key(row) == key:
            latest = row
    if latest is None:
        return None
    invalidated = False
    for sidecar in memory_proposals._sidecar_paths(proposals_path):
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        for _ordinal, row, _digest in memory_proposals.DecisionSidecarStream(sidecar):
            row_kind = str(row.get("kind", "")).lower()
            stamp = str(row["promoted_at"] if "promoted_at" in row else row.get("dismissed_at", ""))
            if (row_kind in REGION_KINDS and stamp
                    and prediction_key(row_kind, memory_proposals._one_line(row["text"])) == key
                    and stamp > latest.get("predicted_at", "")):
                invalidated = True
    return None if invalidated else latest


def count_agent_accepts_since(proposals_path: Path, since: datetime) -> int:
    """Agent-made accepts recorded in the decision sidecar at or after ``since``."""
    from ciao import memory_proposals  # noqa: PLC0415

    count = 0
    for decision in memory_proposals.read_decisions(proposals_path):
        if decision.get("via") != "agent" or decision.get("action") != "accepted":
            continue
        try:
            decided_at = datetime.fromisoformat(str(decision.get("ts", "")))
        except ValueError:
            continue
        if decided_at.tzinfo is not None and decided_at >= since:
            count += 1
    return count


def record_prediction(
    proposals_path: Path,
    *,
    kind: str,
    text: str,
    verdict: str,
    reason: str,
) -> bool:
    """Append a prediction unless the latest one for the row says the same."""
    kind = kind.lower()
    if kind not in REGION_KINDS:
        raise ValueError("predictions are only recorded for [memory] and [profile] rows")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of: {', '.join(VERDICTS)}")
    flat_text = _one_line(text)
    if not flat_text:
        raise ValueError("prediction text is empty")
    flat_reason = _one_line(reason)[:REASON_MAX_CHARS].strip()
    if not flat_reason:
        raise ValueError("prediction reason is empty")
    latest = latest_predictions(proposals_path).get(prediction_key(kind, flat_text))
    if latest is not None and latest.get("verdict") == verdict and latest.get("reason") == flat_reason:
        return False
    row = {
        "predicted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind,
        "text": flat_text,
        "verdict": verdict,
        "reason": flat_reason,
    }
    path = predictions_path(proposals_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def handover_allows(handover: str, verdict: str) -> bool:
    """Whether a workspace's handover setting lets the run act on ``verdict``."""
    if handover not in MEMORY_HANDOVERS:
        return False
    if handover == "dismiss":
        return verdict == "dismiss"
    if handover == "all":
        return verdict in VERDICTS
    return False


def attach_predictions(proposals_path: Path, rows: list[dict[str, Any]]) -> None:
    """Join the prediction that preceded each decision onto History-shaped rows.

    A row gets ``prediction`` only for a region kind with a dated decision and a
    prediction for the same ``(kind, text)`` made at or before that decision and
    after the previous decision on the same fact, so a re-queued fact never
    inherits the verdict recorded for its earlier life.
    """
    by_key: dict[str, list[dict[str, str]]] = {}
    for entry in read_predictions(proposals_path):
        by_key.setdefault(_row_key(entry), []).append(entry)
    if not by_key:
        return
    decided = _decision_times(proposals_path)
    for row in rows:
        row_ts = str(row.get("ts", ""))
        if row_ts:
            decided.setdefault(
                prediction_key(str(row.get("kind", "")), str(row.get("text", ""))), []
            ).append(row_ts)
    for row in rows:
        kind = str(row.get("kind", "")).lower()
        ts = str(row.get("ts", ""))
        if kind not in REGION_KINDS or not ts:
            continue
        key = prediction_key(kind, str(row.get("text", "")))
        # The previous decision on this fact, from the sidecar and the rows in
        # hand (a filtered History page may not carry it).
        earlier = [t for t in decided.get(key, ()) if t < ts]
        floor = max(earlier, default="")
        chosen: dict[str, str] | None = None
        for entry in by_key.get(key, []):
            predicted_at = entry.get("predicted_at", "")
            if predicted_at and floor < predicted_at <= ts:
                chosen = entry
        if chosen is None:
            continue
        verdict = chosen.get("verdict", "")
        row["prediction"] = {
            "verdict": verdict,
            "reason": chosen.get("reason", ""),
            "ts": chosen.get("predicted_at", ""),
            "match": (verdict == "accept") == (row.get("action") == "accepted"),
        }


def agreement(rows: list[dict[str, Any]], *, handover: str) -> dict[str, Any]:
    """Agreement between predictions and the operator over the newest decisions.

    ``rows`` must already have been through :func:`attach_predictions`. Agent
    and auto decisions are not operator judgement and a sweep is expiry, so
    none of them count.
    """
    eligible = [
        row
        for row in rows
        if str(row.get("kind", "")).lower() in REGION_KINDS
        and row.get("action") in ("accepted", "dismissed")
        and row.get("via") == "pwa"
        and row.get("outcome") != "swept"
        and isinstance(row.get("prediction"), dict)
    ]
    eligible.sort(key=lambda row: str(row.get("ts", "")))
    window = eligible[-AGREEMENT_WINDOW:]
    decided = len(window)
    matched = sum(1 for row in window if row["prediction"]["match"])
    accepts = [row for row in window if row["prediction"]["verdict"] == "accept"]
    dismisses = [row for row in window if row["prediction"]["verdict"] == "dismiss"]
    rate = matched / decided if decided else None
    return {
        "decided": decided,
        "matched": matched,
        "rate": rate,
        "window": AGREEMENT_WINDOW,
        "min_decisions": SUGGEST_MIN_DECISIONS,
        "threshold": SUGGEST_MIN_RATE,
        "predicted_accept": len(accepts),
        "accept_matched": sum(1 for row in accepts if row["prediction"]["match"]),
        "predicted_dismiss": len(dismisses),
        "dismiss_matched": sum(1 for row in dismisses if row["prediction"]["match"]),
        "suggest_handover": (
            handover == "off"
            and decided >= SUGGEST_MIN_DECISIONS
            and rate is not None
            and rate >= SUGGEST_MIN_RATE
        ),
    }


def agreement_for_queue(proposals_path: Path, *, handover: str) -> dict[str, Any]:
    from ciao import memory_proposals  # noqa: PLC0415

    rows = memory_proposals.read_decisions(proposals_path)
    attach_predictions(proposals_path, rows)
    return agreement(rows, handover=handover)
