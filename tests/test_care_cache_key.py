"""The judgment cache-key model: completeness, sensitivity and parity, with no model.

Every vault is built under ``tmp_path``. Nothing here reads HOME, the live engine,
a provider or the clock. Keys are compared as produced by ``judgment_key`` on
real ``prepare_one_stale_note`` output, so a key that forgets a dependency shows
up as an equal key where the prepared evidence differs.
"""

from __future__ import annotations

import ast
import json
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import care_cache_key as ck
from ciao import curation_evidence as ce
from ciao import curation_judgments as cj
from ciao import curation_run as cr
from ciao import memory_receipts as mr
from ciao import note_edit_proposals as nep
from ciao import proposal_tracking as pt

REPO = Path(__file__).resolve().parents[1]
WS = "home"
REL = "People/Sofia.md"
OTHER = "People/Mateo.md"
NOTE_TEXT = "---\ntype: person\nupdated: 2024-01-05\n---\n\n# Sofia\n\nSomething durable.\n"
NOTE_TEXT_2 = NOTE_TEXT + "\nA new durable line.\n"
REV = mr.content_revision(NOTE_TEXT)
REV_2 = mr.content_revision(NOTE_TEXT_2)
OTHER_REV = mr.content_revision("# Mateo\n")
PROVIDER = "test-provider"
MODEL = "test-model"
QUEUE_REL = f"{WS}/Workspace/Memory-Proposals.md"


def _vault(base: Path, text: str = NOTE_TEXT) -> Path:
    vault = base / "vault"
    (vault / "Workspace").mkdir(parents=True)
    note = vault / REL
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes(text.encode("utf-8"))
    return vault


def _config(vault: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_vault_root=lambda _name: vault)


def _store(vault: Path) -> Path:
    store = vault.joinpath(*nep.SIDECAR_RELATIVE)
    store.mkdir(parents=True, exist_ok=True)
    return store


def _plan(revision: str = REV) -> cr.RunPlan:
    target = cr.StaleNoteTarget(relative_path=REL, revision=revision)
    item = cr.WorklistItem(
        pass_id=cr.PASS_STALE_NOTE,
        label="Sofia",
        reason=f"revision {revision}",
        keys=(cr.item_key(cr.PASS_STALE_NOTE, REL),),
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
        assert len(data) <= pad_to
        data = data + b" " * (pad_to - len(data))
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


def _open_reference(vault: Path, sid: str = "s-open", reason: str = "stale") -> str:
    row = _file_live(vault, reason=reason)
    _raw_sidecar(vault, _proposal(sid, proposal_id=row))
    return row


def _prepared(refs: tuple[ce.ProposalReference, ...] = (), revision: str = REV) -> ce.PreparedNote:
    return ce.PreparedNote(
        item_key=cr.item_key(cr.PASS_STALE_NOTE, REL),
        relative_path=REL,
        revision=revision,
        references=refs,
    )


_REF = ce.ProposalReference(
    sidecar_id="s-open",
    proposal_id="row-1",
    status="open",
    revision="current",
    operation=nep.REPLACE,
    expected_revision=REV,
    settled_at="",
    accepted=False,
)


def _key(prepared: ce.PreparedOutcome, *, workspace: str = WS, provider: str = PROVIDER, model: str = MODEL):
    result = ck.judgment_key(prepared, workspace=workspace, provider=provider, model=model)
    assert isinstance(result, str), result
    return result


def _inputs(prepared: ce.PreparedNote, *, workspace: str = WS, provider: str = PROVIDER, model: str = MODEL) -> ck.JudgmentKeyInputs:
    result = ck.key_inputs(prepared, workspace=workspace, provider=provider, model=model)
    assert isinstance(result, ck.JudgmentKeyInputs), result
    return result


# ── Field coverage ─────────────────────────────────────────────────────────


def _with_ref(prepared: ce.PreparedNote, **changes: object) -> ce.PreparedNote:
    return replace(prepared, references=(replace(_REF, **changes),))


MUTATIONS = {
    "PreparedNote.item_key": lambda p: replace(p, item_key=p.item_key + "x"),
    "PreparedNote.relative_path": lambda p: replace(p, relative_path=OTHER),
    "PreparedNote.revision": lambda p: replace(p, revision=REV_2),
    "PreparedNote.references": lambda p: replace(
        p, references=(replace(_REF, sidecar_id="s-other"),)
    ),
    "ProposalReference.sidecar_id": lambda p: _with_ref(p, sidecar_id="s-other"),
    "ProposalReference.proposal_id": lambda p: _with_ref(p, proposal_id="row-2"),
    "ProposalReference.status": lambda p: _with_ref(p, status="settled"),
    "ProposalReference.revision": lambda p: _with_ref(p, revision="historical"),
    "ProposalReference.operation": lambda p: _with_ref(p, operation="insert"),
    "ProposalReference.expected_revision": lambda p: _with_ref(p, expected_revision=REV_2),
    "ProposalReference.settled_at": lambda p: _with_ref(p, settled_at="2026-11-19T00:00:00Z"),
    "ProposalReference.accepted": lambda p: _with_ref(p, accepted=True),
}


def test_every_field_of_the_prepared_note_and_its_references_has_a_mutation_row() -> None:
    expected = {f"PreparedNote.{f.name}" for f in fields(ce.PreparedNote)} | {
        f"ProposalReference.{f.name}" for f in fields(ce.ProposalReference)
    }
    assert set(MUTATIONS) == expected


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_field_changes_the_prepared_digest(name: str) -> None:
    base = _prepared((_REF,))
    mutated = MUTATIONS[name](base)
    assert mutated != base, f"the {name} row does not change the prepared note"
    assert _inputs(mutated).prepared_digest != _inputs(base).prepared_digest


# ── Single-dependency sensitivity ──────────────────────────────────────────


def _patch(monkeypatch: pytest.MonkeyPatch, module: object, name: str, value: object) -> None:
    monkeypatch.setattr(module, name, value)


def _change_note_revision(_mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    return _prepared((_REF,), revision=REV_2), {}


def _add_reference(_mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    extra = replace(_REF, sidecar_id="s-b", proposal_id="row-b")
    return _prepared((_REF, extra)), {}


def _remove_reference(_mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    return _prepared(()), {}


def _status_changed(_mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    return _prepared((replace(_REF, status="settled", proposal_id=""),)), {}


def _settled(_mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    return _prepared((replace(_REF, settled_at="2026-11-19T00:00:00Z"),)), {}


def _expected_revision(_mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    return _prepared((replace(_REF, expected_revision=REV_2),)), {}


def _accepted(_mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    return _prepared((replace(_REF, accepted=True),)), {}


def _workspace(_mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    return _prepared((_REF,)), {"workspace": "other"}


def _provider(_mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    return _prepared((_REF,)), {"provider": "other-provider"}


def _model(_mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    return _prepared((_REF,)), {"model": "other-model"}


def _schema_version(mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    _patch(mp, cj, "SCHEMA_VERSION", cj.SCHEMA_VERSION + 1)
    return _prepared((_REF,)), {}


def _system_prompt(mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    _patch(mp, cj, "_SYSTEM_PROMPT", cj._SYSTEM_PROMPT + " ")
    return _prepared((_REF,)), {}


def _prompt_field_cap(mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    _patch(mp, cj, "MAX_PROMPT_FIELD_CHARS", 10)
    return _prepared((_REF,)), {}


def _max_references_in_judgments(mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    _patch(mp, cj, "MAX_REFERENCES", cj.MAX_REFERENCES + 1)
    return _prepared((_REF,)), {}


def _max_references_in_evidence(mp: pytest.MonkeyPatch) -> tuple[ce.PreparedOutcome, dict[str, str]]:
    _patch(mp, ce, "MAX_REFERENCES", ce.MAX_REFERENCES + 1)
    return _prepared((_REF,)), {}


CHANGES = {
    "note revision": _change_note_revision,
    "reference added": _add_reference,
    "reference removed": _remove_reference,
    "reference status": _status_changed,
    "reference settled": _settled,
    "expected revision": _expected_revision,
    "accepted": _accepted,
    "workspace": _workspace,
    "provider": _provider,
    "model": _model,
    "schema version": _schema_version,
    "system prompt": _system_prompt,
    "prompt field cap": _prompt_field_cap,
    "MAX_REFERENCES in curation_judgments": _max_references_in_judgments,
    "MAX_REFERENCES in curation_evidence": _max_references_in_evidence,
}


@pytest.mark.parametrize("name", sorted(CHANGES))
def test_each_single_dependency_changes_the_key(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _key(_prepared((_REF,)))
    prepared, kwargs = CHANGES[name](monkeypatch)
    assert _key(prepared, **kwargs) != before


def test_run_and_batch_ids_do_not_change_the_key() -> None:
    prepared = _prepared((_REF,))
    key = _key(prepared)
    assert _key(prepared) == key
    # The prompt is built with neutral ids, so the per-attempt ids cannot reach the key.
    assert cj._build_prompt(prepared, run_id="run-a", batch_id="batch-a") != cj._build_prompt(
        prepared, run_id="run-b", batch_id="batch-b"
    )
    assert "run-a" not in ck.canonical_payload(_inputs(prepared))


# ── Real preparation fixtures ──────────────────────────────────────────────


def _outcome(vault: Path, *, plan: cr.RunPlan | None = None, config: SimpleNamespace | None = None):
    return ce.prepare_one_stale_note(
        config if config is not None else _config(vault),
        WS,
        vault,
        plan if plan is not None else _plan(),
    )


def _real_key(vault: Path, **kwargs: object) -> str | ck.Uncacheable:
    return ck.judgment_key(_outcome(vault, **kwargs), workspace=WS, provider=PROVIDER, model=MODEL)  # type: ignore[arg-type]


def _must_key(vault: Path, **kwargs: object) -> str:
    result = _real_key(vault, **kwargs)
    assert isinstance(result, str), result
    return result


def test_an_exact_repeat_of_preparation_gives_an_equal_key(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    _open_reference(vault)
    assert _must_key(vault) == _must_key(vault)


def test_no_selected_note_and_deferred_note_are_uncacheable(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    empty = cr.RunPlan(planned=(), deferred=(), budget=cr.RunBudget())
    outcome = ce.prepare_one_stale_note(_config(vault), WS, vault, empty)
    assert isinstance(outcome, ce.NoSelectedNote)
    assert isinstance(ck.judgment_key(outcome, workspace=WS, provider=PROVIDER, model=MODEL), ck.Uncacheable)

    deferred = ck.judgment_key(
        ce.DeferredNote(reason="the note changed since it was planned"),
        workspace=WS,
        provider=PROVIDER,
        model=MODEL,
    )
    assert deferred == ck.Uncacheable(reason="preparation deferred the note: the note changed since it was planned")


def test_a_note_edit_changes_the_key(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    before = _must_key(vault)
    (vault / REL).write_bytes(NOTE_TEXT_2.encode("utf-8"))
    assert _must_key(vault, plan=_plan(REV_2)) != before


def test_a_sidecar_added_changes_the_key(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    _open_reference(vault, sid="s-a", reason="first")
    before = _must_key(vault)
    _open_reference(vault, sid="s-b", reason="second")
    assert _must_key(vault) != before


def test_a_sidecar_changed_changes_the_key(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    row = _open_reference(vault)
    before = _must_key(vault)
    _raw_sidecar(vault, _proposal("s-open", proposal_id=row, revision=OTHER_REV))
    assert _must_key(vault) != before


def test_a_settled_sidecar_removed_changes_the_key(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    _raw_sidecar(vault, _proposal("s-old", settled="2026-11-19T10:00:00Z"))
    before = _must_key(vault)
    (_store(vault) / "s-old.json").unlink()
    assert _must_key(vault) != before


def test_an_open_sidecar_removed_is_uncacheable(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    _open_reference(vault)
    (_store(vault) / "s-open.json").unlink()
    assert isinstance(_real_key(vault), ck.Uncacheable)


def test_a_queue_row_settled_changes_the_key(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    row = _open_reference(vault)
    before = _must_key(vault)
    (vault / "Workspace" / "Memory-Proposals.md").write_text("", encoding="utf-8")
    _raw_sidecar(vault, _proposal("s-open", proposal_id=row, settled="2026-11-19T10:00:00Z"))
    assert _must_key(vault) != before


def test_an_orphan_sidecar_for_this_note_leaves_the_key_unchanged(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    _open_reference(vault)
    before = _must_key(vault)
    _raw_sidecar(vault, _proposal("s-orphan", proposal_id="no-such-row"))
    assert _must_key(vault) == before


def test_a_sidecar_for_another_note_leaves_the_key_unchanged(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    _open_reference(vault)
    before = _must_key(vault)
    other_row = _file_live(vault, relative=OTHER, revision=OTHER_REV, reason="mateo")
    _raw_sidecar(vault, _proposal("s-other", relative=OTHER, revision=OTHER_REV, proposal_id=other_row))
    assert _must_key(vault) == before


def test_a_workspace_bound_to_another_vault_is_uncacheable(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    result = _real_key(vault, config=_config(tmp_path / "elsewhere"))
    assert isinstance(result, ck.Uncacheable)


def test_an_unreadable_sidecar_is_uncacheable(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    _open_reference(vault)
    (_store(vault) / "junk.json").write_bytes(b"not a sidecar")
    assert isinstance(_real_key(vault), ck.Uncacheable)


def test_an_oversize_sidecar_is_uncacheable(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    _open_reference(vault)
    _raw_sidecar(vault, _proposal("s-big"), pad_to=ce.MAX_SIDECAR_BYTES + 1)
    assert isinstance(_real_key(vault), ck.Uncacheable)


def test_513_sidecars_are_uncacheable(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "base")
    store = _store(vault)
    for index in range(ce.MAX_SIDECARS + 1):
        (store / f"s{index:03d}.json").write_bytes(b"{}")
    assert isinstance(_real_key(vault), ck.Uncacheable)


# ── Refusals and incomplete keys ───────────────────────────────────────────


def test_an_empty_required_string_is_an_incomplete_key() -> None:
    prepared = _prepared((_REF,))
    for kwargs in (
        {"workspace": "", "provider": PROVIDER, "model": MODEL},
        {"workspace": WS, "provider": "  ", "model": MODEL},
        {"workspace": WS, "provider": PROVIDER, "model": ""},
        {"workspace": WS, "provider": PROVIDER, "model": "default"},
    ):
        with pytest.raises(ck.IncompleteKey):
            ck.judgment_key(prepared, **kwargs)


def test_an_empty_prepared_field_is_an_incomplete_key() -> None:
    with pytest.raises(ck.IncompleteKey):
        ck.judgment_key(_prepared(revision=""), workspace=WS, provider=PROVIDER, model=MODEL)


def test_a_non_string_is_an_incomplete_key() -> None:
    with pytest.raises(ck.IncompleteKey):
        ck.judgment_key(_prepared((_REF,)), workspace=WS, provider=3, model=MODEL)  # type: ignore[arg-type]


def test_a_bool_is_not_an_integer_in_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, cj, "SCHEMA_VERSION", True)
    with pytest.raises(ck.IncompleteKey):
        ck.judgment_key(_prepared((_REF,)), workspace=WS, provider=PROVIDER, model=MODEL)


def test_a_negative_count_is_an_incomplete_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ce, "MAX_SIDECARS", -1)
    with pytest.raises(ck.IncompleteKey):
        ck.judgment_key(_prepared((_REF,)), workspace=WS, provider=PROVIDER, model=MODEL)


def test_a_non_boolean_accepted_flag_is_an_incomplete_key() -> None:
    bad = replace(_REF, accepted=1)  # type: ignore[arg-type]
    with pytest.raises(ck.IncompleteKey):
        ck.judgment_key(_prepared((bad,)), workspace=WS, provider=PROVIDER, model=MODEL)


def test_incomplete_key_is_a_value_error() -> None:
    assert issubclass(ck.IncompleteKey, ValueError)


# ── Excluded names ─────────────────────────────────────────────────────────


def test_no_excluded_name_appears_in_the_canonical_payload() -> None:
    assert ck.EXCLUDED_FROM_KEY == frozenset(ck.EXCLUSION_REASONS)
    for name, reason in ck.EXCLUSION_REASONS.items():
        assert reason.strip(), f"{name} has no documented reason"
    payload = ck.canonical_payload(_inputs(_prepared((_REF,))))
    for name in ck.EXCLUDED_FROM_KEY:
        assert name not in payload, f"{name} leaked into the key payload"


def test_the_key_has_its_versioned_prefix() -> None:
    assert _key(_prepared((_REF,))).startswith("care-judgment-v1:")


# ── Parity ─────────────────────────────────────────────────────────────────


def _keyed(model: str = MODEL, prepared: ce.PreparedNote | None = None) -> ck.JudgmentKeyInputs:
    return _inputs(prepared if prepared is not None else _prepared((_REF,)), model=model)


def _rec(label: str, inputs: object, digest: str = "d1", validated: bool = True) -> ck.Record:
    return ck.Record(label=label, inputs=inputs, outcome_digest=digest, validated=validated)  # type: ignore[arg-type]


UNCACHEABLE = ck.Uncacheable(reason="preparation selected no note")


def test_a_consistent_set_passes() -> None:
    report = ck.check_parity(
        [
            _rec("A", _keyed(), "d1"),
            _rec("B", _keyed(), "d1"),
        ]
    )
    assert report.ok is True
    assert report.findings == ()
    assert (report.hits, report.misses, report.redundant_misses) == (1, 1, 0)


def test_a_key_that_misses_a_dependency_is_reported_as_incomplete() -> None:
    report = ck.check_parity([_rec("A", _keyed(), "d1"), _rec("B", _keyed(), "d2")])
    assert report.ok is False
    assert ck.IncompleteKeyFinding(first="A", second="B") in report.findings


def test_a_hit_with_a_different_outcome_is_a_stale_hit() -> None:
    report = ck.check_parity([_rec("A", _keyed(), "d1"), _rec("B", _keyed(), "d2")])
    assert ck.StaleHitFinding(label="B", stored_label="A") in report.findings
    assert report.hits == 1


def test_an_uncertain_keyed_outcome_is_reported_as_cached_uncertain() -> None:
    report = ck.check_parity([_rec("A", _keyed(), "invalid:bad", validated=False)])
    assert report.ok is False
    assert report.findings == (ck.CachedUncertainFinding(label="A"),)


def test_an_uncertain_uncacheable_outcome_is_not_a_finding() -> None:
    report = ck.check_parity([_rec("A", UNCACHEABLE, "deferred", validated=False)])
    assert report.ok is True
    assert report.findings == ()


def test_a_declared_change_that_leaves_the_key_unchanged_is_reported() -> None:
    report = ck.check_parity(
        [_rec("A", _keyed(), "d1"), _rec("B", _keyed(), "d1")],
        steps=[ck.Step(label="B", changed="model")],
    )
    assert report.ok is False
    assert report.findings == (
        ck.UnkeyedChangeFinding(label="B", changed="model", baseline="A"),
    )


def test_a_declared_change_that_moves_the_key_passes() -> None:
    report = ck.check_parity(
        [_rec("A", _keyed(), "d1"), _rec("B", _keyed(model="other-model"), "d1")],
        steps=[ck.Step(label="B", changed="model")],
    )
    assert report.ok is True


def test_different_keys_with_the_same_outcome_are_redundant_misses_and_still_pass() -> None:
    report = ck.check_parity(
        [_rec("A", _keyed(), "d1"), _rec("B", _keyed(model="other-model"), "d1")]
    )
    assert report.ok is True
    assert report.findings == ()
    assert (report.hits, report.misses, report.redundant_misses) == (0, 2, 1)


def test_the_report_is_deterministic_and_keeps_input_order() -> None:
    records = [
        _rec("A", _keyed(), "d1"),
        _rec("B", _keyed(), "d2"),
        _rec("C", _keyed(), "d3", validated=False),
    ]
    first = ck.check_parity(records, steps=[ck.Step(label="B", changed="model")])
    second = ck.check_parity(records, steps=[ck.Step(label="B", changed="model")])
    assert first == second
    assert first.findings[0] == ck.IncompleteKeyFinding(first="A", second="B")
    assert first.findings[-1] == ck.UnkeyedChangeFinding(label="B", changed="model", baseline="A")


def test_duplicate_labels_and_unknown_steps_are_caller_errors() -> None:
    with pytest.raises(ValueError):
        ck.check_parity([_rec("A", _keyed()), _rec("A", _keyed())])
    with pytest.raises(ValueError):
        ck.check_parity([_rec("A", _keyed())], steps=[ck.Step(label="Z", changed="model")])


# ── Module guard ───────────────────────────────────────────────────────────


def test_the_module_imports_no_provider_web_scheduler_clock_or_environment() -> None:
    tree = ast.parse((REPO / "ciao" / "care_cache_key.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    forbidden = ("ciao.providers", "ciao.web", "ciao.schedules", "os", "time", "datetime")
    for name in names:
        for bad in forbidden:
            assert not (name == bad or name.startswith(bad + ".")), f"forbidden import {name}"
