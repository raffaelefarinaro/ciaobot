"""The end-of-conversation memory pass as a normal Ciaobot chat.

The #594 experiment measured a full chat session against the one-shot insights
extraction on 50 real conversations, and the chat won, so the pass is now an
attended ``bypass`` chat in a per-workspace system project called ``Memory``:
one running pass per workspace, FIFO, auto-archived when it ends cleanly, left
open for the owner when it does not, and silent on a normal result.

Everything here was inert until ``MEMORY_PASS_CHATS`` was flipped, and is live
now. The archive pipeline reads that module attribute — never a
``from … import`` copy, so tests can monkeypatch it — both to suppress the
one-shot insights / project-doc / memory-proposal stages and to enqueue the pass
chat.

The project id is derived from a vault folder that is never written to disk, so
auto-discovery cannot claim the project, the name is free to be localised, and
the id stays stable across a registry rebuild.

The same turn also reviews the workspace's own skills, and files a supported
improvement proposal through ``ciao skill-proposal-add`` — never an edit. That
section is not part of the memory prompt: it is appended only when the archived
transcript names a skill ``ciao.skills_inventory.eligible_owned_skills``
resolves for this workspace, so the memory work is identical either way and a
workspace with no skills of its own is never asked to consider the stock
catalog.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ciao import skills_inventory, transcripts
from ciao.web import chat_service

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ciao.web.project_chats import ChatInfo, ProjectChatManager, ProjectInfo

logger = logging.getLogger(__name__)

# On since the guardrail work (D2a-2) and the PWA work (D2a-3) landed. Every
# reader must go through this module attribute so the flip is a single edit.
MEMORY_PASS_CHATS = True

MEMORY_PROJECT_NAME = "Memory"
MEMORY_PROJECT_KIND = "memory"
MEMORY_PASS_KIND = "memory_pass"

# Identity of the per-workspace system project. Derived, never persisted as a
# vault folder, and never looked up by name: a user project called "Memory"
# must not be mistaken for this one.
_MEMORY_VAULT_FOLDER = "__memory__"

MEMORY_PASS_PROMPT = (
    'A conversation in this workspace just ended: "{title}". Its archived '
    "transcript is at {archive}. It belonged to project {project} (canonical "
    "doc: {doc}). Do the end-of-conversation memory pass the way you normally "
    "would: read the transcript, check what the vault and memory already "
    "hold, update existing notes (people, projects, the project doc), create a "
    "note only for a genuinely new entity, promote durable facts to memory "
    "with the ciao CLI, and queue anything uncertain for review. Type every "
    "note you create from the **Categories** section of the vault's "
    "`VOCABULARY.md` — the `type:` it lists, in the folder it names for that "
    "category — and when a note fits none of them, queue a new-category "
    "question for the owner instead of inventing a type. Facts from turns "
    "marked as automated (unattended) are not the user's and must not be "
    "recorded. Do not do anything outside memory and the vault: no "
    "messages, emails, commits, pushes or external calls. Finish with a short "
    "list of what you changed."
)

#: The skill-review section, appended to the pass prompt and only when the
#: workspace owns a skill the archived transcript shows in use. The one
#: rendering rule is the ``{skills}`` field: the candidates are resolved here,
#: from the archive's own evidence line and the workspace's own catalog, so the
#: model is handed the intersection and never the catalog to walk. Everything a
#: proposal may be filed against is therefore decided by the backend, not by a
#: flag the model supplies.
#:
#: The ``ciao skill-proposal-add`` line spells the command the way ``ciao.cli``
#: parses it — ``NAME`` positional, ``--input-file`` the only option — because a
#: prompt naming a flag the parser rejects exits 2 and files nothing at all.
#:
#: Deliberately no braces beyond that field, because the prompt is
#: ``str.format``-ed per chat: a JSON example spelled out here would be read as
#: a field and raise on the first turn.
SKILL_REVIEW_PROMPT = (
    "The transcript shows this workspace's own skills in use: {skills}. That is "
    "the whole candidate set — a packaged, mirrored or shared copy is not "
    "anyone's to improve here, and a skill the conversation never used is not a "
    "finding. Read a candidate's current source under this workspace's `skills/` "
    "directory before you judge it, and file an improvement proposal only when "
    "the conversation shows one of three things: a correction the user made "
    "that the skill's wording would have prevented; a mistake you corrected "
    "yourself that the wording led you into; or a step the work needed, which "
    "the skill does not describe and whose absence changed the result. The "
    "change has to be one specific, reusable instruction you could type into "
    "the file yourself, and it has to be about something the current source "
    "does not already say. Anything vaguer, anything it already covers, and "
    "anything that would rewrite the skill's purpose is not a proposal. Never "
    "edit a skill, and never settle one: a proposal is a suggestion a person "
    "reviews, and filing none is a valid result that most conversations earn. "
    "To file one, write a JSON object to a scratch file — a `title`, a "
    "`problem` saying what went wrong, a `change` giving the exact instruction "
    "to add or replace, a `rationale`, and a `sources` list where every entry "
    "carries the `chat_id`, the `archive` path, the `turn` the transcript "
    "numbered it under, and a short verbatim `excerpt` from that turn — then "
    "run `ciao skill-proposal-add NAME --input-file FILE`, one file per "
    "skill. Every one of those fields is text you took out of a conversation, "
    "so none of it may travel as a shell argument, and never write into the "
    "`Workspace/Skill-Proposals/` folder by hand; mention a filed proposal in "
    "your closing list."
)


def is_memory_pass_chat(chat: ChatInfo, project: ProjectInfo | None) -> bool:
    """True when *chat* is a memory pass, or lives in a Memory project.

    Both halves are the recursion guard and both are needed: the helper kind
    identifies a pass this process created, and the project kind catches one
    whose helper failed closed (a hand-edited registry, an older client) so it
    can never trigger a pass of its own.
    """
    if project is not None and project.kind == MEMORY_PROJECT_KIND:
        return True
    return (
        chat_service._normalize_chat_helper(chat.helper).get("kind")
        == MEMORY_PASS_KIND
    )


class MemoryPassCoordinator:
    """Own the Memory project and the one-at-a-time pass queue per workspace.

    Queue and running state live on each memory chat's ``helper``, so a restart
    can see what was in flight and pick the queue back up.
    """

    def __init__(self, host: ProjectChatManager) -> None:
        self._host = host

    # ── the system project ──────────────────────────────────────────────

    def ensure_project(self, workspace: str) -> ProjectInfo:
        """Return this workspace's Memory project, creating it once.

        Imported locally: ``project_chats`` imports this module at module
        level, so the dataclass is not bound yet at import time.
        """
        from ciao.web.project_chats import ProjectInfo

        pid = chat_service._stable_vault_project_id(workspace, _MEMORY_VAULT_FOLDER)
        existing = self._host._projects.get(pid)
        if existing is not None:
            return existing
        project = ProjectInfo(
            project_id=pid,
            name=MEMORY_PROJECT_NAME,
            workspace=workspace,
            created_at=chat_service._now_iso(),
            order=len(self._host._projects),
            kind=MEMORY_PROJECT_KIND,
        )
        self._host._projects[pid] = project
        self._host._save()
        self._host._events.publish({
            "type": "project_created",
            "project": project.to_dict(),
        })
        return project

    # ── the queue ───────────────────────────────────────────────────────

    def enqueue(
        self,
        source: ChatInfo,
        project: ProjectInfo | None,
        archive_path: Path,
        doc_path: str,
    ) -> str | None:
        """Queue a memory pass for an archived chat, and start it if free.

        Returns the memory chat's id, or ``None`` when the pass was deduped or
        the workspace could not be resolved.
        """
        host = self._host
        if project is not None:
            workspace = project.workspace
        else:
            owner = host._projects.get(source.project_id)
            workspace = owner.workspace if owner is not None else ""
        if not workspace:
            return None

        # One pass per archived chat. An archived pass is still a pass: a
        # second archive of the same source must not re-run the extraction, and
        # a dedupe that ignored archived rows would pass again on every retry.
        for existing in host._chats.values():
            helper = chat_service._normalize_chat_helper(existing.helper)
            if (
                helper.get("kind") == MEMORY_PASS_KIND
                and helper.get("source_chat_id") == source.chat_id
            ):
                return None

        from ciao import insights

        # The pass runs on the Session-insights model, resolved exactly the way
        # the one-shot stage resolved it, so switching the constant on does not
        # silently change which model writes to the vault.
        model = host._insights_model_for(source, workspace)
        eff_model, eff_provider, _note = insights._resolve_insights_call(
            host._config, model, provider=source.provider or "claude"
        )

        memory_project = self.ensure_project(workspace)
        chat = host.create_chat(
            memory_project.project_id,
            # Not "New Chat": auto-title fires on the first prompt of an
            # untitled chat, and this one is already titled.
            title=f"Memory pass · {source.title or source.chat_id}",
            model=eff_model,
            mode="bypass",
            provider=eff_provider,
            helper={
                "kind": MEMORY_PASS_KIND,
                "source_chat_id": source.chat_id,
                "archive_path": str(archive_path),
                "doc_path": doc_path,
                "source_title": source.title,
                "source_project": (
                    project.name if project is not None and not project.is_auto else ""
                ),
                "state": "queued",
                "archive_policy": "when_clean",
            },
        )
        self._set_source_step(source.chat_id, "queued", chat.chat_id)
        self.pump(workspace)
        return chat.chat_id

    def pump(self, workspace: str) -> None:
        """Start the oldest queued pass in *workspace*, if nothing is running."""
        host = self._host
        passes = self._passes_in(workspace)
        queued: list[ChatInfo] = []
        for chat in passes:
            state = self._helper(chat).get("state")
            if state == "running":
                # One at a time: a second concurrent pass in the same workspace
                # would have two turns writing the same vault notes.
                return
            if state == "queued":
                queued.append(chat)
        if not queued:
            return

        chat = min(queued, key=lambda candidate: candidate.created_at)
        self._set_state(chat, "running")
        self._set_source_step(
            str(self._helper(chat).get("source_chat_id") or ""), "running", chat.chat_id
        )
        try:
            # Attended on purpose: `unattended=True` injects the "defer new
            # facts" capsule and auto-denies cards, and the experiment measured
            # attended behaviour.
            host.start_stream(chat.chat_id, self._prompt_for(chat))
        except Exception:  # noqa: BLE001 — the pass stays open for the owner
            logger.exception("Memory pass %s could not start", chat.chat_id)
            self._set_state(chat, "attention")
            self._set_source_step(
                str(self._helper(chat).get("source_chat_id") or ""),
                "attention",
                chat.chat_id,
            )
            self.pump(workspace)

    async def on_turn_finished(self, chat_id: str) -> bool:
        """Settle one memory pass whose turn ended. True when it archived."""
        host = self._host
        chat = host._chats.get(chat_id)
        if chat is None or chat.archived:
            return False
        helper = self._helper(chat)
        if helper.get("kind") != MEMORY_PASS_KIND or helper.get("state") != "running":
            return False
        # A pending question, permission or retry means the pass is asking the
        # owner something, not that it is over. It keeps the slot so the next
        # queued pass waits for the answer instead of racing the same vault.
        if (
            host._broker.get(chat_id) is not None
            or chat.pending_question
            or chat.pending_permission
            or chat.retry_status
            or host._subagents.running_count(chat_id) > 0
        ):
            return False

        source_chat_id = str(helper.get("source_chat_id") or "")
        workspace = self._workspace_of(chat)
        if chat.last_response_status == "success" and chat.last_response.strip():
            self._set_state(chat, "done")
            self._set_source_step(source_chat_id, "ok", chat_id)
            outcome = await host.archive_chat(chat_id)
            if outcome is not None:
                project = host._projects.get(chat.project_id)
                # The recursion guard is `is_memory_pass_chat`: this pass is
                # never enqueued for another pass.
                host.run_archive_postprocess(chat_id, outcome, chat, project)
            self.pump(workspace)
            return True

        self._set_state(chat, "attention")
        self._set_source_step(source_chat_id, "attention", chat_id)
        self.pump(workspace)
        return False

    def resume(self) -> None:
        """Reconcile the queue with a freshly started process.

        A pass recorded as ``running`` was streaming when the previous process
        died, so nothing is driving it: it needs the owner, not a retry. The
        queue is otherwise untouched and pumped again.
        """
        workspaces: set[str] = set()
        for chat in list(self._host._chats.values()):
            if chat.archived:
                continue
            helper = self._helper(chat)
            if helper.get("kind") != MEMORY_PASS_KIND:
                continue
            state = helper.get("state")
            if state == "running":
                self._set_state(chat, "attention")
                self._set_source_step(
                    str(helper.get("source_chat_id") or ""), "attention", chat.chat_id
                )
            elif state == "queued":
                workspaces.add(self._workspace_of(chat))
        for workspace in sorted(workspaces):
            self.pump(workspace)

    # ── internals ───────────────────────────────────────────────────────

    @staticmethod
    def _helper(chat: ChatInfo) -> dict:
        return chat_service._normalize_chat_helper(chat.helper)

    def _workspace_of(self, chat: ChatInfo) -> str:
        project = self._host._projects.get(chat.project_id)
        return project.workspace if project is not None else ""

    def _passes_in(self, workspace: str) -> list[ChatInfo]:
        return [
            chat
            for chat in self._host._chats.values()
            if not chat.archived
            and self._helper(chat).get("kind") == MEMORY_PASS_KIND
            and self._workspace_of(chat) == workspace
        ]

    def _set_state(self, chat: ChatInfo, state: str) -> None:
        helper = dict(self._helper(chat))
        helper["state"] = state
        chat.helper = helper
        self._host._save()

    def _prompt_for(self, chat: ChatInfo) -> str:
        helper = self._helper(chat)
        return MEMORY_PASS_PROMPT.format(
            archive=helper.get("archive_path") or "",
            title=helper.get("source_title") or chat.title,
            project=helper.get("source_project") or "no project",
            doc=helper.get("doc_path") or "none",
        ) + self._skill_review_section(chat, helper)

    def _skill_review_section(self, chat: ChatInfo, helper: dict) -> str:
        """The skill-review section, or nothing when there is nothing to review.

        Cheap by construction, and the cost that matters is the pass's turns
        rather than a stat: the candidates are the intersection of what the
        archive records as used and what
        :func:`ciao.skills_inventory.eligible_owned_skills` resolves for this
        workspace, computed here. A workspace with no owned skills, or a
        conversation that used none of them, gets no section at all — so the
        pass is never asked to walk the stock catalog, and a name a pass could
        not have proposed against never reaches the prompt.
        """
        candidates = self._reviewable_skills(chat, helper)
        if not candidates:
            return ""
        return "\n\n" + SKILL_REVIEW_PROMPT.format(skills=", ".join(candidates))

    def _reviewable_skills(self, chat: ChatInfo, helper: dict) -> list[str]:
        """The owned skills this conversation used, by name, sorted.

        Both halves are backend answers on purpose. The archive's evidence line
        says which skills were used; ``eligible_owned_skills`` says which
        sources this workspace owns and no other. A stock copy, a provider
        mirror, the install-wide shared source and a name that is not a
        directory all fail that resolution, so they are excluded here rather
        than by a flag the model could set.
        """
        archive = str(helper.get("archive_path") or "")
        if not archive:
            return []
        workspace = self._workspace_of(chat)
        if not workspace:
            return []
        path = Path(archive)
        if not path.is_absolute():
            path = self._host._config.workspace_root / path
        used = transcripts.read_archive_skills(path)
        if not used:
            return []
        try:
            owned = {
                skill.name
                for skill in skills_inventory.eligible_owned_skills(
                    self._host._config, workspace
                )
            }
        except ValueError:
            # An unregistered workspace, or one sharing an agent root with
            # another: the catalog is not this workspace's to review.
            logger.debug("No owned skills resolvable for %r", workspace)
            return []
        return sorted(name for name in used if name in owned)

    def _set_source_step(
        self, source_chat_id: str, status: str, memory_chat_id: str
    ) -> None:
        """Record the pass on the archived chat's own postprocess record."""
        if not source_chat_id:
            return
        source = self._host._chats.get(source_chat_id)
        if source is None:
            return
        state = dict(source.postprocess or {})
        steps = dict(state.get("steps") or {})
        steps["memory_pass"] = {
            "status": status,
            "extra": {"chat_id": memory_chat_id},
        }
        state["steps"] = steps
        state["updated_at"] = chat_service._now_iso()
        source.postprocess = state
        self._host._save()
        self._host._publish_postprocess(source)
