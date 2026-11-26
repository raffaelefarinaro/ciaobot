"""Source eligibility under the admission fence (#3619).

The check is a step inside `admit_effect`: it runs under the registry owner and the
evidence claim, again right before the effect, and refuses a withdrawn, revoked,
stale-generation, foreign-workspace or changed-bytes source. The effect is a
recording callback; the artifacts and evidence are synthetic.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

import pytest

from ciao import share_admission as sa
from ciao import share_evidence as se
from ciao import share_proposals, share_publish, shared_brain, shared_brain_mapping
from ciao.workspaces import WorkspaceRegistryBusy, acquire_workspace_registry, workspace_registry_claim
from tests.test_share_admission import (  # noqa: F401 - fixtures and helpers
    AUDIENCE, GRANT, LIFETIME, WORKSPACE, Env, _child, _log, _revoke, _wait_for, _withdraw, env, install_grant, linked,
)


def _grant_edit(env: Env, **changes: Any) -> None:
    with se.evidence_claim(env.config, workspace=WORKSPACE):
        state = se._load_state(env.config, WORKSPACE)
        state["grants"][GRANT].update(changes)
        se._save_state(env.config, WORKSPACE, state)


def _evidence_file(env: Env) -> Path:
    key = se._evidence_key("wsl_home_lifetime_0001", "proj", "doc", b'{"decision":"Launch"}')
    return se._evidence_dir(env.config, WORKSPACE) / (key + ".json")


def _tamper_bytes(env: Env) -> None:
    """Rewrite the confirmed snapshot outside any claim: bytes that are not the confirmed ones."""
    path = _evidence_file(env)
    data = json.loads(path.read_text())
    data["canonical_bytes"] = '{"decision":"Leak"}'
    path.write_text(json.dumps(data))


def _generation(env: Env) -> None:
    _grant_edit(env, consent_generation=2)


def _foreign_workspace(env: Env) -> None:
    _grant_edit(env, workspace="other")


def _foreign_lifetime(env: Env) -> None:
    _grant_edit(env, workspace_lifetime_id="wsl_other_lifetime_0002")


SOURCE_CASES: list[tuple[str, Callable[[Env], None], str]] = [
    ("withdrawn", _withdraw, "source_withdrawn"),
    ("revoked", _revoke, "source_revoked"),
    ("stale-generation", _generation, "source_generation_stale"),
    ("foreign-workspace", _foreign_workspace, "source_foreign"),
    ("foreign-lifetime", _foreign_lifetime, "source_foreign"),
    ("changed-bytes", _tamper_bytes, "source_changed"),
]


def _admit_in_thread(env: Env, effect: Any = "default") -> tuple[threading.Thread, list[sa.AdmissionOutcome]]:
    outcome: list[sa.AdmissionOutcome] = []
    worker = threading.Thread(target=lambda: outcome.append(env.admit(effect)))
    worker.start()
    return worker, outcome


# --- the step refuses every ineligible source, with zero effects -------------------------

def test_a_confirmed_source_reaches_the_callback_while_the_fence_is_held(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    real = se.check_source_eligibility
    seen: list[bool] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        try:
            acquire_workspace_registry(env.config, blocking=False).close()
            seen.append(False)
        except WorkspaceRegistryBusy:
            seen.append(True)  # the registry owner is held while the source is read
        return real(*args, **kwargs)

    monkeypatch.setattr(se, "check_source_eligibility", spy)
    assert env.admit().code == "applied" and len(env.effects) == 1
    assert seen == [True, True]  # once at admission, once right before the effect


@pytest.mark.parametrize(("name", "mutate", "code"), SOURCE_CASES, ids=[item[0] for item in SOURCE_CASES])
def test_an_ineligible_source_refuses_with_zero_effects_and_no_attempt(
    env: Env, name: str, mutate: Callable[[Env], None], code: str,
) -> None:
    mutate(env)
    assert env.admit().code == code
    assert env.admit(None).code == code  # the eligibility-only call agrees
    assert env.effects == [] and env.attempts() == []


@pytest.mark.parametrize(("name", "mutate", "code"), SOURCE_CASES, ids=[item[0] for item in SOURCE_CASES])
def test_barrier_between_the_artifact_read_and_the_registry_owner(
    env: Env, monkeypatch: pytest.MonkeyPatch, name: str, mutate: Callable[[Env], None], code: str,
) -> None:
    reached, resume = threading.Event(), threading.Event()
    real = sa.acquire_workspace_registry

    def paused(config: Any, **kwargs: Any) -> Any:
        reached.set()
        assert resume.wait(10)
        return real(config, **kwargs)

    monkeypatch.setattr(sa, "acquire_workspace_registry", paused)
    worker, outcome = _admit_in_thread(env)
    assert reached.wait(10)
    mutate(env)
    resume.set()
    worker.join(20)
    assert [item.code for item in outcome] == [code] and env.effects == []


def test_barrier_between_retries_the_source_is_read_again(env: Env) -> None:
    results = iter([sa.EffectResult("rejected", retryable=True), sa.EffectResult("applied", receipt="r")])

    def flaky(admitted: sa.AdmittedEffect) -> sa.EffectResult:
        env.effects.append(admitted.attempt_id)
        return next(results)

    assert env.admit(flaky).code == "rejected"
    _withdraw(env)
    assert env.admit(flaky).code == "source_withdrawn" and len(env.effects) == 1
    assert [item["status"] for item in env.attempts()] == ["rejected"]


def test_a_withdrawal_waits_for_an_admitted_write_and_nothing_starts_after_it(env: Env) -> None:
    inside, release = threading.Event(), threading.Event()

    def slow(admitted: sa.AdmittedEffect) -> sa.EffectResult:
        inside.set()
        assert release.wait(10)
        env.effects.append(admitted.attempt_id)
        return sa.EffectResult("applied", receipt="r")

    worker, outcome = _admit_in_thread(env, slow)
    assert inside.wait(10)
    done = threading.Event()
    withdrawer = threading.Thread(target=lambda: (_withdraw(env), done.set()))
    withdrawer.start()
    assert not done.wait(0.3)
    release.set()
    worker.join(10), withdrawer.join(10)
    assert done.is_set() and [item.code for item in outcome] == ["applied"]
    assert env.admit().code == "already_applied" and len(env.effects) == 1


# --- the re-read right before the effect --------------------------------------------------

def _tamper_after_journal(
    env: Env, monkeypatch: pytest.MonkeyPatch, tamper: Callable[[Env], None] = _tamper_bytes,
) -> list[int]:
    real = share_publish._write_state
    calls: list[int] = []

    def journal_then_tamper(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            tamper(env)  # the gap between the durable journal and the effect
        return result

    monkeypatch.setattr(share_publish, "_write_state", journal_then_tamper)
    return calls


def test_bytes_changed_after_the_journal_stop_the_effect_and_close_the_attempt(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    _tamper_after_journal(env, monkeypatch)
    outcome = env.admit()
    assert outcome.code == "source_changed" and env.effects == []
    attempts = env.attempts()
    assert [item["status"] for item in attempts] == ["rejected"] and attempts[0]["retryable"] is True
    # Still tampered: a later attempt refuses before any journal entry.
    assert env.admit().code == "source_changed" and len(env.attempts()) == 1 and env.effects == []


def test_a_lost_close_record_after_a_refused_recheck_fails_closed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    real = share_publish._write_state
    calls: list[int] = []

    def journal_tamper_then_fail(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        result = real(*args, **kwargs)
        _tamper_bytes(env)
        return result

    monkeypatch.setattr(share_publish, "_write_state", journal_tamper_then_fail)
    assert env.admit().code == "source_changed" and env.effects == []
    monkeypatch.setattr(share_publish, "_write_state", real)
    assert env.attempts()[0]["status"] == "admitted"
    assert env.admit().code == "attempt_unresolved" and env.effects == []


# --- mutation checks: drop a check and a test must notice ----------------------------------

def test_mutation_dropping_the_recheck_lets_a_post_journal_tamper_through(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    real = se.check_source_eligibility
    calls: list[int] = []

    def first_only(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs) if len(calls) == 1 else {}

    monkeypatch.setattr(se, "check_source_eligibility", first_only)
    _tamper_after_journal(env, monkeypatch)
    assert env.admit().code == "applied" and len(env.effects) == 1  # the mutant writes; the real code does not


@pytest.mark.parametrize(("name", "mutate", "code"), SOURCE_CASES, ids=[item[0] for item in SOURCE_CASES])
def test_mutation_dropping_the_step_changes_every_source_refusal(
    env: Env, monkeypatch: pytest.MonkeyPatch, name: str, mutate: Callable[[Env], None], code: str,
) -> None:
    monkeypatch.setattr(se, "check_source_eligibility", lambda *a, **k: {})
    mutate(env)
    assert env.admit().code != code


def test_mutation_content_addressing_is_only_enforced_by_the_step(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant, evidence file and signed row agree with each other, yet the key is not the bytes' address."""
    monkeypatch.setattr(se, "_evidence_key", lambda *args: "0" * 64)
    assert env.admit().code == "source_changed" and env.effects == []
    monkeypatch.setattr(se, "check_source_eligibility", lambda *a, **k: {})
    assert env.admit().code == "applied"


def test_a_fresh_grant_does_not_revive_the_old_artifact(env: Env) -> None:
    _revoke(env)
    install_grant(env.config, "grant-two")
    assert env.admit().code == "source_revoked" and env.effects == []


# --- two OS processes ------------------------------------------------------------------------

def test_two_processes_a_withdrawal_waits_for_the_other_process_write_then_nothing_starts(env: Env) -> None:
    signals = env.tmp / "src-hold"
    child = _child(env, "hold", signals)
    _wait_for(signals / "inside")
    done = threading.Event()
    thread = threading.Thread(target=lambda: (_withdraw(env), done.set()))
    thread.start()
    assert not done.wait(0.5)
    (signals / "release").write_text("1")
    thread.join(20)
    assert done.is_set() and child.communicate(timeout=20)[0].split() == ["applied"]
    later = _child(env, "hold", env.tmp / "src-after")
    assert later.communicate(timeout=30)[0].split() in (["already_applied"], ["source_withdrawn"])
    assert _log(env.tmp / "src-after") == [] and len(_log(signals)) == 1


@pytest.mark.parametrize(("name", "mutate", "code"), SOURCE_CASES[:2] + SOURCE_CASES[-1:], ids=["withdrawn", "revoked", "changed-bytes"])
def test_two_processes_a_source_made_ineligible_here_writes_nothing_there(
    env: Env, name: str, mutate: Callable[[Env], None], code: str,
) -> None:
    mutate(env)
    signals = env.tmp / ("src-" + name)
    child = _child(env, "hold", signals)
    assert child.communicate(timeout=30)[0].split() == [code]
    assert _log(signals) == [] and env.attempts() == []


# --- a standing (stream entry) source -------------------------------------------------------

STANDING = "grant-standing"


def install_standing(config: Any, grant_id: str = STANDING) -> dict[str, Any]:
    """A live standing grant over a real operator-signed decision, and its signed authorization."""
    link = config.links[0]
    with se.evidence_claim(config, workspace=WORKSPACE):
        state = se._load_state(config, WORKSPACE)
        state["grants"][grant_id] = {
            "grant_id": grant_id, "active": True, "type": "standing", "source_kind": "stream", "workspace": WORKSPACE,
            "workspace_lifetime_id": LIFETIME, "project_id": "proj", "source_id": "doc", "start_revision": 0,
            "consent_generation": 1, "audience": AUDIENCE, "producers": ["memory_extraction"],
            "field_mask": ["decision"], "evidence_key": None, "history": [],
            "brain": {"name": link.name, "identity": link.identity, "lifetime_id": link.lifetime_id},
        }
        se._save_state(config, WORKSPACE, state)
    se.record_operator_decision(
        config, workspace=WORKSPACE, project_id="proj", decision="Launch", reason="", effective_date="2026-11-26",
        session="operator-session")
    entry = se._load_state(config, WORKSPACE)["streams"]["proj"]["entries"][-1]
    canonical = se._canonical({"decision": "Launch"}).decode("utf-8")
    row = {
        "grant_id": grant_id, "grant_type": "standing", "entry_id": entry["entry_id"], "entry_revision": entry["revision"],
        "snapshot_id": None, "consent_generation": 1, "audience": AUDIENCE, "project_id": "proj", "source_id": "doc",
        "producer": "memory_extraction", "relation_kind": "memory_checkpoint", "relation_id": "run-1",
        "relation_evidence": "proof-1", "relation_expires_at": "2099-01-01T00:00:00Z", "field_mask": ["decision"],
        "canonical_bytes": canonical, "digest": se._digest(canonical.encode("utf-8")), "recorded_at": "2026-11-26T00:00:00Z",
    }
    payload = {"workspace": WORKSPACE, "workspace_lifetime_id": LIFETIME, "brain_name": link.name,
               "brain_identity": link.identity, "brain_lifetime_id": link.lifetime_id, "row": row}
    return {"schema": se.SCHEMA, "payload": payload, "signature": se._authorization_signature(config, payload)}


def redraft(env: Env, authorization: dict[str, Any], path: str, key: str) -> Env:
    """A fresh private artifact for another destination path, bound to `authorization`."""
    link = env.config.links[0]
    with shared_brain.checkout_claims_nowait([link.path], env.config), workspace_registry_claim(env.config) as owner:
        snapshot = shared_brain_mapping.resolve_workspace_destination(
            env.config, WORKSPACE, link.name, note_type="decision", relative_path=path, operation="create", owner=owner)
        assert snapshot is not None
        captured = share_proposals._qualify_owned(env.config, WORKSPACE, link, path, "create", owner)
        draft = share_proposals._draft_share(
            env.config, workspace=WORKSPACE, brain=link.name, relative_path=path, operation="create",
            proposed=b"Launch\n", request_key=key, request_fingerprint=key, checkout_claimed=True,
            registry_owner=owner, expected_qualification=captured,
            candidate_binding={"authorization": authorization, **asdict(snapshot)})
    assert isinstance(draft, share_proposals.ShareDraft)
    return replace(env, authorization=authorization, proposal_id=draft.proposal_id, token=draft.artifact["review_token"])


@pytest.fixture
def standing(env: Env) -> Env:
    return redraft(env, install_standing(env.config), "decisions/standing.md", "std")


def _entry_edit(env: Env, change: Callable[[dict[str, Any], list[dict[str, Any]]], None]) -> None:
    """An out-of-band edit of the private state: no claim, so it also works inside the fence.

    The real writers (supersede, revoke) wait for the fence and cannot run in the
    journal-to-effect gap; this is what a writer that bypasses it would do.
    """
    state = se._load_state(env.config, WORKSPACE)
    entries = state["streams"]["proj"]["entries"]
    change(entries[-1], entries)
    se._save_state(env.config, WORKSPACE, state)


def _supersede(env: Env) -> None:
    _entry_edit(env, lambda entry, entries: entry.update(superseded=True))


def _drop_entry(env: Env) -> None:
    _entry_edit(env, lambda entry, entries: entries.clear())


def _revision_mismatch(env: Env) -> None:
    _entry_edit(env, lambda entry, entries: entry.update(revision=entry["revision"] + 5))


def _text_without_revision_bump(env: Env) -> None:
    _entry_edit(env, lambda entry, entries: entry.update(decision="Leak"))


def _boundary_moved(env: Env) -> None:
    _grant_edit_for(env, STANDING, start_revision=5)


def _grant_edit_for(env: Env, grant_id: str, **changes: Any) -> None:
    state = se._load_state(env.config, WORKSPACE)
    state["grants"][grant_id].update(changes)
    se._save_state(env.config, WORKSPACE, state)


def _mask_changed(env: Env) -> None:
    _grant_edit_for(env, STANDING, field_mask=["decision", "reason"])


STANDING_CASES: list[tuple[str, Callable[[Env], None]]] = [
    ("superseded", _supersede),
    ("missing", _drop_entry),
    ("revision-mismatch", _revision_mismatch),
    ("text-without-revision-bump", _text_without_revision_bump),
    ("before-grant-boundary", _boundary_moved),
    ("mask-changed", _mask_changed),
]


def test_a_live_standing_entry_reaches_the_callback_once(standing: Env) -> None:
    assert standing.admit().code == "applied" and len(standing.effects) == 1


@pytest.mark.parametrize(("name", "mutate"), STANDING_CASES, ids=[item[0] for item in STANDING_CASES])
def test_a_changed_standing_entry_refuses_with_zero_effects(standing: Env, name: str, mutate: Callable[[Env], None]) -> None:
    mutate(standing)
    assert standing.admit().code == "source_changed"
    assert standing.admit(None).code == "source_changed"
    assert standing.effects == [] and standing.attempts() == []


@pytest.mark.parametrize(("name", "mutate"), STANDING_CASES, ids=[item[0] for item in STANDING_CASES])
def test_a_standing_entry_changed_after_the_journal_stops_the_effect(
    standing: Env, monkeypatch: pytest.MonkeyPatch, name: str, mutate: Callable[[Env], None],
) -> None:
    _tamper_after_journal(standing, monkeypatch, mutate)
    assert standing.admit().code == "source_changed" and standing.effects == []
    assert [item["status"] for item in standing.attempts()] == ["rejected"]


@pytest.mark.parametrize(("name", "mutate"), STANDING_CASES, ids=[item[0] for item in STANDING_CASES])
def test_mutation_dropping_the_standing_recheck_lets_the_change_through(
    standing: Env, monkeypatch: pytest.MonkeyPatch, name: str, mutate: Callable[[Env], None],
) -> None:
    real = se.check_source_eligibility
    calls: list[int] = []

    def first_only(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs) if len(calls) == 1 else {}

    monkeypatch.setattr(se, "check_source_eligibility", first_only)
    _tamper_after_journal(standing, monkeypatch, mutate)
    assert standing.admit().code == "applied"  # the mutant writes; the real code does not


@pytest.mark.parametrize(("name", "mutate"), STANDING_CASES[:1] + STANDING_CASES[3:4], ids=["superseded", "text-without-revision-bump"])
def test_mutation_dropping_the_standing_branch_is_noticed(
    standing: Env, monkeypatch: pytest.MonkeyPatch, name: str, mutate: Callable[[Env], None],
) -> None:
    """With the whole step gone only the older guard stops it, and under another code."""
    monkeypatch.setattr(se, "check_source_eligibility", lambda *a, **k: {})
    mutate(standing)
    assert standing.admit().code != "source_changed"


# --- fresh consent and fresh drafting ---------------------------------------------------------

def test_after_revoke_fresh_consent_and_a_fresh_draft_are_eligible_again(env: Env) -> None:
    _revoke(env)
    assert env.admit().code == "source_revoked"
    fresh = redraft(env, install_grant(env.config, "grant-two"), "decisions/d2.md", "fresh")
    assert fresh.admit(None).code == "eligible"
    assert fresh.admit().code == "applied" and len(env.effects) == 1
    assert env.admit().code == "source_revoked" and len(env.effects) == 1  # the old artifact stays dead
