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
