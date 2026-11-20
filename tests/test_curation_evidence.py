"""The read-only preparation of one due stale note, with its related proposals.

Every test builds its vault under ``tmp_path`` and hands the preparation a config
whose only answer is that vault. Nothing here reads HOME, the live engine or a
real workspace. Defer assertions compare the exact fixed reason.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import curation_evidence as ce
from ciao import curation_run as cr
from ciao import memory_receipts as mr
from ciao import note_edit_proposals as nep
from ciao import note_receipts as nr
from ciao import proposal_tracking as pt

WS = "home"
REL = "People/Sofia.md"
NOTE_TEXT = "---\ntype: person\nupdated: 2024-01-05\n---\n\n# Sofia\n\nSomething durable.\n"
REV = mr.content_revision(NOTE_TEXT)
QUEUE_REL = f"{WS}/Workspace/Memory-Proposals.md"

R_VAULT = "the workspace's configured vault is not the planned vault"
R_CHANGED = "the note changed since it was planned"
R_PATH = "the planned note path does not name a plain note"
R_NOTE_BIG = "the planned note is larger than the read ceiling"
R_MALFORMED = "a note-edit record cannot be read"
R_SIDECAR_BIG = "a note-edit record is larger than the read ceiling"
R_NO_RECORD = "a queued proposal for this note has no readable record"
R_OTHER_NOTE = "a queued proposal for this note is filed against another note"
R_SHARED_ROW = "two note-edit records name one queue row"
R_COUNT = "the note-edit store holds more records than can be read"
R_TOTAL = "the note-edit store is larger than can be read"
R_QUEUE_BIG = "the proposal queue is larger than the read ceiling"
R_REFS = "more related proposals than the evidence can carry"
R_OS = "a file in the vault could not be read"


def _vault(tmp_path: Path, rel: str = REL, text: str = NOTE_TEXT) -> Path:
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    note = vault / rel
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes(text.encode("utf-8"))
    return vault


def _config(vault: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_vault_root=lambda _name: vault)


def _store(vault: Path) -> Path:
    store = vault.joinpath(*nep.SIDECAR_RELATIVE)
    store.mkdir(parents=True, exist_ok=True)
    return store


def _pad(data: bytes, size: int) -> bytes:
    assert len(data) <= size
    return data + b" " * (size - len(data))


def _plan(relative: str = REL, revision: str = REV, *, keys: tuple[str, ...] | None = None):
    target = cr.StaleNoteTarget(relative_path=relative, revision=revision)
    item = cr.WorklistItem(
        pass_id=cr.PASS_STALE_NOTE,
        label="Sofia",
        reason=f"revision {revision}",
        keys=keys if keys is not None else (cr.item_key(cr.PASS_STALE_NOTE, relative),),
        target=target,
    )
    return cr.RunPlan(planned=(item,), deferred=(), budget=cr.RunBudget())


def _proposal(
    pid: str,
    *,
    relative: str = REL,
    revision: str = REV,
    settled: str = "",
    proposal_id: str | None = None,
) -> nep.NoteEditProposal:
    return nep.NoteEditProposal(
        id=pid,
        workspace=WS,
        relative_path=relative,
        operation=nep.REPLACE,
        expected_revision=revision,
        before="x",
        after="y",
        outcome="update",
        coverage="complete",
        evidence=(),
        reason="stale",
        created_at="2026-11-18T00:00:00Z",
        proposal_id=pid if proposal_id is None else proposal_id,
        settled=settled,
    )


def _raw_sidecar(vault: Path, proposal: nep.NoteEditProposal, pad_to: int | None = None) -> Path:
    """Write a sidecar exactly as ``write_sidecar`` would, optionally space-padded."""
    body = json.dumps(
        {"schema": nep.SIDECAR_SCHEMA, "proposal": proposal.as_dict()},
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"
    data = body.encode("utf-8")
    if pad_to is not None:
        data = _pad(data, pad_to)
    path = _store(vault) / f"{proposal.id}.json"
    path.write_bytes(data)
    return path


def _file_live(vault: Path, relative: str = REL, revision: str = REV, reason: str = "stale") -> str:
    """Queue one live note-edit bullet for this target; return its queue row id."""
    text, source = nep._bullet_fields(relative, nep.REPLACE, revision, reason)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    previous = queue.read_text(encoding="utf-8") if queue.exists() else ""
    queue.write_text(
        previous + f"- [{nep.KIND}] {text} _(from: {source})_\n", encoding="utf-8"
    )
    rows = [
        entry.proposal_id
        for entry in pt.walk_proposal_queue(WS, QUEUE_REL, queue.read_text(encoding="utf-8"))
        if entry.bullet.kind == nep.KIND and entry.bullet.text == text
    ]
    return rows[-1]


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _prepared(refs: tuple[ce.ProposalReference, ...] = (), rel: str = REL) -> ce.PreparedNote:
    return ce.PreparedNote(
        item_key=cr.item_key(cr.PASS_STALE_NOTE, rel),
        relative_path=rel,
        revision=REV,
        references=refs,
    )


# ── Selection ──────────────────────────────────────────────────────────────


def test_an_empty_run_selects_no_note(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    empty = cr.RunPlan(planned=(), deferred=(), budget=cr.RunBudget())

    assert ce.prepare_one_stale_note(_config(vault), WS, vault, empty) == ce.NoSelectedNote()


def test_a_deferred_stale_item_is_never_chosen(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    item = _plan().planned[0]
    plan = cr.RunPlan(planned=(), deferred=(item,), budget=cr.RunBudget(max_items=0))

    assert ce.prepare_one_stale_note(_config(vault), WS, vault, plan) == ce.NoSelectedNote()


def test_an_item_with_two_matching_keys_is_not_selected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    key = cr.item_key(cr.PASS_STALE_NOTE, REL)

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan(keys=(key, key)))

    assert outcome == ce.NoSelectedNote()


# ── Binding and the note itself ────────────────────────────────────────────


def test_a_selected_note_is_prepared_with_its_key_path_and_revision(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == _prepared()


def test_a_config_bound_to_another_vault_defers(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    other = _vault(tmp_path / "elsewhere")

    outcome = ce.prepare_one_stale_note(_config(other), WS, vault, _plan())

    assert outcome == ce.DeferredNote(reason=R_VAULT)


def test_a_changed_note_defers(tmp_path: Path) -> None:
    vault = _vault(tmp_path, text=NOTE_TEXT + "Edited.\n")

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == ce.DeferredNote(reason=R_CHANGED)


def test_a_missing_note_defers(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / REL).unlink()

    # resolve_note_path refuses a path that is not there, before any read.
    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == ce.DeferredNote(reason=R_PATH)


def test_a_symlinked_note_defers(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    real = tmp_path / "real.md"
    real.write_bytes(NOTE_TEXT.encode("utf-8"))
    (vault / REL).unlink()
    os.symlink(real, vault / REL)

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == ce.DeferredNote(reason=R_PATH)


def test_a_filesystem_error_during_resolution_defers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)

    def refuse(*_args: object, **_kwargs: object) -> Path:
        raise PermissionError("denied")

    monkeypatch.setattr(nr, "resolve_note_path", refuse)

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == ce.DeferredNote(reason=R_OS)


def test_a_bom_and_crlf_note_keeps_the_discovery_revision(tmp_path: Path) -> None:
    raw = b"\xef\xbb\xbf" + NOTE_TEXT.replace("\n", "\r\n").encode("utf-8")
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    (vault / "People").mkdir()
    (vault / REL).write_bytes(raw)
    revision = mr.content_revision(raw.decode("utf-8"))

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan(revision=revision))

    assert outcome == ce.PreparedNote(
        item_key=cr.item_key(cr.PASS_STALE_NOTE, REL),
        relative_path=REL,
        revision=revision,
        references=(),
    )


def test_the_note_read_ceiling_is_exactly_64_kib(tmp_path: Path) -> None:
    limit = ce.MAX_NOTE_BYTES
    exact = NOTE_TEXT + "x" * (limit - len(NOTE_TEXT.encode("utf-8")))
    vault = _vault(tmp_path, text=exact)
    assert (vault / REL).stat().st_size == limit

    at_limit = ce.prepare_one_stale_note(
        _config(vault), WS, vault, _plan(revision=mr.content_revision(exact))
    )
    assert at_limit == ce.PreparedNote(
        item_key=cr.item_key(cr.PASS_STALE_NOTE, REL),
        relative_path=REL,
        revision=mr.content_revision(exact),
    )

    over_text = exact + "x"
    (vault / REL).write_bytes(over_text.encode("utf-8"))
    over = ce.prepare_one_stale_note(
        _config(vault), WS, vault, _plan(revision=mr.content_revision(over_text))
    )
    assert over == ce.DeferredNote(reason=R_NOTE_BIG)


# ── Related proposals ──────────────────────────────────────────────────────


def test_open_and_settled_references_for_the_exact_target(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    open_id = _file_live(vault)
    _raw_sidecar(vault, _proposal(open_id))
    settled_id = "s" * 16
    _raw_sidecar(
        vault,
        _proposal(settled_id, settled="2026-11-19T00:00:00Z", revision="b" * 64),
    )
    _raw_sidecar(vault, _proposal("u" * 16, relative="People/Other.md"))
    open_ref = ce.ProposalReference(
        sidecar_id=open_id,
        proposal_id=open_id,
        status="open",
        revision="current",
        operation=nep.REPLACE,
        expected_revision=REV,
        settled_at="",
        accepted=False,
    )
    settled_ref = ce.ProposalReference(
        sidecar_id=settled_id,
        proposal_id="",
        status="settled",
        revision="historical",
        operation=nep.REPLACE,
        expected_revision="b" * 64,
        settled_at="2026-11-19T00:00:00Z",
        accepted=False,
    )

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    expected = tuple(sorted((open_ref, settled_ref), key=lambda ref: ref.sidecar_id))
    assert outcome == _prepared(expected)


def test_an_unsettled_sidecar_without_a_live_row_is_an_orphan(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _raw_sidecar(vault, _proposal("o" * 16))

    assert ce.prepare_one_stale_note(_config(vault), WS, vault, _plan()) == _prepared()


def test_a_stale_bullet_does_not_reopen_a_settled_record(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    live_id = _file_live(vault)
    _raw_sidecar(vault, _proposal(live_id, settled="2026-11-19T00:00:00Z"))

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert isinstance(outcome, ce.PreparedNote)
    assert [(ref.status, ref.proposal_id) for ref in outcome.references] == [
        ("settled", live_id)
    ]


def test_a_path_with_a_double_space_still_matches_its_live_row(tmp_path: Path) -> None:
    rel = "notes/a  b.md"
    vault = _vault(tmp_path, rel=rel)
    live_id = _file_live(vault, relative=rel)
    _raw_sidecar(vault, _proposal(live_id, relative=rel))

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan(relative=rel))

    assert outcome == _prepared(
        (
            ce.ProposalReference(
                sidecar_id=live_id,
                proposal_id=live_id,
                status="open",
                revision="current",
                operation=nep.REPLACE,
                expected_revision=REV,
                settled_at="",
                accepted=False,
            ),
        ),
        rel=rel,
    )


def test_a_live_bullet_without_a_readable_sidecar_defers(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _file_live(vault)

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == ce.DeferredNote(reason=R_NO_RECORD)


def test_a_live_row_whose_record_names_another_note_defers(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    live_id = _file_live(vault)
    _raw_sidecar(vault, _proposal(live_id, relative="People/Other.md"))

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == ce.DeferredNote(reason=R_OTHER_NOTE)


def test_two_records_sharing_one_queue_row_defer(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _raw_sidecar(vault, _proposal("d1" * 8, proposal_id="s" * 16))
    _raw_sidecar(vault, _proposal("d2" * 8, proposal_id="s" * 16))

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == ce.DeferredNote(reason=R_SHARED_ROW)


def test_a_malformed_sidecar_defers(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (_store(vault) / "broken.json").write_text("{not json", encoding="utf-8")

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == ce.DeferredNote(reason=R_MALFORMED)


def test_more_than_twenty_references_defers(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    for index in range(ce.MAX_REFERENCES + 1):
        _raw_sidecar(vault, _proposal(f"{index:016d}", settled="2026-11-19T00:00:00Z"))

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == ce.DeferredNote(reason=R_REFS)


def test_an_absent_store_means_no_proposals(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    assert ce.prepare_one_stale_note(_config(vault), WS, vault, _plan()) == _prepared()


# ── Limits ─────────────────────────────────────────────────────────────────


def test_512_valid_records_pass_and_a_513th_defers_on_count(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    for index in range(ce.MAX_SIDECARS):
        _raw_sidecar(
            vault, _proposal(f"u{index:015d}", relative=f"People/Bulk{index}.md")
        )
    assert len(list(_store(vault).iterdir())) == ce.MAX_SIDECARS

    assert ce.prepare_one_stale_note(_config(vault), WS, vault, _plan()) == _prepared()

    _raw_sidecar(vault, _proposal(f"u{ce.MAX_SIDECARS:015d}", relative="People/Extra.md"))
    assert ce.prepare_one_stale_note(_config(vault), WS, vault, _plan()) == ce.DeferredNote(
        reason=R_COUNT
    )


def test_a_record_of_exactly_the_byte_limit_passes_and_one_more_defers(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    path = _raw_sidecar(
        vault,
        _proposal("p" * 16, relative="People/Padded.md"),
        pad_to=ce.MAX_SIDECAR_BYTES,
    )
    assert path.stat().st_size == ce.MAX_SIDECAR_BYTES
    assert ce.prepare_one_stale_note(_config(vault), WS, vault, _plan()) == _prepared()

    _raw_sidecar(
        vault,
        _proposal("p" * 16, relative="People/Padded.md"),
        pad_to=ce.MAX_SIDECAR_BYTES + 1,
    )
    assert ce.prepare_one_stale_note(_config(vault), WS, vault, _plan()) == ce.DeferredNote(
        reason=R_SIDECAR_BIG
    )


def test_a_store_of_exactly_the_total_limit_passes_and_one_more_record_defers(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    per_file = ce.MAX_STORE_BYTES // 32
    assert per_file == ce.MAX_SIDECAR_BYTES
    for index in range(32):
        _raw_sidecar(
            vault,
            _proposal(f"t{index:02d}" + "0" * 13, relative=f"People/Bulk{index}.md"),
            pad_to=per_file,
        )

    assert ce.prepare_one_stale_note(_config(vault), WS, vault, _plan()) == _prepared()

    _raw_sidecar(
        vault,
        _proposal("t32" + "0" * 13, relative="People/Bulk32.md"),
        pad_to=per_file,
    )
    assert ce.prepare_one_stale_note(_config(vault), WS, vault, _plan()) == ce.DeferredNote(
        reason=R_TOTAL
    )


def test_a_queue_of_exactly_the_byte_limit_passes_and_one_more_byte_defers(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_bytes(_pad(b"# Proposals\n", ce.MAX_QUEUE_BYTES))
    assert queue.stat().st_size == ce.MAX_QUEUE_BYTES
    assert ce.prepare_one_stale_note(_config(vault), WS, vault, _plan()) == _prepared()

    queue.write_bytes(_pad(b"# Proposals\n", ce.MAX_QUEUE_BYTES + 1))
    assert ce.prepare_one_stale_note(_config(vault), WS, vault, _plan()) == ce.DeferredNote(
        reason=R_QUEUE_BIG
    )


def test_note_queue_and_state_bytes_are_unchanged(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    config = _config(vault)
    live_id = _file_live(vault)
    _raw_sidecar(vault, _proposal(live_id))
    before = _tree_digest(vault)

    ce.prepare_one_stale_note(config, WS, vault, _plan())

    assert _tree_digest(vault) == before
