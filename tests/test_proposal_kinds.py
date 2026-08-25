"""Tests for the shared proposal-kind registry.

Three modules once each owned a copy of the proposal-bullet regex and the
three disagreed: the web layer missed ``[profile]``, none matched ``[rehome]``,
and an unregistered kind silently counted as zero. This registry is the single
owner; these tests pin the four kinds, the cross-module agreement, the header
refresh, and the accept-descriptor split.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from ciao import proposal_kinds as pk
from ciao.memory_proposals import _STUB_HEADER


def _fixture_file(tmp_path: Path) -> Path:
    """A queue with one bullet of every registered kind."""
    path = tmp_path / "Workspace" / "Memory-Proposals.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Memory Proposals\n\n"
        "- [memory] durable lesson  _(from: Decisions)_\n"
        "- [profile] prefers terse replies  _(from: User corrections)_\n"
        "- [user] legacy profile label  _(from: Decisions)_\n"
        "- [rehome] move this note to work  _(from: vault-rehome)_\n",
        encoding="utf-8",
    )
    return path


def test_rehome_and_profile_are_matched_by_the_shared_regex() -> None:
    for kind in ("rehome", "profile"):
        bullet = pk.parse_bullet(f"- [{kind}] some content")
        assert bullet is not None
        assert bullet.kind == kind
        assert bullet.text == "some content"


def test_regex_is_derived_from_the_kind_table() -> None:
    """A kind added to KINDS is matched without editing the regex by hand."""
    for kind in pk.KINDS:
        assert pk.parse_bullet(f"  -  [{kind}]  content") is not None


def test_os_audit_counts_all_proposal_kinds(tmp_path: Path) -> None:
    """os_audit's memory-audit scan sees every proposal kind.

    All four kinds must be visible to the counter; previously the web layer
    missed ``[profile]`` and none matched ``[rehome]``. The web layer's own
    proposal-bullet counter was removed with the Settings Context tab, so
    os_audit is the reference implementation here.
    """
    from ciao.os_audit import audit_memory

    path = _fixture_file(tmp_path)

    report = audit_memory(
        vault_root=tmp_path,
        proposal_paths=[path],
        today=datetime.date(2026, 8, 19),
    )
    assert report["pending_memory_proposals"] == 4


def test_resolve_removes_one_matched_bullet(tmp_path: Path) -> None:
    """The queue resolver deletes exactly the matched bullet by unique text.

    Memory proposals are listed and dismissed from the CLI
    (``ciao memory-proposals`` / ``ciao memory-proposal-dismiss``) rather than
    over MCP; this pins the core row-removal logic the CLI reuses.
    """
    from ciao.memory_proposals import dismiss_proposal_by_substring

    path = _fixture_file(tmp_path)
    assert dismiss_proposal_by_substring(path, "durable lesson") is True

    text = path.read_text(encoding="utf-8")
    assert "durable lesson" not in text
    assert "prefers terse replies" in text
    assert "legacy profile label" in text
    assert "move this note to work" in text


def test_unknown_kind_raises_never_silent_zero() -> None:
    with pytest.raises(pk.UnknownKindError) as excinfo:
        pk.accept_for("bogus")
    assert "bogus" in str(excinfo.value)


def test_rehome_descriptor_is_not_a_region_edit() -> None:
    accept = pk.accept_for("rehome")
    assert isinstance(accept, pk.RehomeAccept)
    assert accept.action == "move_file"
    # Structurally a different type: routing on the descriptor class can never
    # reach the region-edit path, which is the whole point of the split.
    assert not isinstance(accept, pk.RegionAccept)


def test_memory_profile_and_user_are_region_edits() -> None:
    assert isinstance(pk.accept_for("memory"), pk.RegionAccept)
    assert isinstance(pk.accept_for("profile"), pk.RegionAccept)
    # Legacy ``user`` normalizes to the profile region, matching resolve_region.
    user_accept = pk.accept_for("user")
    assert isinstance(user_accept, pk.RegionAccept)
    assert user_accept.region == "profile"


def test_stale_header_rewritten_bullets_untouched(tmp_path: Path) -> None:
    """The old header naming ~/.ciao/memory.md is refreshed in place.

    The write-once header bug meant a corrected header never reached existing
    installs; appending to such a file must now rewrite only the header block
    and leave every bullet byte-identical.
    """
    from ciao.memory_proposals import _refresh_header

    stale = (
        "---\n"
        "tags: [ciao, memory, proposals]\n"
        "---\n"
        "# Memory Proposals\n\n"
        "Review and promote durable facts to `~/.ciao/memory.md` or "
        "`~/.ciao/user.md` via the `memory` MCP tool.\n\n"
        "## 2026-08-01\n\n"
        "- [memory] durable lesson  _(from: Decisions)_\n"
        "- [profile] prefers terse replies  _(from: User corrections)_\n"
        "- [user] legacy label  _(from: Decisions)_\n"
        "- [rehome] move this note  _(from: vault-rehome)_\n"
    )
    refreshed = _refresh_header(stale)

    # The new header names the bounded regions, not the deleted ~/.ciao files.
    assert "~/.ciao/memory.md" not in refreshed
    assert "ciao:memory" in refreshed
    assert refreshed.startswith(_STUB_HEADER)
    # Every bullet survives byte-for-byte.
    for bullet in (
        "- [memory] durable lesson  _(from: Decisions)_\n",
        "- [profile] prefers terse replies  _(from: User corrections)_\n",
        "- [user] legacy label  _(from: Decisions)_\n",
        "- [rehome] move this note  _(from: vault-rehome)_\n",
    ):
        assert bullet in refreshed
    # The body below the header is preserved exactly.
    assert "## 2026-08-01\n" in refreshed


def test_refresh_header_leaves_non_header_files_alone(tmp_path: Path) -> None:
    """A file that does not begin with YAML frontmatter is never rewritten."""
    from ciao.memory_proposals import _refresh_header

    text = "# Memory Proposals\n\n- [memory] fact\n"
    assert _refresh_header(text) == text


def test_refresh_header_leaves_user_text_below_header_alone(tmp_path: Path) -> None:
    """Prose between the header and the first batch is preserved."""
    from ciao.memory_proposals import _refresh_header

    stale = (
        "---\n"
        "tags: [ciao, memory, proposals]\n"
        "---\n"
        "# Memory Proposals\n\n"
        "Review to `~/.ciao/memory.md`.\n\n"
        "## 2026-08-01\n\n"
        "- [memory] fact\n"
        "- a hand-written note that is not a bullet\n"
    )
    refreshed = _refresh_header(stale)
    assert "~/.ciao/memory.md" not in refreshed
    assert "- a hand-written note that is not a bullet\n" in refreshed
    assert "- [memory] fact\n" in refreshed


def test_refresh_header_ignores_indented_yaml_lists_in_frontmatter() -> None:
    """A list value inside the frontmatter is not mistaken for a bullet.

    The boundary scan must start only after the frontmatter closes, so the
    header rewrite cannot truncate a file whose frontmatter carries a list.
    """
    from ciao.memory_proposals import _refresh_header

    stale = (
        "---\n"
        "tags:\n"
        "  - ciao\n"
        "  - memory\n"
        "---\n"
        "# Memory Proposals\n\n"
        "Review to `~/.ciao/memory.md`.\n\n"
        "## 2026-08-01\n\n"
        "- [memory] fact\n"
    )
    refreshed = _refresh_header(stale)
    assert "~/.ciao/memory.md" not in refreshed
    assert "- [memory] fact\n" in refreshed
    assert refreshed.startswith(_STUB_HEADER)


def test_append_proposals_refreshes_stale_header_end_to_end(tmp_path: Path) -> None:
    """Appending to an existing install rewrites the stale header in place.

    This is the real fix for the write-once header: a corrected header can now
    reach a queue that already exists on disk, and no bullet below it changes.
    """
    from ciao.memory_proposals import MemoryProposal, append_proposals

    stale = (
        "---\n"
        "tags: [ciao, memory, proposals]\n"
        "---\n"
        "# Memory Proposals\n\n"
        "Review and promote durable facts to `~/.ciao/memory.md` or "
        "`~/.ciao/user.md` via the `memory` MCP tool.\n\n"
        "## 2026-08-01\n\n"
        "- [rehome] move this note  _(from: vault-rehome)_\n"
    )
    vault = tmp_path / "vault"
    out = vault / "Workspace" / "Memory-Proposals.md"
    out.parent.mkdir(parents=True)
    out.write_text(stale, encoding="utf-8")

    append_proposals(
        [MemoryProposal(target="memory", text="new fact", source_section="Decisions")],
        vault,
        source_path=None,
    )

    text = out.read_text(encoding="utf-8")
    # Stale wording gone, corrected header present.
    assert "~/.ciao/memory.md" not in text
    assert "ciao:memory" in text
    assert text.startswith(_STUB_HEADER)
    # The existing rehome bullet is untouched.
    assert "- [rehome] move this note  _(from: vault-rehome)_\n" in text
    # The new bullet is appended.
    assert "- [memory] new fact  _(from: Decisions)_" in text



def test_parse_bullet_returns_none_for_non_bullet_lines() -> None:
    assert pk.parse_bullet("# Heading") is None
    assert pk.parse_bullet("") is None
    assert pk.parse_bullet("- [unknown] unregistered kind") is None
    assert pk.parse_bullet("- [memory]") is None  # empty content is not a bullet


def test_parsed_kind_is_normalised_to_the_registry_spelling() -> None:
    """A capitalised bullet must not desynchronise the regex from the lookup.

    ``BULLET_RE`` is case-insensitive but ``_ACCEPT`` is keyed by the lowercase
    names in ``KINDS``. Returning the matched kind verbatim meant
    ``- [Profile] x`` parsed and then ``accept_for("Profile")`` raised
    ``UnknownKindError`` — and the ``/api/proposals`` scan calls ``accept_for``
    unguarded, so one capitalised bullet 500'd the endpoint and the whole
    review queue vanished from the app.
    """
    for kind in pk.KINDS:
        for spelling in (kind.upper(), kind.capitalize()):
            bullet = pk.parse_bullet(f"- [{spelling}] some content")
            assert bullet is not None, f"regex missed {spelling!r}"
            assert bullet.kind == kind
            # The end-to-end failure: this raised for every capitalised kind.
            pk.accept_for(bullet.kind)
