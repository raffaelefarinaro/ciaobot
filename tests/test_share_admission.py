"""Admission fence and attempt journal for automatic shared-note effects (#3619).

The effect is a recording callback run under the real admission locks and journal,
with synthetic private evidence (#3614) and a real mapped destination (#3616).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from ciao import share_admission as sa
from ciao import share_evidence as se
from ciao import share_proposals, share_publish, shared_brain, shared_brain_guide, shared_brain_mapping
from ciao.config import SharedBrainLink, shared_brain_row, shared_brains_from_rows
from ciao.workspaces import (
    WorkspaceRegistryBusy,
    acquire_workspace_registry,
    replace_workspace_registry,
    workspace_registry_claim,
)
from tests.test_share_publish import Config as PublishConfig, linked  # noqa: F401 - fixture

WORKSPACE = "home"
LIFETIME = "wsl_home_lifetime_0001"
AUDIENCE = "github.com/x/notes"
GRANT = "grant-one"
PATH = "decisions/d1.md"
CANONICAL = '{"decision":"Launch"}'
ROOT = Path(__file__).resolve().parent.parent


class AdmissionConfig(PublishConfig):
    archived = False

    def workspace_names(self) -> list[str]:
        return [] if self.archived else super().workspace_names()

    def workspace(self, name: str | None) -> SimpleNamespace | None:
        if name != WORKSPACE or self.archived:
            return None
        return SimpleNamespace(shared_brains=self.links, lifetime_id=LIFETIME, _lifetime_persisted=True)


def open_config(root: Path) -> AdmissionConfig:
    """A fresh config over the same files: what a second engine process loads."""
    registry = root / "install" / ".runtime" / "workspaces.json"
    raw = registry.read_bytes()
    rows = json.loads(raw)
    links = shared_brains_from_rows(rows[0]["shared_brains"]) if rows else []
    config = AdmissionConfig(root, root / "vault", SharedBrainLink(name="gone", path="/gone", identity="gone"))
    config.links[:] = links
    config.archived = not rows
    config._workspace_registry_bytes = raw
    return config


def local_transport(origin: Path) -> Callable[..., tuple[int, bytes, bytes]]:
    def transport(folder: Path, *args: str, limit: int = 4096, timeout_s: float = 5) -> tuple[int, bytes, bytes]:
        rewritten = tuple(str(origin) if arg == "origin" else arg for arg in args)
        proc = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", "-c", "protocol.file.allow=always",
             "-C", str(folder), *rewritten], check=False, capture_output=True, timeout=5)
        return proc.returncode, proc.stdout[:limit], proc.stderr[:limit]
    return transport


def _write_registry(config: AdmissionConfig) -> None:
    rows = [{"name": WORKSPACE, "vault_root": str(config.vault), "lifetime_id": LIFETIME,
             "shared_brains": [shared_brain_row(link) for link in config.links]}]
    path = config.state_path.parent / "workspaces.json"
    path.write_text(json.dumps(rows))
    config._workspace_registry_bytes = path.read_bytes()


def edit_registry(config: AdmissionConfig, mutate: Callable[[AdmissionConfig], None]) -> None:
    """What a registry writer (unlink, archive, policy edit) does under the owner."""
    with acquire_workspace_registry(config, blocking=True) as owner:
        mutate(config)
        rows = [] if config.archived else [{
            "name": WORKSPACE, "vault_root": str(config.vault), "lifetime_id": LIFETIME,
            "shared_brains": [shared_brain_row(link) for link in config.links]}]
        payload = json.dumps(rows).encode()
        replace_workspace_registry(config, payload, expected=owner.current, owner=owner)
        config._workspace_registry_bytes = payload


def install_grant(config: AdmissionConfig, grant_id: str = GRANT, *, audience: str = AUDIENCE) -> dict[str, Any]:
    """A live snapshot grant plus the signed authorization a producer would mint."""
    link = config.links[0]
    key = se._evidence_key(LIFETIME, "proj", "doc", CANONICAL.encode())
    digest = hashlib.sha256(CANONICAL.encode()).hexdigest()
    mask = ["decision"]
    with se.evidence_claim(config, workspace=WORKSPACE) as claim:
        state = se._load_state(config, WORKSPACE)
        state["grants"][grant_id] = {
            "grant_id": grant_id, "active": True, "type": "snapshot", "workspace": WORKSPACE,
            "workspace_lifetime_id": LIFETIME, "project_id": "proj", "source_id": "doc",
            "consent_generation": 1, "audience": audience, "producers": ["memory_extraction"],
            "field_mask": mask, "evidence_key": key, "history": [],
            "brain": {"name": link.name, "identity": link.identity, "lifetime_id": link.lifetime_id},
        }
        directory = se._evidence_dir(config, WORKSPACE)
        se._write_private(directory / (key + ".json"), json.dumps({
            "schema": se.SCHEMA, "workspace": WORKSPACE, "workspace_lifetime_id": LIFETIME, "project_id": "proj", "source_id": "doc",
            "evidence_key": key, "field_mask": mask, "canonical_bytes": CANONICAL, "digest": digest,
        }).encode(), limit=se.MAX_EVIDENCE_BYTES)
        se._save_state(config, WORKSPACE, state)
    row = {
        "grant_id": grant_id, "grant_type": "snapshot", "entry_id": "", "entry_revision": None, "snapshot_id": key,
        "consent_generation": 1, "audience": audience, "project_id": "proj", "source_id": "doc",
        "producer": "memory_extraction", "relation_kind": "memory_checkpoint", "relation_id": "run-1",
        "relation_evidence": "proof-1", "relation_expires_at": "2099-01-01T00:00:00Z", "field_mask": mask,
        "canonical_bytes": CANONICAL, "digest": digest, "recorded_at": "2026-11-26T00:00:00Z",
    }
    payload = {"workspace": WORKSPACE, "workspace_lifetime_id": LIFETIME, "brain_name": link.name,
               "brain_identity": link.identity, "brain_lifetime_id": link.lifetime_id, "row": row}
    return {"schema": se.SCHEMA, "payload": payload, "signature": se._authorization_signature(config, payload)}


def patch_source(monkeypatch: pytest.MonkeyPatch | None = None) -> None:
    """The memory-checkpoint relation is a producer concern, not an admission one."""
    stub = lambda config, workspace, pass_chat_id, **kwargs: {"signature": "proof-1", "project_id": "proj"}  # noqa: E731
    if monkeypatch is None:
        se._verified_memory_checkpoint = stub  # type: ignore[assignment]
    else:
        monkeypatch.setattr(se, "_verified_memory_checkpoint", stub)


@dataclass
class Env:
    config: AdmissionConfig
    clone: Path
    origin: Path
    tmp: Path
    authorization: dict[str, Any]
    proposal_id: str
    token: str
    effects: list[str]

    def audience(self, link: SharedBrainLink, row: dict[str, Any]) -> str | None:
        return AUDIENCE

    def effect(self, admitted: sa.AdmittedEffect) -> sa.EffectResult:
        self.effects.append(admitted.attempt_id)
        return sa.EffectResult("applied", receipt="rcpt-" + admitted.attempt_id)

    def admit(self, effect: Any = "default", audience: Any = None) -> sa.AdmissionOutcome:
        chosen = self.effect if effect == "default" else effect
        return asyncio.run(sa.admit_effect(
            self.config, workspace=WORKSPACE, proposal_id=self.proposal_id, expected_revision=self.token,
            audience=audience or self.audience, effect=chosen))

    def attempts(self) -> list[dict[str, Any]]:
        artifact = share_publish._artifact_for(self.config, WORKSPACE, self.proposal_id)
        assert artifact is not None
        return list((artifact.get("publication") or {}).get("attempts", []))


@pytest.fixture
def env(linked: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:  # noqa: F811
    base, clone, origin = linked
    monkeypatch.setattr(share_publish, "_network_git", local_transport(origin))
    patch_source(monkeypatch)
    guide = {"version": 1, "scope": "Decisions", "exclusions": [], "types": {
        "decision": {"kind": "collection", "path": "decisions", "operation": "create"}}}
    (clone / shared_brain_guide.GUIDE_NAME).write_text("```ciao-guide\n" + json.dumps(guide) + "\n```\n")
    link = base.link
    load = shared_brain_guide.read_guide(clone, brain_identity=link.identity)
    assert load.guide is not None
    mapping = shared_brain_mapping.approve(link, load, ["decision"], expected_pin=load.guide.pin)
    link = replace(link, guide_mapping=mapping, guide_mapping_generation=mapping.generation)
    link = shared_brain.set_sharing_policy(link, "automatic")
    config = AdmissionConfig(tmp_path, base.vault, link)
    _write_registry(config)
    shared_brain.save_sharing_revocations(config, set(), registry_bytes=config._workspace_registry_bytes)
    authorization = install_grant(config)
    with shared_brain.checkout_claims_nowait([link.path], config), workspace_registry_claim(config) as owner:
        snapshot = shared_brain_mapping.resolve_workspace_destination(
            config, WORKSPACE, link.name, note_type="decision", relative_path=PATH, operation="create", owner=owner)
        assert snapshot is not None
        captured = share_proposals._qualify_owned(config, WORKSPACE, link, PATH, "create", owner)
        draft = share_proposals._draft_share(
            config, workspace=WORKSPACE, brain=link.name, relative_path=PATH, operation="create",
            proposed=b"Launch\n", request_key="adm", request_fingerprint="adm", checkout_claimed=True,
            registry_owner=owner, expected_qualification=captured,
            candidate_binding={"authorization": authorization, **asdict(snapshot)})
    assert isinstance(draft, share_proposals.ShareDraft)
    return Env(config, clone, origin, tmp_path, authorization, draft.proposal_id,
               draft.artifact["review_token"], [])


# --- the effect stays off ---------------------------------------------------------------

def test_publish_share_still_refuses_candidates_and_manual_accept_needs_its_exact_token(env: Env) -> None:
    before = subprocess.run(["git", "--git-dir", str(env.origin), "rev-parse", "main"], check=True,
                            capture_output=True, text=True).stdout
    wrong = asyncio.run(share_publish.publish_share(
        env.config, workspace=WORKSPACE, proposal_id=env.proposal_id, expected_revision="shr_wrong"))
    assert wrong.outcome == "stale_review"
    held = asyncio.run(share_publish.publish_share(
        env.config, workspace=WORKSPACE, proposal_id=env.proposal_id, expected_revision=env.token))
    assert held.outcome == "draft_unavailable" and "fencing" in held.message
    after = subprocess.run(["git", "--git-dir", str(env.origin), "rev-parse", "main"], check=True,
                           capture_output=True, text=True).stdout
    assert before == after and env.attempts() == []


# --- a valid grant reaches the callback once, journaled first ---------------------------

def test_valid_source_and_destination_grant_reaches_the_effect_once_after_the_journal(env: Env) -> None:
    seen: list[dict[str, Any]] = []

    def effect(admitted: sa.AdmittedEffect) -> sa.EffectResult:
        seen.extend(env.attempts())  # the durable record exists BEFORE the write
        env.effects.append(admitted.attempt_id)
        return sa.EffectResult("applied", receipt="rcpt-1")

    outcome = env.admit(effect)
    assert outcome.code == "applied" and outcome.receipt == "rcpt-1" and len(env.effects) == 1
    assert len(seen) == 1 and seen[0]["status"] == "admitted" and seen[0]["attempt_id"] == env.effects[0]
    final = env.attempts()[0]
    assert final["status"] == "applied" and final["receipt"] == "rcpt-1"
    assert final["actor"] == "engine" and final["producer"] == "memory_extraction"
    assert final["target_revision"] and final["destination"] and final["policy_generation"] == 1
    blob = json.dumps(final)
    assert GRANT not in blob and AUDIENCE not in blob and CANONICAL not in blob  # redacted correlations
    again = env.admit()
    assert again.code == "already_applied" and len(env.effects) == 1
    assert sa.reconcile_admission(env.config, workspace=WORKSPACE, proposal_id=env.proposal_id,
                                  expected_revision=env.token).code == "applied"


def test_eligibility_check_writes_nothing_and_stale_token_is_refused(env: Env) -> None:
    assert env.admit(None).code == "eligible" and env.attempts() == []
    stale = asyncio.run(sa.admit_effect(
        env.config, workspace=WORKSPACE, proposal_id=env.proposal_id, expected_revision="shr_old",
        audience=env.audience, effect=env.effect))
    assert stale.code == "stale_review" and env.effects == []


# --- every invalidating writer, before and during admission -----------------------------

def _revoke(env: Env) -> None:
    se.revoke(env.config, workspace=WORKSPACE, grant_id=GRANT)


def _withdraw(env: Env) -> None:
    se.withdraw(env.config, workspace=WORKSPACE, grant_id=GRANT, reason="withdrawn")


def _downgrade(env: Env) -> None:
    edit_registry(env.config, lambda c: c.links.__setitem__(0, shared_brain.set_sharing_policy(c.links[0], "review")))


def _unlink(env: Env) -> None:
    edit_registry(env.config, lambda c: c.links.clear())


def _archive(env: Env) -> None:
    def archive(config: AdmissionConfig) -> None:
        config.archived = True
    edit_registry(env.config, archive)


def _regrant(env: Env) -> None:
    _revoke(env)
    install_grant(env.config, "grant-two")


def _upstream_moves(env: Env) -> None:
    other = env.tmp / "other"
    subprocess.run(["git", "clone", "--quiet", str(env.origin), str(other)], check=True)
    (other / "decisions").mkdir(exist_ok=True)
    (other / "decisions" / "d1.md").write_text("someone else\n")
    git = lambda *a: subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@e.c", "-C", str(other), *a], check=True, capture_output=True)  # noqa: E731
    git("add", "."), git("commit", "--quiet", "-m", "other"), git("push", "--quiet", "origin", "main")


INVALIDATIONS = [
    ("revoke", _revoke, "source_revoked"),
    ("withdraw", _withdraw, "source_withdrawn"),
    ("downgrade", _downgrade, "consent_changed"),
    ("unlink", _unlink, "brain_unavailable"),
    ("archive", _archive, "brain_unavailable"),
    ("revoke-and-regrant", _regrant, "source_revoked"),
    ("upstream-moved", _upstream_moves, "destination_changed"),
]


@pytest.mark.parametrize(("name", "mutate", "code"), INVALIDATIONS, ids=[item[0] for item in INVALIDATIONS])
def test_zero_writes_after_a_completed_invalidation(env: Env, name: str, mutate: Callable[[Env], None], code: str) -> None:
    mutate(env)
    assert env.admit().code == code
    assert env.effects == [] and env.attempts() == []


@pytest.mark.parametrize(("name", "mutate", "code"), INVALIDATIONS, ids=[item[0] for item in INVALIDATIONS])
def test_barrier_before_admission_an_invalidation_in_the_gap_writes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch, name: str, mutate: Callable[[Env], None], code: str,
) -> None:
    """Pause after the artifact is read and before the registry owner is taken."""
    reached, resume = threading.Event(), threading.Event()
    real = sa.acquire_workspace_registry

    def paused(config: Any, **kwargs: Any) -> Any:
        reached.set()
        assert resume.wait(10)
        return real(config, **kwargs)

    monkeypatch.setattr(sa, "acquire_workspace_registry", paused)
    outcome: list[sa.AdmissionOutcome] = []
    worker = threading.Thread(target=lambda: outcome.append(env.admit()))
    worker.start()
    assert reached.wait(10)
    mutate(env)
    resume.set()
    worker.join(20)
    assert [item.code for item in outcome] == [code]
    assert env.effects == []


def test_audience_changed_or_unreadable_refuses_without_an_effect(env: Env) -> None:
    assert env.admit(audience=lambda link, row: "github.com/x/public").code == "audience_changed"
    assert env.admit(audience=lambda link, row: None).code == "audience_unverified"

    def broken(link: SharedBrainLink, row: dict[str, Any]) -> str:
        raise OSError("acl unreadable")

    assert env.admit(audience=broken).code == "audience_unverified"
    assert env.effects == [] and env.attempts() == []


def test_reconsent_needs_a_fresh_grant_and_fresh_draft(env: Env) -> None:
    _revoke(env)
    assert env.admit().code == "source_revoked"
    _downgrade(env)
    edit_registry(env.config, lambda c: c.links.__setitem__(0, shared_brain.set_sharing_policy(c.links[0], "automatic")))
    # Consent is automatic again, but the old artifact is bound to the old grant and generation.
    assert env.admit().code in {"source_revoked", "destination_changed"} and env.effects == []


# --- barriers after admission ------------------------------------------------------------

def test_barrier_after_admission_revoke_and_registry_writers_wait_for_the_bounded_effect(env: Env) -> None:
    inside, release = threading.Event(), threading.Event()

    def slow(admitted: sa.AdmittedEffect) -> sa.EffectResult:
        inside.set()
        assert release.wait(10)
        env.effects.append(admitted.attempt_id)
        return sa.EffectResult("applied", receipt="r")

    outcome: list[sa.AdmissionOutcome] = []
    worker = threading.Thread(target=lambda: outcome.append(env.admit(slow)))
    worker.start()
    assert inside.wait(10)
    with pytest.raises(WorkspaceRegistryBusy):  # unlink/archive/policy writers never queue behind it
        acquire_workspace_registry(env.config, blocking=False)
    revoked = threading.Event()
    revoker = threading.Thread(target=lambda: (_revoke(env), revoked.set()))
    revoker.start()
    assert not revoked.wait(0.3)  # a revoke does not return while an admitted write is in flight
    release.set()
    worker.join(10), revoker.join(10)
    assert revoked.is_set() and [item.code for item in outcome] == ["applied"]
    assert len(env.effects) == 1
    assert env.admit().code == "already_applied" and len(env.effects) == 1


def test_barrier_between_retries_revalidates_every_attempt(env: Env) -> None:
    results = iter([sa.EffectResult("rejected", retryable=True), sa.EffectResult("applied", receipt="r2")])

    def flaky(admitted: sa.AdmittedEffect) -> sa.EffectResult:
        env.effects.append(admitted.attempt_id)
        return next(results)

    assert env.admit(flaky).code == "rejected" and len(env.effects) == 1
    _revoke(env)  # between the attempts
    assert env.admit(flaky).code == "source_revoked" and len(env.effects) == 1


def test_a_retry_after_a_retryable_rejection_is_a_new_journaled_attempt(env: Env) -> None:
    results = iter([sa.EffectResult("rejected", retryable=True), sa.EffectResult("applied", receipt="r2")])

    def flaky(admitted: sa.AdmittedEffect) -> sa.EffectResult:
        env.effects.append(admitted.attempt_id)
        return next(results)

    assert env.admit(flaky).code == "rejected"
    assert env.admit(flaky).code == "applied"
    assert [item["status"] for item in env.attempts()] == ["rejected", "applied"]
    assert [item["attempt"] for item in env.attempts()] == [1, 2]


def test_a_definitive_refusal_is_final(env: Env) -> None:
    env.admit(lambda admitted: (env.effects.append("x"), sa.EffectResult("rejected"))[1])
    assert env.admit().code == "rejected_final" and env.effects == ["x"]


# --- the journal fails closed ------------------------------------------------------------

def test_journal_failure_before_the_effect_writes_nothing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(share_publish, "_write_state", broken)
    assert env.admit().code == "draft_unavailable" and env.effects == []


def test_lost_completion_record_stays_admitted_and_is_never_retried(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    real = share_publish._write_state
    calls: list[int] = []

    def second_fails(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("lost acknowledgement")
        return real(*args, **kwargs)

    monkeypatch.setattr(share_publish, "_write_state", second_fails)
    first = env.admit()
    assert first.code == "indeterminate" and len(env.effects) == 1
    monkeypatch.setattr(share_publish, "_write_state", real)
    assert env.attempts()[0]["status"] == "admitted"
    assert sa.reconcile_admission(env.config, workspace=WORKSPACE, proposal_id=env.proposal_id,
                                  expected_revision=env.token).code == "indeterminate"
    assert env.admit().code == "attempt_unresolved" and len(env.effects) == 1
    # Reconciling is read-only: the journal is unchanged.
    assert env.attempts()[0]["status"] == "admitted"
    assert share_publish.can_retry_publication(
        env.config, workspace=WORKSPACE, proposal_id=env.proposal_id, expected_revision=env.token) is False


def test_an_effect_that_raises_is_indeterminate_and_not_retried(env: Env) -> None:
    def boom(admitted: sa.AdmittedEffect) -> sa.EffectResult:
        env.effects.append("started")
        raise RuntimeError("connection lost after send")

    assert env.admit(boom).code == "indeterminate"
    assert env.admit().code == "attempt_unresolved" and env.effects == ["started"]


# --- event loop and cancellation --------------------------------------------------------

def test_the_event_loop_stays_responsive_during_the_effect_and_cancellation_keeps_the_claims(env: Env) -> None:
    inside, release = threading.Event(), threading.Event()

    def slow(admitted: sa.AdmittedEffect) -> sa.EffectResult:
        inside.set()
        assert release.wait(10)
        env.effects.append(admitted.attempt_id)
        return sa.EffectResult("applied", receipt="r")

    async def scenario() -> tuple[float, bool, bool]:
        task = asyncio.ensure_future(sa.admit_effect(
            env.config, workspace=WORKSPACE, proposal_id=env.proposal_id, expected_revision=env.token,
            audience=env.audience, effect=slow))
        while not inside.is_set():
            await asyncio.sleep(0.01)
        worst, last = 0.0, time.monotonic()
        for _ in range(20):
            await asyncio.sleep(0.01)
            now = time.monotonic()
            worst, last = max(worst, now - last), now
        task.cancel()
        await asyncio.sleep(0.1)
        try:
            acquire_workspace_registry(env.config, blocking=False).close()
            held = False
        except WorkspaceRegistryBusy:
            held = True  # cancelled, but the claims live until the worker settles
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        free = acquire_workspace_registry(env.config, blocking=False)
        free.close()
        return worst, held, True

    worst, held, freed = asyncio.run(scenario())
    assert worst < 0.25 and held and freed
    assert len(env.effects) == 1 and env.attempts()[0]["status"] == "applied"


def test_registry_only_writers_never_wait_on_checkout_or_proposal_locks(env: Env) -> None:
    link = env.config.links[0]
    fields = share_publish._artifact_for(env.config, WORKSPACE, env.proposal_id)["immutable"]  # type: ignore[index]

    async def hold() -> float:
        async with shared_brain._checkout_lock(link.path):
            async with shared_brain._checkout_claim(link.path, env.config):
                async with share_publish._locked_proposal(env.config, WORKSPACE, fields):
                    started = time.monotonic()
                    acquire_workspace_registry(env.config, blocking=False).close()
                    se.revoke(env.config, workspace=WORKSPACE, grant_id=GRANT)
                    return time.monotonic() - started

    assert asyncio.run(hold()) < 2


# --- two OS processes --------------------------------------------------------------------

CHILD = """
import asyncio, json, os, sys, time
from pathlib import Path
from ciao import share_admission as sa, share_publish
from tests import test_share_admission as t
tmp, origin, proposal, token, mode, signal_dir = sys.argv[1:7]
signal_dir = Path(signal_dir)
t.patch_source()
share_publish._network_git = t.local_transport(Path(origin))
config = t.open_config(Path(tmp))
log = signal_dir / "effects.log"
def effect(admitted):
    with log.open("a") as stream:
        stream.write(admitted.attempt_id + "\\n")
    (signal_dir / "inside").write_text("1")
    if mode == "die":
        os._exit(42)
    deadline = time.monotonic() + 20
    while mode == "hold" and not (signal_dir / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return sa.EffectResult("applied", receipt="child")
outcome = asyncio.run(sa.admit_effect(config, workspace="home", proposal_id=proposal, expected_revision=token,
                                      audience=lambda link, row: t.AUDIENCE, effect=effect))
print(outcome.code, flush=True)
"""


def _child(env: Env, mode: str, signals: Path) -> subprocess.Popen[str]:
    signals.mkdir(exist_ok=True)
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, str(env.tmp), str(env.origin), env.proposal_id, env.token, mode, str(signals)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, stdout=subprocess.PIPE, text=True)


def _wait_for(path: Path, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.02)


def _log(signals: Path) -> list[str]:
    path = signals / "effects.log"
    return path.read_text().split() if path.exists() else []


def test_two_processes_a_revoke_waits_for_the_other_process_admitted_write_then_nothing_starts(env: Env) -> None:
    signals = env.tmp / "sig-hold"
    child = _child(env, "hold", signals)
    _wait_for(signals / "inside")
    revoked = threading.Event()
    thread = threading.Thread(target=lambda: (_revoke(env), revoked.set()))
    thread.start()
    assert not revoked.wait(0.5)
    (signals / "release").write_text("1")
    thread.join(20)
    assert revoked.is_set() and child.communicate(timeout=20)[0].split() == ["applied"]
    assert len(_log(signals)) == 1
    again = _child(env, "hold", env.tmp / "sig-after")
    assert again.communicate(timeout=30)[0].split() in (["already_applied"], ["source_revoked"])
    assert _log(env.tmp / "sig-after") == [] and len(_log(signals)) == 1


def test_two_processes_a_dead_effect_is_durably_indeterminate_and_never_repeats(env: Env) -> None:
    signals = env.tmp / "sig-die"
    child = _child(env, "die", signals)
    assert child.wait(timeout=30) == 42
    assert len(_log(signals)) == 1
    assert env.attempts()[0]["status"] == "admitted"
    assert sa.reconcile_admission(env.config, workspace=WORKSPACE, proposal_id=env.proposal_id,
                                  expected_revision=env.token).code == "indeterminate"
    restart = _child(env, "hold", env.tmp / "sig-restart")
    assert restart.communicate(timeout=30)[0].split() == ["attempt_unresolved"]
    assert env.admit().code == "attempt_unresolved"
    assert _log(env.tmp / "sig-restart") == [] and env.effects == [] and len(_log(signals)) == 1


def test_two_processes_a_registry_writer_in_another_process_makes_admission_busy_then_unlink_wins(env: Env) -> None:
    owner = acquire_workspace_registry(env.config, blocking=True)
    try:
        busy = _child(env, "hold", env.tmp / "sig-busy")
        assert busy.communicate(timeout=30)[0].split() == ["registry_busy"]
        assert _log(env.tmp / "sig-busy") == []
    finally:
        owner.close()
    _unlink(env)
    gone = _child(env, "hold", env.tmp / "sig-gone")
    assert gone.communicate(timeout=30)[0].split() == ["brain_unavailable"]
    assert _log(env.tmp / "sig-gone") == []
