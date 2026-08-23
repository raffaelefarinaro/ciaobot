from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.context.capsule import build_context_capsule, context_digest
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def test_capsule_contains_routing_and_entity_hints(tmp_path: Path) -> None:
    (tmp_path / "INDEX.md").write_text(
        "- [People/Alba](./People/Alba.md) (aliases: Alba)\n", encoding="utf-8"
    )
    capsule = build_context_capsule(
        prompt="What did we decide about Alba?",
        workspace="personal",
        project_name="Ciaobot",
        project_context="Improve provider context",
        canonical_doc="Projects/Ciaobot/README.md",
        vault_root=tmp_path,
        legacy_entity_workspace="personal",
    )
    assert "workspace=personal" in capsule
    assert 'project="Ciaobot"' in capsule
    assert "[Alba](./People/Alba.md)" in capsule
    assert "retrieval_hint" not in capsule


def test_capsule_can_omit_stable_facts() -> None:
    capsule = build_context_capsule(
        prompt="hello",
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


def test_prompt_prefix_reads_the_top_level_entity_index(tmp_path: Path) -> None:
    """Entity hints must resolve against `<vault>/INDEX.md`, not a subtree.

    `vault-index --write` writes exactly one index, at the top-level vault
    root. Passing the per-workspace vault root here made `find_entities` read
    a non-existent (in practice, a stale empty stub) index, so every PWA turn
    silently matched nothing while the Codex provider path still worked.
    """
    vault = tmp_path / "memory-vault"
    (vault / "personal").mkdir(parents=True)
    (vault / "INDEX.md").write_text(
        "- [personal/People/Alba](./personal/People/Alba.md) (aliases: Alba)\n", encoding="utf-8"
    )
    # The exact failure condition: a stale, entry-less per-workspace stub.
    (vault / "personal" / "INDEX.md").write_text(
        "# Vault Index\n", encoding="utf-8"
    )

    manager = _make_manager(tmp_path)
    project = manager.create_project("Ciaobot", workspace="personal")
    chat = manager.create_chat(project.project_id)

    prefix = manager._build_prompt_prefix(
        chat, prompt="What did we decide about Alba?"
    )

    assert "[Alba](./personal/People/Alba.md)" in prefix


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

    prefix = manager._build_prompt_prefix(chat, prompt="file a contact")

    assert "workspace=work" in prefix
    # Relative to the provider's cwd, so the model can use it verbatim.
    assert "vault=memory-vault/work" in prefix
    assert "vault=memory-vault/personal" not in prefix


def test_the_vault_fact_is_omitted_for_an_unknown_workspace(tmp_path: Path) -> None:
    """Better to say nothing than to name a path that does not resolve."""
    from ciao.context.capsule import build_context_capsule

    capsule = build_context_capsule(prompt="hi", workspace="work")

    assert "vault=" not in capsule
