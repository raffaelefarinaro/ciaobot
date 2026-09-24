"""Archive post-processing lifecycle collaboration.

``ProjectChatManager`` keeps the chat/project registry, archive write API, and
coordination of provider/chat lifecycle.  ``ArchivePipeline`` owns the work
that happens after an archive is written: the live postprocess record, durable
archive-job manifests, retry/resume/tombstone handling, and archive indexing
hooks.  The manager-facing methods remain thin delegating seams, and calls back
into patched manager methods go through the explicit host protocol below.

The collaborator is deliberately a behavior-preserving move.  It does not
redefine archive persistence, stage ordering, retry budgets, events, or delete
semantics; it only owns their lifecycle state and coordination.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from ciao import job_runs
from ciao.archive_jobs import ArchiveJob
from ciao.config import CiaoConfig
from ciao.web import chat_service
from ciao.web.chat_broker import EventsHub
from ciao.workspace_guide import guide_path as workspace_guide_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ciao.web.project_chats import ArchiveOutcome, ChatInfo, ProjectInfo

logger = logging.getLogger(__name__)

ArchiveInputs = dict[str, object]


def _input_archive_path(inputs: ArchiveInputs) -> Path:
    return cast(Path, inputs["archive_path"])


def _input_trajectory_meta(inputs: ArchiveInputs) -> dict[str, object]:
    raw = inputs.get("trajectory_meta")
    return cast(dict[str, object], raw) if isinstance(raw, dict) else {}


class ArchivePipelineHost(Protocol):
    """The complete typed manager surface used by ``ArchivePipeline``."""

    _config: CiaoConfig
    _chats: dict[str, ChatInfo]
    _projects: dict[str, ProjectInfo]
    _runtime_root: Path
    _loop: asyncio.AbstractEventLoop | None
    _detached_tasks: set[asyncio.Task[object]]

    @property
    def events(self) -> EventsHub: ...

    def _save(self, *, reason: str = ...) -> None: ...

    def _workspace_vault_root(self, workspace: str) -> Path: ...

    def _spawn_detached(
        self, coro: Coroutine[object, object, object], name: str
    ) -> asyncio.Task[object]: ...

    def _on_job_event(self, event: dict[str, object]) -> None: ...

    def _apply_job_event(
        self, chat_id: str, event: dict[str, object]
    ) -> None: ...

    def _publish_postprocess(self, chat: ChatInfo) -> None: ...

    def retry_insights(self, chat_id: str) -> str: ...

    def archive_job_view(self, chat_id: str) -> dict[str, object] | None: ...

    def _make_archive_index_operation(
        self, outcome: ArchiveOutcome
    ) -> Callable[[], None]: ...

    def _run_archive_index_best_effort(
        self, chat_id: str, outcome: ArchiveOutcome, operation: Callable[[], None]
    ) -> None: ...

    def _index_archive_file_off_loop(
        self, chat_id: str, outcome: ArchiveOutcome, operation: Callable[[], None]
    ) -> Coroutine[object, object, None]: ...

    def _begin_postprocess(self, chat_id: str, expected: list[str]) -> None: ...

    def _end_postprocess(self, chat_id: str) -> None: ...

    def _tracked_postprocess(
        self, chat_id: str, coro: Coroutine[object, object, object]
    ) -> Coroutine[object, object, None]: ...

    def _run_job(
        self,
        chat_id: str,
        job: ArchiveJob,
        inputs: ArchiveInputs,
        *,
        stages: list[str] | None = ...,
    ) -> Coroutine[object, object, None]: ...

    def _overlay_job_postprocess(self, chat_id: str, job: ArchiveJob) -> None: ...

    def _resume_job(
        self, chat_id: str, archive_path: Path
    ) -> tuple[ArchiveJob, ArchiveInputs] | tuple[None, None]: ...

    def _launch_job(
        self,
        chat_id: str,
        job: ArchiveJob,
        inputs: ArchiveInputs,
        *,
        stages: list[str] | None = ...,
    ) -> None: ...

    def _new_job_for_chat(self, chat: ChatInfo, inputs: ArchiveInputs) -> ArchiveJob: ...

    def _archive_path_for_chat(self, chat: ChatInfo) -> Path: ...

    def _job_inputs(
        self,
        chat: ChatInfo,
        project: ProjectInfo | None,
        *,
        filtered_jsonl: str = ...,
        session_id: str = ...,
        text_mode: bool = ...,
    ) -> ArchiveInputs: ...

    def _restore_job_inputs(
        self, chat: ChatInfo, project: ProjectInfo | None, job: ArchiveJob
    ) -> ArchiveInputs: ...

    def _persist_job_inputs(self, job: ArchiveJob, inputs: ArchiveInputs) -> None: ...

    def _insights_model_for(self, chat: ChatInfo, workspace: str) -> str: ...


class ArchivePipeline:
    """Own archive postprocess state, manifests, retries, and completion hooks."""

    def __init__(self, host: ArchivePipelineHost) -> None:
        self._host = host
        self._postprocessing: set[str] = set()
        self._jobs: dict[str, ArchiveJob] = {}
        self._tasks: dict[str, asyncio.Task[object]] = {}

    @property
    def postprocessing(self) -> set[str]:
        return self._postprocessing

    @property
    def jobs(self) -> dict[str, ArchiveJob]:
        return self._jobs

    @property
    def tasks(self) -> dict[str, asyncio.Task[object]]:
        return self._tasks

    def attach_job_runs_publisher(self) -> None:
        """Route live job-run events into this manager. Called once at startup.

        Kept out of ``__init__`` on purpose: the publisher is a module-level
        global in :mod:`ciao.job_runs`, and tests build managers freely. Only
        the process that actually serves the PWA should claim it."""
        from ciao import job_runs

        job_runs.set_publisher(self._host._on_job_event)

    def _on_job_event(self, event: dict[str, object]) -> None:
        """Publisher installed into :mod:`ciao.job_runs`. Never raises.

        Job steps can finish on a worker thread, so this hops back onto the
        manager's loop before touching EventsHub."""
        try:
            chat_id = str(event.get("chat_id") or "")
            if not chat_id or chat_id not in self._host._chats:
                return
            loop = self._host._loop
            running = None
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if loop is None and running is not None:
                # Constructed outside a loop (tests, and any future call path):
                # adopt the first loop we are actually called on, so later
                # off-thread events still have somewhere to marshal to.
                self._host._loop = loop = running
            if loop is not None and running is not loop:
                loop.call_soon_threadsafe(self._host._apply_job_event, chat_id, event)
                return
            self._host._apply_job_event(chat_id, event)
        except Exception:  # noqa: BLE001 — telemetry must never break a job
            logger.debug("Failed to handle job event", exc_info=True)

    def _apply_job_event(self, chat_id: str, event: dict[str, object]) -> None:
        """Fold one step event into the chat's postprocess record and announce."""
        try:
            chat = self._host._chats.get(chat_id)
            if chat is None:
                return
            # Only fold steps into a pipeline that is actually running. A tracked
            # job that merely carries a chat_id (a one-off re-run, say) would
            # otherwise create a half-record with no `state`, which every reader
            # then has to treat as neither running nor finished.
            if chat_id not in self._postprocessing:
                return
            state = dict(chat.postprocess or {})
            steps = dict(state.get("steps") or {})
            job = str(event.get("job") or "")
            if not job:
                return
            if event.get("event") == "started":
                state["step"] = job
            else:
                extra = event.get("extra")
                steps[job] = {
                    "status": str(event.get("status") or "ok"),
                    "extra": dict(extra) if isinstance(extra, dict) else {},
                }
                state["steps"] = steps
                # Leave `step` pointing at the last thing that ran: between two
                # steps there is no current one, and blanking it would make the
                # UI flicker back to a generic label for a few milliseconds.
            state["updated_at"] = chat_service._now_iso()
            chat.postprocess = state
            self._host._publish_postprocess(chat)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to apply job event for %s", chat_id, exc_info=True)

    def _publish_postprocess(self, chat: ChatInfo) -> None:
        self._host.events.publish({
            "type": "chat_postprocess",
            "chat_id": chat.chat_id,
            "project_id": chat.project_id,
            "postprocess": dict(chat.postprocess or {}),
        })

    def postprocessing_chat_ids(self) -> list[str]:
        """Chats whose post-archive pipeline is running, for the connect
        snapshot: a client that joins mid-pipeline must not miss it."""
        return sorted(self._postprocessing)

    def _begin_postprocess(self, chat_id: str, expected: list[str]) -> None:
        chat = self._host._chats.get(chat_id)
        if chat is None:
            return
        self._postprocessing.add(chat_id)
        chat.postprocess = {
            "state": "running",
            "step": expected[0] if expected else "",
            "expected": list(expected),
            "steps": {},
            "started_at": chat_service._now_iso(),
            "updated_at": chat_service._now_iso(),
        }
        self._host._publish_postprocess(chat)

    def _end_postprocess(self, chat_id: str) -> None:
        self._postprocessing.discard(chat_id)
        chat = self._host._chats.get(chat_id)
        if chat is None:
            return
        state = dict(chat.postprocess or {})
        # Settle to the manifest's own outcome: a job that still has failed
        # stages is "incomplete" (retryable), one that needs a config/human
        # change is "blocked", and only an all-settled job is "done". Without
        # this a partly-failed pipeline would report success and hide its
        # retry affordance.
        job = self._jobs.get(chat_id)
        job_state = getattr(job, "state", "") if job is not None else ""
        if job_state in ("incomplete", "blocked"):
            state["state"] = job_state
        else:
            state["state"] = "done"
        state["step"] = ""
        state["updated_at"] = chat_service._now_iso()
        chat.postprocess = state
        # Persisted so an archived chat can still report what was learned from
        # it after a restart — the run log rotates, this does not.
        self._host._save()
        self._host._publish_postprocess(chat)

    async def _tracked_postprocess(self, chat_id: str, coro: Coroutine[object, object, object]) -> None:
        """Own the pipeline's start/finish around the existing task body."""
        try:
            await coro
        finally:
            self._host._end_postprocess(chat_id)

    def retry_insights(self, chat_id: str) -> str:
        """Resume the unfinished post-archive stages for an archived chat.

        An archive that already carries insights but whose project fold,
        trajectory or memory writes never landed is exactly the case this
        repairs (see ``ciao/archive_jobs.py``). Returns ``"started"`` when a
        resume task is launched, ``"running"`` when the chat's pipeline is
        already live, ``"complete"`` when nothing is left to do, ``"blocked"``
        when the job needs a config/human change, or ``"not_found"`` /
        ``"not_archived"`` / ``"no_archive"`` for the non-starts. Used by
        ``/api/chats/{chat_id}/retry-insights``.

        The method name is kept for route/back-compat; PWA_API.md documents it
        as "retry unfinished steps".
        """
        chat = self._host._chats.get(chat_id)
        if chat is None:
            return "not_found"
        if not chat.archived:
            return "not_archived"
        if not chat.archive_path:
            return "no_archive"
        if chat_id in self._postprocessing:
            return "running"

        archive_path = self._host._archive_path_for_chat(chat)
        if not archive_path.exists():
            return "no_archive"

        job, inputs = self._host._resume_job(chat_id, archive_path)
        if job is None or inputs is None:
            return "no_archive"
        if job.tombstoned:
            return "complete"
        if not job.unfinished():
            return "complete"
        # An explicit user retry is a deliberate action: always clear failed
        # stages, blocks, and exhausted attempt budgets before launching, even
        # when `resumable()` is nominally non-empty because a *dependent*
        # pending stage kept it so. Otherwise an exhausted `insights` whose
        # dependents are still pending would be skipped, and the launch would
        # run only work that immediately waits for it — a silent no-op retry.
        job.reset_failed(include_blocked=True)
        if not job.resumable():
            return "complete"
        self._host._launch_job(chat_id, job, inputs)
        return "started"

    def retry_archive_steps(self, chat_id: str) -> dict[str, object]:
        """Retry every unfinished stage and report the manifest to the caller.

        The richer sibling of :meth:`retry_insights` used by the postprocess
        UI: same launch path, but it returns the current manifest view so the
        archived-chat panel can render partial completion immediately.
        """
        status = self._host.retry_insights(chat_id)
        return {"status": status, "job": self._host.archive_job_view(chat_id)}

    def archive_job_view(self, chat_id: str) -> dict[str, object] | None:
        """The persisted manifest for a chat, projected for the PWA, or None."""
        from ciao.archive_jobs import load_job, manifest_view, new_job_id

        chat = self._host._chats.get(chat_id)
        if chat is None or not chat.archive_path:
            return None
        job = self._jobs.get(chat_id)
        if job is None:
            job = load_job(
                self._host._runtime_root, new_job_id(chat_id, chat.archive_path)
            )
        if job is None:
            return None
        return manifest_view(job)

    def _delete_archived_transcript(self, chat_id: str) -> None:
        """Remove a deleted chat's archived transcript directory.

        Without this, an explicit delete does not stick. `_discover_archived_chats`
        treats ``<logs_root>/Chats`` as the source of truth and re-imports any
        directory that is not in the registry, so the very next `list_projects()`
        poll brought the chat back — with the same ``chat_id`` and
        ``archive_path``, hence the same `new_job_id`, which `_cancel_archive_job`
        had just tombstoned for good. The resurrected chat could therefore never
        run insights, the project-doc fold, trajectories or memory proposals
        again, and `retry_insights` reported "complete" for a pipeline that had
        never run.

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

    def _cancel_archive_job(
        self, chat_id: str, chat: ChatInfo | None = None
    ) -> None:
        """Tombstone a deleted chat's archive job and stop its live task.

        Called on explicit delete. The tombstone is the durable half: it is
        written even when the manifest does not exist yet, and ``save_job``
        refuses to clear it, so a task that finishes after this point (or a
        startup resume on the next boot) cannot recreate work for the chat.

        ``chat`` is passed in because ``delete_chat`` pops the row from the
        registry first: looking it up here found nothing, so the on-disk
        manifest lookup and the "no manifest yet" tombstone were both dead and
        a deleted chat could leave a live job record behind.
        """
        from ciao.archive_jobs import load_job, tombstone_job

        # Cancel an in-flight stage first: a task awaiting a model call would
        # otherwise resume after the delete and write derived state. The
        # tombstone below then stops any next stage and any startup resume.
        task = self._tasks.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()
        if chat is None:
            chat = self._host._chats.get(chat_id)
        job = self._jobs.pop(chat_id, None)
        if job is None and chat is not None and chat.archive_path:
            from ciao.archive_jobs import new_job_id

            job = load_job(self._host._runtime_root, new_job_id(chat_id, chat.archive_path))
        if job is not None:
            job.tombstoned = True
            job.state = "tombstoned"
            job.blocked_reason = "chat deleted"
            job.save()
            return
        # No manifest yet: create the tombstone so a racing task cannot write
        # one afterwards.
        if chat is not None and chat.archive_path:
            from ciao.archive_jobs import new_job_id

            tombstone_job(
                self._host._runtime_root,
                new_job_id(chat_id, chat.archive_path),
                reason="chat deleted",
                chat_id=chat_id,
                archive_path=chat.archive_path,
            )

    # ── Archive job wiring ────────────────────────────────────────────────

    def _archive_path_for_chat(self, chat: ChatInfo) -> Path:
        archive_path = Path(chat.archive_path)
        if not archive_path.is_absolute():
            archive_path = self._host._config.workspace_root / archive_path
        return archive_path

    def _job_inputs(
        self,
        chat: ChatInfo,
        project: ProjectInfo | None,
        *,
        filtered_jsonl: str = "",
        session_id: str = "",
        text_mode: bool = False,
    ) -> dict[str, object]:
        """Resolve every stage input for one chat's archive job.

        Paths are re-derived from the live config rather than stored, so a
        resume after a workspace move still finds the right guide/vault; the
        JSON-safe subset is persisted on the manifest by
        :meth:`_persist_job_inputs`.
        """
        config = self._host._config
        workspace = project.workspace if project else ""
        is_system_chat = False
        if chat.schedule_id:
            from ciao.schedules import is_system_schedule_id

            is_system_chat = is_system_schedule_id(chat.schedule_id)
        trajectories_enabled = bool(
            getattr(config, "trajectories_enabled", True)
            and session_id
            and filtered_jsonl
        )
        project_doc_path = (
            project.vault_doc_path
            if project and not project.is_auto and not is_system_chat
            else ""
        )
        proposal_vault_root = (
            self._host._workspace_vault_root(workspace) if workspace else None
        )
        guide_path = (
            workspace_guide_path(config.agent_root(workspace))
            if workspace and config.workspace(workspace) is not None
            else None
        )
        return {
            "archive_path": self._host._archive_path_for_chat(chat),
            "config": config,
            "model": self._host._insights_model_for(chat, workspace),
            "provider": chat.provider or "claude",
            "session_id": session_id,
            "filtered_jsonl": filtered_jsonl,
            "text_mode": text_mode,
            "trajectory_meta": {
                "context": project.context if project else "",
                "project_id": chat.project_id,
                "chat_id": chat.chat_id,
                "task_summary": chat.title,
                "workspace": workspace,
            },
            "workspace_root": config.workspace_root,
            "vault_root": config.vault_root,
            "proposal_vault_root": proposal_vault_root,
            "guide_path": guide_path,
            "trajectories_enabled": trajectories_enabled,
            "memory_proposals_enabled": True,
            "project_doc_path": project_doc_path,
        }

    def _insights_model_for(self, chat: ChatInfo, workspace: str) -> str:
        from ciao.insights import resolve_insights_model

        insights_models = getattr(self._host._config, "provider_insights_models", {}) or {}
        return insights_models.get(chat.provider or "", "") or resolve_insights_model(
            self._host._config, workspace or None, chat.provider or None
        )

    def _persist_job_inputs(self, job: ArchiveJob, inputs: dict[str, object]) -> None:
        """Store the JSON-safe subset a resume needs on the manifest."""
        job.inputs.update(
            {
                "model": str(inputs.get("model") or ""),
                "provider": str(inputs.get("provider") or "claude"),
                "session_id": str(inputs.get("session_id") or ""),
                "filtered_jsonl": str(inputs.get("filtered_jsonl") or ""),
                "text_mode": bool(inputs.get("text_mode", False)),
                "trajectory_meta": dict(_input_trajectory_meta(inputs)),
                "trajectories_enabled": bool(inputs.get("trajectories_enabled", True)),
                "memory_proposals_enabled": bool(
                    inputs.get("memory_proposals_enabled", True)
                ),
                "project_doc_path": str(inputs.get("project_doc_path") or ""),
                "workspace": str(
                    _input_trajectory_meta(inputs).get("workspace", "")
                ),
            }
        )

    def _restore_job_inputs(
        self, chat: ChatInfo, project: ProjectInfo | None, job: ArchiveJob
    ) -> dict[str, object]:
        """Rebuild live stage inputs from a persisted manifest + live config."""
        meta = dict(job.inputs.get("trajectory_meta") or {})
        workspace = str(job.inputs.get("workspace") or "")
        if not workspace and project is not None:
            workspace = project.workspace
        config = self._host._config
        guide_path = (
            workspace_guide_path(config.agent_root(workspace))
            if workspace and config.workspace(workspace) is not None
            else None
        )
        proposal_vault_root = (
            self._host._workspace_vault_root(workspace) if workspace else None
        )
        return {
            "archive_path": self._host._archive_path_for_chat(chat),
            "config": config,
            "model": str(job.inputs.get("model") or ""),
            "provider": str(job.inputs.get("provider") or chat.provider or "claude"),
            "session_id": str(job.inputs.get("session_id") or ""),
            "filtered_jsonl": str(job.inputs.get("filtered_jsonl") or ""),
            "text_mode": bool(job.inputs.get("text_mode", False)),
            "trajectory_meta": meta,
            "workspace_root": config.workspace_root,
            "vault_root": config.vault_root,
            "proposal_vault_root": proposal_vault_root,
            "guide_path": guide_path,
            "trajectories_enabled": bool(job.inputs.get("trajectories_enabled", True)),
            "memory_proposals_enabled": bool(
                job.inputs.get("memory_proposals_enabled", True)
            ),
            "project_doc_path": str(job.inputs.get("project_doc_path") or ""),
        }

    def _new_job_for_chat(
        self, chat: ChatInfo, inputs: dict[str, object]
    ) -> ArchiveJob:
        from ciao.archive_jobs import archive_content_revision, create_job

        job = create_job(
            self._host._runtime_root,
            chat_id=chat.chat_id,
            archive_path=chat.archive_path,
            content_revision_value=archive_content_revision(_input_archive_path(inputs)),
        )
        self._host._persist_job_inputs(job, inputs)
        job.save()
        self._jobs[chat.chat_id] = job
        return job

    def _resume_job(
        self, chat_id: str, archive_path: Path
    ) -> tuple[ArchiveJob, dict[str, object]] | tuple[None, None]:
        """Load (or seed) the manifest for an archived chat.

        A chat archived before this feature has no manifest, so one is seeded
        from the archive's current state: insights settled when the section is
        present, trajectory unavailable (the raw JSONL is gone), and the fold
        and proposals pending — which is what makes a legacy archive
        repairable.
        """
        from ciao.archive_jobs import (
            SKIPPED,
            SUCCEEDED,
            archive_content_revision,
            create_job,
            load_job,
            new_job_id,
        )

        chat = self._host._chats.get(chat_id)
        if chat is None:
            return None, None
        project = self._host._projects.get(chat.project_id) if chat.project_id else None
        job = self._jobs.get(chat_id)
        if job is None:
            job = load_job(self._host._runtime_root, new_job_id(chat_id, chat.archive_path))
        if job is None:
            inputs = self._host._job_inputs(chat, project, text_mode=True)
            job = create_job(
                self._host._runtime_root,
                chat_id=chat_id,
                archive_path=chat.archive_path,
                content_revision_value=archive_content_revision(archive_path),
            )
            self._host._persist_job_inputs(job, inputs)
            from ciao.insights import _has_insights_section

            if _has_insights_section(archive_path):
                job.mark("insights", SUCCEEDED)
            if not job.inputs.get("filtered_jsonl"):
                job.mark("trajectory", SKIPPED, "raw session no longer available")
            job.save()
            self._jobs[chat_id] = job
        else:
            inputs = self._host._restore_job_inputs(chat, project, job)
            self._jobs[chat_id] = job
        return job, inputs

    def _launch_job(
        self,
        chat_id: str,
        job: ArchiveJob,
        inputs: dict[str, object],
        *,
        stages: list[str] | None = None,
    ) -> None:
        self._host._begin_postprocess(chat_id, list(stages or job.resumable()))
        task = asyncio.create_task(
            self._host._tracked_postprocess(
                chat_id, self._host._run_job(chat_id, job, inputs, stages=stages)
            )
        )
        # Retained so a delete can cancel an in-flight stage. The tombstone alone
        # is not enough: a stage already awaiting a model call would otherwise
        # resume and write derived state (append insights, fold the doc) after
        # the chat was deleted.
        self._tasks[chat_id] = task

        def _drop_finished(
            _task: asyncio.Task[object], _chat_id: str = chat_id
        ) -> None:
            self._tasks.pop(_chat_id, None)

        task.add_done_callback(_drop_finished)

    async def _run_job(
        self,
        chat_id: str,
        job: ArchiveJob,
        inputs: dict[str, object],
        *,
        stages: list[str] | None = None,
    ) -> None:
        """Check the archive revision, run the stages, settle the record."""
        from ciao.archive_jobs import (
            PENDING,
            RUNNING,
            resume_revision_matches,
        )
        from ciao.insights import run_archive_pipeline

        try:
            # Revision validation runs for every resume, not only an
            # insights-pending one. While insights is still pending/running the
            # recorded revision is the pre-insights one, and the pipeline's own
            # append is accepted only when the on-disk section authenticates
            # against the exact output the pipeline recorded before writing it.
            # Once insights settles, a full-file match against the
            # post-insights revision is required.
            insights_pending = job.status_of("insights") in (PENDING, RUNNING)
            recorded = (
                job.content_revision if insights_pending else job.post_insights_revision
            ) or job.content_revision
            expected_append = job.insights_append_revision if insights_pending else ""
            if not resume_revision_matches(
                _input_archive_path(inputs),
                recorded,
                expected_append_revision=expected_append,
            ):
                if insights_pending:
                    blocked = ["insights"]
                else:
                    # Whatever this resume was actually asked to run and has
                    # not settled — not a hardcoded stage. Blocking
                    # `project_doc_update` unconditionally overwrote the audit
                    # state of a fold that had already succeeded while leaving
                    # the genuinely pending stage untouched, so a retry reset
                    # the fold and could run it a second time.
                    requested = list(stages) if stages else list(job.resumable())
                    blocked = [
                        name
                        for name in requested
                        if job.status_of(name) in (PENDING, RUNNING)
                    ] or list(job.unfinished())
                for name in blocked:
                    job.block(
                        name,
                        "archive content changed since the job was created",
                    )
                job.save()
                return
            await run_archive_pipeline(job, inputs, stages=stages)
        except Exception:  # noqa: BLE001 — the tracked wrapper always settles
            logger.exception("Archive job failed for chat %s", chat_id)
        finally:
            job.save()
            self._host._overlay_job_postprocess(chat_id, job)

    def _overlay_job_postprocess(self, chat_id: str, job: ArchiveJob) -> None:
        """Fold a manifest's stage states into the chat's postprocess record.

        The live step events already fill ``steps`` with counts and paths; the
        manifest adds what they cannot: which stages are still unfinished,
        whether the job is blocked, and the reason. Applied on settle and on
        load so a partially-complete archive reports accurately without a live
        pipeline.
        """
        from ciao.archive_jobs import manifest_view

        chat = self._host._chats.get(chat_id)
        if chat is None:
            return
        view = manifest_view(job)
        state = dict(chat.postprocess or {})
        steps = dict(state.get("steps") or {})
        # Only terminal outcomes become step entries. Pending/running/blocked
        # stages are named by the manifest's `unfinished` list instead; folding
        # them in would make a blocked insights stage read as "insights added".
        terminal = {"ok": "ok", "skipped": "skipped", "error": "error"}
        for name, status in (view.get("steps") or {}).items():
            manifest_status = status.get("status")
            if manifest_status not in terminal:
                continue
            entry = steps.get(name)
            if not isinstance(entry, dict):
                entry = {"status": terminal[manifest_status], "extra": {}}
            entry["manifest_status"] = manifest_status
            steps[name] = entry
        state["steps"] = steps
        state["job"] = view
        if view.get("blocked_reason"):
            state["blocked_reason"] = view["blocked_reason"]
        # Reflect the manifest outcome on the record itself, so a blocked or
        # partly-complete job is visible even before `_end_postprocess` runs.
        if view.get("state") in ("incomplete", "blocked") and state.get("state") != "running":
            state["state"] = view["state"]
        state["updated_at"] = chat_service._now_iso()
        chat.postprocess = state
        self._host._publish_postprocess(chat)

    # ── Startup resume ────────────────────────────────────────────────────

    async def resume_interrupted_jobs(self, *, max_concurrency: int = 2) -> int:
        """Resume eligible local archive jobs left incomplete by a crash.

        Called once at startup after the registry loads. Interrupted ``running``
        stages are made retryable first (nothing is running yet in this
        process), then unfinished jobs with a live chat are resumed with
        bounded concurrency. Blocked and tombstoned jobs are left alone.
        """
        from ciao.archive_jobs import MAX_AUTO_ATTEMPTS, RUNNING, list_jobs

        jobs = list_jobs(self._host._runtime_root)
        semaphore = asyncio.Semaphore(max(1, max_concurrency))
        started = 0
        for job in jobs:
            if job.tombstoned:
                continue
            for name in list(job.stages):
                if job.status_of(name) == RUNNING:
                    # The old process died with the stage in flight; make it
                    # retryable rather than a stuck "running" forever. An
                    # interrupted *final* attempt had already counted toward the
                    # automatic budget, so reset the counter too: otherwise the
                    # stage is pending but immediately excluded by
                    # `resumable()`, and an explicit retry (which resets
                    # failed/running/blocked, not a pending stage) would launch a
                    # pipeline that runs nothing.
                    stage = job.stage(name)
                    stage.status = "pending"
                    if stage.attempts >= MAX_AUTO_ATTEMPTS:
                        stage.attempts = 0
            # The per-stage writes above bypass `mark`, so the job-level state
            # is still the dead process's "running". Recompute it before the
            # save, or a job this pass does not resume (its chat is gone, or it
            # is out of attempts) reports a pipeline that will never move as
            # still in flight.
            job._refresh_state()
            job.save()
            chat = self._host._chats.get(job.chat_id)
            if chat is None or not chat.archived:
                continue
            project = self._host._projects.get(chat.project_id) if chat.project_id else None
            inputs = self._host._restore_job_inputs(chat, project, job)
            if not _input_archive_path(inputs).exists():
                # The archive is gone, so no stage can run. Block (and surface
                # it) rather than silently leaving a stale running/incomplete
                # record that a client then downgrades to done, hiding the
                # retry affordance.
                for name in job.resumable() or job.unfinished():
                    job.block(name, "archive file is missing")
                job.save()
                self._jobs[job.chat_id] = job
                self._host._overlay_job_postprocess(job.chat_id, job)
                continue
            if not job.resumable():
                self._host._overlay_job_postprocess(job.chat_id, job)
                continue
            self._jobs[job.chat_id] = job
            self._host._begin_postprocess(job.chat_id, list(job.resumable()))

            async def _guarded(
                job: ArchiveJob = job, inputs: dict[str, object] = inputs
            ) -> None:
                async with semaphore:
                    await self._host._run_job(job.chat_id, job, inputs)

            task = asyncio.create_task(
                self._host._tracked_postprocess(job.chat_id, _guarded())
            )
            self._host._detached_tasks.add(task)
            task.add_done_callback(self._host._detached_tasks.discard)
            # Also retain it as this chat's live archive task so a delete can
            # cancel it. `_cancel_archive_job` cancels `_archive_tasks`, and a
            # startup-resumed stage awaiting `update_project_doc` would
            # otherwise write the canonical doc after the chat was deleted.
            self._tasks[job.chat_id] = task

            def _drop_resumed(
                _task: asyncio.Task[object], _chat_id: str = job.chat_id
            ) -> None:
                self._tasks.pop(_chat_id, None)

            task.add_done_callback(_drop_resumed)
            started += 1
        return started

    def run_archive_postprocess(
        self,
        chat_id: str,
        outcome: ArchiveOutcome,
        chat_meta: ChatInfo | None,
        project_meta: ProjectInfo | None,
    ) -> None:
        config = self._host._config
        trajectories_enabled = bool(
            getattr(config, "trajectories_enabled", True)
            and outcome.filtered_jsonl is not None
            and outcome.session_id != ""
        )
        run_insights = bool(
            getattr(config, "insights_enabled", False) and outcome.filtered_jsonl
        )
        chat = self._host._chats.get(chat_id)
        if chat is None:
            # Nothing durable to key a manifest on; index the archive below so
            # the file is still searchable.
            pass
        if chat is not None:
            from ciao.archive_jobs import SKIPPED

            # The archive path may not be on the chat yet (this runs right after
            # `archive_chat` set it, but a caller can pass the outcome directly);
            # use the outcome's path as the authoritative one for the job.
            if not chat.archive_path and outcome.path is not None:
                try:
                    chat.archive_path = str(
                        outcome.path.relative_to(self._host._config.workspace_root)
                    )
                except ValueError:
                    chat.archive_path = str(outcome.path)
            inputs = self._host._job_inputs(
                chat,
                project_meta,
                filtered_jsonl=outcome.filtered_jsonl or "",
                session_id=outcome.session_id,
            )
            inputs["archive_path"] = outcome.path
            inputs["trajectories_enabled"] = trajectories_enabled

            # Declare the plan up front so a surface can say "3 steps" honestly
            # and a stage that was never going to run is not reported as a
            # failure. System chats keep insights and memory proposals but skip
            # the project-doc fold (there is no canonical doc to fold into).
            # `project_doc_update` and `memory_proposals` consume the insights
            # text, so they are only planned when extraction actually runs;
            # otherwise there is nothing to fold or route.
            expected: list[str] = []
            if run_insights:
                expected.append("insights")
                if inputs["project_doc_path"]:
                    expected.append("project_doc_update")
            if trajectories_enabled:
                expected.append("trajectory")
            if run_insights and inputs["proposal_vault_root"] is not None:
                expected.append("memory_proposals")

            if expected:
                job = self._host._new_job_for_chat(chat, inputs)
                # Stages that cannot run for this chat settle as skipped now, so
                # the manifest is an accurate plan even before the task starts
                # and a stage that was intentionally never planned is not left
                # pending (which would read as "incomplete" and offer a retry).
                if not run_insights:
                    # Extraction is disabled or there is no transcript, so all
                    # three insights-dependent stages are settled together.
                    job.mark("insights", SKIPPED, "insights disabled or no transcript")
                    job.mark(
                        "project_doc_update", SKIPPED, "no insights extraction planned"
                    )
                    job.mark(
                        "memory_proposals", SKIPPED, "no insights extraction planned"
                    )
                else:
                    if not inputs["project_doc_path"]:
                        job.mark(
                            "project_doc_update", SKIPPED, "no canonical project doc"
                        )
                    if inputs["proposal_vault_root"] is None:
                        if _input_trajectory_meta(inputs).get("workspace"):
                            # The chat runs in a workspace but its vault root did
                            # not resolve: recoverable once the registry is fixed.
                            job.block(
                                "memory_proposals", "workspace owner unavailable"
                            )
                        else:
                            job.mark(
                                "memory_proposals",
                                SKIPPED,
                                "workspace owner unavailable",
                            )
                if not trajectories_enabled:
                    job.mark(
                        "trajectory", SKIPPED, "no session input or trajectories disabled"
                    )
                job.save()

                self._host._begin_postprocess(chat_id, expected)
                task = self._host._spawn_detached(
                    self._host._tracked_postprocess(
                        chat_id, self._host._run_job(chat_id, job, inputs, stages=expected)
                    ),
                    name=f"archive-postprocess-{chat_id}",
                )
                # Retain as the chat's live archive task so a delete can cancel
                # a stage that is mid-model-call; see `_cancel_archive_job`.
                self._tasks[chat_id] = task

                def _drop_archive(
                    _task: asyncio.Task[object], _cid: str = chat_id
                ) -> None:
                    self._tasks.pop(_cid, None)

                task.add_done_callback(_drop_archive)

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



# Compatibility aliases for callers that describe the owner by its role.
ArchivePostprocess = ArchivePipeline
ArchivePostprocessing = ArchivePipeline
