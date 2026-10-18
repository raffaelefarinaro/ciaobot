"""The core prompt carries the vault frontmatter contract.

Every chat, schedule, and subagent renders this prompt, so the rule that
new vault notes open with frontmatter — and rewrites preserve it — lives
here rather than in any one skill. Without it, automation keeps producing
frontmatter-less notes that the review queue can only flag after the fact
(e.g. a monitor file rewritten weekly with no `updated:` for the scanner).
"""

from importlib import resources

from ciao.core_prompt import _system_instructions
from ciao.local_session import MERGE_PROMPT


def _stock(*parts: str) -> str:
    node = resources.files("ciao.stock")
    for part in parts:
        node = node.joinpath(part)
    return node.read_text(encoding="utf-8")


def test_new_vault_notes_require_frontmatter() -> None:
    text = _system_instructions()
    assert "Every vault note you create opens with YAML frontmatter" in text


def test_rewrites_preserve_frontmatter() -> None:
    text = _system_instructions()
    assert "`updated:` moves only through a `note verify` still_valid/update verdict or `vault review keep`" in text


def test_notes_are_typed_from_the_vocabularys_categories_block() -> None:
    """The frontmatter rule is only half a contract without the vocabulary.

    A `type:` is only writable if the agent knows which list to read it comes
    from, and a category the owner added only reaches its own folder if the
    no-fit case is a question for the owner rather than a coined type. The
    core prompt only has to point at the block: the folder each category uses is
    named there, and the surfaces that write notes say so in full.
    """
    text = _system_instructions()
    assert "**Categories** section of `VOCABULARY.md`" in text
    assert "a note that fits none is a new-category question" in text


def test_working_document_routing_is_always_loaded() -> None:
    text = _system_instructions()
    assert "persistent working document" in text
    assert "active vault's `Workspace/`" in text
    assert "Search for an existing related document first" in text
    assert "relative Markdown links, not wikilinks" in text
    assert "An in-chat answer does not need a file" in text


# Issue #1461: shipped prompts must not give conflicting rules.


def test_category_fallback_is_one_rule_everywhere() -> None:
    interrogation = _stock("commands", "interrogation.md")
    remember = _stock("commands", "remember.md")
    assert "use the closest listed value" not in interrogation
    assert "new-category question" in interrogation
    assert "do not file it under the nearest one" in remember
    assert "new-category question" in _system_instructions()


def test_core_prompt_routes_facts_through_remember_not_entry_argv() -> None:
    text = _system_instructions()
    assert "`ciao memory update --region … --action … --entry …`" not in text
    assert "/remember" in text
    assert "memory-proposal-add --text-file" in text
    assert "shell argument" in text


def test_remember_names_a_kind_for_other_categories() -> None:
    remember = _stock("commands", "remember.md")
    assert "works the same way: pick it from the **Categories** section" not in remember
    assert "`--kind` accepts only" in remember


def test_customization_template_matches_the_core_prompt() -> None:
    text = _stock("workspace", "CIAO_CUSTOMIZATION.md")
    assert "vault-search" not in text
    assert "MEMORY.md`: durable workspace memory" not in text
    assert "Add or edit `.runtime/workspaces.json`" not in text
    assert "ciao vault search" in text
    assert "Settings" in text and "Workspaces" in text


def test_updated_rule_agrees_across_prompts() -> None:
    assert "refreshes `updated:` to today" not in _system_instructions()
    assert "Never refresh the note's `updated:` on an ordinary save" in _stock(
        "commands", "remember.md"
    )


def test_cli_skill_example_is_not_an_entry_argv_fact() -> None:
    cli = _stock("skills", "ciao-cli", "SKILL.md")
    assert 'ciao memory update --region memory --action add --entry "Prefers terse' not in cli
    assert "memory-proposal-add --kind memory --text-file" in cli
    assert '{"ok": true, "data": {"queued"' not in cli
    assert "Queued [memory] proposal in" in cli
    caps = _stock("skills", "ciao-capabilities", "SKILL.md")
    assert "`ciao memory update --region {memory,profile} --action {add,replace,remove}`" not in caps


def test_schedule_targets_are_projects_everywhere() -> None:
    core = _system_instructions()
    caps = _stock("skills", "ciao-capabilities", "SKILL.md")
    assert "confirm the target project or chat" not in core
    assert "target project/chat" not in caps
    assert "Automations page" in core


def test_chat_api_is_operator_only_in_capabilities() -> None:
    caps = _stock("skills", "ciao-capabilities", "SKILL.md")
    assert "an in-chat agent can drive" not in caps
    assert "PATCH /api/chats" not in caps
    assert "Do not use curl" in _system_instructions()


def test_merge_prompt_names_no_provider_tool_and_has_no_caps_shouting() -> None:
    assert "AskUserQuestion" not in MERGE_PROMPT
    assert "your question tool" in MERGE_PROMPT
    for word in ("BOTH", "STOP", "NOT"):
        assert word not in MERGE_PROMPT
