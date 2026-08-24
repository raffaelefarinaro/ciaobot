"""Tests for `ciao.vault_index`.

Covers the body-link graph extension: a relative markdown link in a note body
should produce the same kind of edge as frontmatter `related:`, with dedup,
anchor handling, and code-block escaping. Markdown links were previously
*validated* by the linter but produced no edge at all — every case here is a
capability that only wikilinks had.
"""

from __future__ import annotations

from pathlib import Path

from ciao import vault_index as vi


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ---- _extract_body_links ---------------------------------------------------


def test_extract_body_links_basic():
    text = "# Title\n\nSee [Mo](./People/Mo.md) and [Foo](./Projects/Foo.md).\n"
    assert vi._extract_body_links(text) == ["People/Mo", "Projects/Foo"]


def test_extract_body_links_resolve_against_the_containing_note():
    """A markdown destination is relative to the note holding it, not the vault
    root. Resolving from the vault root put every edge in the wrong folder."""
    text = "See [Mo](./Mo.md) and [Foo](../Projects/Foo.md).\n"
    assert vi._extract_body_links(text, "personal/People/Alba.md") == [
        "personal/People/Mo",
        "personal/Projects/Foo",
    ]


def test_extract_body_links_drops_targets_outside_the_vault():
    """A `../` chain that climbs past the vault root is not a vault edge, and
    must not resolve to some same-named note by accident."""
    assert vi._extract_body_links("[out](../../secrets.md)", "People/Alba.md") == []


def test_extract_body_links_skips_frontmatter():
    text = (
        "---\n"
        "related:\n"
        "  - People/X\n"
        "---\n"
        "# Title\n\nBody mentions [Mo](./People/Mo.md).\n"
    )
    # Only the body link should come out (frontmatter is stripped first).
    assert vi._extract_body_links(text) == ["People/Mo"]


def test_extract_body_links_drops_anchor_and_keeps_label_out_of_the_ref():
    text = (
        "# T\n"
        "Refs: [Mo Salah](./People/Mo.md), [Foo](./Projects/Foo.md#Decisions), "
        "[Bar Section](<./Projects/Bar Notes.md#Section>).\n"
    )
    assert vi._extract_body_links(text) == [
        "People/Mo",
        "Projects/Foo",
        "Projects/Bar Notes",
    ]


def test_extract_body_links_skips_non_note_destinations():
    """Only in-vault markdown targets are edges. An external URL, an image, a
    pure in-page anchor, and a leftover wikilink are all body text."""
    text = (
        "# T\n"
        "[site](https://example.com/a.md) ![shot](./images/shot.png) "
        "[top](#Heading) [old](./People/Mo.md) [[People/Mo]]\n"
    )
    assert vi._extract_body_links(text) == ["People/Mo"]


def test_extract_body_links_ignores_escaped_brackets():
    """`\\[label](x.md)` documents the syntax; it is not a link."""
    text = r"# T" "\n" r"Write \[label](./People/Nope.md) like this, but [Mo](./People/Mo.md) is real."
    assert vi._extract_body_links(text) == ["People/Mo"]


def test_extract_body_links_ignores_fenced_code():
    text = (
        "# T\n"
        "Real link: [Mo](./People/Mo.md).\n"
        "```\n"
        "Example: [x](./Should/NotCount.md)\n"
        "```\n"
        "Trailing: [Foo](./Projects/Foo.md).\n"
    )
    assert vi._extract_body_links(text) == ["People/Mo", "Projects/Foo"]


def test_extract_body_links_ignores_inline_code():
    text = "# T\nUse `[x](./Inline/Example.md)` like this, but [Mo](./People/Mo.md) is real.\n"
    assert vi._extract_body_links(text) == ["People/Mo"]


# ---- scan_vault integration -------------------------------------------------


def _scan(tmp_path: Path):
    """Run scan_vault against a synthetic vault rooted at tmp_path."""
    return vi.scan_vault(vault_root=tmp_path)


def test_scan_vault_picks_up_body_markdown_links_as_edges(tmp_path: Path):
    """A relative markdown link must produce a Memory Map edge.

    Before the swap only `[[wikilinks]]` did: a markdown link was validated by
    the linter and then ignored by the graph, so a note authored in plain
    markdown looked unconnected.
    """
    _write(
        tmp_path / "People" / "Mo.md",
        "---\nname: Mo\ntype: person\n---\n# Mo\n",
    )
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\nname: Foo\ntype: project\n---\n# Foo\n\n"
        "Worked with [Mo](../People/Mo.md) on this.\n",
    )

    entries = _scan(tmp_path)
    by_path = {str(e.path): e for e in entries}

    foo = by_path["memory-vault/Projects/Foo.md"]
    mo_path = "memory-vault/People/Mo.md"
    assert mo_path in foo.related, f"expected body link edge, got {foo.related}"


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
        "Also see [Mo](../People/Mo.md) (same target, different source).\n",
    )

    entries = _scan(tmp_path)
    foo = next(e for e in entries if e.path.name == "Foo.md")
    mo_path = "memory-vault/People/Mo.md"
    assert foo.related.count(mo_path) == 1, f"expected dedup, got {foo.related}"


def test_scan_vault_falls_back_to_a_unique_stem(tmp_path: Path):
    """A sibling-relative link that misses still resolves by unique stem.

    `[Mo](./Mo.md)` inside `Projects/` names `Projects/Mo`, which does not
    exist. Rather than drop the edge, `_resolve_related` falls back to the last
    segment when exactly one note in the vault carries that stem — the same
    best-effort rescue a bare `[[Mo]]` used to get.
    """
    _write(
        tmp_path / "People" / "Mo.md",
        "---\nname: Mo\ntype: person\n---\n# Mo\n",
    )
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\nname: Foo\ntype: project\n---\n# Foo\n\nMet [Mo](./Mo.md) yesterday.\n",
    )

    entries = _scan(tmp_path)
    foo = next(e for e in entries if e.path.name == "Foo.md")
    assert "memory-vault/People/Mo.md" in foo.related


def test_scan_vault_skips_self_link(tmp_path: Path):
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\nname: Foo\ntype: project\n---\n# Foo\n\n"
        "Self reference: [Foo](./Foo.md) should not loop.\n",
    )
    entries = _scan(tmp_path)
    foo = next(e for e in entries if e.path.name == "Foo.md")
    assert "memory-vault/Projects/Foo.md" not in foo.related


def test_scan_vault_unresolved_link_dropped_silently(tmp_path: Path):
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\nname: Foo\ntype: project\n---\n# Foo\n\n"
        "Mentions [Nobody](../People/DoesNotExist.md) which has no page.\n",
    )
    entries = _scan(tmp_path)
    foo = next(e for e in entries if e.path.name == "Foo.md")
    assert foo.related == []  # unresolved refs are skipped, not emitted


def test_scan_vault_leftover_wikilink_is_not_an_edge(tmp_path: Path):
    """A pre-migration `[[wikilink]]` must not crash the scan, and must not be
    an edge either: markdown links are the only dialect, so an unconverted
    wikilink is body text until the link migration rewrites it."""
    _write(
        tmp_path / "People" / "Mo.md",
        "---\nname: Mo\ntype: person\n---\n# Mo\n",
    )
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\nname: Foo\ntype: project\n---\n# Foo\n\nOld style [[People/Mo]].\n",
    )
    foo = next(e for e in _scan(tmp_path) if e.path.name == "Foo.md")
    assert foo.related == []


def test_scan_vault_neighbors_walk_uses_body_edges(tmp_path: Path):
    """End-to-end: a body markdown link should make the target reachable via
    neighbors()."""
    _write(
        tmp_path / "People" / "Mo.md",
        "---\nname: Mo\ntype: person\n---\n# Mo\n",
    )
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\nname: Foo\ntype: project\n---\n# Foo\n\nWith [Mo](../People/Mo.md).\n",
    )

    entries = _scan(tmp_path)
    hops = vi.neighbors(entries, "memory-vault/Projects/Foo.md", depth=1)
    paths = [str(e.path) for _, e in hops]
    assert "memory-vault/People/Mo.md" in paths


# ---- strip_references (delete-note backlink cleanup) ------------------------


def test_strip_body_links_label_becomes_plain_text():
    """Deleting a note must strip the markdown links pointing at it.

    Only wikilinks were stripped before, so deleting a note left every markdown
    link to it as a live-looking link to a file that no longer exists.
    """
    body = "Worked with [Mo](./People/Mo.md) on this.\n"
    entries = [
        vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person"),
    ]
    idx = vi._build_filename_index(entries)
    new_body, changed = vi._strip_body_links(body, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert new_body == "Worked with Mo on this.\n"


def test_strip_body_links_keeps_a_multiword_label_as_display_text():
    body = "See [Mo B.](./People/Mo.md) for details.\n"
    entries = [vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person")]
    idx = vi._build_filename_index(entries)
    new_body, changed = vi._strip_body_links(body, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert new_body == "See Mo B. for details.\n"


def test_strip_body_links_resolves_against_the_containing_note():
    """The same destination means different targets from different folders.

    `./Mo.md` inside `People/` is the deleted note; inside `Projects/` it is
    not. Stripping without the source path would erase the wrong link.
    """
    body = "Sibling [Mo](./Mo.md) and cousin [Mo](../Projects/Mo.md).\n"
    entries = [
        vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person"),
        vi.Entry(path=Path("memory-vault/Projects/Mo.md"), title="Mo", type="project"),
    ]
    idx = vi._build_filename_index(entries)
    new_body, changed = vi._strip_body_links(
        body, idx, "memory-vault/People/Mo.md", "People/Alba.md"
    )
    assert changed is True
    assert new_body == "Sibling Mo and cousin [Mo](../Projects/Mo.md).\n"


def test_strip_body_links_leaves_unrelated_links_alone():
    body = "See [Mo](./People/Mo.md) and [Foo](./Projects/Foo.md).\n"
    entries = [
        vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person"),
        vi.Entry(path=Path("memory-vault/Projects/Foo.md"), title="Foo", type="project"),
    ]
    idx = vi._build_filename_index(entries)
    new_body, changed = vi._strip_body_links(body, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert new_body == "See Mo and [Foo](./Projects/Foo.md).\n"


def test_strip_body_links_ignores_fenced_code():
    body = "Real: [Mo](./People/Mo.md).\n```\nExample: [Mo](./People/Mo.md)\n```\n"
    entries = [vi.Entry(path=Path("memory-vault/People/Mo.md"), title="Mo", type="person")]
    idx = vi._build_filename_index(entries)
    new_body, changed = vi._strip_body_links(body, idx, "memory-vault/People/Mo.md")
    assert changed is True
    assert new_body == "Real: Mo.\n```\nExample: [Mo](./People/Mo.md)\n```\n"


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
        "Worked with [Mo](../People/Mo.md) on this.\n",
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


def test_strip_references_matches_a_re_rooted_id(tmp_path: Path):
    """A per-root id has to be compared in its own path space.

    After the re-rooting an id reads `<root>/memory-vault/...`, but a bare scan
    of one vault renders `memory-vault/...`. The two never compared equal, so
    nothing was stripped while the delete went ahead anyway - every migrated
    install accumulated dangling `related:` entries and markdown links, and the
    endpoint cheerfully reported `edited_backlinks: []`.
    """
    _write(tmp_path / "People" / "Mo.md", "---\nname: Mo\ntype: person\n---\n# Mo\n")
    _write(
        tmp_path / "Projects" / "Foo.md",
        "---\n"
        "name: Foo\n"
        "type: project\n"
        "related:\n"
        "  - People/Mo\n"
        "---\n"
        "# Foo\n\n"
        "Worked with [Mo](../People/Mo.md) on this.\n",
    )
    prefix = Path("personal/memory-vault")

    edited = vi.strip_references(
        tmp_path, "personal/memory-vault/People/Mo.md", path_prefix=prefix
    )

    assert edited == ["personal/memory-vault/Projects/Foo.md"]
    foo_text = (tmp_path / "Projects" / "Foo.md").read_text(encoding="utf-8")
    assert "People/Mo" not in foo_text
    assert "Worked with Mo on this." in foo_text


# ---- INDEX.md rendering -----------------------------------------------------


def test_format_md_renders_entries_as_real_relative_links(tmp_path: Path):
    """Index entries were backticked non-links only to keep INDEX.md out of
    Obsidian's graph view. With markdown as the dialect they must be navigable —
    and still not Ciaobot nodes, since `scan_vault` skips generated files."""
    _write(
        tmp_path / "personal" / "People" / "Mo.md",
        "---\nname: Mo\ntype: person\n---\n# Mo\n",
    )

    rendered = vi.format_md(_scan(tmp_path))

    assert "- [personal/People/Mo](./personal/People/Mo.md)" in rendered
    assert "`personal/People/Mo`" not in rendered


def test_format_md_wraps_a_destination_containing_a_space(tmp_path: Path):
    """A bare destination ends at the first whitespace, so `./Mo Salah.md`
    would resolve to `./Mo` and lint as a broken link."""
    _write(
        tmp_path / "People" / "Mo Salah.md",
        "---\nname: Mo\ntype: person\n---\n# Mo\n",
    )

    rendered = vi.format_md(_scan(tmp_path))

    assert "[People/Mo Salah](<./People/Mo Salah.md>)" in rendered


def test_write_index_file_links_memory_only_when_it_exists(tmp_path: Path):
    """A generated file must not manufacture a permanent lint finding.

    An unconditional `[MEMORY](./MEMORY.md)` would be a standing
    `broken_markdown_links` entry — and so `os-audit` exit 1 forever — in every
    vault with no root MEMORY.md.
    """
    from ciao.vault_lint import run_validation

    _write(tmp_path / "People" / "Mo.md", "---\nname: Mo\ntype: person\n---\n# Mo\n")
    entries = _scan(tmp_path)

    vi.write_index_file(entries, tmp_path / "INDEX.md")
    assert "MEMORY" not in (tmp_path / "INDEX.md").read_text(encoding="utf-8")
    assert run_validation(tmp_path)["broken_markdown_links"] == []

    (tmp_path / "MEMORY.md").write_text("# Curated\n", encoding="utf-8")
    vi.write_index_file(entries, tmp_path / "INDEX.md")
    assert "[MEMORY](./MEMORY.md)" in (tmp_path / "INDEX.md").read_text(encoding="utf-8")
    assert run_validation(tmp_path)["broken_markdown_links"] == []
