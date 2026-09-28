"""Tests for the vault entity tagger against a synthetic INDEX.md."""

from __future__ import annotations

from pathlib import Path

from ciao import entity_types
from ciao.context import entity_tagger
from ciao.context.entity_tagger import (
    VaultEntity,
    context_entities,
    find_entities,
    format_entities,
    get_index,
)


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
- [personal/People/Nora](./personal/People/Nora.md) (tags: person; aliases: Nora)
""")

    hits = find_entities("Apollo update with Alba and Nora", tmp_path, workspace="work")
    paths = {e.path for e in hits}
    assert "work/Projects/Apollo" in paths
    assert "personal/Projects/Apollo" not in paths
    assert "personal/People/Nora" not in paths
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


def test_automation_working_files_are_not_matchable(tmp_path: Path) -> None:
    # An automation is its folder's README. Its run data (`raw/*/report`,
    # `PROMPT`) matched every message that said "report", once per file.
    _write_index(tmp_path, """# Vault Index

- [automations/adoption-report/README](./automations/adoption-report/README.md) (tags: automation)
- [automations/adoption-report/PROMPT](./automations/adoption-report/PROMPT.md)
- [automations/adoption-report/raw/sparkscan/report](./automations/adoption-report/raw/sparkscan/report.md)
- [automations/adoption-report/report_text_sparkscan](./automations/adoption-report/report_text_sparkscan.md)
""")
    hits = find_entities(
        "run the adoption-report prompt and send the report",
        tmp_path, workspace="work", index_owns_workspace=True,
    )
    assert [e.path for e in hits] == ["automations/adoption-report/README"]


def test_a_file_name_several_notes_share_is_not_a_name(tmp_path: Path) -> None:
    # `slides` in two project folders is a naming convention; a name a folder
    # note also carries is one entity under two paths and still matches.
    _write_index(tmp_path, """# Vault Index

- [projects/active/maf-pilot/slides](./projects/active/maf-pilot/slides.md)
- [projects/completed/ahm/slides](./projects/completed/ahm/slides.md)
- [projects/active/general/README](./projects/active/general/README.md)
- [projects/active/general/general](./projects/active/general/general.md)
""")
    hits = find_entities(
        "fix the slides for general", tmp_path, workspace="work", index_owns_workspace=True,
    )
    assert sorted(e.path for e in hits) == [
        "projects/active/general/README", "projects/active/general/general",
    ]


def test_the_same_name_in_two_workspaces_of_a_shared_index_still_matches(tmp_path: Path) -> None:
    _write_index(tmp_path, """# Vault Index

- [work/People/Alex](./work/People/Alex.md) (tags: person)
- [personal/People/Alex](./personal/People/Alex.md) (tags: person)
""")
    hits = find_entities("ask Alex", tmp_path, workspace="work")
    assert [e.path for e in hits] == ["work/People/Alex"]


def test_automation_run_files_do_not_cancel_a_real_note_of_the_same_name(
    tmp_path: Path,
) -> None:
    # Run data is often named after what it processed; skipped files must not
    # count toward the shared-name rule and knock the real note out.
    _write_index(tmp_path, """# Vault Index

- [work/Clients/acme](./work/Clients/acme.md)
- [work/automations/crm-sync/raw/acme](./work/automations/crm-sync/raw/acme.md)
""")
    hits = find_entities("call acme", tmp_path, workspace="work")
    assert [e.path for e in hits] == ["work/Clients/acme"]


def test_a_project_called_automations_is_still_matchable(tmp_path: Path) -> None:
    _write_index(tmp_path, """# Vault Index

- [projects/active/automations/README](./projects/active/automations/README.md)
- [projects/active/automations/roadmap](./projects/active/automations/roadmap.md)
""")
    hits = find_entities(
        "update the automations roadmap", tmp_path, workspace="work", index_owns_workspace=True,
    )
    assert sorted(e.path for e in hits) == [
        "projects/active/automations/README", "projects/active/automations/roadmap",
    ]


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
    (tmp_path / "INDEX.md").write_text("- [People/Nora](./People/Nora.md) (aliases: Nora)\n", encoding="utf-8")
    future = (tmp_path / "INDEX.md").stat().st_mtime + 2
    os.utime(tmp_path / "INDEX.md", (future, future))
    # get_index is process-cached; reuse clears when path changes. Same path here,
    # so rely on mtime-based refresh.
    second = find_entities("Alba here Nora", tmp_path)
    names = {e.name for e in second}
    assert names == {"Nora"}


class _RelabelledCategoryParts:
    """A registry stand-in whose folder view the real registry never builds.

    A real ``category_parts()`` maps a folder to *its own* name in both cases,
    which is why threading the registry through this module changes no behaviour:
    the fixed set is what the indexer writes, and a folder outside it already
    fell back to its own name. This stand-in maps one somewhere else, which no
    real registry does, so the test can tell the two apart — read the module
    constant instead of the argument and the category comes back as the folder.
    """

    def category_parts(self) -> dict[str, str]:
        return {"Companies": "Clients", "companies": "Clients"}


def test_get_index_reads_the_vault_registry_and_leaves_the_wire_set_fixed(
    tmp_path: Path,
) -> None:
    """The registry is the seam; the set of folders it maps is not widened.

    ``get_index`` loads the vault's category list from the root it was given, so
    the folder -> category view comes from one place rather than from a copy of it
    in this module. What that view may contain is unchanged, and deliberately: the
    tagger maps ``INDEX.md`` bullets, and the folders in one are the ones the
    indexer wrote, so a category the owner adds must not silently change what a
    bullet resolves to. Pinned from both sides — the custom folder is absent from
    the view, and the note in it still resolves as it did before the file existed.
    """
    _write_index(
        tmp_path,
        "# Vault Index\n\n"
        "- [People/Alba](./People/Alba.md) (tags: person; aliases: Alba)\n"
        "- [Customers/Acme](./Customers/Acme.md) (tags: customer; aliases: Acme)\n",
    )
    (tmp_path / "entity-types.yaml").write_text(
        "- id: customer\n  label: Customer\n  kind: entity\n  folder: Customers\n",
        encoding="utf-8",
    )

    registry = entity_types.load_entity_types(tmp_path)
    assert registry.dir_type_map()["Customers"] == "customer", "the vault did add it"
    assert registry.category_parts() == entity_tagger._CATEGORY_PARTS
    assert "Customers" not in registry.category_parts()

    hits = {e.name: e for e in get_index(tmp_path).find("Alba and Acme")}
    assert hits["Alba"].category == "People"
    assert hits["Acme"].category == "Customers", "its own folder name, as it was"


def test_get_index_resolves_bullets_against_the_view_it_is_handed(tmp_path: Path) -> None:
    """The view comes from the argument, so the one place this set can change is
    ``entity_types._CATEGORY_PART_FOLDERS`` — not a second list here.

    Two roots, because the index is cached by path: the first is handed a view
    that relabels the folder, the second reads the shipped one, and the same
    bullet resolves differently in each. That difference is unobservable in
    production (see the stand-in) and is the only way to see the plumbing.
    """
    bullet = "- [Companies/Acme](./Companies/Acme.md) (tags: client; aliases: Acme)\n"
    handed_in = tmp_path / "handed-in"
    shipped = tmp_path / "shipped"
    handed_in.mkdir()
    shipped.mkdir()
    _write_index(handed_in, bullet)
    _write_index(shipped, bullet)

    relabelled = get_index(handed_in, _RelabelledCategoryParts()).find("Acme")
    from_shipped = get_index(shipped).find("Acme")

    assert [e.category for e in relabelled] == ["Clients"]
    assert [e.category for e in from_shipped] == ["Companies"]


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


def test_context_entities_reads_back_what_format_entities_wrote() -> None:
    entities = [
        VaultEntity(name="Michael Stanton", category="People", path="work/People/Michael Stanton", aliases=()),
        VaultEntity(name="InfoSign", category="Companies", path="work/Companies/InfoSign", aliases=()),
    ]
    prompt = (
        "[CIAO_CONTEXT_BEGIN]\n"
        '[Chat ID: "c1"]\n'
        "today=2026-09-25\n"
        f"{format_entities(entities)}\n"
        "[CIAO_CONTEXT_END]\n\n"
        "Reply to Michael at InfoSign"
    )
    assert context_entities(prompt) == [
        {"name": "Michael Stanton", "path": "work/People/Michael Stanton.md", "category": "people"},
        {"name": "InfoSign", "path": "work/Companies/InfoSign.md", "category": "companie"},
    ]


def test_context_entities_is_empty_without_a_capsule_or_matches() -> None:
    assert context_entities("plain message") == []
    assert context_entities(
        "[CIAO_CONTEXT_BEGIN]\ntoday=2026-09-25\n[CIAO_CONTEXT_END]\n\nhi"
    ) == []
    # The hint grammar only counts inside the leading capsule.
    assert context_entities("mentioned_entities:\n- [A](./a.md) (person)") == []
