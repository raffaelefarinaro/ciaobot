"""The closed `type:` vocabulary and the generated VOCABULARY.md.

`type:` was free text, so every synonym the agent invented became a first-class
category: `doc (1)` rendered next to `document (1)` in INDEX.md, and a vault
grew 21 types where 16 were meant. Types are now a closed set enforced by
`vault_lint`; tags stay open and are only stratified by use, because closing a
382-value vocabulary would destroy what tags are for.
"""

from __future__ import annotations

from pathlib import Path

from ciao.vault_index import (
    CANONICAL_TYPES,
    DIR_TYPE_MAP,
    TYPE_ALIASES,
    canonical_type,
    format_vocabulary,
    main,
    scan_vault,
    vocabulary_report,
)
from ciao.vault_lint import _frontmatter_error, _VaultFile


def _note(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _lint(relative: str, content: str) -> dict[str, str] | None:
    return _frontmatter_error(
        _VaultFile(path=Path(relative), relative=Path(relative), content=content)
    )


# ---- the vocabulary itself --------------------------------------------------


def test_path_inference_can_never_produce_a_rejected_type() -> None:
    """`_infer_type` types the frontmatter-less files, so its whole range must
    be canonical — otherwise the linter would reject notes nobody mistyped."""
    assert set(DIR_TYPE_MAP.values()) <= CANONICAL_TYPES


def test_every_alias_target_is_canonical() -> None:
    """No alias chains: one rename always lands on a final value."""
    for source, target in TYPE_ALIASES.items():
        assert target in CANONICAL_TYPES, f"{source} -> {target}"
        assert target not in TYPE_ALIASES, f"{source} -> {target} is itself aliased"


def test_no_alias_shadows_a_canonical_type() -> None:
    assert not (set(TYPE_ALIASES) & CANONICAL_TYPES)


def test_canonical_type_maps_canonical_alias_and_unknown() -> None:
    assert canonical_type("project") == "project"
    assert canonical_type("  project  ") == "project"
    assert canonical_type("doc") == "document"
    assert canonical_type("frobnicate") == ""
    assert canonical_type("") == ""
    assert canonical_type(None) == ""  # type: ignore[arg-type]


# ---- enforcement -----------------------------------------------------------


def test_canonical_type_passes_the_linter() -> None:
    assert _lint("personal/People/Alba.md", "---\ntype: person\n---\n# Alba\n") is None


def test_aliased_type_is_reported_with_its_target() -> None:
    error = _lint("work/x.md", "---\ntype: doc\n---\n# X\n")
    assert error is not None
    assert error["kind"] == "unknown_type"
    # The target is named so the hygiene routine can apply it as a safe fix.
    assert "document" in error["message"]


def test_unknown_type_with_no_alias_is_reported_without_a_suggestion() -> None:
    error = _lint("work/y.md", "---\ntype: frobnicate\n---\n# Y\n")
    assert error is not None
    assert error["kind"] == "unknown_type"
    assert "use '" not in error["message"]


def test_missing_type_still_reports_missing_not_unknown() -> None:
    error = _lint("work/z.md", "---\ntitle: Z\n---\n# Z\n")
    assert error is not None
    assert error["kind"] == "missing_type"


def test_reserved_filenames_stay_exempt_from_the_vocabulary() -> None:
    """index.md / memory.md / log.md carry no frontmatter by design (and are
    OKF's reserved names); the new check must not start flagging them."""
    for name in ("index.md", "memory.md", "log.md", "INDEX.md"):
        assert _lint(f"personal/{name}", "# heading only\n") is None


# ---- the report ------------------------------------------------------------


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    _note(vault, "personal/People/Alba.md", "---\ntype: person\ntags: [family]\n---\n# Alba\n")
    _note(vault, "personal/notes/a.md", "---\ntype: doc\ntags: [family, once]\n---\n# A\n")
    _note(vault, "work/People/Aymen.md", "---\ntype: person\ntags: [customer]\n---\n# Aymen\n")
    _note(vault, "work/x.md", "---\ntype: frobnicate\n---\n# X\n")
    return vault


def test_report_separates_canonical_counts_from_drift(tmp_path: Path) -> None:
    report = vocabulary_report(scan_vault(_vault(tmp_path)))

    assert report["types"]["person"] == 2
    assert "doc" not in report["types"]

    assert report["type_drift"]["doc"]["suggested"] == "document"
    # Paths keep the vault-dir prefix `Entry.path` carries, so a reported path
    # is one the user can open directly from the repo root.
    assert report["type_drift"]["doc"]["paths"] == ["memory-vault/personal/notes/a.md"]
    # No canonical equivalent: reported, but with nothing to apply.
    assert report["type_drift"]["frobnicate"]["suggested"] == ""


def test_report_records_which_workspaces_use_a_tag(tmp_path: Path) -> None:
    report = vocabulary_report(scan_vault(_vault(tmp_path)))

    assert report["tags"]["family"] == 2
    assert report["tag_workspaces"]["family"] == ["personal"]
    assert report["tag_workspaces"]["customer"] == ["work"]


def test_vocabulary_stratifies_tags_and_never_rejects_one(tmp_path: Path) -> None:
    body = format_vocabulary(scan_vault(_vault(tmp_path)))

    assert "## Types (canonical" in body
    assert "## Types (drift" in body
    assert "`doc` → `document`" in body
    # A one-off tag is surfaced as a candidate, not an error.
    assert "Tags (candidates)" in body
    assert "`once`" in body


def test_vocabulary_is_byte_deterministic(tmp_path: Path) -> None:
    """It carries no timestamp on purpose: the memory agent reads it before
    writing frontmatter, and a timestamp would dirty git on every rebuild."""
    entries = scan_vault(_vault(tmp_path))
    assert format_vocabulary(entries) == format_vocabulary(entries)


def test_write_emits_both_index_and_vocabulary(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    assert main(["--write", "--vault-root", str(vault)]) == 0

    assert (vault / "INDEX.md").is_file()
    vocabulary = (vault / "VOCABULARY.md").read_text(encoding="utf-8")
    assert "do not edit by hand" in vocabulary
    assert "namespace/value" in vocabulary


# ---- generated files are not notes -----------------------------------------


def test_generated_files_are_never_indexed_or_linted(tmp_path: Path) -> None:
    """VOCABULARY.md is generated *about* the vault, so it must be excluded
    everywhere INDEX.md is. Missing one place made it an ordinary indexed note
    (a god-node in the Memory Map) plus a permanent `missing_frontmatter`
    finding, which would have made `os-audit` exit 1 forever.

    Casefolded because OKF spells the reserved names lowercase, so an imported
    bundle or an agent-written folder index produces `index.md`, not `INDEX.md`.
    """
    from ciao.vault_lint import run_validation

    vault = tmp_path / "memory-vault"
    _note(vault, "personal/a.md", "---\ntype: person\n---\n# A\n")
    _note(vault, "personal/index.md", "# Folder index\n")
    _note(vault, "personal/MEMORY.md", "# Curated\n")

    assert main(["--write", "--vault-root", str(vault)]) == 0

    indexed = {Path(entry.path).name for entry in scan_vault(vault)}
    assert indexed == {"a.md"}
    assert run_validation(vault)["frontmatter_errors"] == []


def test_log_md_is_still_content(tmp_path: Path) -> None:
    """`log.md` is a note (a project's chronological history), not a generated
    file — exempt from frontmatter, but it belongs in the index."""
    from ciao.vault_index import is_generated_vault_file

    assert not is_generated_vault_file("log.md")
    for name in ("INDEX.md", "index.md", "VOCABULARY.md", "vocabulary.md", "MEMORY.md"):
        assert is_generated_vault_file(name), name


# ---- root resolution -------------------------------------------------------


def test_a_relative_vault_root_resolves_against_the_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    """The bundled engine's launcher `cd`s into `Ciaobot.app/.../ciao-runtime`
    before exec'ing Python, so resolving a relative `CIAO_VAULT_ROOT` against the
    cwd pointed inside the app bundle. Every vault command run from a routine —
    whose prompts deliberately pass no `--vault-root` — died with a
    FileNotFoundError under the runtime directory, while the same command worked
    by hand from the workspace. Mirrors the fix already made for `.runtime`.
    """
    from ciao.cli import _resolve_vault_root
    from ciao.vault_index import default_vault_root

    workspace = tmp_path / "workspace"
    (workspace / "memory-vault").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    monkeypatch.chdir(elsewhere)

    expected = (workspace / "memory-vault").resolve()
    assert default_vault_root() == expected
    assert _resolve_vault_root() == expected
    # A relative --vault-root gets the same treatment, for the same reason.
    assert _resolve_vault_root("memory-vault") == expected


def test_an_absolute_vault_root_is_still_honoured(tmp_path: Path, monkeypatch) -> None:
    from ciao.cli import _resolve_vault_root

    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path / "workspace"))
    target = tmp_path / "somewhere" / "vault"
    target.mkdir(parents=True)

    assert _resolve_vault_root(target) == target.resolve()
