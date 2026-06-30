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

- `People/Alba` (tags: person, friend; aliases: Alba)
- `People/Anne-Marie-de-Weijer` (tags: colleague; aliases: Anne-Marie)

### project (1)

- `Projects/Ciao-Improvements` (tags: project)
""")
    hits = find_entities("Meeting with Alba about Ciao-Improvements next week", tmp_path)
    paths = {e.path for e in hits}
    assert "People/Alba" in paths
    assert "Projects/Ciao-Improvements" in paths


def test_filters_matches_to_active_workspace_and_shared_roots(tmp_path: Path) -> None:
    _write_index(tmp_path, """# Vault Index

- `personal/Projects/Apollo` (tags: project; aliases: Apollo)
- `work/Projects/Apollo` (tags: project; aliases: Apollo)
- `shared/People/Alba` (tags: person; aliases: Alba)
- `personal/People/Defne` (tags: person; aliases: Defne)
""")

    hits = find_entities("Apollo update with Alba and Defne", tmp_path, workspace="work")
    paths = {e.path for e in hits}
    assert "work/Projects/Apollo" in paths
    assert "shared/People/Alba" in paths
    assert "personal/Projects/Apollo" not in paths
    assert "personal/People/Defne" not in paths


def test_prefixed_entities_match_note_name_without_aliases(tmp_path: Path) -> None:
    _write_index(tmp_path, """# Vault Index

- `client/projects/active/Apollo`
""")

    hits = find_entities("Apollo update", tmp_path, workspace="client")
    assert len(hits) == 1
    assert hits[0].path == "client/projects/active/Apollo"
    assert hits[0].name == "Apollo"
    assert hits[0].category == "Projects"
    assert format_entities(hits) == "mentioned_entities:\n- [[client/projects/active/Apollo]] (project)"


def test_respects_whole_word_and_skips_short_aliases(tmp_path: Path) -> None:
    _write_index(tmp_path, """
- `People/Mo` (tags: family; aliases: Mo)
- `People/Alba` (aliases: Alba)
""")
    # "Mo" is too short (< _MIN_ALIAS_LEN = 3), so it shouldn't match.
    hits = find_entities("Mo said something", tmp_path)
    assert not hits
    # "Alba" is long enough; "Albania" should NOT match because of word boundary.
    hits = find_entities("Albania is a country", tmp_path)
    assert not hits


def test_handles_missing_index(tmp_path: Path) -> None:
    assert find_entities("anything", tmp_path) == []


def test_format_output(tmp_path: Path) -> None:
    _write_index(tmp_path, "- `People/Alba` (aliases: Alba)\n")
    hits = find_entities("hi Alba", tmp_path)
    rendered = format_entities(hits)
    assert "[[People/Alba]]" in rendered
    assert rendered.startswith("mentioned_entities:")


def test_refreshes_on_mtime_change(tmp_path: Path) -> None:
    import os
    _write_index(tmp_path, "- `People/Alba` (aliases: Alba)\n")
    first = find_entities("Alba here", tmp_path)
    assert len(first) == 1
    # Rewrite INDEX.md with a new entity, bump mtime.
    (tmp_path / "INDEX.md").write_text("- `People/Defne` (aliases: Defne)\n", encoding="utf-8")
    future = (tmp_path / "INDEX.md").stat().st_mtime + 2
    os.utime(tmp_path / "INDEX.md", (future, future))
    # get_index is process-cached; reuse clears when path changes. Same path here,
    # so rely on mtime-based refresh.
    second = find_entities("Alba here Defne", tmp_path)
    names = {e.name for e in second}
    assert names == {"Defne"}
