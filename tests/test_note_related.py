"""`ciao note related`: the notes connected to one note, read-only.

Built on a small vault under ``tmp_path``. What is pinned is the relation each
note is reported with, what is deliberately not a mention, the ordering and cap,
and that the lookup leaves no trace (no search hits, no FTS database).
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import async_reads
from ciao import control_plane as cp
from ciao import memory_receipts as mr
from ciao import note_verification as nv
from ciao.note_related import (
    LINKED_FROM,
    LINKS_TO,
    MENTIONS,
    MIN_MENTION_CHARS,
    find_related_notes,
)

TODAY = date(2026, 9, 19)
TARGET = "People/Maurizio.md"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    return vault


def _note(
    vault: Path,
    relative: str,
    body: str = "Something durable.",
    *,
    type_: str = "person",
    extra: str = "",
) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: {type_}\nupdated: 2024-01-05\n{extra}---\n\n# {path.stem}\n\n{body}\n",
        encoding="utf-8",
        newline="",
    )
    return path


def _related(vault: Path, relative: str = TARGET, **kwargs: Any) -> Any:
    return find_related_notes(vault, relative, workspace="personal", today=TODAY, **kwargs)


def _by_path(result: Any) -> dict[str, Any]:
    return {note.relative_path: note for note in result.related}


@pytest.fixture(autouse=True)
def _isolated_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))


def test_links_in_both_directions_are_reported(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _note(vault, TARGET, extra="related:\n  - People/Sofia.md\n")
    _note(vault, "People/Sofia.md")
    _note(vault, "People/Luca.md", extra="related:\n  - People/Maurizio.md\n")

    found = _by_path(_related(vault))

    assert found["People/Sofia.md"].relations == (LINKS_TO,)
    assert found["People/Luca.md"].relations == (LINKED_FROM,)


def test_a_title_or_alias_mention_is_reported(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _note(vault, TARGET, extra="aliases: [Mauro]\n")
    _note(vault, "projects/active/Q4-Launch.md", "Met Mauro today about pricing.", type_="project")
    _note(vault, "projects/active/Mauromania.md", "A Mauromania of calm.", type_="project")
    _note(vault, "People/Sofia.md", "Maurizio owns the launch.")

    found = _by_path(_related(vault))

    assert found["projects/active/Q4-Launch.md"].relations == (MENTIONS,)
    assert found["People/Sofia.md"].relations == (MENTIONS,)
    assert "projects/active/Mauromania.md" not in found


def test_short_aliases_are_not_matched(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    alias = "M" * (MIN_MENTION_CHARS - 1)
    _note(vault, TARGET, extra=f"aliases: [{alias}]\n")
    _note(vault, "People/Sofia.md", f"Spoke to {alias} yesterday.")

    assert _related(vault).related == ()


def test_frontmatter_is_not_a_mention(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _note(vault, TARGET)
    _note(vault, "People/Sofia.md", extra="description: works with Maurizio\n")

    assert _related(vault).related == ()


def test_bookkeeping_is_never_related(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _note(vault, TARGET)
    (vault / "Workspace" / "Memory-Proposals.md").write_text("- Maurizio is VP\n", encoding="utf-8")

    assert _related(vault).related == ()


def test_each_related_note_carries_its_revision_and_due_flag(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _note(vault, TARGET)
    checked = _note(vault, "People/Sofia.md", "Maurizio is VP Marketing.")
    _note(vault, "People/Luca.md", "Maurizio is Head of Sales.")
    text = checked.read_text(encoding="utf-8")
    nv.record_note_check(
        vault,
        nv.NoteCheck(
            relative_path="People/Sofia.md",
            content_revision=mr.content_revision(text),
            outcome="unverified",
            checked_at=TODAY,
            retry_after=TODAY + timedelta(days=30),
        ),
    )

    found = _by_path(_related(vault))

    assert found["People/Sofia.md"].revision == mr.content_revision(text)
    assert found["People/Sofia.md"].due is False
    assert found["People/Luca.md"].due is True


def test_a_note_that_never_goes_stale_is_not_due(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _note(vault, TARGET)
    _note(vault, "journal/daily/2024-01-05.md", "Lunch with Maurizio.", type_="journal")

    found = _by_path(_related(vault))

    assert MENTIONS in found["journal/daily/2024-01-05.md"].relations
    assert found["journal/daily/2024-01-05.md"].due is False


def test_a_recently_updated_note_is_not_due(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _note(vault, TARGET)
    fresh = vault / "People/Sofia.md"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_text(
        f"---\ntype: person\nupdated: {TODAY.isoformat()}\n---\n\n# Sofia\n\nMaurizio is VP Marketing.\n",
        encoding="utf-8",
    )

    assert _by_path(_related(vault))["People/Sofia.md"].due is False


def test_link_relations_rank_before_mentions_and_the_rest_are_counted(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _note(vault, TARGET, extra="related:\n  - People/Zed.md\n")
    _note(vault, "People/Zed.md")
    _note(vault, "People/Yan.md", extra="related:\n  - People/Maurizio.md\n")
    _note(vault, "People/Aaron.md", "Maurizio again.")

    result = _related(vault, limit=2)

    assert [note.relative_path for note in result.related] == ["People/Yan.md", "People/Zed.md"]
    assert result.truncated == 1


def test_a_path_outside_the_vault_is_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (tmp_path / "x.md").write_text("# x\n", encoding="utf-8")

    with pytest.raises(mr.MemoryReceiptError):
        _related(vault, "../x.md")


def test_it_records_no_search_hits(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _note(vault, TARGET)
    _note(vault, "People/Sofia.md", "Maurizio again.")

    assert _related(vault).related

    names = {path.name for path in tmp_path.rglob("*") if path.is_file()}
    assert "vault_search_hits.jsonl" not in names
    assert not {name for name in names if name.endswith((".db", ".sqlite", ".sqlite3"))}


# ── The control-plane operation ────────────────────────────────────────────


class _Install:
    def __init__(self, vaults: dict[str, Path]) -> None:
        self._vaults = vaults
        self.state_path = str(next(iter(vaults.values())).parent / ".runtime" / "state.json")

    def workspace_vault_root(self, workspace: str) -> Path:
        return self._vaults[workspace]

    def workspace(self, name: str) -> Any:
        return SimpleNamespace(name=name) if name in self._vaults else None


class _Plane:
    def __init__(self, install: _Install) -> None:
        self.config = install

    _workspace = cp.CiaoControlPlane._workspace
    note_related = cp.CiaoControlPlane.note_related


def _principal(workspace: str = "personal") -> cp.AgentPrincipal:
    return cp.AgentPrincipal(
        token_id="t1", chat_id="chat-1", project_id="p1", workspace=workspace, provider="claude"
    )


@pytest.fixture
def _executor() -> Any:
    async_reads.reset_vault_read_executor()
    yield
    async_reads.reset_vault_read_executor()


def test_note_related_reads_only_the_callers_workspace_vault(tmp_path: Path, _executor: Any) -> None:
    mine = _vault(tmp_path / "personal")
    theirs = _vault(tmp_path / "work")
    for vault in (mine, theirs):
        _note(vault, TARGET)
    _note(mine, "People/Sofia.md", "Maurizio is VP Marketing.")
    _note(theirs, "People/Elsewhere.md", "Maurizio is Head of Sales.")
    plane = _Plane(_Install({"personal": mine, "work": theirs}))

    reply = asyncio.run(plane.note_related(_principal(), path=TARGET))  # type: ignore[arg-type]

    paths = [row["relative_path"] for row in reply["data"]["related"]]
    assert paths == ["People/Sofia.md"]


def test_note_related_refuses_an_escaping_path(tmp_path: Path, _executor: Any) -> None:
    vault = _vault(tmp_path / "personal")
    plane = _Plane(_Install({"personal": vault}))

    with pytest.raises(cp.ControlPlaneError) as caught:
        asyncio.run(plane.note_related(_principal(), path="../x.md"))  # type: ignore[arg-type]
    assert caught.value.code == "note_related_invalid"

    with pytest.raises(cp.ControlPlaneError) as empty:
        asyncio.run(plane.note_related(_principal(), path=" "))  # type: ignore[arg-type]
    assert empty.value.code == "note_related_invalid"
