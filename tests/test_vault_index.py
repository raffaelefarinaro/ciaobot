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


def test_scan_vault_captures_frontmatter_description(tmp_path: Path):
    _write(
        tmp_path / "Projects" / "Bar.md",
        "---\nname: Bar\ntype: project\ndescription: A short blurb.\n---\n# Bar\n",
    )
    _write(
        tmp_path / "Projects" / "Baz.md",
        "---\nname: Baz\ntype: project\n---\n# Baz\n",
    )

    entries = _scan(tmp_path)
    by_path = {str(e.path): e for e in entries}

    assert by_path["memory-vault/Projects/Bar.md"].description == "A short blurb."
    assert by_path["memory-vault/Projects/Baz.md"].description == ""


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


# ---- strip_references (delete-note backlink cleanup) ------------------------


def test_strip_body_wikilinks_bare_ref_becomes_plain_text():
    body = "Worked with [[People/Mo]] on this.\n"
    entries = [
        vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person"),
    ]
    idx = vi._build_filename_index(entries)
    new_body, changed = vi._strip_body_wikilinks(body, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert new_body == "Worked with Mo on this.\n"


def test_strip_body_wikilinks_alias_kept_as_display_text():
    body = "See [[People/Mo|Mo B.]] for details.\n"
    entries = [vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person")]
    idx = vi._build_filename_index(entries)
    new_body, changed = vi._strip_body_wikilinks(body, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert new_body == "See Mo B. for details.\n"


def test_strip_body_wikilinks_leaves_unrelated_links_alone():
    body = "See [[People/Mo]] and [[Projects/Foo]].\n"
    entries = [
        vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person"),
        vi.Entry(path=Path("memory-vault/Projects/Foo.md"), title="Foo", type="project"),
    ]
    idx = vi._build_filename_index(entries)
    new_body, changed = vi._strip_body_wikilinks(body, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert new_body == "See Mo and [[Projects/Foo]].\n"


def test_strip_body_wikilinks_ignores_fenced_code():
    body = "Real: [[People/Mo]].\n```\nExample: [[People/Mo]]\n```\n"
    entries = [vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person")]
    idx = vi._build_filename_index(entries)
    new_body, changed = vi._strip_body_wikilinks(body, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert new_body == "Real: Mo.\n```\nExample: [[People/Mo]]\n```\n"


def test_strip_frontmatter_related_removes_single_matching_item_keeps_others():
    fm = "name: Foo\ntype: project\nrelated:\n  - People/Mo\n  - Projects/Bar\n"
    entries = [
        vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person"),
        vi.Entry(path=Path("memory-vault/Projects/Bar.md"), title="Bar", type="project"),
    ]
    idx = vi._build_filename_index(entries)
    new_fm, changed = vi._strip_frontmatter_related(fm, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert "People/Mo" not in new_fm
    assert "Projects/Bar" in new_fm
    assert "related:" in new_fm


def test_strip_frontmatter_related_drops_key_when_last_item_removed():
    fm = "name: Foo\ntype: project\nrelated:\n  - People/Mo\n"
    entries = [vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person")]
    idx = vi._build_filename_index(entries)
    new_fm, changed = vi._strip_frontmatter_related(fm, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert "related:" not in new_fm
    assert "name: Foo" in new_fm and "type: project" in new_fm


def test_strip_frontmatter_related_inline_flow_list():
    fm = "name: Foo\nrelated: [People/Mo, Projects/Bar]\n"
    entries = [
        vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person"),
        vi.Entry(path=Path("memory-vault/Projects/Bar.md"), title="Bar", type="project"),
    ]
    idx = vi._build_filename_index(entries)
    new_fm, changed = vi._strip_frontmatter_related(fm, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert "related: [Projects/Bar]" in new_fm


def test_strip_frontmatter_related_single_scalar_drops_key():
    fm = "name: Foo\nrelated: People/Mo\n"
    entries = [vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person")]
    idx = vi._build_filename_index(entries)
    new_fm, changed = vi._strip_frontmatter_related(fm, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert "related" not in new_fm


def test_strip_references_end_to_end_edits_backlinks_before_delete(tmp_path: Path):
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
        "Worked with [[People/Mo]] on this.\n",
    )
    _write(
        tmp_path / "Projects" / "Untouched.md",
        "---\nname: Untouched\ntype: project\n---\n# Untouched\n\nNo relation here.\n",
    )

    edited = vi.strip_references(tmp_path, "memory-vault/People/Mo.md")

    assert edited == ["memory-vault/Projects/Foo.md"]
    foo_text = (tmp_path / "Projects" / "Foo.md").read_text(encoding="utf-8")
    assert "People/Mo" not in foo_text
    assert "related:" not in foo_text
    assert "Worked with Mo on this." in foo_text
    untouched_text = (tmp_path / "Projects" / "Untouched.md").read_text(encoding="utf-8")
    assert untouched_text == "---\nname: Untouched\ntype: project\n---\n# Untouched\n\nNo relation here.\n"

    # The target file itself is untouched by strip_references (deletion is a
    # separate, subsequent step performed by the caller).
    assert (tmp_path / "People" / "Mo.md").exists()
