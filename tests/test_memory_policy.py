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


def _capabilities() -> str:
    """The capabilities index plus every reference file, as one string."""
    root = resources.files("ciao.stock").joinpath("skills", "ciao-capabilities")
    parts = [root.joinpath("SKILL.md").read_text(encoding="utf-8")]
    parts += [
        f.read_text(encoding="utf-8")
        for f in sorted(root.joinpath("references").rglob("*.md"), key=lambda f: str(f))
    ]
    return "\n".join(parts)


# ── The capsule: unattended runs defer, never route around the reviewer ───


def test_capsule_unattended_guidance_defers_approval_requiring_work() -> None:
    text = mp.UNATTENDED_CAPSULE_GUIDANCE
    # The extraction pipeline matches this prefix, so it is part of the format.
    assert text.startswith(mp.UNATTENDED_MARKER)
    assert text.startswith("unattended=true; this turn was fired automatically")
    assert "Do not ask questions or wait for approval" in text
    assert "do not route around the absent reviewer" in text
    assert "Defer and report" in text


def test_capsule_unattended_guidance_arms_a_network_retry() -> None:
    text = mp.UNATTENDED_CAPSULE_GUIDANCE
    assert "ciao chat retry --action set --kind network" in text
    assert "Do not ask questions or wait for approval" in text
    assert "do not route around the absent reviewer" in text
    assert "Defer and report" in text
    assert "Never print secret values (env files, tokens, provider configs) into the conversation." in text


def test_capsule_unattended_guidance_defers_source_and_runtime_edits() -> None:
    text = mp.UNATTENDED_CAPSULE_GUIDANCE
    assert "editing application source code" in text
    # Writing a confident memory fact is not an approval-gated action.
    assert "memory regions" not in text
    assert "hand-editing `.runtime/` state files" in text
    assert "Never print secret values (env files, tokens, provider configs) into the conversation." in text


def test_capsule_renders_the_shared_guidance_verbatim() -> None:
    capsule = build_context_capsule(workspace="work", unattended=True)
    assert mp.UNATTENDED_CAPSULE_GUIDANCE in capsule


@pytest.mark.parametrize("provider", ["claude", "opencode"])
def test_both_providers_declare_schedule_unattended(provider: str) -> None:
    """A new provider cannot ship a different unattended rule silently.

    Both supported providers already declare ``schedule_unattended``; this pins
    the fact so the capsule's deferred-approval contract keeps a provider that
    honors it.
    """
    from ciao.provider_service import capabilities_for

    assert capabilities_for(provider).schedule_unattended is True


# ── The stock assets and docs must not contradict the policy ──────────────


def test_stock_memory_skill_states_the_advisory_cap() -> None:
    role = _stock("skills/ciao-capabilities/references/memory-operations/guide.md")
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
        "skills/ciao-capabilities/references/memory-operations/guide.md",
        "commands/remember.md",
        "commands/interrogation.md",
    ):
        asset = _stock(relative)
        assert "**Categories** section" in asset, relative
        assert "new-category" in asset, relative


def test_the_memory_skill_says_where_a_note_goes_and_who_adds_a_category() -> None:
    role = _stock("skills/ciao-capabilities/references/memory-operations/guide.md")
    assert "Make each new note with `ciao note create" in role
    assert "files it in the category's folder" in role
    assert "put a new-category question on the Unsure list" in role
    # The list is read, never memorized: a category added in Settings must reach
    # the agent without editing this file.
    assert "do not memorize this" in role


def test_the_core_prompt_routes_categories_through_the_memory_skill() -> None:
    from ciao.core_prompt import _system_instructions

    core = _system_instructions()
    assert "Before durable writes or proposal curation, read `" in core and "memory-operations/guide.md`" in core
    skill = _stock("skills/ciao-capabilities/references/memory-operations/guide.md")
    assert "**Categories** section of `<vault>/VOCABULARY.md`" in skill
    assert "If no category fits, put a new-category question on the Unsure list" in skill


def test_capabilities_skill_does_not_claim_nothing_is_durable_without_approval() -> None:
    skill = _capabilities()
    assert "nothing becomes durable without approval" not in skill
    assert "Git is the record and the undo" in skill


def test_mcp_memory_docstring_no_longer_claims_enforcement() -> None:
    server = (REPO / "ciao" / "mcp_server.py").read_text(encoding="utf-8")
    assert "enforces the configured bounded-region limit" not in server
    # The docstring sits in the module-level operation table (S2a) at 4-space
    # indent, not nested under _register_tools; check the line-independent phrase.
    assert "The region cap is" in server and "advisory" in server


def test_memory_design_drops_the_hard_cap_claim_and_states_advisory() -> None:
    doc = (REPO / "docs" / "MEMORY_DESIGN.md").read_text(encoding="utf-8")
    assert "hard-capped" not in doc
    assert "advisory" in doc


def test_proposal_check_exception_is_named_in_docs() -> None:
    """The MCP-only rule must name the supported proposal CLI exception."""
    architecture = (REPO / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    agent_cli = (REPO / "docs" / "AGENT_CLI.md").read_text(encoding="utf-8")
    for doc, name in ((architecture, "ARCHITECTURE.md"), (agent_cli, "AGENT_CLI.md")):
        assert "ciao proposal-check" in doc, name


# ── Nothing is promoted unattended (#728-D) ────────────────────────────────


def test_the_memory_skill_never_edits_or_settles_unattended() -> None:
    """The skill a run loads has to say the same thing the schedule does.

    The schedule prompt and durable-memory guide are read by the same
    unattended turn; a rule stated in only one of them is a rule the other
    surface can contradict, which is how the old "promote it yourself"
    instruction survived in one copy after the other had moved on.
    """
    skill = _stock("skills/ciao-capabilities/references/memory-operations/guide.md")
    assert "Write directly; Git is the record" in skill
    assert "is not written by you at all unattended" in skill
    assert "proposal file under `Workspace/Proposals/`" in skill
    # And it carries the routing contract the two non-owned destinations need.
    assert "A reusable tool or workflow instruction routes to the best matching skill" in skill
    assert "an inventory match is a *candidate*, not proof" in skill
    assert "`upstream-issue` proposal file" in skill


def test_the_remember_command_states_the_provenance_rule() -> None:
    """A direct lesson write carries only provenance ids that actually exist."""
    command = _stock("commands/remember.md")
    lesson_write = command.split("**For a reusable lesson**", 1)[1].split("\n   - specific", 1)[0]
    assert "ciao memory learn --text-file <file>" in lesson_write
    assert "`--source <chat-id>` only when the actual source chat id is available" in lesson_write
    assert "`--request <id>` only when the actual request id is available" in lesson_write
    assert "Never invent ids, turns, archive paths or excerpts" in lesson_write
    assert "dedupes repeated sightings" in lesson_write


def test_the_capabilities_catalog_describes_the_memory_backup() -> None:
    """The catalog has to name the three entry points a user will look for in
    Settings, and say that the engine then runs on its own."""
    skill = " ".join(_stock("skills/ciao-capabilities/references/memory.md").split())
    assert "**Memory backup** (Settings → Advanced → Memory backup)" in skill
    assert "**Set up in Ciaobot**" in skill
    assert "**Copy setup prompt**" in skill
    assert "GitHub CLI signed in" in skill
    assert "every five minutes on its own" in skill


def test_the_capabilities_catalog_describes_the_routing_without_overclaiming() -> None:
    """The catalog is what a user asks "what can it do" and gets an answer from.

    It has to name the lesson route and the drafts, and it has to keep saying
    that the unattended run prepares rather than acts — otherwise the two
    surfaces tell a person opposite things about the same nightly run.
    """
    skill = _stock("skills/ciao-capabilities/references/models-and-extensions.md")
    assert "`skill-edit`" in skill and "`upstream-issue`" in skill
    assert "no transcript excerpt, path, name or credential leaves your machine" in skill
    assert "it never edits a skill or the workspace guide and never opens a public issue" in skill
    assert "`ciao proposal-check`" in skill


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
    assert "Nothing is promoted unattended" in doc
    assert "`x3+` recurrence is a" in doc
    assert "`/remember` keeps a real origin" in doc
    assert "`req:<id>`" in doc


def test_the_development_doc_covers_the_proposal_check_command() -> None:
    """A new CLI surface the contributor doc does not mention is one nobody finds."""
    doc = (REPO / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
    assert "### Proposal files and the Unsure list" in doc
    assert "ciao proposal-check" in doc


# ── The care prompt may only name what the control plane allows (#882) ────


def _care_prompt() -> str:
    """The schedule prompt plus the durable-memory curation references it points at (#1462)."""
    prompt = next(
        entry["prompt"]
        for entry in json.loads(_stock("schedules.json"))["schedules"]
        if entry["schedule_id"] == "system-memory-curation"
    )
    refs = resources.files("ciao.stock").joinpath("skills", "ciao-capabilities", "references", "memory-operations", "references")
    return "\n\n".join(
        [prompt, *(item.read_text(encoding="utf-8") for item in sorted(refs.iterdir(), key=lambda i: i.name) if item.name.endswith(".md"))]
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


def test_stock_memory_assets_route_repo_facts() -> None:
    assert "linked code repository" in _stock("commands/remember.md")


def test_the_capabilities_catalog_names_the_vault_ignore_file() -> None:
    memory = " ".join(_stock("skills/ciao-capabilities/references/memory.md").split())
    assert "`.ciaoignore`" in memory
