"""Archive post-processing.

``ProjectChatManager`` keeps the chat/project registry, archive write API, and
coordination of provider/chat lifecycle. ``ArchivePipeline`` owns what happens
after an archive is written: queueing the memory pass for the archived chat,
indexing the archive for search, and publishing the chat's postprocess record
(the memory pass's own step). The manager-facing methods remain thin
delegating seams, and calls back into patched manager methods go through the
explicit host protocol below.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ciao.config import CiaoConfig
from ciao.web.chat_broker import EventsHub

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ciao.web.project_chats import ArchiveOutcome, ChatInfo, ProjectInfo

logger = logging.getLogger(__name__)


class ArchivePipelineHost(Protocol):
    """The complete typed manager surface used by ``ArchivePipeline``."""

    _config: CiaoConfig
    _chats: dict[str, ChatInfo]
    _projects: dict[str, ProjectInfo]

    @property
    def events(self) -> EventsHub: ...

    def _workspace_vault_root(self, workspace: str) -> Path: ...

    def _spawn_detached(
        self, coro: Coroutine[object, object, object], name: str
    ) -> asyncio.Task[object]: ...

    def _make_archive_index_operation(
        self, outcome: ArchiveOutcome
    ) -> Callable[[], None]: ...

    def _run_archive_index_best_effort(
        self, chat_id: str, outcome: ArchiveOutcome, operation: Callable[[], None]
    ) -> None: ...

    def _index_archive_file_off_loop(
        self, chat_id: str, outcome: ArchiveOutcome, operation: Callable[[], None]
    ) -> Coroutine[object, object, None]: ...

    def enqueue_memory_pass(
        self,
        source: ChatInfo,
        project: ProjectInfo | None,
        archive_path: Path,
        doc_path: str,
        focus: dict[str, str] | None = None,
    ) -> str | None: ...


class ArchivePipeline:
    """Queue the memory pass and index the archive once a chat is archived."""

    def __init__(self, host: ArchivePipelineHost) -> None:
        self._host = host

    def _publish_postprocess(self, chat: ChatInfo) -> None:
        self._host.events.publish({
            "type": "chat_postprocess",
            "chat_id": chat.chat_id,
            "project_id": chat.project_id,
            "postprocess": dict(chat.postprocess or {}),
        })

    def _delete_archived_transcript(self, chat_id: str) -> None:
        """Remove a deleted chat's archived transcript directory.

        Without this, an explicit delete does not stick. `_discover_archived_chats`
        treats ``<logs_root>/Chats`` as the source of truth and re-imports any
        directory that is not in the registry, so the very next `list_projects()`
        poll brought the chat back with the same ``chat_id`` and
        ``archive_path``.

        Scoped to this chat's own directory under the derived transcript
        archive: that tree is Ciaobot-generated, one directory per chat, and the
        user asked for this chat to be deleted. Everything else in the vault is
        left alone.
        """
        chats_root = self._host._config.logs_root / "Chats"
        chat_dir = chats_root / chat_id
        # Defend the path: `chat_id` reaching a filesystem join must not escape
        # the archive root, whatever it contains.
        try:
            resolved = chat_dir.resolve()
            if resolved.parent != chats_root.resolve():
                return
        except OSError:
            return
        if not resolved.is_dir():
            return
        try:
            shutil.rmtree(resolved)
        except OSError:
            logger.warning(
                "Could not delete archived transcript for %s", chat_id, exc_info=True
            )

    def _insights_model_for(self, chat: ChatInfo, workspace: str) -> str:
        from ciao.insights import resolve_insights_model

        return resolve_insights_model(
            self._host._config, workspace or None, chat.provider or "claude"
        )

    @staticmethod
    def _project_doc_path(chat: ChatInfo, project: ProjectInfo | None) -> str:
        """The canonical project doc the memory pass folds into, or ""."""
        if chat.schedule_id:
            from ciao.schedules import is_system_schedule_id

            if is_system_schedule_id(chat.schedule_id):
                return ""
        if project is None or project.is_auto:
            return ""
        return project.vault_doc_path

    def run_archive_postprocess(
        self,
        chat_id: str,
        outcome: ArchiveOutcome,
        chat_meta: ChatInfo | None,
        project_meta: ProjectInfo | None,
        focus: dict[str, str] | None = None,
    ) -> None:
        config = self._host._config
        from ciao.web import memory_pass

        chat = self._host._chats.get(chat_id)
        if chat is not None:
            # The archive path may not be on the chat yet (this runs right after
            # `archive_chat` set it, but a caller can pass the outcome directly).
            if not chat.archive_path and outcome.path is not None:
                try:
                    chat.archive_path = outcome.path.relative_to(
                        config.workspace_root
                    ).as_posix()
                except ValueError:
                    chat.archive_path = str(outcome.path)
            workspace = project_meta.workspace if project_meta else ""
            vault_root = (
                self._host._workspace_vault_root(workspace) if workspace else None
            )
            # This archive's memory work happens in a chat of the app's own. An
            # empty archive or an unresolved vault does not queue a pass with
            # nothing to read, and archiving a pass never queues another.
            if (
                memory_pass.MEMORY_PASS_CHATS
                and not memory_pass.is_memory_pass_chat(chat, project_meta)
                and getattr(config, "insights_enabled", True)
                and outcome.path is not None
                and outcome.turn_count > 0
                and vault_root is not None
            ):
                try:
                    self._host.enqueue_memory_pass(
                        chat,
                        project_meta,
                        outcome.path,
                        self._project_doc_path(chat, project_meta),
                        focus,
                    )
                except Exception:  # noqa: BLE001 — the archive already succeeded
                    logger.exception(
                        "Failed to enqueue a memory pass for %s", chat_id
                    )

        # Index the newly archived file in the FTS5 database. The control
        # plane now runs its own index passes in bounded workers, so this
        # formerly loop-serialized writer can overlap them. Run it through the
        # same off-loop executor and the same per-database `keyed_lock`, or a
        # concurrent scan holds SQLite's write lock past the connection timeout
        # and this write is skipped with "database is locked".
        operation = self._host._make_archive_index_operation(outcome)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # Synchronous caller (CLI/tests): run inline. Best-effort like the
            # async branch — an optional FTS update must never fail an archive
            # that already succeeded.
            self._host._run_archive_index_best_effort(chat_id, outcome, operation)
        else:
            self._host._spawn_detached(
                self._host._index_archive_file_off_loop(chat_id, outcome, operation),
                name=f"archive-index-{chat_id}",
            )

    def _run_archive_index_best_effort(
        self, chat_id: str, outcome: ArchiveOutcome, operation: Callable[[], None]
    ) -> None:
        """Run the archive index write inline, logging but never raising."""
        try:
            operation()
        except Exception:  # noqa: BLE001 — archiving already succeeded
            logger.exception(
                "FTS search: failed to index archived file %s for chat %s",
                outcome.path,
                chat_id,
            )

    def _make_archive_index_operation(
        self, outcome: ArchiveOutcome
    ) -> Callable[[], None]:
        """A closure that indexes one archived file under the shared write lock."""
        config = self._host._config

        def _operation() -> None:
            import sqlite3

            from ciao.async_reads import keyed_lock
            from ciao.fts_search import get_db_path, index_file, init_db

            # Install-owned: the same database the MCP tools, the CLI and
            # startup indexing resolve, so an archived chat cannot land in the
            # legacy global `~/.ciao` index that a second install then clears.
            db_path = get_db_path(Path(config.state_path).parent)
            conn = sqlite3.connect(db_path)
            try:
                with keyed_lock(f"fts-index:{db_path}"):
                    init_db(conn)
                    index_file(
                        conn,
                        config.vault_root,
                        outcome.path,
                        path_base=Path(config.workspace_root),
                    )
            finally:
                conn.close()

        return _operation

    async def _index_archive_file_off_loop(
        self, chat_id: str, outcome: ArchiveOutcome, operation: Callable[[], None]
    ) -> None:
        """Run the archive index write in a bounded worker, logging failures."""
        from ciao.async_reads import run_read

        try:
            await run_read(f"archive-index:{outcome.path}", operation)
        except Exception:  # noqa: BLE001 — archiving already succeeded
            logger.exception(
                "FTS search: failed to index archived file %s for chat %s",
                outcome.path,
                chat_id,
            )

