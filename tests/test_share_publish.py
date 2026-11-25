"""Isolated bare-Git acceptance for shared-note publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import os
import subprocess
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import proposal_actions, share_proposals, share_publish, shared_brain
from ciao.config import SharedBrainLink
from ciao.web.routes_api import memory_receipt_detail, memory_receipts, proposals_history

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
        self.workspaces = {"home": SimpleNamespace(shared_brains=self.links)}

    def workspace_names(self) -> list[str]:
        return list(self.vaults)

    def workspace_vault_root(self, name: str) -> Path:
        return self.vaults[name]

    def chats_roots(self) -> list[str]:
        return []

    def workspace(self, name: str | None) -> SimpleNamespace | None:
        return SimpleNamespace(shared_brains=self.links) if name == "home" else self.workspaces.get(name)


def _save_test_links(config):
    from dataclasses import asdict
    rows = [{"name": name, "shared_brains": [asdict(link) for link in item.shared_brains]}
            for name, item in config.workspaces.items()]
    path = config.state_path.parent / "workspaces.json"
    path.write_text(json.dumps(rows))
    config._workspace_registry_bytes = path.read_bytes()


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
    registry = config.state_path.parent / "workspaces.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps([{"name": "home", "vault_root": str(vault), "shared_brains": [
        {"name": link.name, "path": link.path, "identity": link.identity}
    ]}]))
    config._workspace_registry_bytes = registry.read_bytes()
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


def _receipt_client(config: Config) -> TestClient:
    app = Starlette(routes=[
        Route("/api/memory/receipts/{id}", memory_receipt_detail, methods=["GET"]),
        Route("/api/memory/receipts", memory_receipts, methods=["GET"]),
        Route("/api/proposals/history", proposals_history, methods=["GET"]),
    ])
    app.state.config = config
    return TestClient(app)


def test_git_receipt_api_projection_stays_a_confirmed_remote_publication(
    linked: tuple[Config, Path, Path],
) -> None:
    config, _clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"remote receipt\n", request_key="git-receipt-api",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    published = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert published.outcome == "published"
    client = _receipt_client(config)
    listed = client.get("/api/memory/receipts").json()["rows"]
    row = next(item for item in listed if item["id"] == published.receipt_id)
    assert row["kind"] == "share_publish"
    assert row["undoable"] is True
    assert row.get("scope") is None
    assert row.get("local_only") is None
    assert row.get("sync_claimed") is None
    detail = client.get(f"/api/memory/receipts/{published.receipt_id}").json()
    assert detail["undoable"] is True
    assert detail["commit_oid"] == published.commit_oid
    assert detail["before"] == "before\r\nλ\n"
    assert detail["after"] == "remote receipt\n"
    assert detail.get("scope") is None
    assert detail.get("local_only") is None
    assert detail.get("sync_claimed") is None
    queue = config.vault / "Workspace" / "Memory-Proposals.md"
    proposal_actions.record_decision(
        queue, action="accept",
        text=f"Shared note change proposal {draft.artifact['immutable']['sidecar_id']}",
        kind="share", via="pwa", outcome="published",
        proposal_id=draft.proposal_id, receipt_id=str(published.receipt_id),
    )
    history = client.get("/api/proposals/history").json()["rows"]
    row = next(item for item in history if item.get("kind") == "share")
    assert row["change"]["scope"] is None
    assert row["change"]["commit_oid"] == published.commit_oid
    assert row["change"]["undoable"] is True


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
    # A Git receipt is a confirmed remote publication, not a folder's
    # historical-local-only record.
    assert "scope" not in receipt
    assert "local_only" not in receipt
    assert "sync_claimed" not in receipt


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


def test_reconcile_refuses_historical_checkout_adopted_as_folder(linked) -> None:
    config, clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"published", request_key="reconcile-folder-adoption",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    artifact = draft.artifact
    attempt = {"commit_oid": "a" * 40, "status": "unresolved"}
    share_publish._write_state(config, "home", artifact["immutable"], {"attempts": [attempt]})
    # The checkout itself is outside workspace_root, as real linked folders are.
    folder_link = SharedBrainLink("team", str(clone), f"folder:{clone}", kind="folder")
    config.links = [folder_link]
    config.workspaces["home"].shared_brains = [folder_link]
    _save_test_links(config)
    result = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert result.outcome == "indeterminate"


def test_reconcile_checks_folder_relink_after_checkout_claim(linked, monkeypatch) -> None:
    config, clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"reconcile race", request_key="reconcile-folder-race",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    attempt = {"commit_oid": "a" * 40, "status": "unresolved"}
    share_publish._write_state(config, "home", draft.artifact["immutable"], {"attempts": [attempt]})
    waiting = asyncio.Event()
    original_lock = shared_brain._checkout_lock

    def observe_lock(path: str):
        waiting.set()
        return original_lock(path)

    monkeypatch.setattr(shared_brain, "_checkout_lock", observe_lock)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(share_publish, "_network_git", lambda _folder, *args, **_kwargs: calls.append(args) or (0, b"", b""))
    monkeypatch.setattr(share_publish, "_safe_git", lambda _folder, *args, **_kwargs: calls.append(args) or (0, b"", b""))
    monkeypatch.setattr(share_publish, "_git", lambda _folder, *args, **_kwargs: calls.append(args) or (0, b"", b""), raising=False)

    async def race():
        lock = original_lock(str(clone))
        await lock.acquire()
        task = asyncio.create_task(share_publish.reconcile_share(
            config, workspace="home", proposal_id=draft.proposal_id,
            expected_revision=draft.artifact["review_token"],
        ))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        # A distinct config object represents another process.  Its registry
        # snapshot is replaced while reconciliation waits on the checkout claim.
        other_config = Config(config.workspace_root.parent, config.vault, config.link)
        other_config.workspaces = {"other": SimpleNamespace(shared_brains=[
            SharedBrainLink("team", str(clone), f"folder:{clone}", kind="folder")
        ])}
        other_config.vaults = {"other": config.vault}
        from ciao.workspaces import workspace_registry_lock
        with workspace_registry_lock(config):
            config.workspaces.update(other_config.workspaces)
            config.vaults.update(other_config.vaults)
            _save_test_links(config)
        lock.release()
        return await task

    result = asyncio.run(race())
    assert result.outcome == "indeterminate"
    assert calls == []


def test_reconcile_refuses_cross_workspace_folder_alias_and_runs_no_git(linked, monkeypatch) -> None:
    config, clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"cross workspace alias", request_key="reconcile-cross-workspace-alias",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    share_publish._write_state(config, "home", draft.artifact["immutable"], {
        "attempts": [{"commit_oid": "a" * 40, "status": "unresolved"}],
    })
    alias = clone.parent / "brain-alias"
    alias.symlink_to(clone, target_is_directory=True)
    alias_link = SharedBrainLink("team", str(alias), f"folder:{alias}", kind="folder")
    config.vaults["team"] = config.vault
    config.workspaces["team"] = SimpleNamespace(shared_brains=[alias_link])
    _save_test_links(config)
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(share_publish, "_network_git", lambda *_args, **_kwargs: calls.append(_args) or (0, b"", b""))
    monkeypatch.setattr(share_publish, "_safe_git", lambda *_args, **_kwargs: calls.append(_args) or (0, b"", b""))
    monkeypatch.setattr(share_publish, "run_git_sync_bounded", lambda *_args, **_kwargs: calls.append(_args))
    monkeypatch.setattr(shared_brain, "_git", lambda *_args, **_kwargs: calls.append(_args))

    result = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert result.outcome == "indeterminate"
    assert calls == []


def test_reconcile_reads_persisted_registry_from_independent_config(linked, monkeypatch) -> None:
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.workspaces import persist_workspaces

    config, clone, _origin = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"disk registry", request_key="reconcile-disk-registry",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    share_publish._write_state(config, "home", draft.artifact["immutable"], {
        "attempts": [{"commit_oid": "a" * 40, "status": "unresolved"}],
    })

    env = {
        "CIAO_WORKSPACE": str(config.workspace_root),
        "CIAO_RUNTIME_ROOT": str(config.workspace_root / ".runtime"),
        "PWA_AUTH_TOKEN": "test-token",
    }
    config.workspace_root.mkdir(parents=True, exist_ok=True)
    folder_link = SharedBrainLink("team", str(clone), f"folder:{clone}", kind="folder")
    first = CiaoConfig.from_env(env)
    first.workspaces["team"] = WorkspaceConfig(
        name="team", vault_root=str(config.vault), shared_brains=[folder_link],
        default_provider="claude", disallowed_tools=None, claude_trim={}, color="blue",
        memory_handover="always", memory_approval={}, provider_overrides={},
    )
    persist_workspaces(first)
    second = CiaoConfig.from_env(env)

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(share_publish, "_network_git", lambda *_args, **_kwargs: calls.append(_args) or (0, b"", b""))
    monkeypatch.setattr(share_publish, "_safe_git", lambda *_args, **_kwargs: calls.append(_args) or (0, b"", b""))
    monkeypatch.setattr(share_publish, "run_git_sync_bounded", lambda *_args, **_kwargs: calls.append(_args))
    monkeypatch.setattr(shared_brain, "_git", lambda *_args, **_kwargs: calls.append(_args))
    # Point the exercise config at the same persisted registry snapshot, as a
    # second process would after loading it.
    config.state_path = second.state_path
    result = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert result.outcome == "indeterminate"
    assert calls == []


def test_registry_cas_rejects_second_config_without_clobbering_disk(linked) -> None:
    from ciao.config import CiaoConfig
    from ciao.workspaces import WorkspaceRegistryConflict

    config, _clone, _origin = linked
    root = config.workspace_root
    root.mkdir(parents=True, exist_ok=True)
    env = {
        "CIAO_WORKSPACE": str(root),
        "CIAO_RUNTIME_ROOT": str(root / ".runtime"),
        "PWA_AUTH_TOKEN": "test-token",
    }
    first = CiaoConfig.from_env(env)
    first.persist_workspace_registry()
    stale = CiaoConfig.from_env(env)
    first.workspaces["newer"] = first.workspaces[first.primary_workspace()]
    first.persist_workspace_registry()
    disk_before = (root / ".runtime" / "workspaces.json").read_bytes()
    stale.workspaces.clear()
    with pytest.raises(WorkspaceRegistryConflict):
        stale.persist_workspace_registry()
    assert (root / ".runtime" / "workspaces.json").read_bytes() == disk_before


def _registry_process_write(workspace_root: str, runtime_root: str, ready, proceed, result) -> None:
    from ciao.config import CiaoConfig
    from ciao.workspaces import WorkspaceRegistryConflict

    config = CiaoConfig.from_env({
        "CIAO_WORKSPACE": workspace_root,
        "CIAO_RUNTIME_ROOT": runtime_root,
        "PWA_AUTH_TOKEN": "test-token",
    })
    ready.set()
    proceed.wait(5)
    config.workspaces["child"] = config.workspaces[config.primary_workspace()]
    try:
        config.persist_workspace_registry()
        result.put("written")
    except WorkspaceRegistryConflict:
        result.put("conflict")


def test_registry_multiprocess_stale_writer_cannot_replace_newer_registry(linked) -> None:
    import multiprocessing
    from ciao.config import CiaoConfig

    config, _clone, _origin = linked
    root = config.workspace_root
    root.mkdir(parents=True, exist_ok=True)
    runtime = root / ".runtime"
    env = {"CIAO_WORKSPACE": str(root), "CIAO_RUNTIME_ROOT": str(runtime), "PWA_AUTH_TOKEN": "test-token"}
    first = CiaoConfig.from_env(env)
    first.persist_workspace_registry()
    ready = multiprocessing.Event()
    proceed = multiprocessing.Event()
    result = multiprocessing.Queue()
    process = multiprocessing.Process(
        target=_registry_process_write,
        args=(str(root), str(runtime), ready, proceed, result),
    )
    process.start()
    assert ready.wait(5)
    first.workspaces["parent"] = first.workspaces[first.primary_workspace()]
    first.persist_workspace_registry()
    disk = (runtime / "workspaces.json").read_bytes()
    proceed.set()
    process.join(5)
    assert process.exitcode == 0
    assert result.get(timeout=2) == "conflict"
    assert (runtime / "workspaces.json").read_bytes() == disk


def test_registry_lock_failure_refuses_write(linked, monkeypatch) -> None:
    import ciao.workspaces as workspaces
    from ciao.config import CiaoConfig

    config, _clone, _origin = linked
    root = config.workspace_root
    root.mkdir(parents=True, exist_ok=True)
    env = {"CIAO_WORKSPACE": str(root), "CIAO_RUNTIME_ROOT": str(root / ".runtime"), "PWA_AUTH_TOKEN": "t"}
    config = CiaoConfig.from_env(env)
    before = (root / ".runtime" / "workspaces.json").read_bytes() if (root / ".runtime" / "workspaces.json").exists() else None
    monkeypatch.setattr(workspaces, "lock_exclusive", lambda _fd, **_kwargs: (_ for _ in ()).throw(OSError("lock failure")))
    with pytest.raises(OSError, match="lock failure"):
        config.persist_workspace_registry()
    after = (root / ".runtime" / "workspaces.json").read_bytes() if (root / ".runtime" / "workspaces.json").exists() else None
    assert after == before


def test_async_registry_claim_cancellation_closes_acquired_owner(linked, monkeypatch) -> None:
    from ciao.web.routes_api import _workspace_registry_claim
    import ciao.workspaces as workspaces

    config, _clone, _origin = linked
    entered = threading.Event()
    release = threading.Event()
    original = workspaces.acquire_workspace_registry

    def slow_acquire(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(workspaces, "acquire_workspace_registry", slow_acquire)

    async def exercise():
        task = asyncio.create_task(_workspace_registry_claim(config).__aenter__())
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        owner = await asyncio.to_thread(original, config, blocking=False)
        await asyncio.to_thread(owner.close)

    asyncio.run(exercise())


def test_legacy_lifetime_persistence_holds_registry_serialization(linked, monkeypatch) -> None:
    from ciao.workspaces import workspace_registry_lock

    config, _clone, _origin = linked
    entered = threading.Event()
    finish_persist = threading.Event()
    contender_entered = threading.Event()

    from ciao import workspaces
    original = workspaces.replace_workspace_registry

    def persist_registry(*args, **kwargs):
        entered.set()
        assert finish_persist.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(workspaces, "replace_workspace_registry", persist_registry)
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
    index = share_proposals._load_sidecar(share_proposals._store_dir(config) / f"{index_hash}.json")
    replacement_id = index["immutable"]["sidecar_id"]
    canonical_hash = hashlib.sha256(("home\0sidecar\0" + replacement_id).encode()).hexdigest()
    (share_proposals._store_dir(config) / f"{canonical_hash}.json").unlink()

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
    config.workspaces["other"] = SimpleNamespace(shared_brains=config.links)
    registry = config.state_path.parent / "workspaces.json"
    rows = json.loads(registry.read_bytes())
    registry.write_text(json.dumps([*rows, {**rows[0], "name": "other"}]))
    config._workspace_registry_bytes = registry.read_bytes()
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


def test_legacy_lifetime_refuses_to_adopt_stale_generation(linked):
    config, _, _ = linked
    path = config.state_path.parent / "workspaces.json"
    loaded = config._workspace_registry_bytes
    rows = json.loads(loaded)
    rows.append({"name": "peer", "vault_root": "external"})
    path.write_text(json.dumps(rows))
    peer = path.read_bytes()
    with pytest.raises(share_proposals.ShareDraftError):
        share_proposals._persist_link_lifetime(config, "home", config.link)
    assert config._workspace_registry_bytes == loaded
    assert path.read_bytes() == peer


def test_drift_repair_restart_keeps_manual_draft_and_publish(
    linked: tuple[Config, Path, Path], tmp_path: Path, monkeypatch
) -> None:
    config, clone, origin = linked
    automatic = shared_brain.set_sharing_policy(config.link, "automatic")
    config.link = automatic
    config.links = [automatic]
    config.workspaces["home"].shared_brains = [automatic]
    _save_test_links(config)

    def persist(config_arg, candidate=None, *, owner=None):
        _save_test_links(config_arg)

    monkeypatch.setattr("ciao.workspaces.persist_workspaces", persist)

    before = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"before drift\n", request_key="before-drift",
    )
    assert isinstance(before, share_proposals.ShareDraft)

    git(clone, "remote", "set-url", "origin", "https://github.com/x/elsewhere")
    refused = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"during drift\n", request_key="during-drift",
    )
    assert isinstance(refused, share_proposals.ShareRefusal)
    assert refused.code == "brain_changed"
    fence_dir = config.state_path.parent / "sharing-pending"
    assert config.workspaces["home"].shared_brains[0].sharing_policy == "review"

    git(clone, "remote", "set-url", "origin", HOSTED)
    recovered_link = config.workspaces["home"].shared_brains[0]
    restarted = Config(tmp_path, config.vault, recovered_link)
    restarted.workspaces["home"].shared_brains = [recovered_link]
    _save_test_links(restarted)

    assert shared_brain.recover_sharing_state(restarted)
    assert restarted.workspaces["home"].shared_brains[0].sharing_policy == "review"
    assert list(fence_dir.glob("*.json")) == []

    after = share_proposals.draft_share(
        restarted, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"after restart, manually approved\n", request_key="after-restart",
    )
    assert isinstance(after, share_proposals.ShareDraft)
    result = asyncio.run(share_publish.publish_share(
        restarted, workspace="home", proposal_id=after.proposal_id,
        expected_revision=after.artifact["review_token"],
    ))
    assert result.outcome == "published"


def test_divergent_push_url_refuses_draft_then_repairs_and_publishes(
    linked: tuple[Config, Path, Path]
) -> None:
    config, clone, origin = linked
    git(clone, "remote", "set-url", "--push", "origin", "https://github.com/x/other")
    refused = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"divergent push\n", request_key="divergent-push",
    )
    assert isinstance(refused, share_proposals.ShareRefusal)
    assert refused.code == "brain_changed"

    git(clone, "remote", "set-url", "--push", "origin", HOSTED)
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"repaired push\n", request_key="repaired-push",
    )
    assert isinstance(draft, share_proposals.ShareDraft)

    # Diverge again after review: the publish path must refuse the same draft.
    git(clone, "remote", "set-url", "--push", "origin", "https://github.com/x/other")
    refused_publish = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert refused_publish.outcome == "brain_changed"

    git(clone, "remote", "set-url", "--push", "origin", HOSTED)
    published = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert published.outcome == "published"


def test_multiple_configured_or_fetch_urls_refuse_draft(linked: tuple[Config, Path, Path]) -> None:
    config, clone, _origin = linked
    git(clone, "remote", "set-url", "--add", "origin", "https://github.com/x/other")
    refused = share_proposals.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"ambiguous origin\n", request_key="ambiguous-origin",
    )
    assert isinstance(refused, share_proposals.ShareRefusal)
    assert refused.code == "brain_changed"


def _reload_persisted_config(config: Config):
    from ciao.config import CiaoConfig

    return CiaoConfig.from_env({
        "CIAO_WORKSPACE": str(config.workspace_root),
        "CIAO_RUNTIME_ROOT": str(config.state_path.parent),
        "PWA_AUTH_TOKEN": "test-token",
    })


def _persist_automatic_grant(config) -> SharedBrainLink:
    from ciao.workspaces import persist_workspaces

    workspace = config.workspace("home")
    assert workspace is not None
    granted = shared_brain.set_sharing_policy(workspace.shared_brains[0], "automatic")
    workspace.shared_brains = [granted]
    persist_workspaces(config, dict(config.workspaces))
    return granted


def _alias_drift(kind: str, clone: Path, tmp_path: Path, monkeypatch):
    if kind == "configured":
        git(clone, "remote", "set-url", "origin", "https://github.com/x/elsewhere")

        def repair() -> None:
            git(clone, "remote", "set-url", "origin", HOSTED)

        return repair
    home = tmp_path / f"hostile-{kind}-home"
    home.mkdir(exist_ok=True)
    config_path = home / ".gitconfig"
    if kind == "fetch":
        config_path.write_text(
            f'[url "file:///tmp/hostile-fetch"]\n\tinsteadOf = {HOSTED}\n'
        )
    else:
        config_path.write_text(
            f'[url "file:///tmp/hostile-push"]\n\tpushInsteadOf = {HOSTED}\n'
        )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    def repair() -> None:
        config_path.write_text("")

    return repair


@pytest.mark.parametrize("kind", ["configured", "fetch", "push"])
def test_persisted_automatic_grant_alias_drift_repairs_draft_and_publish(
    linked: tuple[Config, Path, Path], tmp_path: Path, monkeypatch, kind: str
) -> None:
    config, clone, _origin = linked
    live = _reload_persisted_config(config)
    _persist_automatic_grant(live)

    before = share_proposals.draft_share(
        live, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"reviewed before drift\n", request_key=f"before-{kind}",
    )
    assert isinstance(before, share_proposals.ShareDraft)

    repair = _alias_drift(kind, clone, tmp_path, monkeypatch)

    # Publishing the pre-drift draft sees the alias under the fence, refuses,
    # and durably retires the automatic grant.
    refused_publish = asyncio.run(share_publish.publish_share(
        live, workspace="home", proposal_id=before.proposal_id,
        expected_revision=before.artifact["review_token"],
    ))
    assert refused_publish.outcome == "brain_changed"
    after_publish = _reload_persisted_config(config)
    stored = after_publish.workspace("home").shared_brains[0]
    assert stored.sharing_policy == "review"
    assert stored.sharing_consent is None
    assert not shared_brain.sharing_consent_active(stored)

    repair()
    restarted = _reload_persisted_config(config)
    assert shared_brain.recover_sharing_state(restarted)
    assert restarted.workspace("home").shared_brains[0].sharing_policy == "review"
    published = asyncio.run(share_publish.publish_share(
        restarted, workspace="home", proposal_id=before.proposal_id,
        expected_revision=before.artifact["review_token"],
    ))
    assert published.outcome == "published"

    # Grant again, then drift before a fresh draft: the draft path must retire
    # the same grant and a reload must still see review after repair.
    regranted = _persist_automatic_grant(restarted)
    assert shared_brain.sharing_consent_active(regranted)
    repair = _alias_drift(kind, clone, tmp_path, monkeypatch)
    refused_draft = share_proposals.draft_share(
        restarted, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"draft during drift\n", request_key=f"during-{kind}",
    )
    assert isinstance(refused_draft, share_proposals.ShareRefusal)
    assert refused_draft.code == "brain_changed"
    after_draft = _reload_persisted_config(config)
    stored = after_draft.workspace("home").shared_brains[0]
    assert stored.sharing_policy == "review"
    assert stored.sharing_consent is None

    repair()
    final = _reload_persisted_config(config)
    assert shared_brain.recover_sharing_state(final)
    accepted = share_proposals.draft_share(
        final, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"reviewed after repair\n", request_key=f"after-{kind}",
    )
    assert isinstance(accepted, share_proposals.ShareDraft)
    result = asyncio.run(share_publish.publish_share(
        final, workspace="home", proposal_id=accepted.proposal_id,
        expected_revision=accepted.artifact["review_token"],
    ))
    assert result.outcome == "published"


def test_failed_invalidation_surfaces_pending_grant_and_restart_demotes(
    linked: tuple[Config, Path, Path], monkeypatch
) -> None:
    from ciao.workspaces import acquire_workspace_registry, persist_workspaces

    config, clone, _origin = linked
    live = _reload_persisted_config(config)
    granted = _persist_automatic_grant(live)
    git(clone, "remote", "set-url", "origin", "https://github.com/x/elsewhere")

    def fail_registry(*_args, **_kwargs):
        raise OSError("registry disk full")

    monkeypatch.setattr("ciao.workspaces.persist_workspaces", fail_registry)
    with acquire_workspace_registry(live) as owner:
        observation = shared_brain.observe_under_fence(live, granted, owner)
    state = observation.state
    assert state["problem"] == "origin_changed"
    assert state["sharing_policy"] == "review"
    assert state["sharing_grant_pending"] is True
    assert state["sharing_invalidation_failed"] is True
    assert live._sharing_recovery_pending is True
    journal = live.state_path.parent / "sharing-revocations.json"
    assert granted.lifetime_id in journal.read_text()

    # The registry still carries the automatic row; a restart applies the
    # journal and never revives consent after repair.
    monkeypatch.setattr("ciao.workspaces.persist_workspaces", persist_workspaces)
    git(clone, "remote", "set-url", "origin", HOSTED)
    restarted = _reload_persisted_config(config)
    assert shared_brain.recover_sharing_state(restarted)
    stored = restarted.workspace("home").shared_brains[0]
    assert stored.sharing_policy == "review"
    assert stored.sharing_consent is None


def test_hostile_local_config_demotes_grant_without_origin_reads(
    linked: tuple[Config, Path, Path], monkeypatch
) -> None:
    config, clone, _origin = linked
    live = _reload_persisted_config(config)
    _persist_automatic_grant(live)
    git(clone, "config", "filter.x.smudge", "cat")
    calls: list[tuple[str, ...]] = []
    original = shared_brain._git

    def record(folder, *args):
        calls.append(tuple(str(arg) for arg in args))
        return original(folder, *args)

    monkeypatch.setattr(shared_brain, "_git", record)
    refused = share_proposals.draft_share(
        live, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"hostile config\n", request_key="hostile-config",
    )
    assert isinstance(refused, share_proposals.ShareRefusal)
    assert refused.code == "brain_unavailable"
    assert calls == [("config", "--list", "--show-scope")]
    reloaded = _reload_persisted_config(config)
    stored = reloaded.workspace("home").shared_brains[0]
    assert stored.sharing_policy == "review"
    assert stored.sharing_consent is None


def test_status_probe_uses_the_same_alias_observation(
    linked: tuple[Config, Path, Path], tmp_path: Path, monkeypatch
) -> None:
    config, clone, _origin = linked
    live = _reload_persisted_config(config)
    granted = _persist_automatic_grant(live)
    _alias_drift("push", clone, tmp_path, monkeypatch)

    state = shared_brain.brain_state(granted, live, "home")
    assert state["problem"] == "origin_changed"
    assert state["sharing_policy"] == "review"
    assert state["sharing_grant_pending"] is False
    assert state["sharing_invalidation_failed"] is False
    reloaded = _reload_persisted_config(config)
    stored = reloaded.workspace("home").shared_brains[0]
    assert stored.sharing_policy == "review"
    assert stored.sharing_consent is None


def test_publish_rejects_changed_upstream_file_mode(linked):
    config, clone, origin = linked
    draft = share_proposals.draft_share(config, workspace="home", relative_path="n.md", operation="replace", proposed=b"reviewed", request_key="mode-change")
    assert isinstance(draft, share_proposals.ShareDraft)
    assert draft.artifact["immutable"]["base_mode"] == "100755"
    git(clone, "update-index", "--chmod=-x", "n.md")
    git(clone, "commit", "--quiet", "-m", "change mode")
    subprocess.run(["git", *IDENT, "-C", str(clone), "-c", "protocol.file.allow=always", "push", "--quiet", str(origin), "HEAD:refs/heads/main"], check=True)
    outcome = asyncio.run(share_publish.publish_share(config, workspace="home", proposal_id=draft.proposal_id, expected_revision=draft.artifact["review_token"]))
    assert outcome.outcome == "changed_upstream"
    assert not share_publish.list_share_receipts(config, workspace="home")


def test_evidence_candidate_cannot_publish_without_effect_fence(linked, monkeypatch):
    config, _, _ = linked
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="new.md", operation="create",
        proposed=b"candidate", request_key="candidate-fence",
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    artifact = draft.artifact
    artifact["immutable"]["evidence_authorization"] = {"proof": "do-not-expose-proof"}
    artifact["review_token"] = share_proposals._review_token(artifact["immutable"])
    for path in share_proposals._store_dir(config).glob("*.json"):
        stored = share_proposals._load_sidecar(path)
        if stored["immutable"]["sidecar_id"] == artifact["immutable"]["sidecar_id"]:
            share_proposals._write_private(path, json.dumps(artifact, sort_keys=True).encode())
    from ciao.web import proposal_service

    _, by_id = proposal_service._scan_proposal_rows(config)
    monkeypatch.setattr(shared_brain, "brain_state", lambda *args, **kwargs: pytest.fail("blocked preview probed destination"))
    preview = proposal_service.preview_row(config, by_id[draft.proposal_id])
    assert preview["blocked_reason"] == "draft_unavailable"
    assert "evidence_authorization" not in preview and "do-not-expose-proof" not in json.dumps(preview)
    monkeypatch.setattr(share_publish, "_network_git", lambda *args, **kwargs: pytest.fail("publication attempted"))
    result = asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=artifact["review_token"],
    ))
    assert result.outcome == "draft_unavailable"
