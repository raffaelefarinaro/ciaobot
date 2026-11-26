"""Source eligibility under the admission fence (#3619).

The check is a step inside `admit_effect`: it runs under the registry owner and the
evidence claim, again right before the effect, and refuses a withdrawn, revoked,
stale-generation, foreign-workspace or changed-bytes source. The effect is a
recording callback; the artifacts and evidence are synthetic.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from ciao import share_admission as sa
from ciao import share_evidence as se
from ciao import share_publish
from ciao.workspaces import WorkspaceRegistryBusy, acquire_workspace_registry
from tests.test_share_admission import (  # noqa: F401 - fixtures and helpers
    GRANT, WORKSPACE, Env, _child, _log, _revoke, _wait_for, _withdraw, env, install_grant, linked,
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

def _tamper_after_journal(env: Env, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    real = share_publish._write_state
    calls: list[int] = []

    def journal_then_tamper(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            _tamper_bytes(env)  # the gap between the durable journal and the effect
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
