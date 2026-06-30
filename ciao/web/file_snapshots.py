"""Per-chat, per-file content snapshots.

When the agent calls Write/Edit/MultiEdit/NotebookEdit inside a chat, the
project chat manager schedules a snapshot via this store. Each snapshot is a
verbatim copy of the file content at the moment of capture, indexed by
``(chat_id, file_path, seq)``. The PWA reads this back via
``/api/file-history`` and ``/api/file-content`` to render the History and Diff
tabs in the file viewer.

Storage layout::

    .runtime/snapshots/
        <chat_id>/
            <quoted_path>/
                meta.json          [{seq, ts, action, tool, size}, …]
                0001.snap          raw content
                0002.snap
                …

Plain files, no git, no SQLite. The whole store fits in a few MB even for an
active power user — a personal assistant's diff trail is not a write-heavy
workload. Debuggability matters more than throughput.

The store is intentionally append-only: snapshots are never rewritten in place
and ``restore`` writes a new snapshot rather than mutating an old one. That
keeps the audit trail intact and makes recovery from a botched restore a
matter of restoring the previous snapshot again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)


# Cap snapshot size to avoid runaway disk usage if the agent writes a huge
# generated file. Anything over the cap is recorded with content="" and
# size=actual; the UI shows a "too large to snapshot" note. Matches the
# workspace-file viewer's 2 MB read cap.
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024


@dataclass(slots=True)
class SnapshotMeta:
    seq: int
    ts: str
    action: str  # "written" | "edited"
    tool: str   # "Write" | "Edit" | "MultiEdit" | "NotebookEdit"
    size: int
    truncated: bool = False

    def to_dict(self) -> dict:
        d = {
            "seq": self.seq,
            "ts": self.ts,
            "action": self.action,
            "tool": self.tool,
            "size": self.size,
        }
        if self.truncated:
            d["truncated"] = True
        return d


def _quote_path(file_path: str) -> str:
    # `quote(safe="")` URL-encodes slashes too so the result is one flat
    # directory component. We avoid the OS path separator entirely so a
    # malicious agent-supplied path cannot break out of the chat's snapshot
    # dir via "../../" in the encoded form (% sequences won't traverse).
    return quote(file_path, safe="")


class SnapshotStore:
    """File-system-backed snapshot store. One instance per process; serialises
    writes per ``(chat_id, file_path)`` with an in-memory lock map so two
    near-simultaneous Edits to the same file don't clobber each other's seq."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir
        self._base.mkdir(parents=True, exist_ok=True)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Pending debounced snapshot tasks keyed by (chat_id, file_path). New
        # captures cancel the previous one so a burst of Edits collapses into
        # a single snapshot taken after the burst settles.
        self._pending: dict[tuple[str, str], asyncio.TimerHandle] = {}

    def _dir_for(self, chat_id: str, file_path: str) -> Path:
        return self._base / chat_id / _quote_path(file_path)

    def _lock_for(self, chat_id: str, file_path: str) -> asyncio.Lock:
        key = (chat_id, file_path)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def capture(
        self,
        *,
        chat_id: str,
        file_path: str,
        action: str,
        tool: str,
    ) -> SnapshotMeta | None:
        """Read the file from disk and append a snapshot.

        Returns the snapshot metadata, or ``None`` if the file no longer
        exists (the tool deleted it, or moved it, or the path was outside
        the filesystem). Missing files don't raise so callers can fire-and-
        forget from the event loop.
        """
        target = Path(file_path).expanduser()
        if not target.is_file():
            logger.debug("snapshot skipped, not a file: %s", file_path)
            return None

        try:
            data = target.read_bytes()
        except OSError as exc:
            logger.warning("snapshot read failed for %s: %s", file_path, exc)
            return None

        size = len(data)
        truncated = size > MAX_SNAPSHOT_BYTES
        if truncated:
            data = b""

        async with self._lock_for(chat_id, file_path):
            chat_dir = self._dir_for(chat_id, file_path)
            chat_dir.mkdir(parents=True, exist_ok=True)
            meta_path = chat_dir / "meta.json"
            metas = self._load_meta(meta_path)
            seq = (metas[-1]["seq"] + 1) if metas else 1

            # Skip back-to-back duplicate snapshots: if the content hash
            # matches the previous one, no new edit actually happened. Saves
            # disk and prevents history pollution when our hook fires before
            # the CLI has executed the edit (the broker emits ToolUseEvent at
            # tool-call time, not after the file write).
            if not truncated and metas:
                prev = chat_dir / f"{metas[-1]['seq']:04d}.snap"
                if prev.is_file():
                    prev_hash = hashlib.sha256(prev.read_bytes()).hexdigest()
                    cur_hash = hashlib.sha256(data).hexdigest()
                    if prev_hash == cur_hash:
                        logger.debug(
                            "snapshot dedup: %s seq=%s matches prev",
                            file_path,
                            seq,
                        )
                        return SnapshotMeta(**self._normalize_meta(metas[-1]))

            snap_path = chat_dir / f"{seq:04d}.snap"
            try:
                snap_path.write_bytes(data)
            except OSError as exc:
                logger.warning("snapshot write failed for %s: %s", file_path, exc)
                return None

            meta = SnapshotMeta(
                seq=seq,
                ts=datetime.now(UTC).isoformat(),
                action=action,
                tool=tool,
                size=size,
                truncated=truncated,
            )
            metas.append(meta.to_dict())
            meta_path.write_text(json.dumps(metas, indent=2))
            return meta

    def schedule_capture(
        self,
        *,
        chat_id: str,
        file_path: str,
        action: str,
        tool: str,
        delay: float = 1.5,
    ) -> None:
        """Coalesced debounced capture.

        Multiple file-touch tool calls firing within ``delay`` seconds collapse
        into one snapshot taken after the cluster settles. This is the path
        used by ``ProjectChatManager`` from the per-chat broker loop: a tool
        run might queue several Edits in quick succession, and we only need
        one snapshot capturing the final state.
        """
        loop = asyncio.get_running_loop()
        key = (chat_id, file_path)
        pending = self._pending.pop(key, None)
        if pending is not None:
            pending.cancel()

        def _fire() -> None:
            self._pending.pop(key, None)
            asyncio.create_task(self._capture_safe(
                chat_id=chat_id,
                file_path=file_path,
                action=action,
                tool=tool,
            ))

        self._pending[key] = loop.call_later(delay, _fire)

    async def _capture_safe(self, **kwargs) -> None:
        try:
            await self.capture(**kwargs)
        except Exception:
            logger.exception("snapshot capture failed for %s", kwargs.get("file_path"))

    def list_snapshots(self, *, chat_id: str, file_path: str) -> list[dict]:
        """Return snapshot metadata for one (chat, file). Most recent last."""
        meta_path = self._dir_for(chat_id, file_path) / "meta.json"
        return [self._normalize_meta(m) for m in self._load_meta(meta_path)]

    def read_snapshot(
        self,
        *,
        chat_id: str,
        file_path: str,
        seq: int,
    ) -> tuple[bytes, dict] | None:
        """Return (content, metadata) for one snapshot, or None if missing."""
        chat_dir = self._dir_for(chat_id, file_path)
        meta_path = chat_dir / "meta.json"
        metas = self._load_meta(meta_path)
        match = next((m for m in metas if m.get("seq") == seq), None)
        if match is None:
            return None
        snap_path = chat_dir / f"{seq:04d}.snap"
        if not snap_path.is_file():
            return None
        return snap_path.read_bytes(), self._normalize_meta(match)

    def list_files_for_chat(self, chat_id: str) -> list[dict]:
        """Return one entry per file ever touched in this chat, with the most
        recent snapshot metadata. Drives the chat-level "N files touched"
        chip's deduped panel when the frontend wants server-authoritative
        listings instead of relying on its in-memory message history.
        """
        chat_dir = self._base / chat_id
        if not chat_dir.is_dir():
            return []
        out: list[dict] = []
        for fdir in chat_dir.iterdir():
            if not fdir.is_dir():
                continue
            file_path = unquote(fdir.name)
            metas = self._load_meta(fdir / "meta.json")
            if not metas:
                continue
            latest = self._normalize_meta(metas[-1])
            out.append({
                "file_path": file_path,
                "snapshots": len(metas),
                "latest": latest,
            })
        out.sort(key=lambda d: d["latest"]["ts"], reverse=True)
        return out

    def delete_chat(self, chat_id: str) -> None:
        """Drop all snapshots for a chat. Called when a chat is archived or
        deleted so the snapshot tree doesn't accumulate forever."""
        chat_dir = self._base / chat_id
        if chat_dir.is_dir():
            shutil.rmtree(chat_dir, ignore_errors=True)

    @staticmethod
    def _load_meta(meta_path: Path) -> list[dict]:
        if not meta_path.is_file():
            return []
        try:
            data = json.loads(meta_path.read_text() or "[]")
        except (OSError, json.JSONDecodeError):
            logger.warning("snapshot meta corrupt at %s", meta_path)
            return []
        return data if isinstance(data, list) else []

    @staticmethod
    def _normalize_meta(m: dict) -> dict:
        # Defensive copy with stable fields so callers don't accidentally
        # mutate the in-memory metas list.
        return {
            "seq": int(m.get("seq", 0)),
            "ts": str(m.get("ts", "")),
            "action": str(m.get("action", "")),
            "tool": str(m.get("tool", "")),
            "size": int(m.get("size", 0)),
            "truncated": bool(m.get("truncated", False)),
        }
