"""Characterisation tests for the shared vault link primitives.

These pin the behaviour the primitives had while they were private helpers
scattered across `vault_index`, `vault_lint`, and `vault_migrate_links`, so a
later change to one of them cannot quietly alter what the other consumers see.
The interesting cases are the ones the original docstrings called out as bugs
that had already been paid for once.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ciao.vault_links
from ciao.vault_links import (
    FENCED_CODE_RE,
    FM_LIST_ITEM_RE,
    FM_RELATED_KEY_RE,
    FRONTMATTER_RE,
    INLINE_CODE_RE,
    MARKDOWN_LINK_RE,
    WIKILINK_RE,
    alias_separator,
    is_escaped,
    is_link_start,
    parse_wikilink,
)


# ---- the leaf-module invariant ---------------------------------------------


def test_vault_links_imports_nothing_from_ciao() -> None:
    """The seam only works because this module cannot cycle.

    `vault_index`, `vault_lint`, `vault_migrate_links`, and `vault_rehome` all
    import it, and they already import each other in one direction. A single
    `from ciao...` here would reintroduce the tangle the module exists to undo,
    so the property is asserted rather than left to review.
    """
    tree = ast.parse(Path(ciao.vault_links.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    assert [m for m in imported if m.split(".")[0] == "ciao"] == []


# ---- is_escaped ------------------------------------------------------------


def test_is_escaped_counts_backslash_runs() -> None:
    # An odd run escapes; an even run is literal backslashes before the char.
    assert is_escaped(r"\[", 1) is True
    assert is_escaped(r"\\[", 2) is False
    assert is_escaped(r"\\\[", 3) is True
    assert is_escaped("[", 0) is False


# ---- is_link_start ---------------------------------------------------------


def test_is_link_start_rejects_images_and_escapes() -> None:
    assert is_link_start("[Mo](./People/Mo.md)", 0) is True
    assert is_link_start("![alt](x.png)", 1) is False
    assert is_link_start(r"\[not a link](x.md)", 1) is False
    # An even run of backslashes is a literal backslash, so the link is real.
    assert is_link_start(r"\\[Mo](./People/Mo.md)", 2) is True


# ---- parse_wikilink --------------------------------------------------------


def test_parse_wikilink_splits_ref_anchor_alias() -> None:
    assert parse_wikilink("[[People/Mo]]") == ("People/Mo", "", "")
    assert parse_wikilink("[[People/Mo#Bio]]") == ("People/Mo", "Bio", "")
    assert parse_wikilink("[[People/Mo|Mo]]") == ("People/Mo", "", "Mo")
    assert parse_wikilink("[[People/Mo#Bio|Mo]]") == ("People/Mo", "Bio", "Mo")


def test_parse_wikilink_drops_the_table_cell_escape_from_the_ref() -> None:
    """The regression the original docstring documents.

    In a markdown table the alias pipe is spelled `\\|`. Left on the ref it
    produced `People/Mo\\`, which resolved to nothing, and the migration emitted
    `./People/Mo\\.md` while reporting the link as already dead.
    """
    assert parse_wikilink(r"[[People/Mo\|Mo]]") == ("People/Mo", "", "Mo")


# ---- alias_separator -------------------------------------------------------


def test_alias_separator_reports_the_dialect_the_note_used() -> None:
    assert alias_separator(r"[[People/Mo\|Mo]]") == r"\|"
    assert alias_separator("[[People/Mo|Mo]]") == "|"
    assert alias_separator("[[People/Mo]]") == "|"


# ---- WIKILINK_RE -----------------------------------------------------------


def test_wikilink_re_leaves_a_pure_anchor_alone() -> None:
    # Group 1 needs a non-`#` character, so `[[#Heading]]` cannot match.
    assert WIKILINK_RE.search("see [[#Heading]]") is None


def test_wikilink_re_does_not_eat_a_flow_sequence_bracket() -> None:
    """`[` excluded from the ref keeps `people: [[[People/Mo]]]` intact."""
    match = WIKILINK_RE.search("people: [[[People/Mo]]]")
    assert match is not None
    assert match.group(1) == "People/Mo"


# ---- MARKDOWN_LINK_RE ------------------------------------------------------


def test_markdown_link_re_captures_bare_angle_and_titled_destinations() -> None:
    bare = MARKDOWN_LINK_RE.search("[Mo](./People/Mo.md)")
    assert bare is not None
    assert (bare.group("label"), bare.group("bare")) == ("Mo", "./People/Mo.md")

    angle = MARKDOWN_LINK_RE.search("[Mo](<./People/Mo Salah.md>)")
    assert angle is not None
    assert angle.group("angle") == "./People/Mo Salah.md"

    titled = MARKDOWN_LINK_RE.search('[Mo](./People/Mo.md "Tooltip")')
    assert titled is not None
    assert titled.group("bare") == "./People/Mo.md"


# ---- block structure -------------------------------------------------------


def test_code_and_frontmatter_patterns_match_their_blocks() -> None:
    assert FENCED_CODE_RE.search("a\n```\n[[Mo]]\n```\nb") is not None
    assert INLINE_CODE_RE.search("prose `[[Mo]]` prose") is not None
    frontmatter = FRONTMATTER_RE.match("---\ntype: person\n---\nbody\n")
    assert frontmatter is not None
    assert frontmatter.group(1) == "type: person"


def test_related_key_and_list_item_patterns() -> None:
    inline = FM_RELATED_KEY_RE.match("related: [People/Mo, Ideas/Thing]")
    assert inline is not None
    assert inline.group(2) == "[People/Mo, Ideas/Thing]"
    assert FM_RELATED_KEY_RE.match("relatedTo:") is not None

    item = FM_LIST_ITEM_RE.match("  - People/Mo")
    assert item is not None
    assert item.group(2) == "People/Mo"
