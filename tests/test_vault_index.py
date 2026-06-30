"""Tests for `ciao.vault_index`.

Covers the body-wikilink graph extension: body `[[wikilinks]]` should produce
the same kind of edge as frontmatter `related:`, with dedup, anchor handling,
and code-block escaping.
"""

from __future__ import annotations

from pathlib import Path

from ciao import vault_index as vi


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ---- _extract_body_wikilinks ------------------------------------------------


def test_extract_body_wikilinks_basic():
    text = "# Title\n\nSee [[People/Mo]] and [[Projects/Foo]].\n"
    assert vi._extract_body_wikilinks(text) == ["People/Mo", "Projects/Foo"]


def test_extract_body_wikilinks_skips_frontmatter():
    text = (
        "---\n"
        "related:\n"
        "  - [[People/X]]\n"
        "---\n"
        "# Title\n\nBody mentions [[People/Mo]].\n"
    )
    # Only the body wikilink should come out (frontmatter is stripped first).
    assert vi._extract_body_wikilinks(text) == ["People/Mo"]


def test_extract_body_wikilinks_handles_alias_and_anchor():
    text = (
        "# T\n"
        "Refs: [[People/Mo|Mo]], [[Projects/Foo#Decisions]], "
        "[[Projects/Bar#Section|Bar Section]].\n"
    )
    assert vi._extract_body_wikilinks(text) == [
        "People/Mo",
        "Projects/Foo",
        "Projects/Bar",
    ]


def test_extract_body_wikilinks_skips_pure_anchors_and_empty():
    text = "# T\n[[#OnlyAnchor]] [[ ]] [[]] should be ignored.\n"
    assert vi._extract_body_wikilinks(text) == []


def test_extract_body_wikilinks_ignores_fenced_code():
    text = (
        "# T\n"
        "Real link: [[People/Mo]].\n"
        "```\n"
        "Example: [[Should/NotCount]]\n"
        "```\n"
        "Trailing: [[Projects/Foo]].\n"
    )
    assert vi._extract_body_wikilinks(text) == ["People/Mo", "Projects/Foo"]


def test_extract_body_wikilinks_ignores_inline_code():
    text = "# T\nUse `[[Inline/Example]]` like this, but [[People/Mo]] is real.\n"
    assert vi._extract_body_wikilinks(text) == ["People/Mo"]


# ---- scan_vault integration -------------------------------------------------


def _scan(tmp_path: Path):
    """Run scan_vault against a synthetic vault rooted at tmp_path."""
    return vi.scan_vault(vault_root=tmp_path)


def test_scan_vault_picks_up_body_wikilinks_as_edges(tmp_path: Path):
    _write(
        tmp_path / "People" / "Mo.md",
        "---\nname: Mo\ntype: person\n---\n# Mo\n",
    )
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\nname: Foo\ntype: project\n---\n# Foo\n\n"
        "Worked with [[People/Mo]] on this.\n",
    )

    entries = _scan(tmp_path)
    by_path = {str(e.path): e for e in entries}

    foo = by_path["memory-vault/Projects/Foo.md"]
    mo_path = "memory-vault/People/Mo.md"
    assert mo_path in foo.related, f"expected body wikilink edge, got {foo.related}"


def test_scan_vault_assigns_workspace_from_first_path_segment(tmp_path: Path):
    _write(
        tmp_path / "client" / "projects" / "active" / "Apollo.md",
        "---\ntitle: Apollo\ntype: project\n---\n# Apollo\n",
    )
    _write(
        tmp_path / "shared" / "People" / "Alba.md",
        "---\ntitle: Alba\ntype: person\n---\n# Alba\n",
    )

    entries = _scan(tmp_path)
    by_path = {str(e.path): e for e in entries}
    assert by_path["memory-vault/client/projects/active/Apollo.md"].workspace == "client"
    assert by_path["memory-vault/shared/People/Alba.md"].workspace == "shared"

    rendered = vi.format_md(entries)
    assert "## Client" in rendered
    assert "## Shared" in rendered


def test_scan_vault_dedupes_frontmatter_and_body_edges(tmp_path: Path):
    _write(
        tmp_path / "People" / "Mo.md",
        "---\nname: Mo\ntype: person\n---\n# Mo\n",
    )
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\n"
        "name: Foo\n"
        "type: project\n"
        "related:\n"
        "  - People/Mo\n"
        "---\n"
        "# Foo\n\n"
        "Also see [[People/Mo]] (same target, different source).\n",
    )

    entries = _scan(tmp_path)
    foo = next(e for e in entries if e.path.name == "Foo.md")
    mo_path = "memory-vault/People/Mo.md"
    assert foo.related.count(mo_path) == 1, f"expected dedup, got {foo.related}"


def test_scan_vault_resolves_bare_filename_wikilink(tmp_path: Path):
    """`[[Mo]]` (bare stem) resolves to People/Mo when the stem is unique."""
    _write(
        tmp_path / "People" / "Mo.md",
        "---\nname: Mo\ntype: person\n---\n# Mo\n",
    )
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\nname: Foo\ntype: project\n---\n# Foo\n\nMet [[Mo]] yesterday.\n",
    )

    entries = _scan(tmp_path)
    foo = next(e for e in entries if e.path.name == "Foo.md")
    assert "memory-vault/People/Mo.md" in foo.related


def test_scan_vault_skips_self_link(tmp_path: Path):
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\nname: Foo\ntype: project\n---\n# Foo\n\n"
        "Self reference: [[Projects/Foo]] should not loop.\n",
    )
    entries = _scan(tmp_path)
    foo = next(e for e in entries if e.path.name == "Foo.md")
    assert "memory-vault/Projects/Foo.md" not in foo.related


def test_scan_vault_unresolved_wikilink_dropped_silently(tmp_path: Path):
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\nname: Foo\ntype: project\n---\n# Foo\n\n"
        "Mentions [[People/DoesNotExist]] which has no page.\n",
    )
    entries = _scan(tmp_path)
    foo = next(e for e in entries if e.path.name == "Foo.md")
    assert foo.related == []  # unresolved refs are skipped, not emitted


def test_scan_vault_neighbors_walk_uses_body_edges(tmp_path: Path):
    """End-to-end: a body wikilink should make the target reachable via neighbors()."""
    _write(
        tmp_path / "People" / "Mo.md",
        "---\nname: Mo\ntype: person\n---\n# Mo\n",
    )
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\nname: Foo\ntype: project\n---\n# Foo\n\nWith [[People/Mo]].\n",
    )

    entries = _scan(tmp_path)
    hops = vi.neighbors(entries, "memory-vault/Projects/Foo.md", depth=1)
    paths = [str(e.path) for _, e in hops]
    assert "memory-vault/People/Mo.md" in paths
