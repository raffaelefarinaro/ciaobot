"""The scoped evidence drill-down for recall (issue #460).

`vault_search` answers with `_public_snippet`: the FTS-highlighted lines only,
inside SQLite's 32-token budget. That is a privacy property — it is what keeps
an unrelated private line out of a recall answer — and the core prompt turns it
into a rule by forbidding a generic full-note read for a pure recall question.

The rule has a failure mode. A snippet can keep the clause that names a value
and drop the clause that qualifies, negates, or supersedes it, and recall then
answers confidently from a fragment whose meaning is the opposite of the note's.
`fts_search.expand_note` is the bounded repair, and these tests pin both halves
of it: that it recovers the omitted qualification, and that it does not become
the full-note read the rule exists to prevent.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import fts_search
from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal

# One fixture note carrying every shape the drill-down has to get right:
#
# * the decisive clause ("billed at 180") shares no term with the query, so the
#   snippet drops it while keeping "retainer rate is 120";
# * the credential lines sit in a SIBLING block, so a correct expansion of the
#   Billing block must not reach them;
# * one credential sits INSIDE the expanded block, so the redaction fallback is
#   exercised too.
NORTHWIND = """---
id: note-northwind
tags: [retainer, billing]
---

# Northwind retainer

## Billing

The Northwind retainer rate is 120 per hour.
Superseded by the 2026-06 amendment: work logged since then is billed at 180.
Billing portal password: hunter2-shared

## Access

api_key: ac-live-7e2c9a441b
Door code for the Via Verde studio is 4417.
"""

QUERY = "Northwind retainer rate"


def _vault(tmp_path: Path, workspace: str = "personal") -> tuple[Path, Path]:
    """Return ``(install_base, vault_root)`` with the fixture note in place."""
    base = tmp_path / "install"
    vault = base / workspace / "memory-vault"
    (vault / "projects").mkdir(parents=True, exist_ok=True)
    (vault / "projects" / "Northwind.md").write_text(NORTHWIND, encoding="utf-8")
    return base, vault


def _indexed(base: Path, *vaults: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    fts_search.init_db(conn)
    for vault in vaults:
        fts_search.index_vault(conn, vault, path_base=base)
    return conn


def _key(workspace: str = "personal") -> str:
    return os.path.join(workspace, "memory-vault", "projects", "Northwind.md")


def _expand(conn: sqlite3.Connection, base: Path, vault: Path, key: str, **kw):
    return fts_search.expand_note(
        conn,
        base,
        vault,
        key,
        kw.pop("query", QUERY),
        path_prefix=fts_search.vault_key_prefix(vault, base),
        **kw,
    )


def _text(result: dict) -> str:
    return "\n".join(section["text"] for section in result["sections"])


# ── The gap the drill-down exists to close ─────────────────────────────────


def test_snippet_drops_the_qualification_that_reverses_the_answer(
    tmp_path: Path,
) -> None:
    """The premise: snippets alone answer this question wrongly.

    Not a test of new code — a test of the baseline, kept here so the drill-down
    is never "fixing" a gap that has quietly closed. If a future snippet change
    starts including the superseding clause, this fails and the whole drill-down
    needs re-justifying rather than silently becoming dead weight.
    """
    base, vault = _vault(tmp_path)
    conn = _indexed(base, vault)
    rows = fts_search.search_vault(
        conn, QUERY, path_prefix=fts_search.vault_key_prefix(vault, base)
    )
    assert rows, "the fixture note must match the query"
    snippet = rows[0]["snippet"]
    assert "120" in snippet
    assert "180" not in snippet
    assert "amendment" not in snippet


def test_expansion_recovers_the_superseding_clause(tmp_path: Path) -> None:
    base, vault = _vault(tmp_path)
    conn = _indexed(base, vault)
    result = _expand(conn, base, vault, _key())
    assert result is not None
    body = _text(result)
    assert "180" in body
    assert "amendment" in body
    assert result["reason"] == "matched"
    assert result["path"] == _key()
    assert result["title"] == "Northwind retainer"


def test_expansion_reports_the_current_revision(tmp_path: Path) -> None:
    """A path is the reference, so staleness shows up as a changed revision."""
    base, vault = _vault(tmp_path)
    conn = _indexed(base, vault)
    before = _expand(conn, base, vault, _key())
    note = vault / "projects" / "Northwind.md"
    note.write_text(NORTHWIND.replace("180", "210"), encoding="utf-8")
    after = _expand(conn, base, vault, _key())
    assert before is not None and after is not None
    assert before["revision"] != after["revision"]
    assert "210" in _text(after)


# ── What the drill-down must still refuse ──────────────────────────────────


def test_expansion_does_not_return_a_sibling_block(tmp_path: Path) -> None:
    """The secret next door is adjacent in the file, not in the answer.

    This is the property `_public_snippet` was protecting, and the one a
    "just read the note" drill-down would have thrown away.
    """
    base, vault = _vault(tmp_path)
    conn = _indexed(base, vault)
    result = _expand(conn, base, vault, _key())
    assert result is not None
    body = _text(result)
    assert "ac-live-7e2c9a441b" not in body
    assert "4417" not in body
    assert "Via Verde" not in body
    assert [s["heading"] for s in result["sections"]] == ["Billing"]


def test_expansion_under_the_title_does_not_swallow_a_later_section(
    tmp_path: Path,
) -> None:
    """Every heading is a boundary, including a deeper one.

    The distinguishing case for `_section_bounds`. A note's body often starts
    directly under its H1, and a section that ran to the next heading of the
    same *or a higher* level would make that H1 enclose the whole file — so the
    very first drill-down would have returned the note, secrets included. The
    fixture above cannot see the difference, because its match sits under a
    `##` that a sibling `##` already terminates.
    """
    base = tmp_path / "install"
    vault = base / "personal" / "memory-vault"
    vault.mkdir(parents=True)
    (vault / "Lead.md").write_text(
        "# Northwind retainer\n\n"
        "The Northwind retainer rate is 120 per hour.\n"
        "Superseded by the 2026-06 amendment: billed at 180 since then.\n\n"
        "## Access\n\n"
        "Door code for the studio is 4417.\n",
        encoding="utf-8",
    )
    conn = _indexed(base, vault)
    key = os.path.join("personal", "memory-vault", "Lead.md")
    result = _expand(conn, base, vault, key)
    assert result is not None
    body = _text(result)
    assert "180" in body
    assert "4417" not in body


def test_expansion_redacts_a_credential_inside_the_window(tmp_path: Path) -> None:
    base, vault = _vault(tmp_path)
    conn = _indexed(base, vault)
    result = _expand(conn, base, vault, _key())
    assert result is not None
    body = _text(result)
    assert "hunter2-shared" not in body
    assert "[redacted]" in body


def test_expansion_omits_frontmatter(tmp_path: Path) -> None:
    base, vault = _vault(tmp_path)
    conn = _indexed(base, vault)
    result = _expand(conn, base, vault, _key())
    assert result is not None
    assert result["frontmatter_omitted"] is True
    assert "note-northwind" not in _text(result)


def test_expansion_refuses_another_workspaces_note(tmp_path: Path) -> None:
    base, personal = _vault(tmp_path, "personal")
    _, work = _vault(tmp_path, "work")
    conn = _indexed(base, personal, work)
    personal_prefix = fts_search.vault_key_prefix(personal, base)
    assert (
        fts_search.expand_note(
            conn, base, personal, _key("work"), QUERY, path_prefix=personal_prefix
        )
        is None
    )
    # ... and the same key is fine for the workspace that owns it.
    assert (
        fts_search.expand_note(
            conn,
            base,
            work,
            _key("work"),
            QUERY,
            path_prefix=fts_search.vault_key_prefix(work, base),
        )
        is not None
    )


@pytest.mark.parametrize(
    "key",
    [
        "../../etc/passwd",
        "personal/memory-vault/../../work/memory-vault/projects/Northwind.md",
        "/etc/passwd",
        "personal/memory-vault/projects/Absent.md",
        "",
    ],
)
def test_expansion_refuses_a_key_that_is_not_an_indexed_note(
    tmp_path: Path, key: str
) -> None:
    base, vault = _vault(tmp_path)
    conn = _indexed(base, vault)
    assert _expand(conn, base, vault, key) is None


def test_expansion_refuses_a_search_opted_out_note(tmp_path: Path) -> None:
    """`search: false` takes a note out of recall, drill-down included."""
    base, vault = _vault(tmp_path)
    (vault / "projects" / "Private.md").write_text(
        "---\nsearch: false\n---\n\n# Private\n\nThe Northwind retainer rate is 999.\n",
        encoding="utf-8",
    )
    conn = _indexed(base, vault)
    key = os.path.join("personal", "memory-vault", "projects", "Private.md")
    assert _expand(conn, base, vault, key) is None


def test_expansion_refuses_a_transcript(tmp_path: Path) -> None:
    """Chat logs live in `transcript_fts`; the drill-down only reads notes.

    The archive is placed INSIDE the vault on purpose. Promoted out of it, a
    transcript would also fail the containment check, and this test would then
    prove nothing about which FTS table the drill-down consults.
    """
    base, vault = _vault(tmp_path)
    logs = vault / "Logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "2026-06-01.md").write_text(
        "# Log\n\nThe Northwind retainer rate came up again.\n", encoding="utf-8"
    )
    conn = sqlite3.connect(":memory:")
    fts_search.init_db(conn)
    fts_search.index_vault(conn, vault, path_base=base)
    fts_search.index_logs(conn, vault, logs_root=logs, path_base=base)
    assert fts_search.search_logs(conn, QUERY), "the transcript must be indexed"
    key = os.path.join("personal", "memory-vault", "Logs", "2026-06-01.md")
    assert (vault.parent.parent / key).is_file(), "the transcript is inside the vault"
    assert _expand(conn, base, vault, key) is None


def test_expansion_refuses_a_symlink_out_of_the_vault(tmp_path: Path) -> None:
    """The index vouches for the key; the filesystem still has to agree."""
    base, vault = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Secrets.md").write_text(
        "# Secrets\n\nThe Northwind retainer rate is 999.\n", encoding="utf-8"
    )
    link = vault / "projects" / "Linked.md"
    link.symlink_to(outside / "Secrets.md")
    conn = _indexed(base, vault)
    key = os.path.join("personal", "memory-vault", "projects", "Linked.md")
    assert _expand(conn, base, vault, key) is None


def test_expansion_fails_closed_for_an_unresolvable_scope(tmp_path: Path) -> None:
    """`NO_MATCH_KEY_PREFIX` must match nothing, here as in search."""
    base, vault = _vault(tmp_path)
    conn = _indexed(base, vault)
    assert (
        fts_search.expand_note(
            conn,
            base,
            vault,
            _key(),
            QUERY,
            path_prefix=fts_search.NO_MATCH_KEY_PREFIX,
        )
        is None
    )


# ── Bounds ─────────────────────────────────────────────────────────────────


def test_expansion_is_bounded_in_windows_lines_and_characters(
    tmp_path: Path,
) -> None:
    base = tmp_path / "install"
    vault = base / "personal" / "memory-vault"
    vault.mkdir(parents=True)
    blocks = "\n".join(
        f"## Block {i}\n\n" + "\n".join(f"retainer line {i}-{j}" for j in range(60))
        for i in range(8)
    )
    (vault / "Big.md").write_text(f"# Big\n\n{blocks}\n", encoding="utf-8")
    conn = _indexed(base, vault)
    key = os.path.join("personal", "memory-vault", "Big.md")
    result = _expand(conn, base, vault, key, query="retainer")
    assert result is not None
    assert 0 < len(result["sections"]) <= fts_search.EXPAND_MAX_WINDOWS
    assert result["truncated"] is True
    assert (
        sum(len(s["text"]) for s in result["sections"])
        <= fts_search.EXPAND_MAX_CHARS
    )
    for section in result["sections"]:
        assert section["text"].count("\n") + 1 <= fts_search.EXPAND_MAX_LINES


def test_expansion_falls_back_to_one_block_when_no_line_matches(
    tmp_path: Path,
) -> None:
    base, vault = _vault(tmp_path)
    conn = _indexed(base, vault)
    result = _expand(conn, base, vault, _key(), query="zzz-nothing-here")
    assert result is not None
    assert result["reason"] == "no_line_match"
    assert len(result["sections"]) == 1
    assert "ac-live-7e2c9a441b" not in _text(result)


# ── Control-plane surface ──────────────────────────────────────────────────


def _plane(base: Path) -> CiaoControlPlane:
    config = SimpleNamespace(
        workspace_root=base,
        vault_root=base / "personal" / "memory-vault",
        state_path=base / ".runtime" / "state.json",
        workspace=lambda name: SimpleNamespace(name=name),
    )
    return CiaoControlPlane(
        config,
        project_chat_manager=SimpleNamespace(
            _workspace_vault_root=lambda ws: base / ws / "memory-vault"
        ),
        schedule_manager=SimpleNamespace(),
    )


def _principal(workspace: str = "personal") -> McpPrincipal:
    return McpPrincipal(
        token_id="t", chat_id="c", project_id="p", workspace=workspace, provider="claude"
    )


def test_control_plane_expand_returns_bounded_context(tmp_path: Path) -> None:
    base, _vault_root = _vault(tmp_path)
    plane = _plane(base)
    rows = asyncio.run(plane.vault_search(_principal(), QUERY))["data"]
    assert rows, "the search must find the note first"
    result = asyncio.run(plane.vault_expand(_principal(), rows[0]["path"], QUERY))
    body = "\n".join(s["text"] for s in result["data"]["sections"])
    assert "180" in body
    assert "ac-live-7e2c9a441b" not in body


def test_control_plane_expand_rejects_a_foreign_path(tmp_path: Path) -> None:
    base, _personal = _vault(tmp_path, "personal")
    _vault(tmp_path, "work")
    plane = _plane(base)
    asyncio.run(plane.vault_search(_principal("work"), QUERY))
    with pytest.raises(ControlPlaneError) as excinfo:
        asyncio.run(plane.vault_expand(_principal("personal"), _key("work"), QUERY))
    assert excinfo.value.code == "note_not_matched"


def test_control_plane_expand_requires_a_path(tmp_path: Path) -> None:
    base, _vault_root = _vault(tmp_path)
    plane = _plane(base)
    with pytest.raises(ControlPlaneError) as excinfo:
        asyncio.run(plane.vault_expand(_principal(), "  ", QUERY))
    assert excinfo.value.code == "invalid_request"


# ── Catalog and prompt wiring ──────────────────────────────────────────────


def test_expand_is_an_auto_approved_read_tool() -> None:
    from ciao.execution_modes import AUTO_APPROVED_MCP_TOOLS

    assert "vault_expand" in AUTO_APPROVED_MCP_TOOLS


def test_core_prompt_directs_recall_to_the_bounded_drill_down() -> None:
    """The prompt must offer the drill-down *and* keep the rule it refines."""
    from ciao.core_prompt import _system_instructions

    text = _system_instructions()
    assert "vault_expand" in text
    assert "do not open a full vault note with a generic file-read tool" in text
