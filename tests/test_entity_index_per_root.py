"""Entity hints resolve against the index that covers the chat's root.

The Claude path was fixed during the read sweep (``ChatService._entity_index_root``);
the Codex provider still read ``config.vault_root`` — the directory the migration
empties. Entity enrichment is fail-open, so the symptom was silence, not an error.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ciao.context import entity_tagger
from ciao.models import AgentRequest
from ciao.providers.codex import CodexProvider


def _request(prompt: str, workspace: str) -> AgentRequest:
    return AgentRequest(
        prompt=prompt,
        model="gpt-test",
        mode="auto",
        provider="codex",
        extra_env={"CIAO_ACTIVE_WORKSPACE": workspace},
    )


def _migrated_config(root: Path) -> SimpleNamespace:
    """Per-root layout: each agent root owns its own INDEX.md, and the shared
    vault root is the emptied one the migration left behind."""
    for name, entity in (("personal", "Alba"), ("work", "Bruno")):
        vault = root / name / "memory-vault"
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "INDEX.md").write_text(
            f"- [People/{entity}](./People/{entity}.md) (aliases: {entity})\n",
            encoding="utf-8",
        )
    (root / "memory-vault").mkdir(exist_ok=True)  # emptied by the migration
    return SimpleNamespace(
        workspace_root=root,
        vault_root=root / "memory-vault",
        agent_root=lambda n: root / n,
        agent_vault_root=lambda n: root / n / "memory-vault",
        # 'personal' owns unprefixed entries in the shared layout; the per-root
        # indexes are unprefixed too, which is what used to hide work's entities.
        legacy_entity_workspace=lambda: "personal",
    )


def test_codex_finds_entities_in_the_chats_own_root(tmp_path: Path) -> None:
    provider = CodexProvider(tmp_path, config=_migrated_config(tmp_path))

    rendered = provider._runtime_context(_request("Ask Alba about it", "personal"))

    assert "People/Alba" in rendered


def test_codex_does_not_leak_entities_from_another_root(tmp_path: Path) -> None:
    provider = CodexProvider(tmp_path, config=_migrated_config(tmp_path))

    rendered = provider._runtime_context(_request("Ask Alba about it", "work"))

    assert "Alba" not in rendered


def test_codex_falls_back_to_the_shared_vault_without_a_workspace(tmp_path: Path) -> None:
    (tmp_path / "memory-vault").mkdir()
    (tmp_path / "memory-vault" / "INDEX.md").write_text(
        "- [People/Cara](./People/Cara.md) (aliases: Cara)\n", encoding="utf-8"
    )
    config = SimpleNamespace(
        workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        agent_root=lambda n: tmp_path,          # not re-rooted
        agent_vault_root=lambda n: tmp_path / n / "memory-vault",
        legacy_entity_workspace=lambda: "",
    )
    provider = CodexProvider(tmp_path, config=config)

    rendered = provider._runtime_context(_request("Ask Cara", ""))

    assert "People/Cara" in rendered


def test_two_roots_stay_cached_side_by_side(tmp_path: Path) -> None:
    """A single cache slot re-parsed the index on every workspace switch."""
    entity_tagger._index_cache.clear()
    roots = []
    for name in ("personal", "work"):
        vault = tmp_path / name / "memory-vault"
        vault.mkdir(parents=True)
        (vault / "INDEX.md").write_text(
            f"- [People/{name}](./People/{name}.md) (aliases: {name})\n", encoding="utf-8"
        )
        roots.append(vault)

    first = [entity_tagger.get_index(r) for r in roots]
    second = [entity_tagger.get_index(r) for r in reversed(roots)]

    assert first[0] is second[1]
    assert first[1] is second[0]
    assert len(entity_tagger._index_cache) == 2


def test_the_index_cache_stays_bounded(tmp_path: Path) -> None:
    entity_tagger._index_cache.clear()
    for i in range(entity_tagger._INDEX_CACHE_LIMIT + 3):
        entity_tagger.get_index(tmp_path / f"root{i}")

    assert len(entity_tagger._index_cache) <= entity_tagger._INDEX_CACHE_LIMIT


def test_a_work_chat_sees_its_own_unprefixed_entities(tmp_path: Path) -> None:
    """The live regression: per-root indexes are unprefixed, so the legacy rule
    admitted only the one workspace that owns unprefixed entries. Every entity in
    the work root was invisible to work chats."""
    provider = CodexProvider(tmp_path, config=_migrated_config(tmp_path))

    rendered = provider._runtime_context(_request("Ask Bruno about it", "work"))

    assert "People/Bruno" in rendered


def test_the_shared_layout_still_scopes_by_prefix(tmp_path: Path) -> None:
    """The filter must keep working where it is still the right answer."""
    vault = tmp_path / "memory-vault"
    (vault / "personal").mkdir(parents=True)
    (vault / "work").mkdir(parents=True)
    (vault / "INDEX.md").write_text(
        "- [personal/People/Alba](./personal/People/Alba.md) (aliases: Alba)\n"
        "- [work/People/Bruno](./work/People/Bruno.md) (aliases: Bruno)\n",
        encoding="utf-8",
    )
    config = SimpleNamespace(
        workspace_root=tmp_path,
        vault_root=vault,
        agent_root=lambda n: tmp_path,          # not re-rooted
        agent_vault_root=lambda n: vault,
        legacy_entity_workspace=lambda: "personal",
    )
    provider = CodexProvider(tmp_path, config=config)

    rendered = provider._runtime_context(_request("Ask Alba and Bruno", "work"))

    assert "work/People/Bruno" in rendered
    assert "Alba" not in rendered


# -- the Claude path (capsule), which shared the same filter -----------------


def test_the_capsule_shows_a_work_chat_its_own_entities(tmp_path: Path) -> None:
    from ciao.context.capsule import build_context_capsule

    vault = tmp_path / "work" / "memory-vault"
    vault.mkdir(parents=True)
    (vault / "INDEX.md").write_text(
        "- [People/Bruno](./People/Bruno.md) (aliases: Bruno)\n", encoding="utf-8"
    )

    capsule = build_context_capsule(
        prompt="Ask Bruno about it",
        workspace="work",
        vault_root=vault,
        legacy_entity_workspace="personal",
        entity_index_owns_workspace=True,
    )

    assert "People/Bruno" in capsule


def test_the_capsule_still_scopes_a_shared_index(tmp_path: Path) -> None:
    from ciao.context.capsule import build_context_capsule

    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "INDEX.md").write_text(
        "- [personal/People/Alba](./personal/People/Alba.md) (aliases: Alba)\n",
        encoding="utf-8",
    )

    capsule = build_context_capsule(
        prompt="Ask Alba about it",
        workspace="work",
        vault_root=vault,
        legacy_entity_workspace="personal",
        entity_index_owns_workspace=False,
    )

    assert "Alba" not in capsule


# -- the chat manager's two answers must agree ------------------------------


def test_the_chat_manager_reports_per_root_only_once_migrated(tmp_path: Path) -> None:
    """The root and the flag come from the same receipt, so they cannot disagree."""
    from ciao.web.project_chats import ProjectChatManager

    migrated = SimpleNamespace(
        workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        agent_root=lambda n: tmp_path / n,
        agent_vault_root=lambda n: tmp_path / n / "memory-vault",
    )
    shared = SimpleNamespace(
        workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        agent_root=lambda n: tmp_path,
        agent_vault_root=lambda n: tmp_path / "memory-vault",
    )
    per_root = ProjectChatManager._entity_index_is_per_root
    root_of = ProjectChatManager._entity_index_root

    after = SimpleNamespace(_config=migrated)
    before = SimpleNamespace(_config=shared)

    assert per_root(after, "work") is True
    assert root_of(after, "work") == tmp_path / "work" / "memory-vault"
    assert per_root(before, "work") is False
    assert root_of(before, "work") == tmp_path / "memory-vault"
    # No workspace means no per-root claim, in either layout.
    assert per_root(after, "") is False
