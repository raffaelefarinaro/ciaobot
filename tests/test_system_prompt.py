"""The core prompt carries the vault frontmatter contract.

Every chat, schedule, and subagent renders this prompt, so the rule that
new vault notes open with frontmatter — and rewrites preserve it — lives
here rather than in any one skill. Without it, automation keeps producing
frontmatter-less notes that the review queue can only flag after the fact
(e.g. a monitor file rewritten weekly with no `updated:` for the scanner).
"""

from ciao.core_prompt import _system_instructions


def test_new_vault_notes_require_frontmatter() -> None:
    text = _system_instructions()
    assert "Every vault note you create opens with YAML frontmatter" in text


def test_rewrites_preserve_frontmatter() -> None:
    text = _system_instructions()
    assert "preserves that block and refreshes `updated:` to today" in text


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
