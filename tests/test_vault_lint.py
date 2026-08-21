import argparse
from collections import Counter
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ciao import cli, vault_lint

def test_cli_help():
    res = subprocess.run([sys.executable, "scripts/vault-lint.py", "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "Vault hygiene linter" in res.stdout

@pytest.fixture
def temp_vault(tmp_path):
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    people = vault / "People"
    people.mkdir()
    (people / "Alice.md").write_text("Hello [Bob](./Bob.md)", encoding="utf-8")
    return vault


def _page(body: str = "", *, page_type: object = "note") -> str:
    rendered_type = yaml.safe_dump(
        {"type": page_type},
        sort_keys=False,
    ).strip()
    return f"---\n{rendered_type}\n---\n{body}"


def _single_page_vault(tmp_path: Path, body: str) -> Path:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text(_page(body), encoding="utf-8")
    return vault


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        ("# No frontmatter\n", "missing_frontmatter"),
        ("---\ntype: [\n---\n", "malformed_frontmatter"),
        ("---\n- note\n---\n", "malformed_frontmatter"),
        ("---\ntitle: Missing type\n---\n", "missing_type"),
        ("---\ntype: '   '\n---\n", "missing_type"),
        ("---\ntype: null\n---\n", "invalid_type"),
        ("---\ntype: 42\n---\n", "invalid_type"),
    ],
)
def test_frontmatter_validation_reports_one_error_per_page(
    tmp_path: Path,
    content: str,
    kind: str,
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text(content, encoding="utf-8")

    issues = vault_lint.run_validation(vault)

    assert issues["frontmatter_errors"] == [
        {
            "source": "Page.md",
            "kind": kind,
            "message": {
                "missing_frontmatter": "frontmatter is missing",
                "malformed_frontmatter": "frontmatter is malformed",
                "missing_type": "frontmatter type is missing or empty",
                "invalid_type": "frontmatter type must be a string",
            }[kind],
        }
    ]


def test_valid_frontmatter_has_no_error(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text(_page("# Page\n"), encoding="utf-8")

    assert vault_lint.run_validation(vault)["frontmatter_errors"] == []


def test_reserved_frontmatter_exemptions_are_case_insensitive(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    for name in ("INDEX.md", "memory.md", "LoG.Md"):
        (vault / name).write_text("No frontmatter\n", encoding="utf-8")

    assert vault_lint.run_validation(vault)["frontmatter_errors"] == []


def test_readme_still_requires_frontmatter(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "README.md").write_text("# Project\n", encoding="utf-8")

    errors = vault_lint.run_validation(vault)["frontmatter_errors"]
    assert [(item["source"], item["kind"]) for item in errors] == [
        ("README.md", "missing_frontmatter")
    ]


def test_relative_markdown_links_images_and_references(tmp_path: Path) -> None:
    vault = _single_page_vault(
        tmp_path,
        "[ok](notes/ok.md)\n"
        "![missing image](images/missing.png)\n"
        "[ref]: notes/missing.md\n",
    )
    (vault / "notes").mkdir()
    (vault / "notes" / "ok.md").write_text(_page("ok"), encoding="utf-8")

    broken = vault_lint.run_validation(vault)["broken_markdown_links"]

    assert [(item["target"], item["kind"]) for item in broken] == [
        ("images/missing.png", "missing_target"),
        ("notes/missing.md", "missing_target"),
    ]


def test_markdown_link_normalization(tmp_path: Path) -> None:
    vault = _single_page_vault(
        tmp_path,
        '[title](notes/ok.md "Optional title")\n'
        "[query](notes/ok.md?raw=1#section)\n"
        "[encoded](notes/space%20name.md)\n",
    )
    notes = vault / "notes"
    notes.mkdir()
    (notes / "ok.md").write_text(_page(), encoding="utf-8")
    (notes / "space name.md").write_text(_page(), encoding="utf-8")

    assert vault_lint.run_validation(vault)["broken_markdown_links"] == []


def test_markdown_link_ignores_non_local_and_documented_examples(
    tmp_path: Path,
) -> None:
    vault = _single_page_vault(
        tmp_path,
        "[web](https://example.com/a)\n"
        "[mail](mailto:test@example.com)\n"
        "[protocol relative](//example.com/a)\n"
        "[root](/docs/a.md)\n"
        "[anchor](#status)\n"
        "[empty]()\n"
        "[placeholder](<path/<name>.md>)\n"
        "`[inline](missing-inline.md)`\n"
        "```md\n[fenced](missing-fenced.md)\n```\n",
    )

    assert vault_lint.run_validation(vault)["broken_markdown_links"] == []


def test_escaped_markdown_link_is_ignored(tmp_path: Path) -> None:
    vault = _single_page_vault(tmp_path, r"\[example](missing.md)")
    assert vault_lint.run_validation(vault)["broken_markdown_links"] == []


def test_reserved_file_markdown_links_are_still_validated(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "INDEX.md").write_text("[missing](missing.md)\n", encoding="utf-8")

    broken = vault_lint.run_validation(vault)["broken_markdown_links"]
    assert [(item["source"], item["target"]) for item in broken] == [
        ("INDEX.md", "missing.md")
    ]


def test_markdown_link_outside_vault_does_not_probe_target(tmp_path: Path) -> None:
    vault = _single_page_vault(tmp_path, "[outside](../private.md)\n")
    (tmp_path / "private.md").write_text("secret", encoding="utf-8")

    broken = vault_lint.run_validation(vault)["broken_markdown_links"]

    assert broken == [
        {
            "source": "Page.md",
            "target": "../private.md",
            "resolved": "../private.md",
            "kind": "outside_vault",
        }
    ]


def test_excluded_sources_are_skipped_but_targets_remain_valid(
    tmp_path: Path,
) -> None:
    vault = _single_page_vault(tmp_path, "[log](Logs/existing.md)\n")
    logs = vault / "Logs"
    logs.mkdir()
    (logs / "existing.md").write_text("target\n", encoding="utf-8")
    templates = vault / "Templates"
    templates.mkdir()
    (templates / "source.md").write_text(
        "[leak](missing-from-excluded-source.md)\n",
        encoding="utf-8",
    )

    issues = vault_lint.run_validation(vault)

    assert issues["broken_markdown_links"] == []
    assert not any(
        item["source"].startswith(("Logs/", "Templates/"))
        for item in issues["frontmatter_errors"]
    )


def test_source_symlink_outside_vault_is_not_read(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    external_note = external / "note.md"
    external_note.write_text("[leak](missing-external-target.md)\n", encoding="utf-8")
    (vault / "linked.MD").symlink_to(external_note)

    issues = vault_lint.run_validation(vault)

    assert not any(
        item["source"] == "linked.MD"
        for item in issues["frontmatter_errors"] + issues["broken_markdown_links"]
    )


def test_outside_target_presence_does_not_change_sanitized_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _single_page_vault(tmp_path, "[outside](../private.md)\n")
    outside = tmp_path / "private.md"
    original_resolve = Path.resolve

    def reject_outside_probe(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> Path:
        if Path(os.path.normpath(path.as_posix())) == outside:
            raise AssertionError("outside target was resolved")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", reject_outside_probe)
    absent = vault_lint.run_validation(vault)["broken_markdown_links"]
    outside.write_text("private\n", encoding="utf-8")
    present = vault_lint.run_validation(vault)["broken_markdown_links"]

    expected = [
        {
            "source": "Page.md",
            "target": "../private.md",
            "resolved": "../private.md",
            "kind": "outside_vault",
        }
    ]
    assert absent == expected
    assert present == expected


def test_external_schemes_are_ignored_even_when_malformed(tmp_path: Path) -> None:
    vault = _single_page_vault(
        tmp_path,
        "[http](http://[)\n"
        "[custom](custom+scheme://[)\n",
    )

    assert vault_lint.run_validation(vault)["broken_markdown_links"] == []


def test_balanced_destinations_and_nested_labels_are_parsed(tmp_path: Path) -> None:
    vault = _single_page_vault(
        tmp_path,
        "[balanced](docs/a(b).md)\n"
        "[outer [nested label]](missing-nested-label.md)\n",
    )
    docs = vault / "docs"
    docs.mkdir()
    (docs / "a(b).md").write_text(_page(), encoding="utf-8")

    broken = vault_lint.run_validation(vault)["broken_markdown_links"]

    assert [(item["target"], item["kind"]) for item in broken] == [
        ("missing-nested-label.md", "missing_target")
    ]


def test_nested_markdown_targets_and_directories_resolve(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    source = vault / "section" / "Page.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        _page(
            "[child](./child.md)\n"
            "[asset](../assets/file.md)\n"
            "[directory](../assets)\n"
        ),
        encoding="utf-8",
    )
    (source.parent / "child.md").write_text(_page(), encoding="utf-8")
    assets = vault / "assets"
    assets.mkdir()
    (assets / "file.md").write_text(_page(), encoding="utf-8")

    assert vault_lint.run_validation(vault)["broken_markdown_links"] == []


def test_markdown_findings_are_deterministically_ordered(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "b.md").write_text(
        _page("![image](missing-b.png)\n"),
        encoding="utf-8",
    )
    (vault / "a.md").write_text(
        _page("[inline](missing-a.md)\n[ref]: missing-ref-a.md\n"),
        encoding="utf-8",
    )

    broken = vault_lint.run_validation(vault)["broken_markdown_links"]

    assert [(item["source"], item["target"]) for item in broken] == [
        ("a.md", "missing-a.md"),
        ("a.md", "missing-ref-a.md"),
        ("b.md", "missing-b.png"),
    ]


def test_run_validation_reads_each_included_markdown_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text(_page(), encoding="utf-8")
    nested = vault / "nested"
    nested.mkdir()
    (nested / "Child.MD").write_text(_page(), encoding="utf-8")
    logs = vault / "Logs"
    logs.mkdir()
    (logs / "excluded.md").write_text(_page(), encoding="utf-8")
    original_read_text = Path.read_text
    reads: list[Path] = []

    def counted_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.is_relative_to(vault):
            reads.append(path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read_text)
    vault_lint.run_validation(vault)

    assert Counter(path.relative_to(vault) for path in reads) == Counter({
        Path("Page.md"): 1,
        Path("nested/Child.MD"): 1,
    })


def test_memory_links_survive_post_walk_root_iterdir_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    workspace = vault / "alternate"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text(
        "[Alice](../People/Alice.md)\n", encoding="utf-8"
    )
    people = vault / "People"
    people.mkdir()
    (people / "Alice.md").write_text(_page(), encoding="utf-8")
    original_iterdir = Path.iterdir

    def fail_root_iterdir(path: Path):
        if path == vault:
            raise OSError("workspace root listing unavailable")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_root_iterdir)

    issues = vault_lint.run_validation(vault)

    assert issues["orphans"] == []


@pytest.mark.parametrize(
    ("target", "decoded"),
    [
        ("%43%3A%5Cprivate%5Cfile.md", r"C:\private\file.md"),
        ("%5C%5Cserver%5Cshare%5Cfile.md", r"\\server\share\file.md"),
    ],
)
def test_percent_decoded_windows_absolute_targets_are_ignored_without_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    decoded: str,
) -> None:
    vault = _single_page_vault(tmp_path, f"[windows]({target})\n")
    original_resolve = Path.resolve

    def reject_windows_probe(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> Path:
        if decoded in path.as_posix():
            raise AssertionError("decoded Windows target was resolved")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", reject_windows_probe)

    assert vault_lint.run_validation(vault)["broken_markdown_links"] == []


def test_run_validation_raises_on_full_traversal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()

    def fail_walk(*args: object, **kwargs: object):
        raise OSError("permission denied")

    monkeypatch.setattr(vault_lint.os, "walk", fail_walk)

    with pytest.raises(vault_lint.VaultTraversalError, match="permission denied"):
        vault_lint.run_validation(vault)


def test_run_validation_raises_on_partial_traversal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()

    def partial_walk(
        root: Path,
        *,
        topdown: bool,
        onerror,
        followlinks: bool,
    ):
        yield str(root), [], []
        onerror(OSError("cannot read subtree"))

    monkeypatch.setattr(vault_lint.os, "walk", partial_walk)

    with pytest.raises(vault_lint.VaultTraversalError, match="cannot read subtree"):
        vault_lint.run_validation(vault)


def test_percent_decoded_non_local_targets_are_ignored_without_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _single_page_vault(
        tmp_path,
        "[absolute](%2Fetc/passwd)\n"
        "[protocol](%2F%2Fexample.com/path)\n"
        "[anchor](%23status)\n",
    )
    original_resolve = Path.resolve
    rejected = {"/etc/passwd", "//example.com/path"}

    def reject_non_local_probe(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> Path:
        if os.path.normpath(path.as_posix()) in rejected:
            raise AssertionError("decoded non-local target was resolved")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", reject_non_local_probe)

    assert vault_lint.run_validation(vault)["broken_markdown_links"] == []


def test_malformed_decoded_local_target_is_reported_without_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _single_page_vault(tmp_path, "[bad](bad%00name.md)\n")
    original_resolve = Path.resolve

    def reject_malformed_probe(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> Path:
        if "\x00" in path.as_posix():
            raise AssertionError("malformed local target was resolved")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", reject_malformed_probe)

    assert vault_lint.run_validation(vault)["broken_markdown_links"] == [
        {
            "source": "Page.md",
            "target": "bad%00name.md",
            "resolved": "bad%00name.md",
            "kind": "missing_target",
        }
    ]


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_vault_lint_cli_rejects_missing_or_non_directory_root(
    tmp_path: Path,
    root_kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "missing-vault"
    if root_kind == "file":
        root.write_text("not a vault\n", encoding="utf-8")

    result = cli._vault_lint_command(argparse.Namespace(vault_root=root))

    captured = capsys.readouterr()
    assert result == 1
    assert "Vault root" in captured.err
    assert "Vault is clean!" not in captured.out


def test_vault_lint_cli_reports_traversal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()

    def fail_walk(*args: object, **kwargs: object):
        raise OSError("cannot inspect vault")

    monkeypatch.setattr(vault_lint.os, "walk", fail_walk)

    result = cli._vault_lint_command(argparse.Namespace(vault_root=vault))

    captured = capsys.readouterr()
    assert result == 1
    assert "Vault inspection failed" in captured.err
    assert "cannot inspect vault" in captured.err
    assert "Vault is clean!" not in captured.out


def test_vault_lint_cli_reports_frontmatter_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text("# Missing metadata\n", encoding="utf-8")

    result = cli._vault_lint_command(argparse.Namespace(vault_root=vault))
    output = capsys.readouterr().out

    assert result == 1
    assert "### Frontmatter Errors" in output
    assert "### Broken Markdown Links" not in output


def test_vault_lint_cli_reports_markdown_links_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = _single_page_vault(tmp_path, "[missing](missing.md)\n")

    result = cli._vault_lint_command(argparse.Namespace(vault_root=vault))
    output = capsys.readouterr().out

    assert result == 1
    assert "### Frontmatter Errors" not in output
    assert "### Broken Markdown Links" in output


def test_vault_lint_cli_reports_new_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text("[missing](missing.md)\n", encoding="utf-8")

    result = cli._vault_lint_command(argparse.Namespace(vault_root=vault))

    output = capsys.readouterr().out
    assert result == 1
    assert "### Frontmatter Errors" in output
    assert "Page.md" in output
    assert "missing_frontmatter" in output
    assert "### Broken Markdown Links" in output
    assert "missing.md" in output
    assert "missing_target" in output


def test_vault_lint_cli_clean_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text(_page(), encoding="utf-8")

    result = cli._vault_lint_command(argparse.Namespace(vault_root=vault))

    assert result == 0
    assert capsys.readouterr().out.strip() == "Vault is clean!"

def test_dead_link_lands_in_broken_markdown_links(temp_vault):
    """A dead link is reported once, in `broken_markdown_links`.

    The separate `broken_links` bucket existed only for wikilinks; with one
    dialect, two buckets for one condition could only disagree.
    """
    issues = vault_lint.run_validation(temp_vault)
    assert "broken_links" not in issues
    assert len(issues["broken_markdown_links"]) == 1
    finding = issues["broken_markdown_links"][0]
    assert finding["source"] == "People/Alice.md"
    assert finding["resolved"] == "People/Bob.md"
    assert finding["kind"] == "missing_target"

def test_orphan_detection(temp_vault):
    people = temp_vault / "People"
    (people / "Bob.md").write_text("Profile of Bob", encoding="utf-8")
    (people / "Charlie.md").write_text("Hello", encoding="utf-8")
    
    issues = vault_lint.run_validation(temp_vault)
    assert "People/Charlie.md" in issues["orphans"]
    assert "People/Bob.md" not in issues["orphans"]

def test_duplicate_detection(temp_vault):
    people = temp_vault / "People"
    (people / "Alice-Smith.md").write_text("Alice Smith", encoding="utf-8")
    (people / "AliceSmith.md").write_text("Alice Smith duplicate", encoding="utf-8")
    
    issues = vault_lint.run_validation(temp_vault)
    assert len(issues["duplicates"]) == 1
    assert "People/Alice-Smith.md" in issues["duplicates"][0]
    assert "People/AliceSmith.md" in issues["duplicates"][0]


def test_ignores_links_in_code_and_escaped(temp_vault):
    """Link syntax inside code spans/fences, backslash-escaped, or holding a
    `<placeholder>` is documentation, not a real link (issue #129)."""
    (temp_vault / "People" / "Guide.md").write_text(
        "Use `[x](./Nonexistent.md)` in prose.\n\n"
        "```\n[x](./AlsoNonexistent.md)\n```\n\n"
        "Escaped: \\[x](./EscapedTarget.md)\n"
        "Placeholder: [x](./projects/active/<folder>/<folder>.md)\n",
        encoding="utf-8",
    )
    issues = vault_lint.run_validation(temp_vault)
    bad = {b["target"] for b in issues["broken_markdown_links"]}
    assert not any(
        name in target
        for target in bad
        for name in ("Nonexistent", "EscapedTarget", "<folder>")
    ), bad


def test_common_stems_not_flagged_as_duplicates(temp_vault):
    """One README/log per project is normal, not a duplicate page (#129)."""
    projects = temp_vault / "projects"
    (projects / "a").mkdir(parents=True)
    (projects / "b").mkdir(parents=True)
    (projects / "a" / "README.md").write_text("A", encoding="utf-8")
    (projects / "b" / "README.md").write_text("B", encoding="utf-8")
    issues = vault_lint.run_validation(temp_vault)
    assert all("README.md" not in "".join(dup) for dup in issues["duplicates"])


def test_excludes_venv_and_tool_dirs(temp_vault):
    """A venv/node_modules checked out in the vault must not be scanned (#129)."""
    venv = temp_vault / "work" / "automations" / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "doc.md").write_text("[x](./NopeTarget.md)", encoding="utf-8")
    issues = vault_lint.run_validation(temp_vault)
    assert not any(
        "NopeTarget" in b["target"] for b in issues["broken_markdown_links"]
    )


def test_incoming_links_resolve_against_the_linking_note(tmp_path: Path) -> None:
    """An incoming link is credited by *location*, not by filename stem.

    `../People/Bob.md` from `Projects/` is a link to `People/Bob`; the identical
    `./Bob.md` from `Projects/` is not. Resolving a markdown destination without
    the linking note's directory would credit both, hiding a real orphan.
    """
    vault = tmp_path / "memory-vault"
    (vault / "People").mkdir(parents=True)
    (vault / "Projects").mkdir(parents=True)
    (vault / "People" / "Bob.md").write_text(_page(), encoding="utf-8")
    (vault / "People" / "Carol.md").write_text(_page(), encoding="utf-8")
    (vault / "Projects" / "Apollo.md").write_text(
        _page("Worked with [Bob](../People/Bob.md); also [Carol](./Carol.md).\n"),
        encoding="utf-8",
    )

    issues = vault_lint.run_validation(vault)

    assert "People/Bob.md" not in issues["orphans"]
    assert "People/Carol.md" in issues["orphans"]
    # The miss is still reported as a link fault, in the one bucket there is.
    assert [item["resolved"] for item in issues["broken_markdown_links"]] == [
        "Projects/Carol.md"
    ]


def test_leftover_wikilink_is_neither_a_link_nor_a_finding(tmp_path: Path) -> None:
    """A pre-migration vault must lint cleanly rather than crash or flag.

    Markdown links are the only dialect, so `[[Bob]]` is body text: it grants no
    incoming link and produces no broken-link finding of its own.
    """
    vault = tmp_path / "memory-vault"
    (vault / "People").mkdir(parents=True)
    (vault / "People" / "Bob.md").write_text(_page(), encoding="utf-8")
    (vault / "People" / "Alice.md").write_text(
        _page("Hello [[Bob]] and [[Nowhere]].\n"), encoding="utf-8"
    )

    issues = vault_lint.run_validation(vault)

    assert issues["broken_markdown_links"] == []
    assert "People/Bob.md" in issues["orphans"]


def test_a_resolving_cross_root_link_is_not_a_broken_link(tmp_path: Path) -> None:
    """After the re-rooting, linking another workspace MUST leave the vault.

    Judged lexically against one vault, every cross-root link came back
    `outside_vault`, so a correctly migrated install carried a standing count of
    "broken" links that all resolved — 20 of them on the first real migration.
    """
    for name in ("personal", "work"):
        (tmp_path / name / "memory-vault" / "People").mkdir(parents=True)
    (tmp_path / "work" / "memory-vault" / "People" / "Colleague.md").write_text(
        "---\ntype: person\n---\n# Colleague\n", encoding="utf-8"
    )
    source = tmp_path / "personal" / "memory-vault" / "People" / "User.md"
    source.write_text(
        "---\ntype: person\n---\n"
        "# User\n"
        "- [Colleague](../../../work/memory-vault/People/Colleague.md)\n"
        "- [Ghost](../../../work/memory-vault/People/Ghost.md)\n"
        "- [Escapee](../../../../outside-install/Secret.md)\n",
        encoding="utf-8",
    )
    # A real file OUTSIDE the install. Existence must not rescue it: leaving the
    # install is the thing being rejected, and a lexical-containment check that
    # answered "yes" to everything would otherwise pass this test unnoticed.
    outside = tmp_path.parent / "outside-install"
    outside.mkdir(exist_ok=True)
    (outside / "Secret.md").write_text("private\n", encoding="utf-8")
    vault = tmp_path / "personal" / "memory-vault"

    without = vault_lint.run_validation(vault)["broken_markdown_links"]
    with_install = vault_lint.run_validation(vault, install_root=tmp_path)[
        "broken_markdown_links"
    ]

    # Without the install root nothing can be judged, so all three are flagged.
    assert len(without) == 3
    flagged = {item["target"] for item in with_install}
    assert "../../../work/memory-vault/People/Colleague.md" not in flagged
    assert "../../../work/memory-vault/People/Ghost.md" in flagged
    assert "../../../../outside-install/Secret.md" in flagged
