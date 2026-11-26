"""Direct tests for `vault_reference_rewrite.rewrite_references`.

The function repoints a note's references at notes that moved. Frontmatter
`related:` and CRLF handling is covered in `test_vault_index.py`; these cover
the body: both link dialects, the alias spelling, and the spans left alone.
"""

from __future__ import annotations

from pathlib import Path

from ciao import vault_index as vi
from ciao.vault_reference_rewrite import rewrite_references


def _rewrite(tmp_path: Path, text: str, source: str = "Notes/Hub.md") -> tuple[str, list[dict]]:
    vault = tmp_path / "memory-vault"
    (vault / "projects" / "active" / "demo").mkdir(parents=True)
    (vault / "projects" / "active" / "demo" / "demo.md").write_bytes(b"# Demo\n")
    index = vi.build_filename_index(vi.scan_vault(vault))
    return rewrite_references(
        text,
        source,
        source,
        {"projects/active/demo/demo": "projects/completed/demo/demo"},
        index,
        moved_by_resolved={"memory-vault/projects/active/demo/demo.md": "projects/completed/demo/demo"},
    )


def test_wikilink_is_repointed_and_keeps_its_alias(tmp_path: Path) -> None:
    text = "See [[projects/active/demo/demo|the demo]].\n"
    rewritten, changes = _rewrite(tmp_path, text)
    assert "[[projects/completed/demo/demo|the demo]]" in rewritten
    assert changes


def test_wikilink_in_a_fence_or_inline_code_is_left_alone(tmp_path: Path) -> None:
    text = "```\n[[projects/active/demo/demo]]\n```\n`[[projects/active/demo/demo]]`\n"
    rewritten, changes = _rewrite(tmp_path, text)
    assert rewritten == text
    assert not changes


def test_a_note_that_names_no_moved_target_is_unchanged(tmp_path: Path) -> None:
    text = "Nothing to see: [[Notes/Other]].\n"
    rewritten, changes = _rewrite(tmp_path, text)
    assert rewritten == text
    assert not changes
