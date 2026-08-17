from __future__ import annotations

from pathlib import Path

from ciao.context.capsule import build_context_capsule, context_digest


def test_capsule_contains_routing_and_retrieval_hints(tmp_path: Path) -> None:
    (tmp_path / "INDEX.md").write_text(
        "- `People/Alba` (aliases: Alba)\n", encoding="utf-8"
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
    assert "[[People/Alba]]" in capsule
    assert "retrieval_hint=Use vault_search" in capsule


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
