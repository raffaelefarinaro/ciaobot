"""Isolated bare-Git acceptance for shared-note publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import share_proposals, share_publish, shared_brain
from ciao.config import SharedBrainLink

IDENT = ("-c", "user.name=Test", "-c", "user.email=test@example.com")
HOSTED = "https://github.com/x/notes"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *IDENT, "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class Config:
    def __init__(self, root: Path, vault: Path, link: SharedBrainLink) -> None:
        self.workspace_root = root / "install"
        self.state_path = self.workspace_root / ".runtime" / "state.json"
        self.vault = vault
        self.vault_root = vault
        self.vaults = {"home": vault}
        self.link = link
        self.links = [link]
        self.logs_root = root / "logs"

    def workspace_names(self) -> list[str]:
        return list(self.vaults)

    def workspace_vault_root(self, name: str) -> Path:
        return self.vaults[name]

    def workspace(self, name: str | None) -> SimpleNamespace | None:
        return SimpleNamespace(shared_brains=self.links) if name in self.vaults else None


@pytest.fixture
def linked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path, Path]:
    async def no_refresh(_link: SharedBrainLink, _config: Any) -> dict[str, str]:
        return {"outcome": "up_to_date"}

    monkeypatch.setattr(shared_brain, "update_brain", no_refresh)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(seed)], check=True)
    (seed / "n.md").write_bytes(b"before\r\n\xce\xbb\n")
    os.chmod(seed / "n.md", 0o755)
    (seed / "gone.md").write_bytes(b"remove me\n")
    git(seed, "add", ".")
    git(seed, "commit", "--quiet", "-m", "seed")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "--quiet", "-u", "origin", "main")
    clone = tmp_path / "brain"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)], check=True)
    git(clone, "remote", "set-url", "origin", HOSTED)
    link = SharedBrainLink(name="notes", path=str(clone), identity="github.com/x/notes")
    vault = tmp_path / "vault"
    vault.mkdir()
    config = Config(tmp_path, vault, link)
    monkeypatch.setattr(
        share_publish.shared_brain,
        "_HARDEN",
        share_publish.shared_brain._HARDEN + ("-c", "user.name=Test", "-c", "user.email=test@example.com"),
    )

    def transport(
        folder: Path, *args: str, limit: int = 4096,
        timeout_s: float = shared_brain.FETCH_TIMEOUT_S,
    ) -> tuple[int, bytes, bytes]:
        rewritten = tuple(str(origin) if arg == "origin" else arg for arg in args)
        proc = subprocess.run(
            ["git", *IDENT, "-c", "protocol.file.allow=always", "-C", str(folder), *rewritten],
            check=False, capture_output=True, timeout=min(timeout_s, 5),
        )
        return proc.returncode, proc.stdout[:limit], proc.stderr[:limit]

    monkeypatch.setattr(share_publish, "_network_git", transport)
    return config, clone, origin


def test_publish_uses_reviewed_bytes_and_is_idempotent(linked: tuple[Config, Path, Path]) -> None:
    config, clone, origin = linked
    (clone / "n.md").write_bytes(b"uncommitted local change")
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"reviewed\r\ncontent\n", request_key="publish-one",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    token = draft.artifact["review_token"]
    result = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=token))
    assert result.outcome == "published"
    assert result.commit_oid and result.receipt_id
    remote = subprocess.run(["git", "--git-dir", str(origin), "show", f"{result.commit_oid}:n.md"], check=True, capture_output=True).stdout
    assert remote == b"reviewed\r\ncontent\n"
    mode = subprocess.run(["git", "--git-dir", str(origin), "ls-tree", result.commit_oid, "n.md"], check=True, capture_output=True, text=True).stdout.split()[0]
    assert mode == "100755"
    changed_paths = subprocess.run(["git", "--git-dir", str(origin), "diff-tree", "--no-commit-id", "--name-only", "-r", result.commit_oid], check=True, capture_output=True, text=True).stdout.splitlines()
    assert changed_paths == ["n.md"]
    assert (clone / "n.md").read_bytes() == b"uncommitted local change"
    assert git(clone, "diff", "--cached", "--", "n.md") == ""
    replay = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=token))
    assert replay.outcome == "already_published"
    assert replay.commit_oid == result.commit_oid and replay.receipt_id == result.receipt_id
    assert len(share_publish.list_share_receipts(config, workspace="home")) == 1
    receipt = share_publish.get_share_receipt(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=token)
    assert receipt and receipt["commit_oid"] == result.commit_oid and receipt["published_blob_oid"]


def test_publish_retries_only_after_a_persisted_retryable_rejection(linked, monkeypatch) -> None:
    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"retry after a recorded rejection", request_key="retry-rejected-attempt",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    fields = draft.artifact["immutable"]
    attempt = {
        "attempt": 1, "commit_oid": "a" * 40,
        "parent_oid": fields["base_commit"],
        "proposal_id": fields["proposal_id"], "revision": fields["revision"],
        "destination_fingerprint": fields["destination_fingerprint"],
        "ref": fields["upstream_ref"], "status": "rejected", "retryable": True,
    }
    share_publish._write_state(config, "home", fields, {"attempts": [attempt]})
    token = draft.artifact["review_token"]
    assert share_publish.can_retry_publication(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=token,
    )
    pushes = 0
    transport = share_publish._network_git

    def count_push(folder: Path, *args: str, limit: int = 4096, timeout_s: float = 60.0):
        nonlocal pushes
        if args and args[0] == "push":
            pushes += 1
        return transport(folder, *args, limit=limit, timeout_s=timeout_s)

    monkeypatch.setattr(share_publish, "_network_git", count_push)
    result = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=token,
    ))
    assert result.outcome == "published"
    assert pushes == 1
    assert not share_publish.can_retry_publication(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=token,
    )


def test_receipt_enumeration_skips_malformed_canonical_and_index(linked, monkeypatch) -> None:
    config, _clone, _origin = linked
    valid = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"one", request_key="receipt-valid-one",
    )
    assert isinstance(valid, share_proposals.ShareDraft)
    one = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=valid.proposal_id,
        expected_revision=valid.artifact["review_token"],
    ))
    assert one.receipt_id
    store = share_proposals._store_dir(config)
    canonical = store / (
        hashlib.sha256(("home\0sidecar\0" + valid.artifact["immutable"]["sidecar_id"]).encode()).hexdigest() + ".json"
    )
    artifact = json.loads(canonical.read_text(encoding="utf-8"))
    malformed_fields = dict(artifact["immutable"])
    malformed_fields["sidecar_id"] = "malformed-canonical"
    malformed_artifact = dict(artifact, immutable=malformed_fields)
    malformed_artifact["review_token"] = share_proposals._review_token(malformed_fields)
    malformed_canonical = store / (
        hashlib.sha256(("home\0sidecar\0malformed-canonical").encode()).hexdigest() + ".json"
    )
    share_proposals._write_private(malformed_canonical, json.dumps(malformed_artifact, sort_keys=True).encode())
    malformed_canonical_artifact = json.loads(malformed_canonical.read_text(encoding="utf-8"))
    malformed_canonical_artifact["immutable"].pop("sidecar_id")
    malformed_canonical_artifact["review_token"] = share_proposals._review_token(
        malformed_canonical_artifact["immutable"]
    )
    share_proposals._write_private(
        malformed_canonical, json.dumps(malformed_canonical_artifact, sort_keys=True).encode()
    )
    malformed_index = store / "request-index-malformed.json"
    malformed_index_artifact = dict(artifact, publication={"receipts": None})
    share_proposals._write_private(
        malformed_index, json.dumps(malformed_index_artifact, sort_keys=True).encode()
    )
    listed = share_publish.list_share_receipts(config, workspace="home")
    assert [receipt["id"] for receipt in listed] == [one.receipt_id]


def test_publish_rechecks_link_after_waiting_for_checkout_claim(linked, monkeypatch) -> None:
    config, clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"after", request_key="remove-wins",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    token = draft.artifact["review_token"]
    lock = shared_brain._checkout_lock(str(clone))
    waiting = asyncio.Event()
    original_lock = shared_brain._checkout_lock
    original_network = share_publish._network_git
    pushes: list[tuple[str, ...]] = []

    def observe_lock(path: str):
        waiting.set()
        return original_lock(path)

    def observe_network(folder: Path, *args: str, limit: int = 4096):
        if args and args[0] == "push":
            pushes.append(args)
        return original_network(folder, *args, limit=limit)

    monkeypatch.setattr(shared_brain, "_checkout_lock", observe_lock)
    monkeypatch.setattr(share_publish, "_network_git", observe_network)

    async def race():
        await lock.acquire()
        task = asyncio.create_task(share_publish.publish_share(
            config, workspace="home", proposal_id=draft.proposal_id,
            expected_revision=token,
        ))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        config.links = []  # settings removal wins the checkout claim
        lock.release()
        return await task

    result = asyncio.run(race())
    assert result.outcome == "brain_unavailable"
    assert pushes == []


def test_archive_claim_wins_before_publish_can_push(linked, monkeypatch) -> None:
    from ciao.web.routes_api import _checkout_claims_for_paths
    from ciao.workspaces import workspace_registry_lock

    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"archive race", request_key="archive-wins-publish",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    waiting = asyncio.Event()
    original_lock = shared_brain._checkout_lock
    original_network = share_publish._network_git
    lock_calls = 0
    pushes: list[tuple[str, ...]] = []

    def observe_lock(path: str):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            waiting.set()
        return original_lock(path)

    def observe_network(folder: Path, *args: str, limit: int = 4096):
        if args and args[0] == "push":
            pushes.append(args)
        return original_network(folder, *args, limit=limit)

    monkeypatch.setattr(shared_brain, "_checkout_lock", observe_lock)
    monkeypatch.setattr(share_publish, "_network_git", observe_network)

    async def race():
        async with _checkout_claims_for_paths(config, [config.link.path]):
            publish = asyncio.create_task(share_publish.publish_share(
                config, workspace="home", proposal_id=draft.proposal_id,
                expected_revision=draft.artifact["review_token"],
            ))
            await asyncio.wait_for(waiting.wait(), timeout=2)
            # Archive unregisters the workspace while it owns the checkout
            # claim; publication must re-read this state after waiting.
            with workspace_registry_lock(config):
                config.links = []
        return await publish

    result = asyncio.run(race())
    assert result.outcome == "brain_unavailable"
    assert pushes == []


def test_publish_claim_wins_before_archive_can_remove_link(linked, monkeypatch) -> None:
    from ciao.web.routes_api import _checkout_claims_for_paths

    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"publish wins", request_key="publish-wins-archive",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    entered_publish = threading.Event()
    release_publish = threading.Event()
    archive_waiting = asyncio.Event()
    original_publish = share_publish._publish_locked
    original_lock = shared_brain._checkout_lock
    calls = 0

    def pause_publish(*args, **kwargs):
        entered_publish.set()
        assert release_publish.wait(timeout=5)
        return original_publish(*args, **kwargs)

    def observe_lock(path: str):
        nonlocal calls
        calls += 1
        if calls == 2:
            archive_waiting.set()
        return original_lock(path)

    monkeypatch.setattr(share_publish, "_publish_locked", pause_publish)
    monkeypatch.setattr(shared_brain, "_checkout_lock", observe_lock)

    async def race():
        publish = asyncio.create_task(share_publish.publish_share(
            config, workspace="home", proposal_id=draft.proposal_id,
            expected_revision=draft.artifact["review_token"],
        ))
        assert await asyncio.to_thread(entered_publish.wait, 2)

        async def archive_after_publish():
            async with _checkout_claims_for_paths(config, [config.link.path]):
                config.links = []

        archive = asyncio.create_task(archive_after_publish())
        await asyncio.wait_for(archive_waiting.wait(), timeout=2)
        release_publish.set()
        result = await publish
        await archive
        return result

    result = asyncio.run(race())
    assert result.outcome == "published"
    assert config.links == []


@pytest.mark.parametrize("relink", [False, True])
def test_share_undo_rechecks_link_lifetime_after_waiting_for_claim(linked, monkeypatch, relink: bool) -> None:
    config, clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"published", request_key=f"undo-link-race-{relink}",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    published = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert published.outcome == "published"
    old = config.link
    lock = shared_brain._checkout_lock(str(clone))
    waiting = asyncio.Event()
    original_lock = shared_brain._checkout_lock

    def observe_lock(path: str):
        waiting.set()
        return original_lock(path)

    monkeypatch.setattr(shared_brain, "_checkout_lock", observe_lock)

    async def race():
        await lock.acquire()
        task = asyncio.create_task(share_publish.draft_share_reversal(
            config, workspace="home", receipt_id=published.receipt_id or "",
        ))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        config.links = [SharedBrainLink(name="notes", path=old.path, identity=old.identity)] if relink else []
        lock.release()
        return await task

    result = asyncio.run(race())
    assert isinstance(result, share_proposals.ShareRefusal)
    assert result.code == "brain_changed"


def test_share_receipt_undo_queues_same_inverse_without_publishing(linked: tuple[Config, Path, Path]) -> None:
    config, clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"published bytes\n", request_key="undo-source",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    published = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert published.outcome == "published"
    detail = share_publish.get_share_receipt_detail(
        config, workspace="home", receipt_id=published.receipt_id or ""
    )
    assert detail and detail["immutable"]["original"] == "before\r\nλ\n"
    before_pushes = [row for row in share_publish._artifact_for(config, "home", draft.proposal_id)["publication"]["attempts"]]

    first = asyncio.run(share_publish.draft_share_reversal(
        config, workspace="home", receipt_id=published.receipt_id or ""
    ))
    second = asyncio.run(share_publish.draft_share_reversal(
        config, workspace="home", receipt_id=published.receipt_id or ""
    ))
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first == second
    # The inverse is a distinct pending replace draft carrying the original bytes.
    from ciao.proposal_tracking import walk_proposal_queue
    queue = (config.vault / "Workspace" / "Memory-Proposals.md").read_text(encoding="utf-8")
    entry = next(row for row in walk_proposal_queue("home", "home/Workspace/Memory-Proposals.md", queue) if row.bullet.kind == "share" and row.bullet.text != f"Shared note change proposal {detail['immutable']['sidecar_id']}")
    inverse = share_proposals.load_share(config, workspace="home", proposal_id=entry.proposal_id)
    assert isinstance(inverse, dict)
    assert inverse["immutable"]["operation"] == "replace"
    assert inverse["immutable"]["proposed"] == "before\r\nλ\n"
    assert first["review_path"] == share_proposals.proposal_tracking.proposal_review_path("home", inverse["immutable"]["review_key"])
    assert len(share_publish._artifact_for(config, "home", draft.proposal_id)["publication"]["attempts"]) == len(before_pushes)
    remote = subprocess.run(
        ["git", "--git-dir", str(_origin), "show", f"{published.commit_oid}:n.md"],
        check=True, capture_output=True,
    ).stdout
    assert remote == b"published bytes\n"


def test_share_receipt_undo_refuses_a_later_destination_edit(linked: tuple[Config, Path, Path]) -> None:
    config, clone, origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"published bytes\n", request_key="undo-conflict-source",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    published = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert published.outcome == "published"
    git(clone, "reset", "--hard", published.commit_oid or "")
    (clone / "n.md").write_bytes(b"another writer\n")
    git(clone, "add", "n.md")
    git(clone, "commit", "--quiet", "-m", "another writer")
    subprocess.run(["git", *IDENT, "-C", str(clone), "-c", "protocol.file.allow=always", "push", "--quiet", str(origin), "HEAD:refs/heads/main"], check=True)
    refusal = asyncio.run(share_publish.draft_share_reversal(
        config, workspace="home", receipt_id=published.receipt_id or ""
    ))
    assert isinstance(refusal, share_proposals.ShareRefusal)
    assert refusal.code == "changed_upstream"


@pytest.mark.parametrize(
    ("operation", "path", "content", "inverse", "inverse_content"),
    [
        ("create", "new.md", b"new note\n", "delete", None),
        ("delete", "gone.md", None, "create", "remove me\n"),
    ],
)
def test_share_receipt_undo_inverts_create_and_delete(
    linked: tuple[Config, Path, Path], operation: str, path: str,
    content: bytes | None, inverse: str, inverse_content: str | None,
) -> None:
    from ciao.proposal_tracking import walk_proposal_queue

    config, _clone, _origin = linked
    source = share_proposals.draft_share(
        config, workspace="home", relative_path=path, operation=operation,
        proposed=content, request_key="undo-" + operation,
    )
    assert isinstance(source, share_proposals.ShareDraft)
    published = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=source.proposal_id,
        expected_revision=source.artifact["review_token"],
    ))
    assert published.outcome == "published"
    inverse_result = asyncio.run(share_publish.draft_share_reversal(
        config, workspace="home", receipt_id=published.receipt_id or ""
    ))
    assert isinstance(inverse_result, dict)
    queue = (config.vault / "Workspace" / "Memory-Proposals.md").read_text(encoding="utf-8")
    rows = list(walk_proposal_queue("home", "home/Workspace/Memory-Proposals.md", queue))
    inverse_row = next(row for row in reversed(rows) if row.bullet.kind == "share")
    artifact = share_proposals.load_share(config, workspace="home", proposal_id=inverse_row.proposal_id)
    assert isinstance(artifact, dict)
    assert artifact["immutable"]["operation"] == inverse
    assert artifact["immutable"]["proposed"] == inverse_content


def test_legacy_publication_attempt_reconciles_without_lifetime_or_active_link(linked) -> None:
    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"published legacy", request_key="legacy-reconcile",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    pushed = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert pushed.outcome == "published"
    artifact = share_publish._artifact_for(config, "home", draft.proposal_id)
    assert artifact is not None
    artifact["immutable"].pop("link_lifetime_id")
    artifact["review_token"] = share_proposals._review_token(artifact["immutable"])
    legacy_receipt = dict(artifact["publication"]["receipts"][0])
    for key in ("link_lifetime_id", "path", "brain", "identity", "branch"):
        legacy_receipt.pop(key, None)
    artifact["publication"]["receipts"] = [legacy_receipt]
    digest = hashlib.sha256(
        ("home\0sidecar\0" + artifact["immutable"]["sidecar_id"]).encode()
    ).hexdigest()
    artifact_path = share_proposals._store_dir(config) / f"{digest}.json"
    for stored_path in share_proposals._store_dir(config).glob("*.json"):
        stored_artifact = share_proposals._load_sidecar(stored_path)
        if stored_artifact["immutable"]["sidecar_id"] == artifact["immutable"]["sidecar_id"]:
            share_proposals._write_private(stored_path, json.dumps(artifact, sort_keys=True).encode())
    listed = share_publish.list_share_receipts(config, workspace="home")
    assert len(listed) == 1 and listed[0]["id"] == legacy_receipt["id"]
    assert listed[0]["path"] == "n.md" and listed[0]["brain"] == "notes"
    detail = share_publish.get_share_receipt_detail(
        config, workspace="home", receipt_id=legacy_receipt["id"]
    )
    assert detail is not None and detail["receipt"]["branch"] == "main"
    enriched = share_publish._make_receipt(
        _clone, artifact["immutable"], legacy_receipt["commit_oid"]
    )
    share_publish._write_state(
        config, "home", artifact["immutable"], {"receipt": enriched}
    )
    stored = share_publish._artifact_for(config, "home", draft.proposal_id)
    assert stored is not None
    assert stored["publication"]["receipts"][0]["path"] == "n.md"
    config.links = []
    result = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=artifact["review_token"],
    ))
    assert result.outcome == "already_published"
    receipts = share_publish.list_share_receipts(config, workspace="home")
    assert len(receipts) == 1
    assert receipts[0]["id"] == legacy_receipt["id"]
    assert receipts[0]["path"] == "n.md"
    assert receipts[0]["brain"] == "notes"
    assert receipts[0]["branch"] == "main"
    assert "link_lifetime_id" not in receipts[0]


def test_legacy_lifetime_persistence_holds_registry_serialization(linked, monkeypatch) -> None:
    from ciao.workspaces import workspace_registry_lock

    config, _clone, _origin = linked
    entered = threading.Event()
    finish_persist = threading.Event()
    contender_entered = threading.Event()

    def persist_registry() -> None:
        entered.set()
        assert finish_persist.wait(timeout=2)
        registry = config.workspace_root / ".runtime" / "workspaces.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(config, "persist_workspace_registry", persist_registry, raising=False)
    initializer = threading.Thread(
        target=share_proposals._persist_link_lifetime,
        args=(config, "home", config.link),
    )
    initializer.start()
    assert entered.wait(timeout=2)

    def settings_save() -> None:
        with workspace_registry_lock(config):
            contender_entered.set()

    saver = threading.Thread(target=settings_save)
    saver.start()
    assert not contender_entered.wait(timeout=0.05)
    finish_persist.set()
    initializer.join(timeout=2)
    saver.join(timeout=2)
    assert not initializer.is_alive() and not saver.is_alive()
    assert contender_entered.is_set()


def test_legacy_unattempted_review_gets_new_consent_token_and_recovers_interrupted_cleanup(linked, monkeypatch) -> None:
    config, _clone, _origin = linked
    legacy = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"old exact proposal", request_key="legacy-pending",
    )
    assert isinstance(legacy, share_proposals.ShareDraft)
    artifact = share_publish._artifact_for(config, "home", legacy.proposal_id)
    assert artifact is not None
    artifact["immutable"].pop("link_lifetime_id")
    artifact["review_token"] = share_proposals._review_token(artifact["immutable"])
    digest = hashlib.sha256(
        ("home\0sidecar\0" + artifact["immutable"]["sidecar_id"]).encode()
    ).hexdigest()
    share_proposals._write_private(
        share_proposals._store_dir(config) / f"{digest}.json",
        json.dumps(artifact, sort_keys=True).encode(),
    )
    original_retire = share_publish._retire_legacy_queue_row
    def interrupted(*_args, **_kwargs):
        raise OSError("simulated interruption after replacement was queued")
    monkeypatch.setattr(share_publish, "_retire_legacy_queue_row", interrupted)
    with pytest.raises(OSError, match="simulated interruption"):
        share_publish.refresh_legacy_draft(
            config, workspace="home", proposal_id=legacy.proposal_id,
            expected_revision=artifact["review_token"],
        )
    queue = config.vault / "Workspace" / "Memory-Proposals.md"
    queued = queue.read_text(encoding="utf-8")
    assert f"Shared note change proposal {artifact['immutable']['sidecar_id']}" in queued
    monkeypatch.setattr(share_publish, "_retire_legacy_queue_row", original_retire)
    refreshed = share_publish.refresh_legacy_draft(
        config, workspace="home", proposal_id=legacy.proposal_id,
        expected_revision=artifact["review_token"],
    )
    assert isinstance(refreshed, share_proposals.ShareDraft)
    assert refreshed.proposal_id != legacy.proposal_id
    assert refreshed.artifact["review_token"] != artifact["review_token"]
    assert refreshed.artifact["immutable"]["proposed"] == "old exact proposal"
    assert refreshed.artifact["immutable"]["link_lifetime_id"] == config.link.lifetime_id
    queue_after = queue.read_text(encoding="utf-8")
    assert f"Shared note change proposal {artifact['immutable']['sidecar_id']}" not in queue_after
    assert queue_after.count("Shared note change proposal") == 1
    assert share_publish.has_publication_attempt(
        config, workspace="home", proposal_id=legacy.proposal_id,
        expected_revision=artifact["review_token"],
    ) is False


def test_legacy_refresh_waits_for_queue_before_registry_lock(linked, monkeypatch) -> None:
    from ciao.workspaces import workspace_registry_lock

    config, _clone, _origin = linked
    legacy = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"legacy", request_key="legacy-lock-order",
    )
    assert isinstance(legacy, share_proposals.ShareDraft)
    artifact = share_publish._artifact_for(config, "home", legacy.proposal_id)
    assert artifact is not None
    artifact["immutable"].pop("link_lifetime_id")
    artifact["review_token"] = share_proposals._review_token(artifact["immutable"])
    for path in share_proposals._store_dir(config).glob("*.json"):
        stored = share_proposals._load_sidecar(path)
        if stored["immutable"]["sidecar_id"] == artifact["immutable"]["sidecar_id"]:
            share_proposals._write_private(path, json.dumps(artifact, sort_keys=True).encode())

    queue = config.vault / "Workspace" / "Memory-Proposals.md"
    original_lock_factory = share_proposals._thread_queue_lock
    queue_requested = threading.Event()
    registry_probe_entered = threading.Event()
    result: list[object] = []

    def observe_queue_lock(path: Path):
        queue_requested.set()
        return original_lock_factory(path)

    monkeypatch.setattr(share_proposals, "_thread_queue_lock", observe_queue_lock)
    held_queue_lock = original_lock_factory(queue)
    held_queue_lock.acquire()
    worker = threading.Thread(target=lambda: result.append(share_publish.refresh_legacy_draft(
        config, workspace="home", proposal_id=legacy.proposal_id,
        expected_revision=artifact["review_token"],
    )))
    worker.start()
    try:
        assert queue_requested.wait(timeout=2)

        def probe_registry() -> None:
            with workspace_registry_lock(config):
                registry_probe_entered.set()

        probe = threading.Thread(target=probe_registry)
        probe.start()
        assert registry_probe_entered.wait(timeout=2), "refresh held registry while waiting for queue"
        probe.join(timeout=2)
    finally:
        held_queue_lock.release()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert result and isinstance(result[0], share_proposals.ShareDraft)


def test_legacy_refresh_rechecks_publication_reservation_under_queue_lock(linked, monkeypatch) -> None:
    config, _clone, _origin = linked
    legacy = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"legacy", request_key="legacy-reservation-race",
    )
    assert isinstance(legacy, share_proposals.ShareDraft)
    artifact = share_publish._artifact_for(config, "home", legacy.proposal_id)
    assert artifact is not None
    artifact["immutable"].pop("link_lifetime_id")
    artifact["review_token"] = share_proposals._review_token(artifact["immutable"])
    sidecar = share_proposals._store_dir(config) / (
        hashlib.sha256(("home\0sidecar\0" + artifact["immutable"]["sidecar_id"]).encode()).hexdigest() + ".json"
    )
    share_proposals._write_private(sidecar, json.dumps(artifact, sort_keys=True).encode())
    original_lookup = share_publish._artifact_for
    reserved = False

    def reserve_after_initial_read(*args, **kwargs):
        nonlocal reserved
        visible = original_lookup(*args, **kwargs)
        if visible is not None and not reserved:
            reserved = True
            current = share_proposals._load_sidecar(sidecar)
            current["publication"] = {"attempts": [{"status": "pending"}], "receipts": []}
            share_proposals._write_private(sidecar, json.dumps(current, sort_keys=True).encode())
        return visible

    monkeypatch.setattr(share_publish, "_artifact_for", reserve_after_initial_read)
    result = share_publish.refresh_legacy_draft(
        config, workspace="home", proposal_id=legacy.proposal_id,
        expected_revision=artifact["review_token"],
    )
    assert isinstance(result, share_proposals.ShareRefusal)
    assert result.code == "publication_pending"
    queue = config.vault / "Workspace" / "Memory-Proposals.md"
    text = queue.read_text(encoding="utf-8")
    assert text.count("Shared note change proposal") == 1
    assert artifact["immutable"]["sidecar_id"] in text


def test_legacy_refresh_fails_closed_after_index_write_without_canonical(linked, monkeypatch) -> None:
    config, _clone, _origin = linked
    legacy = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"legacy", request_key="legacy-index-crash",
    )
    assert isinstance(legacy, share_proposals.ShareDraft)
    artifact = share_publish._artifact_for(config, "home", legacy.proposal_id)
    assert artifact is not None
    artifact["immutable"].pop("link_lifetime_id")
    artifact["review_token"] = share_proposals._review_token(artifact["immutable"])
    for path in share_proposals._store_dir(config).glob("*.json"):
        stored = share_proposals._load_sidecar(path)
        if stored["immutable"]["sidecar_id"] == artifact["immutable"]["sidecar_id"]:
            share_proposals._write_private(path, json.dumps(artifact, sort_keys=True).encode())
    monkeypatch.setattr(share_proposals, "MAX_PER_WORKSPACE", 1)
    monkeypatch.setattr(share_proposals, "MAX_PER_BRAIN", 1)

    original_write = share_proposals._write_private
    request_key = "legacy-refresh:" + artifact["immutable"]["sidecar_id"]
    index_hash = hashlib.sha256(("home\0" + request_key).encode()).hexdigest()
    failed = False

    def crash_after_index(path: Path, data: bytes) -> None:
        nonlocal failed
        original_write(path, data)
        if path.name == f"{index_hash}.json" and not failed:
            failed = True
            raise OSError("simulated crash after index write")

    monkeypatch.setattr(share_proposals, "_write_private", crash_after_index)
    first = share_publish.refresh_legacy_draft(
        config, workspace="home", proposal_id=legacy.proposal_id,
        expected_revision=artifact["review_token"],
    )
    assert isinstance(first, share_proposals.ShareRefusal)
    queue = config.vault / "Workspace" / "Memory-Proposals.md"
    assert f"Shared note change proposal {artifact['immutable']['sidecar_id']}" in queue.read_text(encoding="utf-8")

    monkeypatch.setattr(share_proposals, "_write_private", original_write)
    retried = share_publish.refresh_legacy_draft(
        config, workspace="home", proposal_id=legacy.proposal_id,
        expected_revision=artifact["review_token"],
    )
    assert isinstance(retried, share_proposals.ShareRefusal)
    assert retried.code == "draft_corrupt"
    after = queue.read_text(encoding="utf-8")
    assert after.count("Shared note change proposal") == 1
    assert f"Shared note change proposal {artifact['immutable']['sidecar_id']}" in after
    new_artifact = share_proposals._load_sidecar(
        share_proposals._store_dir(config) / f"{index_hash}.json"
    )
    new_fields = new_artifact["immutable"]
    new_canonical = share_proposals._store_dir(config) / (
        hashlib.sha256(
            ("home\0sidecar\0" + new_fields["sidecar_id"]).encode()
        ).hexdigest() + ".json"
    )
    assert not new_canonical.exists()


def test_legacy_refresh_can_replace_a_row_at_share_capacity(linked, monkeypatch) -> None:
    config, _clone, _origin = linked
    legacy = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"legacy", request_key="legacy-capacity",
    )
    assert isinstance(legacy, share_proposals.ShareDraft)
    artifact = share_publish._artifact_for(config, "home", legacy.proposal_id)
    assert artifact is not None
    artifact["immutable"].pop("link_lifetime_id")
    artifact["review_token"] = share_proposals._review_token(artifact["immutable"])
    for path in share_proposals._store_dir(config).glob("*.json"):
        stored = share_proposals._load_sidecar(path)
        if stored["immutable"]["sidecar_id"] == artifact["immutable"]["sidecar_id"]:
            share_proposals._write_private(path, json.dumps(artifact, sort_keys=True).encode())
    monkeypatch.setattr(share_proposals, "MAX_PER_WORKSPACE", 1)
    monkeypatch.setattr(share_proposals, "MAX_PER_BRAIN", 1)
    replacement = share_publish.refresh_legacy_draft(
        config, workspace="home", proposal_id=legacy.proposal_id,
        expected_revision=artifact["review_token"],
    )
    assert isinstance(replacement, share_proposals.ShareDraft)
    queue = config.vault / "Workspace" / "Memory-Proposals.md"
    assert queue.read_text(encoding="utf-8").count("Shared note change proposal") == 1


def test_publish_rejects_stale_review_and_upstream_change(linked: tuple[Config, Path, Path]) -> None:
    config, clone, origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"after", request_key="publish-two",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    stale = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision="shr_stale"))
    assert stale.outcome == "stale_review"
    seed = clone / "n.md"
    seed.write_bytes(b"other edit")
    git(clone, "add", "n.md")
    git(clone, "commit", "--quiet", "-m", "other writer")
    # The test transport makes remote updates available through the same bare repository.
    subprocess.run(["git", *IDENT, "-C", str(clone), "-c", "protocol.file.allow=always", "push", "--quiet", str(origin), "HEAD:refs/heads/main"], check=True)
    changed = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=draft.artifact["review_token"]))
    assert changed.outcome == "changed_upstream"
    assert not share_publish.list_share_receipts(config, workspace="home")


def test_share_preview_preserves_exact_text_and_null_for_absent_file(linked: tuple[Config, Path, Path]) -> None:
    from ciao.web import proposal_service

    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"exact\r\nafter\n", request_key="preview-replace",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    _, by_id = proposal_service._scan_proposal_rows(config)
    preview = proposal_service.preview_row(config, by_id[draft.proposal_id])
    assert preview["before"] == "before\r\nλ\n"
    assert preview["after"] == "exact\r\nafter\n"
    assert preview["operation"] == "replace"
    assert preview["branch"] == "main"
    assert preview["path"] == "n.md"
    assert preview["revision"] == draft.artifact["review_token"]
    assert preview["blocked_reason"] is None

    created = share_proposals.draft_share(
        config, workspace="home", relative_path="new.md", operation="create",
        proposed=b"created\n", request_key="preview-create",
    )
    assert isinstance(created, share_proposals.ShareDraft)
    _, by_id = proposal_service._scan_proposal_rows(config)
    create_preview = proposal_service.preview_row(config, by_id[created.proposal_id])
    assert create_preview["before"] is None
    assert create_preview["after"] == "created\n"
    assert create_preview["operation"] == "create"


def test_publish_reconciles_lost_ack_and_serializes_duplicate_accepts(
    linked: tuple[Config, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"accepted once", request_key="publish-lost-ack",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    transport = share_publish._network_git
    push_count = 0

    def lose_ack(
        folder: Path, *args: str, limit: int = 4096,
        timeout_s: float = share_publish.shared_brain.FETCH_TIMEOUT_S,
    ) -> tuple[int, bytes, bytes]:
        nonlocal push_count
        result = transport(folder, *args, limit=limit, timeout_s=timeout_s)
        if args and args[0] == "push":
            push_count += 1
            return 1, b"", b"connection timed out after remote accepted update"
        return result

    monkeypatch.setattr(share_publish, "_network_git", lose_ack)
    token = draft.artifact["review_token"]

    async def both():
        return await asyncio.gather(*(
            share_publish.publish_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=token)
            for _ in range(2)
        ))

    first, second = asyncio.run(both())
    assert {first.outcome, second.outcome} == {"published", "already_published"}
    assert first.commit_oid == second.commit_oid
    assert first.receipt_id == second.receipt_id
    assert push_count == 1
    assert len(share_publish.list_share_receipts(config, workspace="home")) == 1


def test_dismissal_during_fetch_prevents_new_push(
    linked: tuple[Config, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"dismissed while fetching", request_key="publish-dismiss-race",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    fetch_started = threading.Event()
    resume_fetch = threading.Event()
    push_count = 0
    real_fetch = share_publish._fetch
    real_transport = share_publish._network_git

    def suspended_fetch(*args, **kwargs):
        fetch_started.set()
        assert resume_fetch.wait(5)
        return real_fetch(*args, **kwargs)

    def count_push(
        folder: Path, *args: str, limit: int = 4096,
        timeout_s: float = share_publish.shared_brain.FETCH_TIMEOUT_S,
    ):
        nonlocal push_count
        if args and args[0] == "push":
            push_count += 1
        return real_transport(folder, *args, limit=limit, timeout_s=timeout_s)

    monkeypatch.setattr(share_publish, "_fetch", suspended_fetch)
    monkeypatch.setattr(share_publish, "_network_git", count_push)

    async def race():
        publishing = asyncio.create_task(share_publish.publish_share(
            config, workspace="home", proposal_id=draft.proposal_id,
            expected_revision=draft.artifact["review_token"],
        ))
        assert await asyncio.to_thread(fetch_started.wait, 5)
        from ciao.web import proposal_service

        _rows, by_id = proposal_service._scan_proposal_rows(config)
        ctx = by_id[draft.proposal_id]
        try:
            await asyncio.to_thread(
                proposal_service._rewrite_queue_single,
                Path(ctx["path"]), ctx["line"], ctx["row"]["raw"],
                ctx["row"]["text"], ctx["row"]["kind"], False,
                ctx["workspace"], Path(config.workspace_vault_root(ctx["workspace"])),
                config=config, proposal_id=draft.proposal_id,
            )
        finally:
            resume_fetch.set()
        return await asyncio.wait_for(publishing, timeout=5)

    result = asyncio.run(race())
    assert result.outcome == "draft_corrupt"
    assert push_count == 0


def test_pending_attempt_precedes_bounded_unlocked_push_timeout(
    linked: tuple[Config, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"reserved before bounded push", request_key="publish-bounded-unlocked-push",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    transport = share_publish._network_git
    queue = Path(config.workspace_vault_root("home")) / share_proposals._QUEUE
    observed_timeout: list[float] = []

    def timeout_push(
        folder: Path, *args: str, limit: int = 4096,
        timeout_s: float = share_publish.shared_brain.FETCH_TIMEOUT_S,
    ):
        if args and args[0] == "push":
            observed_timeout.append(timeout_s)
            artifact = share_publish._artifact_for(config, "home", draft.proposal_id)
            assert artifact is not None
            assert artifact["publication"]["attempts"][-1]["status"] == "pending"

            acquired = threading.Event()
            lock_error: list[Exception] = []

            def inspect_queue_lock():
                try:
                    with share_proposals._thread_queue_lock(queue), share_publish.memory_receipts.queue_lock(queue, timeout_s=0.2):
                        acquired.set()
                except Exception as exc:
                    lock_error.append(exc)

            contender = threading.Thread(target=inspect_queue_lock)
            contender.start()
            contender.join(timeout=2)
            assert not contender.is_alive()
            assert not lock_error
            assert acquired.is_set()
            return -1, b"", b"git command timed out"
        return transport(folder, *args, limit=limit, timeout_s=timeout_s)

    monkeypatch.setattr(share_publish, "_network_git", timeout_push)
    result = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))

    assert result.outcome == "indeterminate"
    assert observed_timeout == [share_publish.PUSH_TIMEOUT_S]
    assert share_publish.PUSH_TIMEOUT_S <= share_publish.memory_receipts.QUEUE_LOCK_TIMEOUT_S - 5
    artifact = share_publish._artifact_for(config, "home", draft.proposal_id)
    assert artifact is not None
    assert artifact["publication"]["attempts"][-1]["status"] == "pending"


def test_directory_sync_failure_aborts_before_push(
    linked: tuple[Config, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"directory sync must complete", request_key="publish-directory-sync-failure",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    real_fsync = os.fsync
    calls = 0
    push_count = 0
    real_transport = share_publish._network_git

    def fail_directory_sync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated directory fsync failure")
        real_fsync(fd)

    def count_push(folder: Path, *args: str, limit: int = 4096, timeout_s: float = 60.0):
        nonlocal push_count
        if args and args[0] == "push":
            push_count += 1
        return real_transport(folder, *args, limit=limit, timeout_s=timeout_s)

    monkeypatch.setattr(share_proposals.os, "fsync", fail_directory_sync)
    monkeypatch.setattr(share_publish, "_network_git", count_push)
    result = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))

    assert result.outcome == "draft_unavailable"
    assert calls == 2
    assert push_count == 0


def test_publish_fails_closed_when_canonical_sidecar_is_missing(
    linked: tuple[Config, Path, Path],
) -> None:
    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"canonical artifact is required", request_key="publish-missing-canonical",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    fields = draft.artifact["immutable"]
    canonical = share_proposals._store_dir(config) / (
        hashlib.sha256(("home\0sidecar\0" + fields["sidecar_id"]).encode()).hexdigest() + ".json"
    )
    canonical.unlink()

    assert share_publish._artifact_for(config, "home", draft.proposal_id) is None
    result = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert result.outcome == "draft_corrupt"


def test_recorded_attempt_reconciles_after_link_removal(linked: tuple[Config, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"write accepted", request_key="publish-reconcile",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    write_state = share_publish._write_state

    def fail_receipt(config_arg, workspace: str, fields, update):
        if "receipt" in update:
            raise OSError("simulated crash before receipt")
        return write_state(config_arg, workspace, fields, update)

    monkeypatch.setattr(share_publish, "_write_state", fail_receipt)
    token = draft.artifact["review_token"]
    pending = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=token))
    assert pending.outcome == "indeterminate"
    config.links = []
    monkeypatch.setattr(share_publish, "_write_state", write_state)
    reconciled = asyncio.run(share_publish.reconcile_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=token))
    assert reconciled.outcome == "published"
    assert reconciled.commit_oid and reconciled.receipt_id
    assert len(share_publish.list_share_receipts(config, workspace="home")) == 1


def test_lost_ack_with_misleading_rejection_stderr_never_retries_after_rewrite(
    linked: tuple[Config, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, clone, origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"do not republish after rewrite", request_key="publish-rewrite-after-ack",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    transport = share_publish._network_git
    push_count = 0

    def accept_then_rewrite(
        folder: Path, *args: str, limit: int = 4096,
        timeout_s: float = share_publish.shared_brain.FETCH_TIMEOUT_S,
    ) -> tuple[int, bytes, bytes]:
        nonlocal push_count
        result = transport(folder, *args, limit=limit, timeout_s=timeout_s)
        if args and args[0] == "push":
            push_count += 1
            assert result[0] == 0
            commit = args[-1].split(":", 1)[0]
            parent = git(clone, "rev-parse", commit + "^")
            subprocess.run(["git", "--git-dir", str(origin), "update-ref", "refs/heads/main", parent], check=True)
            return 1, b"", b"remote: ! refs/heads/main:refs/heads/main [rejected] (fetch first)"
        return result

    monkeypatch.setattr(share_publish, "_network_git", accept_then_rewrite)
    token = draft.artifact["review_token"]
    first = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=token))
    assert first.outcome == "indeterminate"
    artifact = share_publish._artifact_for(config, "home", draft.proposal_id)
    assert artifact and artifact["publication"]["attempts"][0]["status"] == "pending"
    retry = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=token))
    assert retry.outcome == "indeterminate"
    assert push_count == 1


def test_publish_create_and_delete(linked: tuple[Config, Path, Path]) -> None:
    config, _clone, origin = linked
    created = share_proposals.draft_share(
        config, workspace="home", relative_path="new.md", operation="create",
        proposed=b"new note\r\n", request_key="publish-create",
    )
    assert isinstance(created, share_proposals.ShareDraft)
    result = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=created.proposal_id, expected_revision=created.artifact["review_token"]))
    assert result.outcome == "published" and result.commit_oid
    remote = subprocess.run(["git", "--git-dir", str(origin), "show", f"{result.commit_oid}:new.md"], check=True, capture_output=True).stdout
    assert remote == b"new note\r\n"
    deleted = share_proposals.draft_share(
        config, workspace="home", relative_path="gone.md", operation="delete",
        proposed=None, request_key="publish-delete",
    )
    assert isinstance(deleted, share_proposals.ShareDraft)
    result = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=deleted.proposal_id, expected_revision=deleted.artifact["review_token"]))
    assert result.outcome == "published" and result.commit_oid
    exists = subprocess.run(["git", "--git-dir", str(origin), "cat-file", "-e", f"{result.commit_oid}:gone.md"], check=False)
    assert exists.returncode != 0


def test_push_attempt_limit_persists_across_racing_tips(linked: tuple[Config, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    config, _clone, origin = linked
    racer = origin.parent / "racer"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(racer)], check=True)
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"must wait", request_key="publish-race-limit",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    transport = share_publish._network_git
    push_count = 0

    def race(
        folder: Path, *args: str, limit: int = 4096,
        timeout_s: float = share_publish.shared_brain.FETCH_TIMEOUT_S,
    ) -> tuple[int, bytes, bytes]:
        nonlocal push_count
        if args and args[0] == "push":
            push_count += 1
            git(racer, "pull", "--quiet", "--ff-only", "origin", "main")
            (racer / f"other-{push_count}.md").write_text(f"racer {push_count}\n", encoding="utf-8")
            git(racer, "add", ".")
            git(racer, "commit", "--quiet", "-m", f"racer {push_count}")
            git(racer, "push", "--quiet", "origin", "main")
        return transport(folder, *args, limit=limit, timeout_s=timeout_s)

    monkeypatch.setattr(share_publish, "_network_git", race)
    result = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=draft.artifact["review_token"]))
    assert result.outcome == "indeterminate"
    assert push_count == share_publish.MAX_ATTEMPTS == 3
    artifact = share_publish._artifact_for(config, "home", draft.proposal_id)
    assert artifact and len(artifact["publication"]["attempts"]) == 3


def test_protected_branch_refusal_is_bounded_and_persisted(linked: tuple[Config, Path, Path]) -> None:
    config, _clone, origin = linked
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'protected branch update failed' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"will be refused", request_key="publish-protected",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    result = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=draft.artifact["review_token"]))
    assert result.outcome == "push_refused"
    assert "protected branch" in result.message
    artifact = share_publish._artifact_for(config, "home", draft.proposal_id)
    assert artifact and len(artifact["publication"]["attempts"]) == 1
    assert not artifact["publication"].get("receipts")


def test_concurrent_workspaces_publish_distinct_notes_and_conflict_on_same_note(
    linked: tuple[Config, Path, Path],
) -> None:
    config, _clone, origin = linked
    other_vault = origin.parent / "other-vault"
    other_vault.mkdir()
    config.vaults["other"] = other_vault
    other_link = config.links[0]
    config.links = [other_link]

    def file(workspace: str, path: str, content: bytes, key: str):
        draft = share_proposals.draft_share(
            config, workspace=workspace, relative_path=path, operation="create" if path != "n.md" else "replace",
            proposed=content, request_key=key,
        )
        assert isinstance(draft, share_proposals.ShareDraft)
        return draft

    proposals = [
        file("home", "left.md", b"left", "multi-left"),
        file("other", "right.md", b"right", "multi-right"),
        file("home", "n.md", b"home change", "same-home"),
        file("other", "n.md", b"other change", "same-other"),
    ]

    async def publish_all():
        return await asyncio.gather(*(
            share_publish.publish_share(
                config, workspace=draft.artifact["immutable"]["workspace"],
                proposal_id=draft.proposal_id, expected_revision=draft.artifact["review_token"],
            )
            for draft in proposals
        ))

    results = asyncio.run(publish_all())
    assert [results[0].outcome, results[1].outcome] == ["published", "published"]
    assert {results[2].outcome, results[3].outcome} == {"published", "changed_upstream"}
    head = git(origin.parent, "--git-dir", str(origin), "rev-parse", "refs/heads/main")
    paths = subprocess.run(["git", "--git-dir", str(origin), "ls-tree", "-r", "--name-only", head], check=True, capture_output=True, text=True).stdout.splitlines()
    assert {"left.md", "right.md"}.issubset(paths)
