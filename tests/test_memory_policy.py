"""One accurate memory and unattended-execution policy, pinned to its copies.

The review that produced this file (AI-01) found four surfaces disagreeing:
the architecture claimed new memory needs review while archive-time extraction
called ``auto_promote_memory=True`` (that auto-apply went away with the one-shot
pipeline in #627, so this file's rewrite in the same PR retired the contrast
rather than pinning it); the memory agent and the ``/remember`` command claimed
the typed path enforces the region cap while ``update_region`` documents and
implements an advisory one; and the unattended capsule said "do not ask" without
saying what to do with work that requires approval.

``ciao/memory_policy.py`` is now the single statement of that policy. These
tests pin it to every shipped copy — the capsule, the stock assets, the MCP
docstring, and ``docs/ARCHITECTURE.md`` — so the next edit cannot quietly
reintroduce a hard-cap claim or a different unattended rule.
"""

from __future__ import annotations

import json
import re
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
        "memory_pass",
        "unattended_curation",
        "direct_edit",
        "proposal_acceptance",
    }


def test_the_memory_pass_writes_live_and_curation_is_narrower() -> None:
    """The archive-time auto-apply is gone; the attended pass replaced it.

    The post-archive writer is the memory pass, a chat a person is present for:
    it promotes a confident fact itself and queues what it is unsure of. Curation
    still runs with no reviewer at all, so it may only consolidate what a region
    already holds and file every new fact as a proposal.
    """
    memory_pass = mp.context_policy("memory_pass")
    curation = mp.unattended_policy()
    assert memory_pass.promotes_new_region_facts == mp.PROMOTE_ATTENDED
    assert memory_pass.queues_uncertain is True
    assert curation.promotes_new_region_facts == mp.PROMOTE_REVIEWED
    assert curation.consolidates_regions == "at_threshold"
    assert curation.undo_log_required is True
    # Both may write vault destinations; only the pass is attended.
    assert memory_pass.writes_vault is True
    assert curation.writes_vault is True
    assert memory_pass.approval == "attended"
    assert memory_pass.approval != curation.approval


def test_only_attended_contexts_promote_a_new_region_fact() -> None:
    """No unattended row may write one, and every attended row is attended-marked.

    This is the machine-readable half of the ARCHITECTURE table row: the memory
    pass promotes live, the nightly curator never does.
    """
    for key in mp.CONTEXT_KEYS:
        policy = mp.context_policy(key)
        if policy.promotes_new_region_facts == mp.PROMOTE_ATTENDED:
            assert policy.approval == "attended", key
        else:
            assert policy.approval == "unattended", key


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


def test_capsule_renders_the_shared_guidance_verbatim() -> None:
    capsule = build_context_capsule(workspace="work", unattended=True)
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


def test_stock_memory_skill_states_the_advisory_cap() -> None:
    role = _stock("skills/ciao-memory/SKILL.md")
    assert "The cap is advisory on every path" in role
    assert "reports `over_cap`" in role
    assert "enforces the cap" not in role


def test_remember_command_no_longer_claims_enforcement() -> None:
    command = _stock("commands/remember.md")
    assert "enforces the cap" not in command
    assert "region cap is advisory on every path" in command
    assert "over_cap" in command


def test_stock_prompts_route_categories_to_the_vocabulary_block() -> None:
    """Every surface that writes a note must name the block that lists them.

    The category list is the owner's: it gains rows in Settings, so an agent
    that memorized it silently writes notes against a vocabulary that no longer
    exists. Each of these files has to point at the **Categories** section, say
    the note is filed in the folder it names, and route the no-fit case to the
    owner instead of a coined `type:`.
    """
    for relative in (
        "skills/ciao-memory/SKILL.md",
        "commands/remember.md",
        "commands/interrogation.md",
    ):
        asset = _stock(relative)
        assert "**Categories** section" in asset, relative
        assert "new-category" in asset, relative


def test_the_memory_skill_says_where_a_note_goes_and_who_adds_a_category() -> None:
    role = _stock("skills/ciao-memory/SKILL.md")
    assert "Read it before writing a note and file the note in the folder its line names" in role
    assert "queue a new-category question in `Workspace/Memory-Proposals.md`" in role
    # The list is read, never memorized: a category added in Settings must reach
    # the agent without editing this file.
    assert "do not memorize this" in role


def test_the_core_prompt_points_at_the_same_block() -> None:
    from ciao.core_prompt import _system_instructions

    core = _system_instructions()
    assert "**Categories** section of `VOCABULARY.md`" in core
    assert "a note that fits none is a new-category question" in core


def test_curation_schedule_defers_and_never_hard_caps() -> None:
    skill = next(entry["prompt"] for entry in json.loads(_stock("schedules.json"))["schedules"] if entry["schedule_id"] == "system-memory-curation")
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
    # The docstring sits in the module-level operation table (S2a) at 4-space
    # indent, not nested under _register_tools; check the line-independent phrase.
    assert "The region cap is" in server and "advisory" in server


def test_architecture_doc_states_the_policy_matrix() -> None:
    doc = (REPO / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    # The matrix's own section, the narrower-curation rationale, and the
    # deferred-approval rule must all be present.
    assert "### Memory write policy matrix" in doc
    assert "Why curation is narrower than the memory pass" in doc
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
    agent_cli = (REPO / "docs" / "AGENT_CLI.md").read_text(encoding="utf-8")
    for doc, name in ((architecture, "ARCHITECTURE.md"), (agent_cli, "AGENT_CLI.md")):
        assert "ciao memory-proposal-add" in doc, name
        assert "ciao memory-proposals" in doc, name
        assert "ciao memory-proposal-dismiss" in doc, name


# ── Nothing is promoted unattended (#728-D) ────────────────────────────────


def test_the_care_schedule_no_longer_instructs_unattended_promotion_or_settlement() -> None:
    """The three instructions that contradicted the autonomy rule are gone.

    An x3+ learning is a *routing priority*, not permission: the old Pass 4 and
    Pass 7 told an unattended run to write it into AGENTS.md or a skill, and
    Pass 9 told it to settle proposals. An unattended run can verify neither
    destination nor rejection, so each of those is now a filed proposal a
    person decides.
    """
    prompt = next(
        entry["prompt"]
        for entry in json.loads(_stock("schedules.json"))["schedules"]
        if entry["schedule_id"] == "system-memory-curation"
    )
    assert "into canonical guidance (the AGENTS.md body or the relevant skill/doc)" not in prompt
    assert "belongs here as a standing instruction, citing its sources" not in prompt
    assert "Only settle a proposal after its change is actually in place" not in prompt
    # And each is replaced by the routed proposal, named with the commands.
    assert "**Route** an entry at x3 or more" in prompt
    assert "a priority order and not a licence to edit" in prompt
    assert "You never edit a skill or this workspace's guide body" in prompt
    assert "you never settle a proposal" in prompt
    assert "ciao skill-proposal-add NAME --input-file FILE" in prompt
    assert "ciao skill-draft-add --input-file FILE" in prompt
    assert "ciao skill-drafts" in prompt


def test_the_care_schedule_ground_rules_list_the_new_deferrals() -> None:
    """The deferred list is the run's own statement of what it may not do.

    A deferral nobody reads is not a deferral, and the two additions are the two
    actions an unattended run is most likely to reach for precisely because the
    old passes told it to. So the list the run reads, the capsule the model is
    given, and the policy module all three have to name them.
    """
    from ciao.memory_policy import UNATTENDED_CAPSULE_GUIDANCE, unattended_deferrals

    prompt = next(
        entry["prompt"]
        for entry in json.loads(_stock("schedules.json"))["schedules"]
        if entry["schedule_id"] == "system-memory-curation"
    )
    listed = next(
        line for line in prompt.splitlines() if "The full deferred list:" in line
    )
    actions = {action.action for action in unattended_deferrals()}
    # The policy module is the machine-readable statement, and both new
    # deferrals are in it with the reason a person can act on.
    assert any("Edit a skill" in action for action in actions)
    assert any("Settle a skill proposal" in action for action in actions)
    assert any("public GitHub issue" in action for action in actions)
    # The run's own list names both, and the capsule names both, so no surface
    # can say something different about what an unattended turn may not do.
    for phrase in (
        "editing a skill or the AGENTS.md guide body",
        "settling a skill proposal or draft",
    ):
        assert phrase in listed, phrase
        assert phrase in UNATTENDED_CAPSULE_GUIDANCE, phrase
    # The entries an unattended run now has to report rather than perform.
    assert "Report every one under **What needs you**" in prompt


def test_the_memory_skill_never_edits_or_settles_unattended() -> None:
    """The skill a run loads has to say the same thing the schedule does.

    The schedule prompt and the `ciao-memory` skill are read by the same
    unattended turn; a rule stated in only one of them is a rule the other
    surface can contradict, which is how the old "promote it yourself"
    instruction survived in one copy after the other had moved on.
    """
    skill = _stock("skills/ciao-memory/SKILL.md")
    assert "Propose a skill; never edit or settle one unattended" in skill
    assert "it may **not** edit a skill, edit the `AGENTS.md` body, or settle a proposal or draft" in skill
    assert "A recurring learning is a *routing* priority" in skill
    # And it carries the routing contract the two non-owned destinations need.
    assert "A reusable tool/workflow instruction routes to the best matching skill" in skill
    assert "an inventory match is a *candidate*, not proof" in skill
    assert "never as a `sources` entry or a `turn`" in skill
    assert "`ciao skill-draft-add --input-file FILE`" in skill


def test_the_remember_command_states_the_provenance_rule() -> None:
    """A `/remember` of a lesson has to keep the origin it really has.

    The command is the one surface that produces a learning from a chat nobody
    archived, so the rule that a request id replaces a manufactured turn has to
    be where the model reads it, and the negative has to be explicit: a citation
    to a conversation that never happened is what the whole field exists to stop.
    """
    command = _stock("commands/remember.md")
    assert "A lesson keeps the origin it really has" in command
    assert "there is no transcript turn to cite" in command
    assert "--request <id>" in command
    assert "cites it as `req:<id>`" in command
    assert "Never manufacture a turn, a chat id, an archive path or an `excerpt`" in command
    assert "`--request` is only accepted for `--kind learnings`" in command
    # And the immediate-write path says the same, so the two agree.
    assert "carrying the same `--request <id>` the queued copy used" in command


def test_the_capabilities_catalog_describes_the_routing_without_overclaiming() -> None:
    """The catalog is what a user asks "what can it do" and gets an answer from.

    It has to name the lesson route and the drafts, and it has to keep saying
    that the unattended run prepares rather than acts — otherwise the two
    surfaces tell a person opposite things about the same nightly run.
    """
    skill = _stock("skills/ciao-capabilities/SKILL.md")
    assert "A reusable lesson in `Workspace/Learnings.md` routes the same way" in skill
    assert "an inventory match is a candidate, not proof" in skill
    assert "the issue body is sanitized" in skill
    assert "it never edits a skill or the workspace guide, never settles a proposal, and never opens a public issue" in skill
    assert "`ciao skill-draft-approve`" in skill


def test_the_routing_contract_is_documented() -> None:
    """The contract has to be in the architecture doc, not only in the prompts.

    Three surfaces state it — the memory-pass section, the policy module and this
    table — and they drift silently unless something says they are describing one
    thing. Each of the four routes and the three cross-cutting rules is pinned
    here so removing one is a deliberate edit rather than an omission.
    """
    doc = (REPO / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "### Lesson routing contract" in doc
    for row in (
        "A skill this workspace owns under `skills/`",
        "A packaged, mirrored or shared skill",
        "No skill at all",
    ):
        assert row in doc, row
    assert "An inventory match is a candidate, not proof" in doc
    assert "`sources` entry or a `turn`, because those would claim" in doc
    assert "Nothing is promoted unattended" in doc
    assert "`x3+` recurrence is a" in doc
    assert "`/remember` keeps a real origin" in doc
    assert "`req:<id>`" in doc
    assert "A filed upstream issue is not a deployed lesson" in doc


def test_the_development_doc_covers_the_draft_commands() -> None:
    """A new CLI surface the contributor doc does not mention is one nobody finds."""
    doc = (REPO / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
    assert "### Lesson routing and skill drafts" in doc
    for command in (
        "ciao skill-draft-add",
        "ciao skill-drafts",
        "ciao skill-draft-approve",
        "ciao skill-draft-reject",
    ):
        assert command in doc, command
    assert "Sanitizing is a gate, not a filter" in doc
    assert "The attended step is the only step" in doc
    # And the lesson-routing carve-out in the proposal filer is documented, so a
    # reader does not read "one or the other" as a loosened check.
    assert "accepts a lesson with no `sources`" in doc


# ── The care prompt may only name what the control plane allows (#882) ────


def _care_prompt() -> str:
    return next(
        entry["prompt"]
        for entry in json.loads(_stock("schedules.json"))["schedules"]
        if entry["schedule_id"] == "system-memory-curation"
    )


# verb as typed on the CLI -> the wire action agent_cli.resolve() sends.
_VAULT_REVIEW_WIRE = {
    "list": "list",
    "show": "inspect",
    "keep": "decide",
    "trash": "trash",
    "restore": "restore",
    "complete": "complete",
    "restore-completed": "restore_completed",
    "delete": "delete",
}


def test_every_vault_review_verb_maps_to_the_wire_action_agent_cli_sends() -> None:
    """Keeps _VAULT_REVIEW_WIRE honest: it is derived from agent_cli, not a guess."""
    from ciao import agent_cli

    parser = agent_cli.build_parser()
    for verb, wire in _VAULT_REVIEW_WIRE.items():
        argv = ["vault", "review", verb]
        if verb == "show":
            argv.append("Some/Note.md")
        elif verb not in {"list"}:
            argv += ["--candidate", "c1"]
            if verb == "delete":
                argv += ["--confirm", "c1"]
        operation, arguments = agent_cli.resolve(parser.parse_args(argv))
        assert operation == "vault_review"
        assert arguments["action"] == wire, verb


def test_unattended_care_prompt_only_names_vault_review_verbs_allowed_unattended() -> None:
    """#882: the prompt must never tell an unattended run to do what the control plane refuses."""
    from ciao.vault_review import ATTENDED_ONLY_ACTIONS

    prompt = _care_prompt()
    mentioned = set(re.findall(r"`?ciao vault review ([a-z]+(?:-[a-z]+)*)", prompt))
    assert mentioned, "the prompt no longer names any vault review verb; update this guard"
    unknown = mentioned - set(_VAULT_REVIEW_WIRE)
    assert not unknown, f"unknown vault review verbs in the prompt: {sorted(unknown)}"
    refused = {verb for verb in mentioned if _VAULT_REVIEW_WIRE[verb] in ATTENDED_ONLY_ACTIONS}
    assert not refused, f"the unattended prompt tells the agent to run {sorted(refused)}, which unattended_forbidden refuses"


def test_unattended_care_prompt_says_every_review_decision_is_attended_only() -> None:
    from ciao.vault_review import ATTENDED_ONLY_ACTIONS

    prompt = _care_prompt()
    assert "Keep and archiving" not in prompt
    assert "archiving non-destructively" not in prompt
    sentence = next(s for s in prompt.split(". ") if "is refused unattended" in s)
    for verb, wire in _VAULT_REVIEW_WIRE.items():
        if wire in ATTENDED_ONLY_ACTIONS:
            assert verb in sentence, f"the prompt does not say `{verb}` is attended-only"


def test_every_noun_verb_in_the_unattended_care_prompt_is_a_real_agent_cli_verb() -> None:
    """Catches a typo'd or removed `ciao <noun> <verb>` the run would hit as a usage error."""
    from ciao import agent_cli

    prompt = _care_prompt()
    parser = agent_cli.build_parser()
    for noun, verb in set(re.findall(r"`ciao (memory|context|chat|project|file|note) ([a-z]+(?:-[a-z]+)*)", prompt)):
        argv = [noun, verb, "--help"]
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(argv)
        assert excinfo.value.code == 0, f"`ciao {noun} {verb}` is not a command"
