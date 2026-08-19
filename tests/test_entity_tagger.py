"""Tests for the vault entity tagger against a synthetic INDEX.md."""

from __future__ import annotations

from pathlib import Path

from ciao.context.entity_tagger import find_entities, format_entities, get_index


def _write_index(tmp_path: Path, body: str) -> Path:
    (tmp_path / "INDEX.md").write_text(body, encoding="utf-8")
    return tmp_path


def test_matches_names_and_aliases(tmp_path: Path) -> None:
    _write_index(tmp_path, """# Vault Index

## Personal

### person (2)

- [People/Alba](./People/Alba.md) (tags: person, friend; aliases: Alba)
- [People/Anne-Marie-de-Weijer](./People/Anne-Marie-de-Weijer.md) (tags: colleague; aliases: Anne-Marie)

### project (1)

- [Projects/Ciaobot-Improvements](./Projects/Ciaobot-Improvements.md) (tags: project)
""")
    hits = find_entities("Meeting with Alba about Ciaobot-Improvements next week", tmp_path)
    paths = {e.path for e in hits}
    assert "People/Alba" in paths
    assert "Projects/Ciaobot-Improvements" in paths


def test_filters_matches_to_the_active_workspace_only(tmp_path: Path) -> None:
    """One name can exist in two workspaces; only the active one may match.

    There is no cross-workspace escape hatch. A `shared/` prefix was once
    treated as visible everywhere, but nothing could read such a note — every
    workspace-scoped tool roots at `<vault>/<workspace>` — and a real
    two-workspace vault had no notes in both trees. It is now an ordinary
    non-matching prefix.
    """
    _write_index(tmp_path, """# Vault Index

- [personal/Projects/Apollo](./personal/Projects/Apollo.md) (tags: project; aliases: Apollo)
- [work/Projects/Apollo](./work/Projects/Apollo.md) (tags: project; aliases: Apollo)
- [shared/People/Alba](./shared/People/Alba.md) (tags: person; aliases: Alba)
- [personal/People/Defne](./personal/People/Defne.md) (tags: person; aliases: Defne)
""")

    hits = find_entities("Apollo update with Alba and Defne", tmp_path, workspace="work")
    paths = {e.path for e in hits}
    assert "work/Projects/Apollo" in paths
    assert "personal/Projects/Apollo" not in paths
    assert "personal/People/Defne" not in paths
    assert "shared/People/Alba" not in paths


def test_unprefixed_legacy_entities_require_an_explicit_owner(
    tmp_path: Path,
) -> None:
    _write_index(tmp_path, "- [People/Alba](./People/Alba.md) (aliases: Alba)\n")

    assert find_entities("Alba", tmp_path, workspace="work") == []
    assert find_entities(
        "Alba",
        tmp_path,
        workspace="personal",
        legacy_workspace="personal",
    )
    assert find_entities(
        "Alba",
        tmp_path,
        workspace="work",
        legacy_workspace="personal",
    ) == []


def test_prefixed_entities_match_note_name_without_aliases(tmp_path: Path) -> None:
    _write_index(tmp_path, """# Vault Index

- [client/projects/active/Apollo](./client/projects/active/Apollo.md)
""")

    hits = find_entities("Apollo update", tmp_path, workspace="client")
    assert len(hits) == 1
    assert hits[0].path == "client/projects/active/Apollo"
    assert hits[0].name == "Apollo"
    assert hits[0].category == "Projects"
    assert format_entities(hits) == (
        "mentioned_entities:\n"
        "- [Apollo](./client/projects/active/Apollo.md) (project)"
    )


def test_respects_whole_word_and_skips_short_aliases(tmp_path: Path) -> None:
    _write_index(tmp_path, """
- [People/Mo](./People/Mo.md) (tags: family; aliases: Mo)
- [People/Alba](./People/Alba.md) (aliases: Alba)
""")
    # "Mo" is too short (< _MIN_ALIAS_LEN = 3), so it shouldn't match.
    hits = find_entities("Mo said something", tmp_path)
    assert not hits
    # "Alba" is long enough; "Albania" should NOT match because of word boundary.
    hits = find_entities("Albania is a country", tmp_path)
    assert not hits


def test_readme_folds_to_folder_not_shared_filename(tmp_path: Path) -> None:
    # Every project folder has a README; matching on the bare "README" token
    # must not light up every project at once. Each README folds to its folder.
    _write_index(tmp_path, """# Vault Index

- [personal/projects/active/consulting/README](./personal/projects/active/consulting/README.md) (tags: project)
- [personal/projects/active/thailand-2027/README](./personal/projects/active/thailand-2027/README.md) (tags: project)
""")
    # A stray "README" mention (e.g. from an injected file path) matches nothing.
    assert find_entities("please open the README file", tmp_path, workspace="personal") == []
    # A genuine folder-name mention still resolves, once, to that project.
    hits = find_entities("update the consulting project", tmp_path, workspace="personal")
    assert [e.path for e in hits] == ["personal/projects/active/consulting/README"]
    assert hits[0].name == "consulting"


def test_structural_log_and_index_files_are_not_matchable(tmp_path: Path) -> None:
    # "log"/"index" are structural filenames whose bare names are common words;
    # they should never be surfaced as entities.
    _write_index(tmp_path, """# Vault Index

- [personal/projects/active/consulting/log](./personal/projects/active/consulting/log.md) (tags: project, log)
- [personal/projects/active/consulting/index](./personal/projects/active/consulting/index.md) (tags: project)
""")
    assert find_entities("check the log and the index", tmp_path, workspace="personal") == []


def test_handles_missing_index(tmp_path: Path) -> None:
    assert find_entities("anything", tmp_path) == []


def test_format_output(tmp_path: Path) -> None:
    _write_index(tmp_path, "- [People/Alba](./People/Alba.md) (aliases: Alba)\n")
    hits = find_entities("hi Alba", tmp_path)
    rendered = format_entities(hits)
    assert "- [Alba](./People/Alba.md) (people)" in rendered
    assert rendered.startswith("mentioned_entities:")


def test_refreshes_on_mtime_change(tmp_path: Path) -> None:
    import os
    _write_index(tmp_path, "- [People/Alba](./People/Alba.md) (aliases: Alba)\n")
    first = find_entities("Alba here", tmp_path)
    assert len(first) == 1
    # Rewrite INDEX.md with a new entity, bump mtime.
    (tmp_path / "INDEX.md").write_text("- [People/Defne](./People/Defne.md) (aliases: Defne)\n", encoding="utf-8")
    future = (tmp_path / "INDEX.md").stat().st_mtime + 2
    os.utime(tmp_path / "INDEX.md", (future, future))
    # get_index is process-cached; reuse clears when path changes. Same path here,
    # so rely on mtime-based refresh.
    second = find_entities("Alba here Defne", tmp_path)
    names = {e.name for e in second}
    assert names == {"Defne"}


def test_index_bullets_written_by_vault_index_are_parseable(tmp_path: Path) -> None:
    """`vault_index` writes INDEX.md and this module reads it back.

    The two are coupled by the bullet format and nothing else, so switching the
    index from a backticked path to a real markdown link silently blanks every
    entity hint unless `_BULLET_RE` moves with it.
    """
    from ciao.vault_index import scan_vault, write_index_file

    note = tmp_path / "personal" / "People" / "Alba.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: person\ntitle: Alba\naliases: [Alba]\n---\n# Alba\n",
        encoding="utf-8",
    )
    write_index_file(scan_vault(tmp_path), tmp_path / "INDEX.md")

    hits = find_entities("what about Alba", tmp_path, workspace="personal")

    assert [e.path for e in hits] == ["personal/People/Alba"]


def test_format_entities_quotes_a_path_with_spaces(tmp_path: Path) -> None:
    """A bare destination ends at the first space, so `./People/Mo Salah.md`
    would resolve to `./People/Mo` — the angle-bracket form keeps it whole."""
    _write_index(
        tmp_path,
        "- [People/Mo Salah](<./People/Mo Salah.md>) (aliases: Mo Salah)\n",
    )
    rendered = format_entities(find_entities("call Mo Salah", tmp_path))
    assert "[Mo Salah](<./People/Mo Salah.md>)" in rendered
