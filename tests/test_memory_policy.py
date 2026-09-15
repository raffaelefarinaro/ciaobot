"""One accurate memory and unattended-execution policy, pinned to its copies.

The review that produced this file (AI-01) found four surfaces disagreeing:
the architecture claimed new memory needs review while archive extraction
called ``auto_promote_memory=True``; the memory agent and the ``/remember``
command claimed the typed path enforces the region cap while ``update_region``
documents and implements an advisory one; and the unattended capsule said "do
not ask" without saying what to do with work that requires approval.

``ciao/memory_policy.py`` is now the single statement of that policy. These
tests pin it to every shipped copy — the capsule, the stock assets, the MCP
docstring, and ``docs/ARCHITECTURE.md`` — so the next edit cannot quietly
reintroduce a hard-cap claim or a different unattended rule.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from ciao.context.capsule import build_context_capsule
from ciao import memory_policy as mp


REPO = Path(__file__).resolve().parents[1]


def _stock(relative: str) -> str:
    return resources.files("ciao.stock").joinpath(relative).read_text(encoding="utf-8")


def test_every_policy_row_declares_advisory_caps_only() -> None:
    """Acceptance: no shipped instruction claims a hard cap while it is advisory."""
    assert mp.CAP_SEMANTICS_ADVISORY == "advisory"
    for policy in mp.CONTEXT_POLICIES:
        assert policy.cap_semantics == mp.CAP_SEMANTICS_ADVISORY, policy.key


def test_the_matrix_covers_the_reviewed_contexts() -> None:
    assert set(mp.CONTEXT_KEYS) == {
        "attended_explicit_remember",
        "archive_extraction",
        "unattended_curation",
        "direct_edit",
        "proposal_acceptance",
    }


def test_archive_auto_applies_but_unattended_curation_does_not() -> None:
    """Readers must be able to tell why curation is narrower than archiving."""
    archive = mp.context_policy("archive_extraction")
    curation = mp.unattended_policy()
    assert archive.promotes_new_region_facts == mp.PROMOTE_AUTO
    assert curation.promotes_new_region_facts == mp.PROMOTE_REVIEWED
    assert curation.consolidates_regions == "at_threshold"
    assert curation.undo_log_required is True
    # Archive time may write vault destinations; curation may only consolidate.
    assert archive.writes_vault is True
    assert curation.writes_vault is True
    assert archive.approval != curation.approval


def test_the_review_destination_is_always_the_unsure_bucket() -> None:
    assert "review" in mp.MEMORY_DESTINATIONS
    for key in mp.CONTEXT_KEYS:
        policy = mp.context_policy(key)
        if policy.promotes_new_region_facts == mp.PROMOTE_REVIEWED:
            assert policy.queues_uncertain is True


def test_unknown_policy_key_raises() -> None:
    with pytest.raises(KeyError):
        mp.context_policy("not_a_context")


# ── The capsule: unattended runs defer, never route around the reviewer ───


def test_capsule_unattended_guidance_defers_approval_requiring_work() -> None:
    text = mp.UNATTENDED_CAPSULE_GUIDANCE
    # The extraction pipeline matches this prefix, so it is part of the format.
    assert text.startswith(mp.UNATTENDED_MARKER)
    assert text.startswith("unattended=true; this turn was fired automatically")
    assert "Do not ask questions or wait for approval" in text
    assert "do not route around the absent reviewer" in text
    assert "Defer and report" in text


def test_insights_extractor_and_capsule_share_one_marker() -> None:
    """The marker cannot diverge between the capsule and the extractor again."""
    from ciao import insights

    assert insights._UNATTENDED_MARKER == mp.UNATTENDED_MARKER


def test_capsule_renders_the_shared_guidance_verbatim() -> None:
    capsule = build_context_capsule(prompt="hi", workspace="work", unattended=True)
    assert mp.UNATTENDED_CAPSULE_GUIDANCE in capsule


def test_dangerous_unattended_actions_all_defer() -> None:
    actions = "\n".join(
        f"{item.action} {item.reason}" for item in mp.unattended_deferrals()
    )
    for needle in (
        "always-loaded",
        "Trash",
        "another workspace",
        "automation",
        "GitHub issue",
        "git",
    ):
        assert needle in actions, needle


@pytest.mark.parametrize("provider", ["claude", "opencode"])
def test_both_providers_declare_schedule_unattended(provider: str) -> None:
    """A new provider cannot ship a different unattended rule silently.

    Both supported providers already declare ``schedule_unattended``; this pins
    the fact so the capsule's deferred-approval contract keeps a provider that
    honors it.
    """
    from ciao.provider_service import capabilities_for

    assert capabilities_for(provider).schedule_unattended is True


def test_both_providers_keep_approval_requiring_tools_pre_approval_free() -> None:
    """The unattended deferral must not be weakened by the pre-approved set.

    An unattended run forces bypass on every provider, so a tool on the
    auto-approved list runs with no card at all. The tools that back the
    deferred actions — a vault-note delete/trash via ``vault_review``, an
    arbitrary command via ``background_run_start``, a schedule lifecycle move
    — must therefore stay off that list for Claude and opencode alike.
    """
    from ciao.execution_modes import (
        AUTO_APPROVED_MCP_TOOLS,
        CONTROL_PLANE_PREAPPROVED_MODES,
        MCP_SERVER_NAME,
    )
    from ciao.providers.opencode import control_plane_permission_rules

    assert "vault_review" not in AUTO_APPROVED_MCP_TOOLS
    assert "background_run_start" not in AUTO_APPROVED_MCP_TOOLS
    assert "schedule_action" not in AUTO_APPROVED_MCP_TOOLS
    # opencode's enumerated allow rules derive from the same tuple, so they
    # cannot drift from Claude's ``allowed_tools``.
    allowed = {rule["permission"] for rule in control_plane_permission_rules()}
    assert f"{MCP_SERVER_NAME}_vault_review" not in allowed
    assert CONTROL_PLANE_PREAPPROVED_MODES == frozenset({"auto", "bypass"})


def test_vault_note_mutation_is_refused_on_an_unattended_turn(tmp_path: Path) -> None:
    """The one deferred action Ciaobot enforces in code, pinned by its code."""
    from types import SimpleNamespace

    from ciao import vault_review as review
    from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal

    note = tmp_path / "People" / "A.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\ntype: note\n---\nAn unlinked note.\n", encoding="utf-8")
    candidate = review.generate_candidates(tmp_path, workspace="personal")[0]

    chat = SimpleNamespace(user_turn_count=1, user_turn_unattended={"0": True})
    plane = CiaoControlPlane(
        SimpleNamespace(
            workspace=lambda name: object(),
            workspace_vault_root=lambda name: tmp_path,
        ),
        project_chat_manager=SimpleNamespace(get_chat=lambda chat_id: chat),
        schedule_manager=SimpleNamespace(),
    )
    principal = McpPrincipal(
        token_id="token-1",
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="opencode",
    )

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.vault_review(principal, "trash", candidate_id=candidate.candidate_id)
    assert excinfo.value.code == "unattended_forbidden"
    assert note.exists(), "a deferred delete must not touch the note"


# ── The stock assets and docs must not contradict the matrix ──────────────


def test_stock_memory_agent_states_the_advisory_cap() -> None:
    role = _stock("agents/memory.md")
    assert "The cap is advisory on every path" in role
    assert "reports `over_cap`" in role
    assert "enforces the cap" not in role


def test_remember_command_no_longer_claims_enforcement() -> None:
    command = _stock("commands/remember.md")
    assert "enforces the cap" not in command
    assert "region cap is advisory on every path" in command
    assert "over_cap" in command


def test_curation_skill_defers_and_never_hard_caps() -> None:
    skill = _stock("skills/memory-curation/SKILL.md")
    assert "You are an unattended run: defer, never ask, never route around" in skill
    assert "The region cap is advisory." in skill
    # The consolidation contract the architecture test also pins must survive.
    assert "Do not promote new facts into the bounded" in skill
    assert "Workspace/Memory-Consolidations.md" in skill


def test_capabilities_skill_does_not_claim_nothing_is_durable_without_approval() -> None:
    skill = _stock("skills/ciao-capabilities/SKILL.md")
    assert "nothing becomes durable without approval" not in skill
    assert "no unattended run promotes a new always-loaded fact" in skill


def test_mcp_memory_docstring_no_longer_claims_enforcement() -> None:
    server = (REPO / "ciao" / "mcp_server.py").read_text(encoding="utf-8")
    assert "enforces the configured bounded-region limit" not in server
    assert "The region cap is\n            advisory" in server


def test_architecture_doc_states_the_policy_matrix() -> None:
    doc = (REPO / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    # The matrix's own section, the narrower-curation rationale, and the
    # deferred-approval rule must all be present.
    assert "### Memory write policy matrix" in doc
    assert "Why curation is narrower than archive extraction" in doc
    assert "Unattended runs defer; they do not route around the missing" in doc
    # The old contradictory phrase is gone.
    assert "Promoting NEW facts into a region stays user-reviewed" not in doc
    assert "except rare archive-time \"User corrections\"" not in doc


def test_memory_design_drops_the_hard_cap_claim_and_states_advisory() -> None:
    doc = (REPO / "docs" / "MEMORY_DESIGN.md").read_text(encoding="utf-8")
    assert "hard-capped" not in doc
    assert "advisory" in doc


def test_proposal_cli_exception_is_named_in_docs() -> None:
    """The MCP-only rule must name the supported proposal CLI exception."""
    architecture = (REPO / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    mcp = (REPO / "docs" / "MCP.md").read_text(encoding="utf-8")
    for doc, name in ((architecture, "ARCHITECTURE.md"), (mcp, "MCP.md")):
        assert "ciao memory-proposal-add" in doc, name
        assert "ciao memory-proposals" in doc, name
        assert "ciao memory-proposal-dismiss" in doc, name
