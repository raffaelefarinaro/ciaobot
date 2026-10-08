"""Note writes under Windows text-mode newline translation.

On Windows, ``Path.write_text``/``read_text`` (like every text-mode ``open``
with the default ``newline=None``) translate ``\\n`` to ``\\r\\n`` on write
and back on read. The note protocol is byte-exact — a revision hashes the
file's own bytes — so a caller that hashes translated text holds a revision
the file never had, and the writer refuses rather than overwrites. The
folds read exact bytes throughout, so translated fixtures cannot move them.

CI run 37783639151 showed the failure shape: with R0's ``read_text`` fold
reads (and with a translated test-side revision) every Windows write died
as a ``RevisionConflict`` without any concurrent edit. These tests pin both
halves by simulating the translation on any host — ``write_text`` gains the
``\\r\\n`` bytes a Windows write would have produced, ``read_text`` takes
them away again — so a POSIX run exercises the Windows path.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from pathlib import Path

import pytest

from ciao import memory_receipts as mr
from ciao import note_entries as ne
from ciao import note_receipts as nr
from ciao import project_doc_update as pdu


@pytest.fixture
def windows_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows text-mode newline translation for ``Path`` IO, on any host.

    Only the default ``newline=None`` translates; an explicit ``newline``
    (which every production write passes as ``""``) goes through untouched,
    exactly as on Windows.
    """
    real_write = pathlib.Path.write_text
    real_read = pathlib.Path.read_text

    def write_text(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if newline is None and isinstance(data, str):
            return real_write(
                self, data.replace("\n", "\r\n"), encoding, errors, ""
            )
        return real_write(self, data, encoding, errors, newline)

    def read_text(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if newline is None:
            raw = real_read(self, encoding, errors, "")
            return raw.replace("\r\n", "\n").replace("\r", "\n")
        return real_read(self, encoding, errors, newline)

    monkeypatch.setattr(pathlib.Path, "write_text", write_text)
    monkeypatch.setattr(pathlib.Path, "read_text", read_text)


def _patch_oneshot(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    async def fake_oneshot(prompt: object, **kwargs: object) -> str:
        return reply

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "Workspace").mkdir(exist_ok=True)
    return tmp_path


def _applied_ids(vault: Path) -> list[str]:
    rows = mr.read_receipts(mr.journal_path(vault, None))
    return [
        str(row.get("id", ""))
        for row in rows
        if row.get("kind") == "note_apply" and row.get("status") == mr.APPLIED
    ]


def test_project_fold_survives_windows_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_text: None
) -> None:
    """A translated fixture is still the same note to a byte-exact fold."""
    vault = _vault(tmp_path)
    doc = vault / "project.md"
    doc.write_text("## Open loops\n- Pick a queue backend.\n", encoding="utf-8")
    assert doc.read_bytes().endswith(b"backend.\r\n")
    _patch_oneshot(
        monkeypatch,
        json.dumps({
            "action": "update",
            "index": 1,
            "text": "- Pick a queue backend. Resolved: Redis Streams.",
        }),
    )

    wrote = asyncio.run(pdu.update_project_doc(
        doc_path=doc,
        insights_md="## Decisions\n- Chose Redis.\n",
        model="m",
    ))

    assert wrote is True
    assert doc.read_bytes() == (
        b"## Open loops\r\n"
        b"- Pick a queue backend. Resolved: Redis Streams.\r\n"
    )
    undone = mr.undo_receipt(_applied_ids(vault)[-1], vault_root=vault)
    assert undone["status"] == mr.UNDONE
    assert doc.read_bytes() == b"## Open loops\r\n- Pick a queue backend.\r\n"


def test_person_fold_survives_windows_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_text: None
) -> None:
    """The add half reads the same exact bytes under translation too."""
    vault = _vault(tmp_path)
    note = vault / "Mo.md"
    note.write_text("## Notes\n- Lives in Porto.\n", encoding="utf-8")
    _patch_oneshot(
        monkeypatch,
        json.dumps({
            "action": "add",
            "section": "Notes",
            "text": "- Based in Lisbon.",
        }),
    )

    wrote = asyncio.run(pdu.fold_fact_into_person_note(
        note_path=note, fact="Based in Lisbon.", model="m",
    ))

    assert wrote is True
    assert note.read_bytes() == (
        b"## Notes\r\n- Lives in Porto.\r\n- Based in Lisbon.\r\n"
    )
    undone = mr.undo_receipt(_applied_ids(vault)[-1], vault_root=vault)
    assert undone["status"] == mr.UNDONE
    assert note.read_bytes() == b"## Notes\r\n- Lives in Porto.\r\n"


def test_byte_exact_revision_survives_windows_translation(
    tmp_path: Path, windows_text: None
) -> None:
    """A caller hashing the file's own bytes writes under translation."""
    vault = _vault(tmp_path)
    (vault / "notes").mkdir(exist_ok=True)
    relative = "notes/mo.md"
    note = vault / relative
    note.write_text(
        "- Landlord is Mr Silva [verified: 2020-01-01]\n- Speaks Greek\n",
        encoding="utf-8",
    )
    text = note.read_bytes().decode("utf-8")
    entries = ne.parse_note_entries(
        text, note_path=relative, workspace="personal"
    ).entries

    receipt = nr.apply_entry_edit(
        vault_root=vault,
        relative_path=relative,
        expected_revision=mr.content_revision(text),
        identity=entries[1].identity,
        fingerprint=entries[1].fingerprint,
        replacement="- Speaks Portuguese",
        actor="proposal-accept",
        source="fold",
        workspace="personal",
    )

    assert receipt["changed"] is True
    assert note.read_bytes().split(b"\n")[0].endswith(b"[verified: 2020-01-01]\r")


def test_translated_revision_is_refused_not_overwritten(
    tmp_path: Path, windows_text: None
) -> None:
    """A revision of translated text is stale: refusal, no write, no row."""
    vault = _vault(tmp_path)
    (vault / "notes").mkdir(exist_ok=True)
    relative = "notes/mo.md"
    note = vault / relative
    note.write_text("## Notes\n- Lives in Porto.\n", encoding="utf-8")
    before = note.read_bytes()
    entries = ne.parse_note_entries(
        note.read_text(encoding="utf-8"), note_path=relative, workspace="personal"
    ).entries

    with pytest.raises(mr.RevisionConflict):
        nr.apply_entry_edit(
            vault_root=vault,
            relative_path=relative,
            # The translated read: LF text the CRLF file does not hold.
            expected_revision=mr.content_revision(note.read_text(encoding="utf-8")),
            identity=entries[0].identity,
            fingerprint=entries[0].fingerprint,
            replacement="- Lives in Lisbon.",
            actor="proposal-accept",
            source="fold",
            workspace="personal",
        )

    assert note.read_bytes() == before
    assert _applied_ids(vault) == []
