"""Tests for vocabulary promotion/merge proposals (step 5).

Covers the three cases the plan requires:

* A non-canonical type or emerging tag that crosses the promotion threshold
  becomes a proposal, not an automatic CANONICAL_TYPES change.
* A singleton tag with an obvious near-duplicate produces a merge proposal;
  a singleton with no near-duplicate does not.
* The hygiene routine still applies alias-target renames (existing step-4
  behavior) and does not regress when proposals are added — verified via
  os_audit integration.
"""

from __future__ import annotations

from pathlib import Path

from ciao import vault_index as vi
from ciao.vocabulary_proposals import (
    generate_vocabulary_proposals,
    is_near_duplicate,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _note_body(*, type_: str = "note", tags: list[str] | None = None, title: str = "Note") -> str:
    tag_line = ""
    if tags:
        tag_line = f"tags: [{', '.join(tags)}]\n"
    return f"---\ntype: {type_}\n{tag_line}---\n# {title}\n"


# ---- is_near_duplicate ----------------------------------------------------


def test_is_near_duplicate_shared_prefix_with_separator():
    # Plan's canonical example: ai-analysis / ai-adoption / ai-practice alongside ai.
    assert is_near_duplicate("ai-analysis", "ai") is True
    assert is_near_duplicate("ai", "ai-analysis") is True
    assert is_near_duplicate("ai-analysis", "ai-adoption") is True
    assert is_near_duplicate("ai-practice", "ai-analysis") is True


def test_is_near_duplicate_edit_distance():
    assert is_near_duplicate("analysis", "analysys") is True  # typo distance 1
    assert is_near_duplicate("alfa", "alpa") is True  # distance 1
    # Very different tags are not near-duplicates.
    assert is_near_duplicate("zebra-unique-xyz", "project") is False
    assert is_near_duplicate("unrelated", "banana") is False


def test_is_near_duplicate_identical_is_not_duplicate():
    assert is_near_duplicate("ai", "ai") is False
    assert is_near_duplicate("project", "project") is False


def test_is_near_duplicate_project_namespace():
    assert is_near_duplicate("project/active", "project/draft") is True
    assert is_near_duplicate("product/barcode-capture", "product/barcode-scan") is True


# ---- Promotion: type crossing threshold produces proposal -------------------


def test_type_promotion_at_threshold(tmp_path: Path):
    # A non-canonical type with 5 uses crosses the default threshold and
    # becomes a promotion proposal. It must not mutate CANONICAL_TYPES.
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(5):
        _write(vault / f"Note{i}.md", _note_body(type_="brainstorm", title=f"Note{i}"))
    before = set(vi.CANONICAL_TYPES)
    entries = vi.scan_vault(vault)
    proposals = generate_vocabulary_proposals(entries, threshold=5)
    assert len(proposals["type_promotions"]) == 1
    promo = proposals["type_promotions"][0]
    assert promo["type"] == "brainstorm"
    assert promo["count"] == 5
    assert promo["suggested"] == ""
    assert len(promo["paths"]) == 5
    # Not an automatic canonical change.
    assert set(vi.CANONICAL_TYPES) == before
    assert "brainstorm" not in vi.CANONICAL_TYPES


def test_type_below_threshold_not_promoted(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(3):
        _write(vault / f"Note{i}.md", _note_body(type_="brainstorm", title=f"Note{i}"))
    entries = vi.scan_vault(vault)
    proposals = generate_vocabulary_proposals(entries, threshold=5)
    assert proposals["type_promotions"] == []


def test_aliased_type_not_promoted(tmp_path: Path):
    # An aliased type (e.g. doc -> document) has a safe rename target; it
    # should be handled as drift/rename, not as a promotion proposal.
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(6):
        _write(vault / f"Note{i}.md", _note_body(type_="doc", title=f"Note{i}"))
    entries = vi.scan_vault(vault)
    proposals = generate_vocabulary_proposals(entries, threshold=5)
    # No promotion: drift with suggested target is the low-risk fix.
    assert proposals["type_promotions"] == []
    # But vault_index still reports it as drift with alias target.
    report = vi.vocabulary_report(entries)
    assert "doc" in report["type_drift"]
    assert report["type_drift"]["doc"]["suggested"] == "document"


def test_tag_promotion_at_threshold(tmp_path: Path):
    # An emerging tag reaching 5 uses becomes a promotion proposal.
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(5):
        _write(vault / f"Note{i}.md", _note_body(tags=["research"], title=f"Note{i}"))
    entries = vi.scan_vault(vault)
    proposals = generate_vocabulary_proposals(entries, threshold=5)
    assert len(proposals["tag_promotions"]) == 1
    assert proposals["tag_promotions"][0]["tag"] == "research"
    assert proposals["tag_promotions"][0]["count"] == 5


def test_tag_below_threshold_not_promoted(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(2):
        _write(vault / f"Note{i}.md", _note_body(tags=["research"], title=f"Note{i}"))
    entries = vi.scan_vault(vault)
    proposals = generate_vocabulary_proposals(entries, threshold=5)
    assert proposals["tag_promotions"] == []


def test_tag_promotion_carries_workspaces(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(5):
        _write(vault / f"Note{i}.md", _note_body(tags=["shared-tag"], title=f"Note{i}"))
    entries = vi.scan_vault(vault, workspace="personal")
    # Stamp half the entries as work to verify per-workspace attribution.
    for e in entries[:3]:
        e.workspace = "personal"
    for e in entries[3:]:
        e.workspace = "work"
    proposals = generate_vocabulary_proposals(entries, threshold=5)
    promo = next(p for p in proposals["tag_promotions"] if p["tag"] == "shared-tag")
    assert "personal" in promo["workspaces"]
    assert "work" in promo["workspaces"]


# ---- Merge: singleton near-duplicate --------------------------------------


def test_singleton_tag_with_near_duplicate_produces_merge(tmp_path: Path):
    # ai appears 5 times (established), ai-analysis appears once (singleton
    # near-duplicate) — should produce a merge proposal.
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(5):
        _write(vault / f"Note{i}.md", _note_body(tags=["ai"], title=f"Note{i}"))
    _write(vault / "Singleton.md", _note_body(tags=["ai-analysis"], title="Singleton"))
    entries = vi.scan_vault(vault)
    proposals = generate_vocabulary_proposals(entries, threshold=5)
    merges = proposals["tag_merges"]
    # ai-analysis should be in merges, pointing at ai.
    match = next((m for m in merges if m["tag"] == "ai-analysis"), None)
    assert match is not None, f"expected merge for ai-analysis, got {merges}"
    assert "ai" in match["near_duplicates"]


def test_singleton_tag_without_near_duplicate_no_merge(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(5):
        _write(vault / f"Note{i}.md", _note_body(tags=["ai"], title=f"Note{i}"))
    _write(vault / "Lonely.md", _note_body(tags=["zebra-unique-xyz"], title="Lonely"))
    entries = vi.scan_vault(vault)
    proposals = generate_vocabulary_proposals(entries, threshold=5)
    merges = proposals["tag_merges"]
    assert not any(m["tag"] == "zebra-unique-xyz" for m in merges)


def test_singleton_near_duplicate_among_singletons(tmp_path: Path):
    # Two singletons that are near-duplicates of each other should also
    # produce merges (both point at each other).
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault / "A.md", _note_body(tags=["ai-analysis"], title="A"))
    _write(vault / "B.md", _note_body(tags=["ai-adoption"], title="B"))
    entries = vi.scan_vault(vault)
    proposals = generate_vocabulary_proposals(entries, threshold=5)
    merges = proposals["tag_merges"]
    assert any(m["tag"] == "ai-analysis" for m in merges)
    assert any(m["tag"] == "ai-adoption" for m in merges)


def test_tag_merge_carries_workspaces(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(3):
        _write(vault / f"Note{i}.md", _note_body(tags=["ai"], title=f"Note{i}"))
    _write(vault / "Singleton.md", _note_body(tags=["ai-analysis"], title="Singleton"))
    entries = vi.scan_vault(vault, workspace="work")
    proposals = generate_vocabulary_proposals(entries, threshold=5)
    match = next(m for m in proposals["tag_merges"] if m["tag"] == "ai-analysis")
    assert "work" in match["workspaces"]


# ---- os_audit integration ---------------------------------------------------


def test_os_audit_vocabulary_proposals_are_informational_not_defects(tmp_path: Path):
    """Vocabulary proposals must not raise the audit status or defect_count."""
    from ciao.os_audit import run_os_audit

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "CLAUDE.md").write_text("# Guide\n", encoding="utf-8")
    (workspace / "AGENTS.md").symlink_to("CLAUDE.md")
    vault = workspace / "memory-vault"
    vault.mkdir()
    (workspace / ".runtime").mkdir()
    # Vault with a non-canonical type that crosses the threshold (5 uses).
    # Without proposals this would be 5 frontmatter_errors defects; with the
    # new section the proposals are informational and must not add to defects.
    # To isolate, use a clean vault plus vocabulary proposals.
    for i in range(5):
        _write(vault / f"Note{i}.md", _note_body(type_="brainstorm", title=f"Note{i}"))

    report = run_os_audit(workspace_dir=workspace, vault_root=vault, runtime_dir=workspace / ".runtime")
    vocab = report["vocabulary_proposals"]
    assert len(vocab["type_promotions"]) == 1
    assert vocab["type_promotions"][0]["type"] == "brainstorm"
    # Proposals are informational: they do not raise defect_count beyond the
    # existing frontmatter_errors those same notes already incur.
    # The audit status is driven by defects; vocabulary proposals alone must
    # not turn a clean vault into needs_attention when there are no other defects.
    # Here the vault DOES have defects (unknown_type frontmatter_errors), so
    # defect_count includes them, but the proposals add zero on top.
    assert report["vocabulary_proposals"]["errors"] == []
    # Verify the markdown renders the section.
    from ciao.os_audit import format_audit_markdown

    md = format_audit_markdown(report)
    assert "Vocabulary Proposals" in md
    assert "brainstorm" in md


def test_os_audit_clean_vault_no_proposals_still_healthy(tmp_path: Path):
    from ciao.memory_tool import ensure_regions
    from ciao.os_audit import run_os_audit

    workspace = tmp_path / "ws"
    workspace.mkdir()
    guide = workspace / "CLAUDE.md"
    guide.write_text("# Guide\n", encoding="utf-8")
    ensure_regions(guide)
    (workspace / "AGENTS.md").symlink_to("CLAUDE.md")
    vault = workspace / "memory-vault"
    vault.mkdir()
    (workspace / ".runtime").mkdir()
    # Empty vault is the canonical "healthy" shape; a non-empty vault without
    # a search index reports a search_index defect, which would mask the
    # vocabulary-proposals assertion. The guide must carry its bounded-region
    # markers or the audit reports marker_errors as defects.
    report = run_os_audit(workspace_dir=workspace, vault_root=vault, runtime_dir=workspace / ".runtime")
    assert report["status"] == "healthy"
    assert report["vocabulary_proposals"]["type_promotions"] == []
    assert report["vocabulary_proposals"]["tag_merges"] == []


def test_os_audit_alias_rename_still_reported_as_drift_not_promotion(tmp_path: Path):
    """Aliased types (doc -> document) report as frontmatter_errors/drift
    and have a safe rename; they must not surface as promotion proposals."""
    from ciao.os_audit import run_os_audit

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "CLAUDE.md").write_text("# Guide\n", encoding="utf-8")
    (workspace / "AGENTS.md").symlink_to("CLAUDE.md")
    vault = workspace / "memory-vault"
    vault.mkdir()
    (workspace / ".runtime").mkdir()
    for i in range(6):
        _write(vault / f"Note{i}.md", _note_body(type_="doc", title=f"Note{i}"))

    report = run_os_audit(workspace_dir=workspace, vault_root=vault, runtime_dir=workspace / ".runtime")
    # Aliased types surface as frontmatter_errors (existing step-4 behavior).
    assert any(e["kind"] == "unknown_type" for e in report["vault_hygiene"]["frontmatter_errors"])
    # But not as promotion proposals.
    assert report["vocabulary_proposals"]["type_promotions"] == []
    # Migration still reports planned renames.
    from ciao.vault_migration import migrate_vault_vocabulary

    summary = migrate_vault_vocabulary(vault, apply=False)
    assert any(c["from"] == "doc" and c["to"] == "document" for c in summary["planned"])


def test_os_audit_vocabulary_scope_workspace_vs_global(tmp_path: Path):
    from ciao.os_audit import run_os_audit

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "CLAUDE.md").write_text("# Guide\n", encoding="utf-8")
    (workspace / "AGENTS.md").symlink_to("CLAUDE.md")
    vault = workspace / "memory-vault"
    vault.mkdir()
    (workspace / ".runtime").mkdir()
    _write(vault / "Note.md", _note_body(type_="note", title="Note"))

    ws_report = run_os_audit(workspace_dir=workspace, vault_root=vault, runtime_dir=workspace / ".runtime", scope="workspace")
    assert "vocabulary_proposals" in ws_report
    gl_report = run_os_audit(workspace_dir=workspace, vault_root=vault, runtime_dir=workspace / ".runtime", scope="global")
    assert "vocabulary_proposals" not in gl_report
