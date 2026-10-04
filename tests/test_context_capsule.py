from __future__ import annotations

import inspect
from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.context.capsule import TASK_BOARD_HINT, build_context_capsule, context_digest
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def test_capsule_contains_routing_facts_and_no_retrieval() -> None:
    """Recall is the agent's job (`ciao vault search`), not a per-turn push.

    The capsule used to prepend `mentioned_entities:` links for every INDEX.md
    name the prompt mentioned. #723 measured it end to end on a synthetic and a
    live vault, with Sonnet and a cheap opencode model: no accuracy or cost
    difference, so it was removed.
    """
    capsule = build_context_capsule(
        workspace="personal",
        project_name="Ciaobot",
        project_context="Improve provider context",
        canonical_doc="Projects/Ciaobot/README.md",
    )
    assert "workspace=personal" in capsule
    assert 'project="Ciaobot"' in capsule
    assert "mentioned_entities" not in capsule
    assert "retrieval_hint" not in capsule


def test_capsule_can_omit_stable_facts() -> None:
    capsule = build_context_capsule(
        workspace="personal",
        project_name="Ciaobot",
        include_stable=False,
    )
    assert "today=" in capsule
    assert "workspace=personal" not in capsule
    assert "project=\"Ciaobot\"" not in capsule


def test_context_digest_changes_only_when_routing_changes() -> None:
    values = {
        "workspace": "personal",
        "gws_profile": "",
        "project_name": "General",
        "project_context": "",
        "canonical_doc": "",
    }
    assert context_digest(**values) == context_digest(**values)
    assert context_digest(**values) != context_digest(
        **{**values, "project_context": "new"}
    )


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal", "work")
        },
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def test_the_capsule_names_the_workspace_vault_path(tmp_path: Path) -> None:
    """The workspace *name* is not a location.

    The fanned-out `system-memory-curation@work` routine dispatched into the work
    workspace correctly, and the agent still created 14 person notes under
    `memory-vault/personal/People/` — because it was told `workspace=work` and had
    to guess where that workspace's vault was. It guessed from precedent, and the
    precedent was 95 notes the old single-workspace curator had misfiled. Routing
    the run is only half the fix; the write target has to be stated.
    """
    vault = tmp_path / "memory-vault"
    (vault / "work").mkdir(parents=True)
    (vault / "INDEX.md").write_text("", encoding="utf-8")

    manager = _make_manager(tmp_path)
    project = manager.create_project("Ciaobot", workspace="work")
    chat = manager.create_chat(project.project_id)

    prefix = manager._build_prompt_prefix(chat)

    assert "workspace=work" in prefix
    # Relative to the provider's cwd, so the model can use it verbatim.
    assert "vault=memory-vault/work" in prefix
    assert "vault=memory-vault/personal" not in prefix


def test_the_vault_fact_is_omitted_for_an_unknown_workspace(tmp_path: Path) -> None:
    """Better to say nothing than to name a path that does not resolve."""
    from ciao.context.capsule import build_context_capsule

    capsule = build_context_capsule(workspace="work")

    assert "vault=" not in capsule


# #1040 (B6): the capsule gains exactly one line about the task board. These pin
# the two halves of that promise, because either failure is silent and expensive:
# a capsule that grew into a board dump would spend every turn's context on work
# nobody asked for, and one that named no recipe would leave the agent unable to
# find the board it was told about.


def hint_line(capsule: str) -> str:
    """The capsule's one board line, or `''` when it carries none."""
    return next((line for line in capsule.splitlines() if line.startswith("task_board=")), "")


def test_the_capsule_names_the_task_board_and_its_reading_recipe() -> None:
    capsule = build_context_capsule(workspace="personal")

    assert "task_board=" in capsule
    # The verbs an agent actually has, not a prose description of them.
    for verb in ("ciao task list", "ciao task get", "ciao task delegate"):
        assert verb in capsule
    # And the two rules that make the board safe to use at all: fetch what this
    # turn needs rather than the whole board, and the user owns completion.
    assert "never the whole board" in capsule
    assert "only they mark a task" in capsule


def test_the_board_hint_is_bounded_and_carries_no_task_bodies() -> None:
    """A hint, never the board.

    Two independent guards on the same promise. Size: the capsule is prepended to
    every provider turn, so an unbounded line here is paid on every turn of every
    chat — #723 is the precedent for measuring what a per-turn push actually
    costs. Content: a task record is the user's own Markdown, and #1002 keeps
    `Workspace/Tasks/` out of recall, review and curation precisely because it is
    bookkeeping rather than a note; a capsule that quoted titles or bodies would
    leak it into every model context with none of those filters around it.
    """
    assert "\n" not in TASK_BOARD_HINT
    # One line of prose with room to spare, not a second surface.
    assert len(TASK_BOARD_HINT) <= 400

    capsule = build_context_capsule(
        workspace="personal",
        project_name="Ciaobot",
        project_context="Improve provider context",
        canonical_doc="Projects/Ciaobot/README.md",
    )
    line = hint_line(capsule)
    assert line
    # The `_field` bound the builder applies, plus the key it is written under.
    assert len(line) <= len("task_board=") + 400
    # No frontmatter, no vault path, no prose field: the recipe names verbs and
    # nothing a task record would carry.
    for leaked in ("Tasks/", ".md", "title:", "description:", "status:", "due:"):
        assert leaked not in line, leaked


def test_the_board_hint_is_a_constant_and_not_a_routing_fact() -> None:
    """It must not make every capsule look like its routing changed.

    `context_digest` decides whether the stable block is re-sent to a provider
    session. It reads exactly the five routing facts and nothing else, so the hint
    — which never varies — cannot reach it: a board hint that fed the digest would
    re-send the whole stable block on every turn of every session forever.
    """
    assert tuple(inspect.signature(context_digest).parameters) == (
        "workspace",
        "gws_profile",
        "project_name",
        "project_context",
        "canonical_doc",
    )
    # And the hint is byte-identical in both capsules: it is a constant wearing a
    # workspace's name, not a fact about one.
    personal = hint_line(build_context_capsule(workspace="personal"))
    work = hint_line(build_context_capsule(workspace="work"))
    assert personal
    assert personal == work


def test_the_board_hint_travels_with_the_stable_block_only() -> None:
    """After a provider session's first turn it goes, with the rest of the stable facts.

    A hint repeated on every turn of a long session is the per-turn push #723
    removed, and the agent that read it on turn one still has it in context.
    """
    with_stable = build_context_capsule(workspace="personal", include_stable=True)
    without_stable = build_context_capsule(workspace="personal", include_stable=False)

    assert "task_board=" in with_stable
    assert "task_board=" not in without_stable
    # The parts that must never be dropped are untouched by that choice.
    assert "today=" in without_stable

