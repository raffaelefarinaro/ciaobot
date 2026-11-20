"""The read-only preparation of one due stale note, with its related proposals.

Every test builds its vault under ``tmp_path`` and hands the preparation a config
whose only answer is that vault. Nothing here reads HOME, the live engine or a
real workspace.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

from ciao import curation_evidence as ce
from ciao import curation_run as cr
from ciao import memory_receipts as mr
from ciao import note_edit_proposals as nep
from ciao import proposal_tracking as pt

WS = "home"
REL = "People/Sofia.md"
NOTE_TEXT = "---\ntype: person\nupdated: 2024-01-05\n---\n\n# Sofia\n\nSomething durable.\n"
REV = mr.content_revision(NOTE_TEXT)
QUEUE_REL = f"{WS}/Workspace/Memory-Proposals.md"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    (vault / "People").mkdir()
    return vault


def _config(vault: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_vault_root=lambda _name: vault)


def _write(vault: Path, relative: str, data: bytes) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


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
    workspace: str = WS,
) -> nep.NoteEditProposal:
    return nep.NoteEditProposal(
        id=pid,
        workspace=workspace,
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
        proposal_id=pid,
        settled=settled,
    )


def _file_live(vault: Path, relative: str = REL, revision: str = REV, reason: str = "stale") -> str:
    """Queue one live note-edit bullet for this target; return its queue row id."""
    text, source = nep._bullet_fields(relative, nep.RESTAMP, revision, reason)
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


# ── Selection ──────────────────────────────────────────────────────────────


def test_an_empty_run_selects_no_note(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    empty = cr.RunPlan(planned=(), deferred=(), budget=cr.RunBudget())

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, empty)

    assert outcome == ce.NoSelectedNote()


def test_a_deferred_stale_item_is_never_chosen(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]
    item = _plan().planned[0]
    plan = cr.RunPlan(planned=(), deferred=(item,), budget=cr.RunBudget(max_items=0))

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, plan)

    assert outcome == ce.NoSelectedNote()


def test_an_item_with_two_matching_keys_is_not_selected(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]
    key = cr.item_key(cr.PASS_STALE_NOTE, REL)

    outcome = ce.prepare_one_stale_note(
        _config(vault), WS, vault, _plan(keys=(key, key))
    )

    assert outcome == ce.NoSelectedNote()


# ── Binding and the note itself ────────────────────────────────────────────


def test_a_selected_note_is_prepared_with_its_key_path_and_revision(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == ce.PreparedNote(
        item_key=cr.item_key(cr.PASS_STALE_NOTE, REL),
        relative_path=REL,
        revision=REV,
        references=(),
    )


def test_a_config_bound_to_another_vault_defers(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]
    other = _vault(tmp_path / "elsewhere")

    outcome = ce.prepare_one_stale_note(_config(other), WS, vault, _plan())

    assert isinstance(outcome, ce.DeferredNote)


def test_a_changed_note_defers(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, (NOTE_TEXT + "Edited.\n").encode("utf-8")).parents[1]

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert isinstance(outcome, ce.DeferredNote)


def test_a_missing_note_defers(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert isinstance(outcome, ce.DeferredNote)


def test_a_symlinked_note_defers(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    real = _write(tmp_path, "real.md", NOTE_TEXT.encode("utf-8"))
    os.symlink(real, vault / REL)

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert isinstance(outcome, ce.DeferredNote)


def test_a_bom_and_crlf_note_keeps_the_discovery_revision(tmp_path: Path) -> None:
    raw = b"\xef\xbb\xbf" + NOTE_TEXT.replace("\n", "\r\n").encode("utf-8")
    vault = _write(_vault(tmp_path), REL, raw).parents[1]
    revision = mr.content_revision(raw.decode("utf-8"))

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan(revision=revision))

    assert isinstance(outcome, ce.PreparedNote)
    assert outcome.revision == revision


def test_the_note_read_ceiling_is_exactly_64_kib(tmp_path: Path) -> None:
    limit = 64 * 1024
    exact = NOTE_TEXT + "x" * (limit - len(NOTE_TEXT.encode("utf-8")))
    vault = _write(_vault(tmp_path), REL, exact.encode("utf-8")).parents[1]
    assert len(exact.encode("utf-8")) == limit
    revision = mr.content_revision(exact)

    at_limit = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan(revision=revision))
    assert isinstance(at_limit, ce.PreparedNote)

    _write(vault, REL, (exact + "x").encode("utf-8"))
    over = ce.prepare_one_stale_note(
        _config(vault), WS, vault, _plan(revision=mr.content_revision(exact + "x"))
    )
    assert isinstance(over, ce.DeferredNote)


# ── Related proposals ──────────────────────────────────────────────────────


def test_open_and_settled_references_for_the_exact_target(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]
    config = _config(vault)
    open_id = _file_live(vault)
    nep.write_sidecar(config, _proposal(open_id))
    settled_id = "s" * 16
    nep.write_sidecar(
        config, _proposal(settled_id, settled="2026-11-19T00:00:00Z", revision="b" * 64)
    )
    nep.write_sidecar(config, _proposal("u" * 16, relative="People/Other.md"))

    outcome = ce.prepare_one_stale_note(config, WS, vault, _plan())

    assert isinstance(outcome, ce.PreparedNote)
    by_id = {ref.sidecar_id: ref for ref in outcome.references}
    assert set(by_id) == {open_id, settled_id}
    assert by_id[open_id].status == "open"
    assert by_id[open_id].proposal_id == open_id
    assert by_id[open_id].revision == "current"
    assert by_id[settled_id].status == "settled"
    assert by_id[settled_id].revision == "historical"


def test_an_unsettled_sidecar_without_a_live_row_is_an_orphan(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]
    config = _config(vault)
    nep.write_sidecar(config, _proposal("o" * 16))

    outcome = ce.prepare_one_stale_note(config, WS, vault, _plan())

    assert isinstance(outcome, ce.PreparedNote)
    assert outcome.references == ()


def test_a_stale_bullet_does_not_reopen_a_settled_record(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]
    config = _config(vault)
    live_id = _file_live(vault)
    nep.write_sidecar(config, _proposal(live_id, settled="2026-11-19T00:00:00Z"))

    outcome = ce.prepare_one_stale_note(config, WS, vault, _plan())

    assert isinstance(outcome, ce.PreparedNote)
    assert [(ref.status, ref.proposal_id) for ref in outcome.references] == [
        ("settled", live_id)
    ]


def test_a_live_bullet_without_a_readable_sidecar_defers(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]
    _file_live(vault)

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert isinstance(outcome, ce.DeferredNote)


def test_a_malformed_sidecar_defers(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]
    store = vault.joinpath(*nep.SIDECAR_RELATIVE)
    store.mkdir(parents=True)
    (store / "broken.json").write_text("{not json", encoding="utf-8")

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert isinstance(outcome, ce.DeferredNote)


def test_a_store_over_the_file_count_limit_defers(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]
    store = vault.joinpath(*nep.SIDECAR_RELATIVE)
    store.mkdir(parents=True)
    for index in range(ce.MAX_SIDECARS + 1):
        (store / f"f{index:04d}.json").write_text("{}", encoding="utf-8")

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert isinstance(outcome, ce.DeferredNote)


def test_more_than_twenty_references_defers(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]
    config = _config(vault)
    for index in range(ce.MAX_REFERENCES + 1):
        nep.write_sidecar(
            config,
            _proposal(f"{index:016d}", settled="2026-11-19T00:00:00Z"),
        )

    outcome = ce.prepare_one_stale_note(config, WS, vault, _plan())

    assert isinstance(outcome, ce.DeferredNote)


def test_an_absent_store_means_no_proposals(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]

    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, _plan())

    assert outcome == ce.PreparedNote(
        item_key=cr.item_key(cr.PASS_STALE_NOTE, REL),
        relative_path=REL,
        revision=REV,
        references=(),
    )


def test_note_queue_and_state_bytes_are_unchanged(tmp_path: Path) -> None:
    vault = _write(_vault(tmp_path), REL, NOTE_TEXT.encode("utf-8")).parents[1]
    config = _config(vault)
    live_id = _file_live(vault)
    nep.write_sidecar(config, _proposal(live_id))
    before = _tree_digest(vault)

    ce.prepare_one_stale_note(config, WS, vault, _plan())

    assert _tree_digest(vault) == before
