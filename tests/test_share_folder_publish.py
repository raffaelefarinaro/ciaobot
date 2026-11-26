"""Isolated, no-Git acceptance for publishing one new note into an ordinary folder."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import proposal_actions, share_proposals, share_publish, shared_brain
from ciao.config import SharedBrainLink
from ciao.os_support import folder_io
from ciao.web import proposal_service
from ciao.web.routes_api import memory_receipt_detail, memory_receipts, proposals_history


class Config:
    def __init__(self, root: Path, vault: Path, links: list[SharedBrainLink]) -> None:
        self.workspace_root = root / "install"
        self.state_path = self.workspace_root / ".runtime" / "state.json"
        self.vault = vault
        self.vault_root = vault
        self.vaults = {"home": vault}
        self.links = links
        self.workspaces = {"home": SimpleNamespace(name="home", shared_brains=links)}

    def workspace_names(self) -> list[str]:
        return list(self.vaults)

    def workspace_vault_root(self, name: str) -> Path:
        return self.vaults[name]

    def chats_roots(self) -> list[str]:
        return []

    def workspace(self, name: str | None) -> SimpleNamespace | None:
        return self.workspaces.get(name) if name in self.vaults else None


def _persist(config: Config) -> None:
    path = config.state_path.parent / "workspaces.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "name": name,
            "vault_root": str(config.vaults[name]),
            "shared_brains": [asdict(link) for link in item.shared_brains],
        }
        for name, item in config.workspaces.items()
    ]
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    config._workspace_registry_bytes = path.read_bytes()


def _make_config(root: Path, folder: Path) -> tuple[Config, SharedBrainLink]:
    vault = root / "vault"
    vault.mkdir()
    link = SharedBrainLink(
        name="team", path=str(folder.resolve()), identity=f"folder:{folder.resolve()}",
        lifetime_id="lifetime-one", kind="folder",
    )
    config = Config(root, vault, [link])
    _persist(config)
    return config, link


@pytest.fixture(autouse=True)
def _ancestors_stop_at_the_test_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound the ancestor-stability snapshot to directories this test owns.

    ``ancestor_stamps`` walks to the filesystem root. Above ``tmp_path`` that
    chain is ``pytest-N``, ``pytest-of-<user>`` and the system temp directory,
    which every other concurrent pytest run (parallel agents, xdist workers,
    stress loops) creates and deletes entries in, so their ctime/mtime/nlink
    move between the before and after snapshots and a good publish reads as
    "path changed during the write" (outcome ``indeterminate``). Swap detection
    inside the test tree is unchanged; swaps above it are not exercised here.
    """
    real = folder_io.ancestor_stamps
    root = tmp_path.stat()

    def bounded(parent_fd: int, max_levels: int = 512):
        stamps = real(parent_fd, max_levels)
        current = os.dup(parent_fd)
        try:
            for depth in range(1, len(stamps) + 1):
                upper = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=current)
                os.close(current)
                current = upper
                info = os.fstat(current)
                if (info.st_dev, info.st_ino) == (root.st_dev, root.st_ino):
                    return stamps[:depth]
        finally:
            os.close(current)
        return stamps

    monkeypatch.setattr(folder_io, "ancestor_stamps", bounded)


@pytest.fixture
def folder(tmp_path: Path) -> tuple[Config, Path, SharedBrainLink]:
    root = tmp_path / "case"
    root.mkdir()
    shared = root / "cloud folder"
    (shared / "notes").mkdir(parents=True)
    config, link = _make_config(root, shared)
    return config, shared, link


def _no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("folder publishing must not invoke Git")

    monkeypatch.setattr(shared_brain, "_git", fail)
    monkeypatch.setattr(share_publish, "_safe_git", fail)
    monkeypatch.setattr(share_publish, "_network_git", fail)
    monkeypatch.setattr(share_publish, "run_git_sync_bounded", fail)


def _draft(config: Config, content: bytes = b"# exact\r\n\xce\xbb\n", path: str = "notes/new.md",
           operation: str | None = "create", key: str = "folder-one"):
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path=path, operation=operation,
        proposed=content, request_key=key,
    )
    assert isinstance(draft, share_proposals.ShareDraft), draft
    return draft


def test_evidence_blocked_preview_never_probes_or_stages_folder(folder, monkeypatch):
    config, shared, _ = folder
    draft = _draft(config)
    artifact = draft.artifact
    artifact["immutable"]["evidence_authorization"] = {"proof": "do-not-expose-proof"}
    artifact["review_token"] = share_proposals._review_token(artifact["immutable"])
    for path in share_proposals._store_dir(config).glob("*.json"):
        stored = share_proposals._load_sidecar(path)
        if stored["immutable"]["sidecar_id"] == artifact["immutable"]["sidecar_id"]:
            share_proposals._write_private(path, json.dumps(artifact, sort_keys=True).encode())
    before = sorted(str(path.relative_to(shared)) for path in shared.rglob("*"))
    _, rows = proposal_service._scan_proposal_rows(config)
    for name in ("_folder_target", "_folder_staging_root", "_folder_same_mount"):
        monkeypatch.setattr(share_publish, name, lambda *args, **kwargs: pytest.fail("blocked preview touched the folder"))
    monkeypatch.setattr(shared_brain, "brain_state", lambda *args, **kwargs: pytest.fail("blocked preview probed the folder"))
    preview = proposal_service.preview_row(config, rows[draft.proposal_id])
    assert preview["blocked_reason"] == "draft_unavailable"
    assert "do-not-expose-proof" not in json.dumps(preview)
    assert sorted(str(path.relative_to(shared)) for path in shared.rglob("*")) == before


def _publish(config: Config, draft: share_proposals.ShareDraft) -> share_publish.PublishOutcome:
    return asyncio.run(share_publish.publish_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))


def _attempts(config: Config, draft: share_proposals.ShareDraft) -> list[dict]:
    artifact = share_publish._artifact_for(config, "home", draft.proposal_id)
    assert artifact is not None
    return artifact.get("publication", {}).get("attempts", [])


def _staging_root(config: Config) -> str:
    store = share_proposals._storage_safe(config, "home")
    store_fd = folder_io.open_directory_nofollow(str(store))
    try:
        staging_fd, _identity = folder_io.open_or_create_private_dir(store_fd, "staging")
        os.close(staging_fd)
    finally:
        os.close(store_fd)
    return str(store / "staging")


def test_folder_create_publishes_exact_bytes_without_git_and_replays(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    _no_git(monkeypatch)
    content = b"# exact\r\n\xce\xbb\n"
    draft = _draft(config, content)
    fields = draft.artifact["immutable"]
    assert fields["link_kind"] == "folder"
    assert fields["upstream_ref"] == ""
    assert fields["original"] is None
    assert fields["folder_chain"]

    result = _publish(config, draft)
    assert result.outcome == "published"
    assert (shared / "notes" / "new.md").read_bytes() == content
    assert list((shared / "notes").iterdir()) == [shared / "notes" / "new.md"]
    assert not any(path.name.startswith(".ciao-share-") for path in shared.rglob("*"))

    receipt = share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    )
    assert receipt is not None
    assert receipt["link_kind"] == "folder"
    assert receipt["scope"] == "historical_local_write"
    assert receipt["local_only"] is True
    assert receipt["sync_claimed"] is False
    assert receipt["undoable"] is False
    assert receipt["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert receipt["id"] == result.receipt_id
    assert share_publish.is_share_receipt_undoable(receipt) is False
    assert "exact" not in json.dumps(receipt)

    replay = _publish(config, draft)
    assert replay.outcome == "already_published"
    assert replay.receipt_id == result.receipt_id
    assert (shared / "notes" / "new.md").read_bytes() == content

    queue = (config.vault / "Workspace" / "Memory-Proposals.md").read_text(encoding="utf-8")
    assert "notes/new.md" not in queue
    assert "exact" not in queue
    sidecar = share_proposals._store_dir(config)
    assert sidecar.stat().st_mode & 0o077 == 0


def _receipt_client(config: Config) -> TestClient:
    app = Starlette(routes=[
        Route("/api/memory/receipts/{id}", memory_receipt_detail, methods=["GET"]),
        Route("/api/memory/receipts", memory_receipts, methods=["GET"]),
        Route("/api/proposals/history", proposals_history, methods=["GET"]),
    ])
    app.state.config = config
    return TestClient(app)


def test_folder_receipt_api_projection_is_a_historical_local_write(folder) -> None:
    config, _shared, _link = folder
    draft = _draft(config, b"folder receipt api\n", path="notes/receipt-api.md", key="folder-receipt-api")
    published = _publish(config, draft)
    assert published.outcome == "published"
    client = _receipt_client(config)
    listed = client.get("/api/memory/receipts").json()["rows"]
    row = next(item for item in listed if item["id"] == published.receipt_id)
    assert row["kind"] == "share_publish"
    assert row["undoable"] is False
    assert row["scope"] == "historical_local_write"
    assert row["local_only"] is True
    assert row["sync_claimed"] is False
    detail = client.get(f"/api/memory/receipts/{published.receipt_id}").json()
    assert detail["undoable"] is False
    assert detail["scope"] == "historical_local_write"
    assert detail["local_only"] is True
    assert detail["sync_claimed"] is False
    assert detail["before"] is None
    assert detail["after"] == "folder receipt api\n"
    queue = config.vault / "Workspace" / "Memory-Proposals.md"
    proposal_actions.record_decision(
        queue, action="accept",
        text=f"Shared note change proposal {draft.artifact['immutable']['sidecar_id']}",
        kind="share", via="pwa", outcome="published",
        proposal_id=draft.proposal_id, receipt_id=str(published.receipt_id),
    )
    history = client.get("/api/proposals/history").json()["rows"]
    row = next(item for item in history if item.get("kind") == "share")
    assert row["change"]["scope"] == "historical_local_write"
    assert row["change"]["local_only"] is True
    assert row["change"]["sync_claimed"] is False
    assert row["change"]["undoable"] is False
    assert not row["commit_oid"]


def _tree_snapshot(shared: Path) -> dict[str, tuple[int, int, bytes | None]]:
    rows: dict[str, tuple[int, int, bytes | None]] = {}
    for path in sorted(shared.rglob("*")):
        info = path.lstat()
        rows[str(path.relative_to(shared))] = (
            info.st_ino, info.st_mtime_ns, path.read_bytes() if path.is_file() else None,
        )
    return rows


def _forbid_mutation_and_network(monkeypatch: pytest.MonkeyPatch, shared: Path) -> None:
    """Any Git, network, staging or link call, and any destructive call that
    touches the shared folder (by path or by directory descriptor), fails."""
    import shutil
    import socket

    _no_git(monkeypatch)

    root = str(shared.resolve())

    def always(name: str):
        def raiser(*_args, **_kwargs):
            raise AssertionError(f"unsupported folder update called {name}")
        return raiser

    def guarded(name: str, real):
        def wrapper(*args, **kwargs):
            touches = kwargs.get("dir_fd") is not None or kwargs.get("src_dir_fd") is not None or any(
                isinstance(arg, (str, Path)) and os.path.realpath(arg).startswith(root) for arg in args
            )
            if touches:
                raise AssertionError(f"unsupported folder update called os.{name} on the shared folder")
            return real(*args, **kwargs)
        return wrapper

    for name in ("unlink", "remove", "rename", "replace", "rmdir", "truncate", "link", "symlink", "mkdir"):
        monkeypatch.setattr(os, name, guarded(name, getattr(os, name)))
    for name in ("rmtree", "move", "copy", "copyfile"):
        monkeypatch.setattr(shutil, name, always("shutil." + name))
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, always("socket." + name))
    for name in (
        "link_no_clobber", "create_staging_dir", "create_staged_file",
        "discard_staging", "open_or_create_private_dir",
    ):
        monkeypatch.setattr(share_publish.folder_io, name, always("folder_io." + name))


def test_folder_existing_note_drafts_a_version_bound_diff_and_never_writes(folder) -> None:
    config, shared, _link = folder
    target = shared / "notes" / "existing.md"
    target.write_bytes(b"keep these bytes\r\n")
    before = _tree_snapshot(shared)
    version = "sha256:" + hashlib.sha256(b"keep these bytes\r\n").hexdigest()
    for operation in (None, "replace", "delete"):
        proposed = None if operation == "delete" else b"replacement\n"
        draft = share_proposals.draft_share(
            config, workspace="home", relative_path="notes/existing.md",
            operation=operation, proposed=proposed, request_key=f"existing-{operation}",
        )
        assert isinstance(draft, share_proposals.ShareDraft), draft
        fields = draft.artifact["immutable"]
        assert fields["operation"] == ("delete" if operation == "delete" else "replace")
        assert fields["original"] == "keep these bytes\r\n"
        assert fields["proposed"] == (None if operation == "delete" else "replacement\n")
        assert fields["base_blob_oid"] == version
        assert fields["base_commit"] is None
        assert fields["folder_chain"]
    refusal = share_proposals.draft_share(
        config, workspace="home", relative_path="notes/existing.md",
        operation="create", proposed=b"replacement\n", request_key="existing-create",
    )
    assert isinstance(refusal, share_proposals.ShareRefusal)
    assert refusal.code == "folder_update_unsupported"
    assert _tree_snapshot(shared) == before


def test_folder_existing_note_that_cannot_be_read_safely_is_refused(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    (shared / "notes" / "big.md").write_bytes(b"x" * (share_proposals.MAX_BYTES + 1))
    (shared / "notes" / "binary.md").write_bytes(b"\xff\xfe")
    (shared / "notes" / "secret.md").write_bytes(b"ghp_" + b"a" * 36 + b"\n")
    (shared / "notes" / "cloud.md").write_bytes(b"evicted\n")
    for name, code in (
        ("big.md", "too_large"), ("binary.md", "invalid_utf8"), ("secret.md", "secret_detected"),
    ):
        refusal = share_proposals.draft_share(
            config, workspace="home", relative_path=f"notes/{name}", operation="replace",
            proposed=b"new\n", request_key="unsafe-" + name,
        )
        assert isinstance(refusal, share_proposals.ShareRefusal)
        assert refusal.code == code, (name, refusal)
    monkeypatch.setattr(folder_io, "is_dataless", lambda _info: True)
    refusal = share_proposals.draft_share(
        config, workspace="home", relative_path="notes/cloud.md", operation="replace",
        proposed=b"new\n", request_key="dataless",
    )
    assert isinstance(refusal, share_proposals.ShareRefusal)
    assert refusal.code == "unsafe_target"
    assert (shared / "notes" / "cloud.md").read_bytes() == b"evicted\n"


@pytest.mark.parametrize("operation", ["replace", "delete"])
def test_folder_existing_note_publish_is_unsupported_with_no_git_network_or_destructive_call(
    folder, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    config, shared, _link = folder
    target = shared / "notes" / "existing.md"
    target.write_bytes(b"original\n")
    proposed = None if operation == "delete" else b"proposed\n"
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="notes/existing.md",
        operation=operation, proposed=proposed, request_key="unsupported-" + operation,
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    before = _tree_snapshot(shared)
    _forbid_mutation_and_network(monkeypatch, shared)
    result = _publish(config, draft)
    assert result.outcome == "folder_update_unsupported"
    assert result.receipt_id is None and result.commit_oid is None
    monkeypatch.undo()
    assert _tree_snapshot(shared) == before
    artifact = share_publish._artifact_for(config, "home", draft.proposal_id)
    assert artifact is not None
    assert "publication" not in artifact
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None


@pytest.mark.parametrize("operation", ["replace", "delete"])
def test_folder_existing_note_preview_surfaces_diff_root_path_and_capability_reason(
    folder, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    config, shared, _link = folder
    target = shared / "notes" / "existing.md"
    target.write_bytes(b"original\n")
    proposed = None if operation == "delete" else b"proposed\n"
    draft = share_proposals.draft_share(
        config, workspace="home", relative_path="notes/existing.md",
        operation=operation, proposed=proposed, request_key="preview-" + operation,
    )
    assert isinstance(draft, share_proposals.ShareDraft)
    _rows, by_id = proposal_service._scan_proposal_rows(config)
    ctx = by_id[draft.proposal_id]
    # Review and auto-accept both read this one projection; it must not probe
    # staging or reach Git.
    for name in ("_folder_staging_root", "_folder_same_mount"):
        monkeypatch.setattr(share_publish, name, lambda *a, **k: pytest.fail("preview touched staging"))
    monkeypatch.setattr(shared_brain, "_git", lambda *a, **k: pytest.fail("preview reached Git"))
    preview = proposal_service.preview_row(config, ctx)
    assert preview["blocked_reason"] == "folder_update_unsupported"
    assert preview["operation"] == operation
    assert preview["before"] == "original\n"
    assert preview["after"] == (None if operation == "delete" else "proposed\n")
    assert preview["publish_retry_available"] is False
    manual = preview["folder_manual"]
    assert manual == {
        "root": str(shared.resolve()),
        "path": "notes/existing.md",
        "absolute_path": str(shared.resolve() / "notes" / "existing.md"),
        "operation": operation,
        "reviewed_version": "sha256:" + hashlib.sha256(b"original\n").hexdigest(),
        "current_version": "sha256:" + hashlib.sha256(b"original\n").hexdigest(),
        "state": "ready",
        "reason": "folder_update_unsupported",
    }
    assert preview == proposal_service.preview_row(config, ctx)

    # An external edit moves the folder off the reviewed version.
    target.write_bytes(b"edited elsewhere\n")
    changed = proposal_service.preview_row(config, ctx)["folder_manual"]
    assert changed["state"] == "changed"
    assert changed["current_version"] == "sha256:" + hashlib.sha256(b"edited elsewhere\n").hexdigest()
    assert changed["reviewed_version"] == manual["reviewed_version"]

    # A manual application is observed, never claimed as an engine publish.
    target.write_bytes(proposed) if proposed is not None else target.unlink()
    applied = proposal_service.preview_row(config, ctx)
    assert applied["folder_manual"]["state"] == "applied"
    assert applied["blocked_reason"] == "folder_update_unsupported"
    assert _publish(config, draft).outcome == "folder_update_unsupported"
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None

    if operation == "replace":
        target.unlink()
        assert proposal_service.preview_row(config, ctx)["folder_manual"]["state"] == "changed"
        assert proposal_service.preview_row(config, ctx)["folder_manual"]["current_version"] is None
        target.symlink_to(config.workspace_root.parent)
        unsafe = proposal_service.preview_row(config, ctx)["folder_manual"]
        assert unsafe["state"] == "unavailable"
        assert unsafe["current_version"] is None


def test_folder_create_preview_has_no_manual_surface(folder) -> None:
    config, _shared, _link = folder
    draft = _draft(config, b"fresh\n", path="notes/fresh.md", key="fresh-preview")
    _rows, by_id = proposal_service._scan_proposal_rows(config)
    assert "folder_manual" not in proposal_service.preview_row(config, by_id[draft.proposal_id])


def test_folder_cloud_placeholder_blocks_a_create_before_and_after_review(folder) -> None:
    config, shared, _link = folder
    (shared / "notes" / ".evicted.md.icloud").write_bytes(b"placeholder")
    refusal = share_proposals.draft_share(
        config, workspace="home", relative_path="notes/evicted.md", operation="create",
        proposed=b"never\n", request_key="placeholder-draft",
    )
    assert isinstance(refusal, share_proposals.ShareRefusal)
    assert refusal.code == "unsafe_target"

    draft = _draft(config, b"reviewed\n", path="notes/late.md", key="placeholder-late")
    (shared / "notes" / ".late.md.icloud").write_bytes(b"placeholder")
    before = _tree_snapshot(shared)
    result = _publish(config, draft)
    assert result.outcome == "unsafe_target"
    assert not (shared / "notes" / "late.md").exists()
    assert _tree_snapshot(shared) == before
    assert _attempts(config, draft) == []


_EXTERNAL_WRITER = (
    "import os, sys\n"
    "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)\n"
    "os.write(fd, sys.argv[2].encode()); os.fsync(fd); os.close(fd)\n"
)


@pytest.mark.parametrize("external", ["different\n", "reviewed\n"])
def test_folder_external_writer_process_wins_the_create_race_and_equal_bytes_never_succeed(
    folder, monkeypatch: pytest.MonkeyPatch, external: str,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"reviewed\n", path="notes/proc.md", key="proc-" + external.strip())
    original = share_publish.folder_io.link_no_clobber
    target = shared / "notes" / "proc.md"

    def process_writes_first(*args, **kwargs):
        subprocess.run(
            [sys.executable, "-I", "-c", _EXTERNAL_WRITER, str(target), external],
            check=True, timeout=30,
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", process_writes_first)
    first = _publish(config, draft)
    assert first.outcome == "indeterminate"
    assert first.receipt_id is None
    assert target.read_bytes() == external.encode()
    assert [item.name for item in (shared / "notes").iterdir()] == ["proc.md"]
    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", original)
    replay = _publish(config, draft)
    assert replay.outcome != "published" and replay.receipt_id is None
    assert target.read_bytes() == external.encode()
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None
    assert not any(path.name.startswith(".ciao-share-") for path in shared.rglob("*"))


def test_folder_ancestor_swapped_for_a_symlink_before_the_link_writes_nothing_outside(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"reviewed\n", path="notes/anc.md", key="ancestor-symlink")
    outside = config.workspace_root.parent / "outside-dir"
    outside.mkdir()
    original = share_publish.folder_io.link_no_clobber
    moved = shared / "notes-moved"

    def swap_then_link(*args, **kwargs):
        (shared / "notes").rename(moved)
        (shared / "notes").symlink_to(outside, target_is_directory=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", swap_then_link)
    result = _publish(config, draft)
    assert result.outcome != "published" and result.receipt_id is None
    assert list(outside.iterdir()) == []


def test_folder_crash_between_staging_and_link_leaves_no_destination_and_no_success(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"reviewed\n", path="notes/crash.md", key="staged-crash")

    class Crash(BaseException):
        pass

    def crash(*_args, **_kwargs):
        raise Crash

    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", crash)
    with pytest.raises(Crash):
        _publish(config, draft)
    monkeypatch.undo()
    assert not (shared / "notes" / "crash.md").exists()
    attempts = _attempts(config, draft)
    assert attempts and attempts[-1]["status"] == "pending"
    # An equal-bytes copy that appears afterwards is not evidence of the crashed attempt.
    (shared / "notes" / "crash.md").write_bytes(b"reviewed\n")
    replay = _publish(config, draft)
    assert replay.outcome != "published" and replay.receipt_id is None
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None


def test_folder_concurrent_external_create_wins_and_nothing_is_overwritten(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"reviewed\n", path="notes/race.md", key="race-create")
    original = share_publish.folder_io.link_no_clobber

    def external_writer_wins(
        staging_fd: int, source_name: str, parent_fd: int, final_name: str,
        *, expected_dev: int, expected_ino: int, token: str,
    ) -> None:
        fd = os.open(
            final_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644,
            dir_fd=parent_fd,
        )
        try:
            os.write(fd, b"external writer\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        original(
            staging_fd, source_name, parent_fd, final_name,
            expected_dev=expected_dev, expected_ino=expected_ino, token=token,
        )

    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", external_writer_wins)
    result = _publish(config, draft)
    assert result.outcome == "indeterminate"
    assert (shared / "notes" / "race.md").read_bytes() == b"external writer\n"
    assert list((shared / "notes").iterdir()) == [shared / "notes" / "race.md"]
    attempts = _attempts(config, draft)
    assert attempts[-1]["status"] == "pending"
    assert share_proposals.share_dismissal_block(
        config, workspace="home",
        bullet=SimpleNamespace(
            kind="share",
            target=draft.artifact["immutable"]["sidecar_id"],
            text=f"Shared note change proposal {draft.artifact['immutable']['sidecar_id']}",
        ),
        proposal_id=draft.proposal_id,
    ) is not None


def test_folder_matching_external_bytes_are_not_treated_as_a_completed_write(
    folder,
) -> None:
    config, shared, _link = folder
    content = b"same bytes, different inode\n"
    draft = _draft(config, content, path="notes/same.md", key="same-bytes")
    (shared / "notes" / "same.md").write_bytes(content)
    result = _publish(config, draft)
    assert result.outcome == "changed_upstream"
    assert result.receipt_id is None
    assert (shared / "notes" / "same.md").read_bytes() == content
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None
    assert _attempts(config, draft) == []


def test_folder_atomic_link_materializes_exactly_one_of_two_concurrent_writers(
    folder,
) -> None:
    _config, shared, _link = folder
    parts = ("notes", "winner.md")
    parent_fd, _chain = folder_io.open_chain(str(shared), parts)
    staging = []
    try:
        staging_root = _staging_root(_config)
        for name, content in (("a", b"first\n"), ("b", b"second\n")):
            staging_fd, _identity = folder_io.create_staging_dir(staging_root, f".ciao-share-{name}.d")
            dev, ino = folder_io.create_staged_file(staging_fd, "note", content, name)
            staging.append((staging_fd, dev, ino, name))
        barrier = threading.Barrier(2)

        def link(item) -> str:
            staging_fd, dev, ino, token = item
            barrier.wait(timeout=5)
            try:
                folder_io.link_no_clobber(
                    staging_fd, "note", parent_fd, "winner.md",
                    expected_dev=dev, expected_ino=ino, token=token,
                )
            except FileExistsError:
                return "lost"
            return "won"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(link, staging))
    finally:
        for staging_fd, _dev, _ino, _token in staging:
            os.close(staging_fd)
        os.close(parent_fd)
    assert outcomes == ["lost", "won"]
    assert (shared / "notes" / "winner.md").read_bytes() in {b"first\n", b"second\n"}


def test_folder_pending_attempt_without_a_destination_is_indeterminate_and_not_retried(
    folder,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"not linked\n", path="notes/pending.md", key="pending-crash")
    fields = draft.artifact["immutable"]
    token = secrets.token_urlsafe(12)
    attempt = {
        "attempt": 1, "kind": "folder", "status": "pending", "token": token,
        "staging_name": f".ciao-share-{token}.d",
        "staging_root": _staging_root(config),
        "proposal_id": fields["proposal_id"], "revision": fields["revision"],
        "destination_fingerprint": fields["destination_fingerprint"],
        "path": fields["path"],
    }
    share_publish._write_state(config, "home", fields, {"attempts": [attempt]})

    result = _publish(config, draft)
    assert result.outcome == "indeterminate"
    assert not (shared / "notes" / "pending.md").exists()
    assert len(_attempts(config, draft)) == 1
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None
    reconciled = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert reconciled.outcome == "indeterminate"
    assert len(_attempts(config, draft)) == 1


def test_folder_crash_after_link_without_flush_marker_is_indeterminate(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    _no_git(monkeypatch)
    content = b"linked before the receipt\n"
    draft = _draft(config, content, path="notes/crashed.md", key="crash-after-link")
    fields = draft.artifact["immutable"]
    token = secrets.token_urlsafe(12)
    staging_name = f".ciao-share-{token}.d"
    parts = tuple(fields["path"].split("/"))
    parent_fd, _chain = folder_io.open_chain(str(shared), parts)
    try:
        staging_fd, staging_identity = folder_io.create_staging_dir(_staging_root(config), staging_name)
        try:
            temp_dev, temp_ino = folder_io.create_staged_file(staging_fd, "note", content, token)
            folder_io.link_no_clobber(
                staging_fd, "note", parent_fd, parts[-1],
                expected_dev=temp_dev, expected_ino=temp_ino, token=token,
            )
            folder_io.fsync_directory(parent_fd)
        finally:
            os.close(staging_fd)
    finally:
        os.close(parent_fd)
    attempt = {
        "attempt": 1, "kind": "folder", "status": "pending", "token": token,
        "staging_name": staging_name,
        "staging_root": _staging_root(config),
        "staging_dev": staging_identity.dev, "staging_ino": staging_identity.ino,
        "temp_dev": temp_dev, "temp_ino": temp_ino,
        "proposal_id": fields["proposal_id"], "revision": fields["revision"],
        "destination_fingerprint": fields["destination_fingerprint"],
        "path": fields["path"],
    }
    share_publish._write_state(config, "home", fields, {"attempts": [attempt]})

    reconciled = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert reconciled.outcome == "indeterminate"
    assert (shared / "notes" / "crashed.md").read_bytes() == content
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None

    # A durable "linked" marker alone is still before the directory flush.
    attempt["status"] = "linked"
    share_publish._write_state(config, "home", fields, {"attempts": [attempt]})
    linked_only = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert linked_only.outcome == "indeterminate"

    # A later durable flush marker, written after the directory flush, makes
    # the same live staged inode confirmable without writing again.
    attempt["status"] = "flushed"
    share_publish._write_state(config, "home", fields, {"attempts": [attempt]})
    confirmed = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert confirmed.outcome == "published"
    assert not (Path(_staging_root(config)) / staging_name).exists()
    assert not any(path.name.startswith(".ciao-share-") for path in shared.rglob("*"))


def test_folder_cleanup_never_unlinks_a_replaced_staging_file(folder) -> None:
    _config, shared, _link = folder
    parent_fd, _chain = folder_io.open_chain(str(shared), ("notes", "cleanup.md"))
    staging_name = ".ciao-share-cleanup.d"
    staging_fd = -1
    try:
        staging_fd, _identity = folder_io.create_staging_dir(_staging_root(_config), staging_name)
        dev, ino = folder_io.create_staged_file(staging_fd, "note", b"ours\n", "cleanup-token")
        os.unlink("note", dir_fd=staging_fd)
        fd = os.open(
            "note", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=staging_fd,
        )
        try:
            os.write(fd, b"external replacement\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        folder_io.discard_staging(
            staging_fd, staging_name, _staging_root(_config),
            expected_dev=dev, expected_ino=ino, token="cleanup-token",
        )
        # The replacement is not ours: it must survive, and the non-empty
        # staging directory must not be removed.
        replacement = folder_io.stat_entry(staging_fd, "note")
        assert replacement is not None
        note_fd = folder_io.open_regular(staging_fd, "note")
        try:
            assert folder_io.read_all(note_fd, 64) == b"external replacement\n"
        finally:
            os.close(note_fd)
        assert folder_io.stat_entry(parent_fd, staging_name) is None
        assert os.path.isdir(os.path.join(_staging_root(_config), staging_name))
    finally:
        if staging_fd >= 0:
            try:
                os.unlink("note", dir_fd=staging_fd)
            except OSError:
                pass
            os.close(staging_fd)
        try:
            os.rmdir(os.path.join(_staging_root(_config), staging_name))
        except OSError:
            pass
        os.close(parent_fd)


def test_folder_source_swap_before_link_is_indeterminate_and_not_published(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"reviewed\n", path="notes/swap.md", key="source-swap")
    original = share_publish.folder_io.link_no_clobber

    def swap_source(
        staging_fd: int, source_name: str, parent_fd: int, final_name: str,
        *, expected_dev: int, expected_ino: int, token: str,
    ) -> None:
        os.unlink(source_name, dir_fd=staging_fd)
        fd = os.open(
            source_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644,
            dir_fd=staging_fd,
        )
        try:
            os.write(fd, b"external\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        original(
            staging_fd, source_name, parent_fd, final_name,
            expected_dev=expected_dev, expected_ino=expected_ino, token=token,
        )

    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", swap_source)
    result = _publish(config, draft)
    assert result.outcome == "indeterminate"
    assert not (shared / "notes" / "swap.md").exists()
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None
    attempt = _attempts(config, draft)[-1]
    assert attempt["status"] == "pending"
    parent_fd, _chain = folder_io.open_chain(str(shared), ("notes", "swap.md"))
    staging_fd = -1
    try:
        staging_fd = folder_io.open_staging_dir(
            _staging_root(config), attempt["staging_name"],
            expected_dev=attempt["staging_dev"], expected_ino=attempt["staging_ino"],
        )
        note_fd = folder_io.open_regular(staging_fd, "note")
        try:
            assert folder_io.read_all(note_fd, 64) == b"external\n"
        finally:
            os.close(note_fd)
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)
        os.close(parent_fd)


def test_folder_parent_detached_after_link_is_indeterminate(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    nested = shared / "nested"
    moved = shared / "nested-moved"
    nested.mkdir()
    draft = _draft(config, b"detached\n", path="nested/detached.md", key="detached-parent")
    original = share_publish.folder_io.link_no_clobber

    def detach_after_link(
        staging_fd: int, source_name: str, parent_fd: int, final_name: str,
        *, expected_dev: int, expected_ino: int, token: str,
    ) -> None:
        original(
            staging_fd, source_name, parent_fd, final_name,
            expected_dev=expected_dev, expected_ino=expected_ino, token=token,
        )
        nested.rename(moved)
        nested.mkdir()

    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", detach_after_link)
    result = _publish(config, draft)
    assert result.outcome == "indeterminate"
    assert (moved / "detached.md").read_bytes() == b"detached\n"
    assert not (nested / "detached.md").exists()
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None


def test_folder_ambiguous_link_error_stays_indeterminate(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"ambiguous\n", path="notes/ambiguous.md", key="ambiguous-link")

    def ambiguous(*_args, **_kwargs):
        raise OSError(errno.EIO, "I/O error after the link may have completed")

    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", ambiguous)
    result = _publish(config, draft)
    assert result.outcome == "indeterminate"
    assert not (shared / "notes" / "ambiguous.md").exists()
    attempt = _attempts(config, draft)[-1]
    assert attempt["status"] == "pending"
    assert not share_publish.can_retry_publication(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    )
    reconciled = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert reconciled.outcome == "indeterminate"


def test_folder_reused_inode_and_matching_bytes_without_tag_are_not_success(folder) -> None:
    config, shared, _link = folder
    content = b"reused inode bytes\n"
    draft = _draft(config, content, path="notes/reused.md", key="reused-inode")
    fields = draft.artifact["immutable"]
    token = secrets.token_urlsafe(12)
    staging_name = f".ciao-share-{token}.d"
    parent_fd, _chain = folder_io.open_chain(str(shared), tuple(fields["path"].split("/")))
    staging_fd = -1
    try:
        final = os.open(
            "reused.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=parent_fd,
        )
        try:
            os.write(final, content)
            os.fsync(final)
            final_stat = os.fstat(final)
        finally:
            os.close(final)
        staging_fd, identity = folder_io.create_staging_dir(_staging_root(config), staging_name)
        os.link("reused.md", "note", src_dir_fd=parent_fd, dst_dir_fd=staging_fd)
        attempt = {
            "attempt": 1, "kind": "folder", "status": "pending", "token": token,
            "staging_name": staging_name,
            "staging_root": _staging_root(config),
            "staging_dev": identity.dev, "staging_ino": identity.ino,
            "temp_dev": final_stat.st_dev, "temp_ino": final_stat.st_ino,
            "proposal_id": fields["proposal_id"], "revision": fields["revision"],
            "destination_fingerprint": fields["destination_fingerprint"],
            "path": fields["path"],
        }
        share_publish._write_state(config, "home", fields, {"attempts": [attempt]})
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)
        os.close(parent_fd)

    reconciled = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert reconciled.outcome == "indeterminate"
    assert (shared / "notes" / "reused.md").read_bytes() == content
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None


def test_folder_parent_away_and_back_inside_link_is_indeterminate(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    nested = shared / "nested"
    moved = shared / "nested-moved"
    nested.mkdir()
    draft = _draft(config, b"moved back\n", path="nested/back.md", key="move-away-back")
    original = share_publish.folder_io.link_no_clobber

    def move_away_and_back_inside_link(
        staging_fd: int, source_name: str, parent_fd: int, final_name: str,
        *, expected_dev: int, expected_ino: int, token: str,
    ) -> None:
        nested.rename(moved)
        original(
            staging_fd, source_name, parent_fd, final_name,
            expected_dev=expected_dev, expected_ino=expected_ino, token=token,
        )
        moved.rename(nested)

    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", move_away_and_back_inside_link)
    result = _publish(config, draft)
    assert result.outcome == "indeterminate"
    assert result.receipt_id is None
    assert (nested / "back.md").read_bytes() == b"moved back\n"
    assert _attempts(config, draft)[-1]["status"] == "pending"
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None


def test_folder_destination_swap_at_receipt_uses_the_verified_inode(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"verified inode\n", path="notes/swap-receipt.md", key="receipt-swap")
    original_make = share_publish._make_folder_receipt
    seen: dict[str, int] = {}

    def swap_before_receipt(fields, token, proposed, entry):
        seen["dev"] = entry.st_dev
        seen["ino"] = entry.st_ino
        target = shared / "notes" / "swap-receipt.md"
        target.unlink()
        target.write_bytes(b"external at receipt\n")
        return original_make(fields, token, proposed, entry)

    monkeypatch.setattr(share_publish, "_make_folder_receipt", swap_before_receipt)
    result = _publish(config, draft)
    assert result.outcome == "published"
    receipt = share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    )
    assert receipt is not None
    assert receipt["file_dev"] == seen["dev"]
    assert receipt["file_ino"] == seen["ino"]
    assert (shared / "notes" / "swap-receipt.md").read_bytes() == b"external at receipt\n"


def test_folder_destination_deletion_at_receipt_keeps_historical_inode(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"deleted at receipt\n", path="notes/delete-receipt.md", key="receipt-delete")
    original_make = share_publish._make_folder_receipt
    seen: dict[str, int] = {}

    def delete_before_receipt(fields, token, proposed, entry):
        seen["ino"] = entry.st_ino
        (shared / "notes" / "delete-receipt.md").unlink()
        return original_make(fields, token, proposed, entry)

    monkeypatch.setattr(share_publish, "_make_folder_receipt", delete_before_receipt)
    result = _publish(config, draft)
    assert result.outcome == "published"
    receipt = share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    )
    assert receipt is not None
    assert receipt["file_ino"] == seen["ino"]
    assert not (shared / "notes" / "delete-receipt.md").exists()


def test_folder_cross_volume_requires_manual_export(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"manual\n", path="notes/manual.md", key="cross-volume")
    monkeypatch.setattr(share_publish, "_folder_same_mount", lambda *_args: False)
    result = _publish(config, draft)
    assert result.outcome == "manual_export_required"
    assert result.receipt_id is None
    assert not (shared / "notes" / "manual.md").exists()
    assert _attempts(config, draft) == []
    assert not any(path.name.startswith(".ciao-share-") for path in shared.rglob("*"))


def test_folder_unsupported_link_requires_manual_export(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"no link support\n", path="notes/link-unsupported.md", key="link-unsupported")

    def unsupported(*_args, **_kwargs):
        raise folder_io.UnsupportedFilesystem(errno.ENOTSUP, "hard links unsupported")

    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", unsupported)
    result = _publish(config, draft)
    assert result.outcome == "manual_export_required"
    assert not (shared / "notes" / "link-unsupported.md").exists()
    attempt = _attempts(config, draft)[-1]
    assert attempt["status"] == "rejected"
    assert attempt["retryable"] is False
    assert attempt["reason"] == "unsupported_mount"
    assert not any(path.name.startswith(".ciao-share-") for path in shared.rglob("*"))
    assert not any(name.startswith(".ciao-share-") for name in os.listdir(_staging_root(config)))


@pytest.mark.parametrize(
    "error_code",
    [
        errno.ENOSPC, errno.EDQUOT, errno.EACCES, errno.EPERM, errno.EROFS,
        errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EMLINK, errno.EXDEV,
        errno.EINVAL, errno.EIO, errno.ENOTSUP, errno.EEXIST,
    ],
)
def test_folder_commit_then_each_classified_error_stays_indeterminate(
    folder, monkeypatch: pytest.MonkeyPatch, error_code: int,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"committed then error\n", path="notes/commit-error.md", key="commit-error")
    original = share_publish.folder_io.link_no_clobber

    def commit_then_error(
        staging_fd: int, source_name: str, parent_fd: int, final_name: str,
        *, expected_dev: int, expected_ino: int, token: str,
    ) -> None:
        original(
            staging_fd, source_name, parent_fd, final_name,
            expected_dev=expected_dev, expected_ino=expected_ino, token=token,
        )
        raise OSError(error_code, "link succeeded but the syscall reported an error")

    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", commit_then_error)
    result = _publish(config, draft)
    assert result.outcome == "indeterminate"
    assert (shared / "notes" / "commit-error.md").read_bytes() == b"committed then error\n"
    assert _attempts(config, draft)[-1]["status"] == "pending"
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None
    reconciled = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert reconciled.outcome == "indeterminate"


@pytest.mark.parametrize("status", ["pending", "linked"])
def test_folder_replaced_destination_after_unproven_link_stays_indeterminate(
    folder, status: str,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"unproven\n", path="notes/replaced-pending.md", key="replaced-unproven")
    fields = draft.artifact["immutable"]
    token = secrets.token_urlsafe(12)
    staging_name = f".ciao-share-{token}.d"
    parent_fd, _chain = folder_io.open_chain(str(shared), tuple(fields["path"].split("/")))
    staging_fd = -1
    try:
        staging_fd, identity = folder_io.create_staging_dir(_staging_root(config), staging_name)
        temp_dev, temp_ino = folder_io.create_staged_file(staging_fd, "note", b"unproven\n", token)
        target = shared / "notes" / "replaced-pending.md"
        target.write_bytes(b"external replacement\n")
        attempt = {
            "attempt": 1, "kind": "folder", "status": status, "token": token,
            "staging_name": staging_name,
            "staging_root": _staging_root(config),
            "staging_dev": identity.dev, "staging_ino": identity.ino,
            "temp_dev": temp_dev, "temp_ino": temp_ino,
            "proposal_id": fields["proposal_id"], "revision": fields["revision"],
            "destination_fingerprint": fields["destination_fingerprint"],
            "path": fields["path"],
        }
        share_publish._write_state(config, "home", fields, {"attempts": [attempt]})
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)
        os.close(parent_fd)

    reconciled = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert reconciled.outcome == "indeterminate"
    assert target.read_bytes() == b"external replacement\n"
    preserved = _attempts(config, draft)[-1]
    assert preserved["status"] == status
    assert "retryable" not in preserved
    assert "reason" not in preserved
    assert os.path.isdir(os.path.join(_staging_root(config), staging_name))
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None


def test_folder_unverified_rename_observability_requires_manual_export(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"no rename guard\n", path="notes/no-guard.md", key="no-rename-guard")
    monkeypatch.setattr(share_publish.folder_io, "verified_rename_observability", lambda _root: False)
    result = _publish(config, draft)
    assert result.outcome == "manual_export_required"
    assert not (shared / "notes" / "no-guard.md").exists()
    assert _attempts(config, draft) == []
    assert not any(path.name.startswith(".ciao-share-") for path in shared.rglob("*"))


def test_folder_mount_staging_uses_trusted_staging_on_destination_volume(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    mounted = config.workspace_root.parent / "mounted volume"
    mounted.mkdir()
    engine_root = _staging_root(config)
    engine_ino = os.stat(engine_root).st_ino

    def not_engine_mount(left, _right):
        try:
            ino = os.fstat(left).st_ino
        except (TypeError, OSError):
            ino = os.stat(left).st_ino
        return ino != engine_ino

    monkeypatch.setattr(share_publish.folder_io, "same_mount", not_engine_mount)
    monkeypatch.setattr(share_publish, "_folder_same_mount", not_engine_mount)
    monkeypatch.setattr(
        share_publish, "_folder_volume_root",
        lambda _fields, _parent: str(mounted),
    )
    draft = _draft(config, b"mounted save\n", path="notes/mounted.md", key="mounted-ok")
    result = _publish(config, draft)
    assert result.outcome == "published"
    assert (shared / "notes" / "mounted.md").read_bytes() == b"mounted save\n"
    attempt = _attempts(config, draft)[-1]
    assert attempt["staging_root"] == str(mounted / share_publish.MOUNT_STAGING_DIR)
    assert not any(path.name.startswith(".ciao-share-") for path in shared.rglob("*"))


def test_folder_mount_staging_inside_a_synced_tree_requires_manual_export(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    engine_root = _staging_root(config)
    engine_ino = os.stat(engine_root).st_ino

    def not_engine_mount(left, _right):
        try:
            ino = os.fstat(left).st_ino
        except (TypeError, OSError):
            ino = os.stat(left).st_ino
        return ino != engine_ino

    monkeypatch.setattr(share_publish.folder_io, "same_mount", not_engine_mount)
    monkeypatch.setattr(share_publish, "_folder_same_mount", not_engine_mount)
    monkeypatch.setattr(
        share_publish, "_folder_volume_root",
        lambda _fields, _parent: str(shared),
    )
    draft = _draft(config, b"unsafe staging\n", path="notes/unsafe-staging.md", key="mounted-unsafe")
    result = _publish(config, draft)
    assert result.outcome == "manual_export_required"
    assert not (shared / "notes" / "unsafe-staging.md").exists()
    assert _attempts(config, draft) == []
    assert not any(path.name.startswith(".ciao-share-") for path in shared.rglob("*"))


def test_folder_mount_staging_unavailable_requires_manual_export(
    folder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("file", encoding="utf-8")
    engine_root = _staging_root(config)
    engine_ino = os.stat(engine_root).st_ino

    def not_engine_mount(left, _right):
        try:
            ino = os.fstat(left).st_ino
        except (TypeError, OSError):
            ino = os.stat(left).st_ino
        return ino != engine_ino

    monkeypatch.setattr(share_publish.folder_io, "same_mount", not_engine_mount)
    monkeypatch.setattr(share_publish, "_folder_same_mount", not_engine_mount)
    monkeypatch.setattr(
        share_publish, "_folder_volume_root",
        lambda _fields, _parent: str(not_a_directory),
    )
    draft = _draft(config, b"no staging\n", path="notes/no-staging.md", key="mounted-missing")
    result = _publish(config, draft)
    assert result.outcome == "manual_export_required"
    assert not (shared / "notes" / "no-staging.md").exists()
    assert _attempts(config, draft) == []


def test_folder_hardlink_probe_failure_requires_manual_export(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    monkeypatch.setattr(share_publish.folder_io, "verified_hardlink_support", lambda _root: False)
    draft = _draft(config, b"no hardlink\n", path="notes/no-hardlink.md", key="no-hardlink")
    result = _publish(config, draft)
    assert result.outcome == "manual_export_required"
    assert not (shared / "notes" / "no-hardlink.md").exists()
    assert _attempts(config, draft) == []


def test_folder_mkdir_to_open_replacement_is_not_chmodded(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, shared, _link = folder
    parent_fd, _chain = folder_io.open_chain(str(shared), ("notes", "race.md"))
    name = ".ciao-race-staging"
    original_open = folder_io.os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and path == name and dir_fd == parent_fd:
            swapped = True
            os.rmdir(name, dir_fd=parent_fd)
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            substitute_fd = original_open(
                name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0), dir_fd=parent_fd,
            )
            try:
                os.fchmod(substitute_fd, 0o755)
            finally:
                os.close(substitute_fd)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    try:
        monkeypatch.setattr(folder_io.os, "open", racing_open)
        with pytest.raises(folder_io.UnsupportedFilesystem):
            folder_io.open_or_create_private_dir(parent_fd, name)
        assert swapped
        assert os.stat(os.path.join(str(shared), "notes", name)).st_mode & 0o777 == 0o755
    finally:
        monkeypatch.setattr(folder_io.os, "open", original_open)
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError:
            pass
        os.close(parent_fd)


def test_folder_staging_owner_mismatch_is_refused_without_chmod(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, shared, _link = folder
    parent_fd, _chain = folder_io.open_chain(str(shared), ("notes", "owner.md"))
    name = ".ciao-owner-staging"
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    os.chmod(os.path.join(str(shared), "notes", name), 0o700)
    try:
        import ciao.os_support.private as private_mod

        monkeypatch.setattr(private_mod.os, "geteuid", lambda: os.getuid() + 1)
        with pytest.raises(folder_io.UnsupportedFilesystem):
            folder_io.open_or_create_private_dir(parent_fd, name)
        assert os.stat(os.path.join(str(shared), "notes", name)).st_mode & 0o777 == 0o700
    finally:
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError:
            pass
        os.close(parent_fd)


def test_folder_staging_linux_acl_grant_is_refused_without_chmod(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, shared, _link = folder
    parent_fd, _chain = folder_io.open_chain(str(shared), ("notes", "linux-acl.md"))
    name = ".ciao-linux-acl-staging"
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    monkeypatch.setattr(folder_io.sys, "platform", "linux")
    monkeypatch.setattr(folder_io, "descriptor_posix_acl_blob", lambda _fd: b"acl")
    monkeypatch.setattr(folder_io, "acl_blob_names_other_principals", lambda _blob: True)
    try:
        with pytest.raises(folder_io.UnsupportedFilesystem):
            folder_io.open_or_create_private_dir(parent_fd, name)
        assert os.stat(os.path.join(str(shared), "notes", name)).st_mode & 0o777 == 0o700
    finally:
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError:
            pass
        os.close(parent_fd)


def _force_mounted_staging(monkeypatch: pytest.MonkeyPatch, config: Config, mounted: Path) -> None:
    engine_root = _staging_root(config)
    engine_ino = os.stat(engine_root).st_ino

    def not_engine_mount(left, _right):
        try:
            ino = os.fstat(left).st_ino
        except (TypeError, OSError):
            ino = os.stat(left).st_ino
        return ino != engine_ino

    monkeypatch.setattr(share_publish.folder_io, "same_mount", not_engine_mount)
    monkeypatch.setattr(share_publish, "_folder_same_mount", not_engine_mount)
    monkeypatch.setattr(share_publish, "_folder_volume_root", lambda _fields, _parent: str(mounted))


def test_folder_staging_symlink_target_permissions_are_unchanged(
    folder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    mounted = tmp_path / "mounted-symlink"
    mounted.mkdir()
    target = tmp_path / "symlink-target"
    target.mkdir()
    os.chmod(target, 0o755)
    (mounted / share_publish.MOUNT_STAGING_DIR).symlink_to(target, target_is_directory=True)
    _force_mounted_staging(monkeypatch, config, mounted)
    draft = _draft(config, b"no symlink chmod\n", path="notes/symlink-staging.md", key="symlink-staging")
    result = _publish(config, draft)
    assert result.outcome == "manual_export_required"
    assert (mounted / share_publish.MOUNT_STAGING_DIR).is_symlink()
    assert os.stat(target).st_mode & 0o777 == 0o755
    assert not (shared / "notes" / "symlink-staging.md").exists()
    assert _attempts(config, draft) == []


def test_folder_preexisting_staging_mode_is_not_modified(
    folder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    mounted = tmp_path / "mounted-mode"
    mounted.mkdir()
    staging = mounted / share_publish.MOUNT_STAGING_DIR
    staging.mkdir(mode=0o755)
    os.chmod(staging, 0o755)
    _force_mounted_staging(monkeypatch, config, mounted)
    draft = _draft(config, b"no mode chmod\n", path="notes/mode-staging.md", key="mode-staging")
    result = _publish(config, draft)
    assert result.outcome == "manual_export_required"
    assert os.stat(staging).st_mode & 0o777 == 0o755
    assert not (shared / "notes" / "mode-staging.md").exists()


def test_folder_preexisting_staging_acl_grant_is_refused_without_change(
    folder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("the extended-ACL check is macOS-specific")
    config, shared, _link = folder
    mounted = tmp_path / "mounted-acl"
    mounted.mkdir()
    staging = mounted / share_publish.MOUNT_STAGING_DIR
    staging.mkdir(mode=0o700)
    added = subprocess.run(
        ["chmod", "+a", "everyone allow read", str(staging)],
        capture_output=True, text=True, check=False,
    )
    if added.returncode != 0:
        pytest.skip(f"this filesystem cannot set an ACL: {added.stderr.strip()}")
    _force_mounted_staging(monkeypatch, config, mounted)
    draft = _draft(config, b"no acl chmod\n", path="notes/acl-staging.md", key="acl-staging")
    result = _publish(config, draft)
    assert result.outcome == "manual_export_required"
    assert os.stat(staging).st_mode & 0o777 == 0o700
    fd = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        assert not folder_io.is_private_descriptor_strict(fd)
    finally:
        os.close(fd)
    assert not (shared / "notes" / "acl-staging.md").exists()


def test_folder_inaccessible_mount_root_requires_manual_export(
    folder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    mounted = tmp_path / "mounted-inaccessible"
    mounted.mkdir()
    os.chmod(mounted, 0)
    try:
        _force_mounted_staging(monkeypatch, config, mounted)
        draft = _draft(config, b"no mount access\n", path="notes/inaccessible-mount.md", key="inaccessible-mount")
        result = _publish(config, draft)
        assert result.outcome == "manual_export_required"
        assert not (shared / "notes" / "inaccessible-mount.md").exists()
        assert _attempts(config, draft) == []
    finally:
        os.chmod(mounted, 0o755)


def test_folder_sync_owned_staging_is_refused_without_modification(
    folder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    mounted = tmp_path / "mounted-sync-owned"
    mounted.mkdir()
    staging = mounted / share_publish.MOUNT_STAGING_DIR
    staging.mkdir(mode=0o700)
    staging_inode = os.stat(staging).st_ino
    original_strict = folder_io.is_private_descriptor_strict

    def sync_owned(fd: int) -> bool:
        if os.fstat(fd).st_ino == staging_inode:
            return False
        return original_strict(fd)

    monkeypatch.setattr(folder_io, "is_private_descriptor_strict", sync_owned)
    _force_mounted_staging(monkeypatch, config, mounted)
    draft = _draft(config, b"sync owned\n", path="notes/sync-owned.md", key="sync-owned")
    result = _publish(config, draft)
    assert result.outcome == "manual_export_required"
    assert os.stat(staging).st_mode & 0o777 == 0o700
    assert not (shared / "notes" / "sync-owned.md").exists()


def test_folder_move_before_ancestor_snapshot_is_indeterminate(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    nested = shared / "nested"
    moved = shared / "nested-moved"
    nested.mkdir()
    draft = _draft(config, b"snapshot window\n", path="nested/snapshot.md", key="snapshot-window")
    original_stamps = share_publish.folder_io.ancestor_stamps
    calls = 0

    def move_around_first_snapshot(parent_fd: int):
        nonlocal calls
        if calls == 0:
            calls += 1
            nested.rename(moved)
            stamps = original_stamps(parent_fd)
            moved.rename(nested)
            return stamps
        return original_stamps(parent_fd)

    monkeypatch.setattr(share_publish.folder_io, "ancestor_stamps", move_around_first_snapshot)
    result = _publish(config, draft)
    assert result.outcome == "indeterminate"
    assert (nested / "snapshot.md").read_bytes() == b"snapshot window\n"
    assert _attempts(config, draft)[-1]["status"] == "pending"
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None


def test_folder_higher_ancestor_away_and_back_is_indeterminate(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    root = shared.parent
    moved = root.parent / (root.name + "-moved")
    nested = shared / "nested"
    nested.mkdir()
    draft = _draft(config, b"higher ancestor\n", path="nested/higher.md", key="higher-ancestor")
    original = share_publish.folder_io.link_no_clobber

    def move_root_away_and_back(
        staging_fd: int, source_name: str, parent_fd: int, final_name: str,
        *, expected_dev: int, expected_ino: int, token: str,
    ) -> None:
        root.rename(moved)
        try:
            original(
                staging_fd, source_name, parent_fd, final_name,
                expected_dev=expected_dev, expected_ino=expected_ino, token=token,
            )
        finally:
            moved.rename(root)

    monkeypatch.setattr(share_publish.folder_io, "link_no_clobber", move_root_away_and_back)
    result = _publish(config, draft)
    assert result.outcome == "indeterminate"
    assert (nested / "higher.md").read_bytes() == b"higher ancestor\n"
    assert _attempts(config, draft)[-1]["status"] == "pending"
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None


def test_folder_degraded_probe_reconciles_existing_flushed_attempt(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    content = b"flushed before capability loss\n"
    draft = _draft(config, content, path="notes/flushed-first.md", key="flushed-first")
    fields = draft.artifact["immutable"]
    token = secrets.token_urlsafe(12)
    staging_name = f".ciao-share-{token}.d"
    parts = tuple(fields["path"].split("/"))
    parent_fd, _chain = folder_io.open_chain(str(shared), parts)
    try:
        staging_fd, identity = folder_io.create_staging_dir(_staging_root(config), staging_name)
        try:
            temp_dev, temp_ino = folder_io.create_staged_file(staging_fd, "note", content, token)
            folder_io.link_no_clobber(
                staging_fd, "note", parent_fd, parts[-1],
                expected_dev=temp_dev, expected_ino=temp_ino, token=token,
            )
            folder_io.fsync_directory(parent_fd)
        finally:
            os.close(staging_fd)
    finally:
        os.close(parent_fd)
    attempt = {
        "attempt": 1, "kind": "folder", "status": "flushed", "token": token,
        "staging_name": staging_name,
        "staging_root": _staging_root(config),
        "staging_dev": identity.dev, "staging_ino": identity.ino,
        "temp_dev": temp_dev, "temp_ino": temp_ino,
        "proposal_id": fields["proposal_id"], "revision": fields["revision"],
        "destination_fingerprint": fields["destination_fingerprint"],
        "path": fields["path"],
    }
    share_publish._write_state(config, "home", fields, {"attempts": [attempt]})
    monkeypatch.setattr(share_publish.folder_io, "verified_rename_observability", lambda _root: False)
    result = _publish(config, draft)
    assert result.outcome == "published"
    assert result.receipt_id is not None
    assert (shared / "notes" / "flushed-first.md").read_bytes() == content


def test_folder_degraded_probe_keeps_unresolved_pending_attempt_indeterminate(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"pending first\n", path="notes/pending-first.md", key="pending-first")
    fields = draft.artifact["immutable"]
    token = secrets.token_urlsafe(12)
    attempt = {
        "attempt": 1, "kind": "folder", "status": "pending", "token": token,
        "staging_name": f".ciao-share-{token}.d",
        "staging_root": _staging_root(config),
        "proposal_id": fields["proposal_id"], "revision": fields["revision"],
        "destination_fingerprint": fields["destination_fingerprint"],
        "path": fields["path"],
    }
    share_publish._write_state(config, "home", fields, {"attempts": [attempt]})
    monkeypatch.setattr(share_publish.folder_io, "verified_rename_observability", lambda _root: False)
    result = _publish(config, draft)
    assert result.outcome == "indeterminate"
    assert not (shared / "notes" / "pending-first.md").exists()
    assert _attempts(config, draft)[-1]["status"] == "pending"


def test_folder_copied_tag_and_equal_bytes_do_not_prove_the_write(folder) -> None:
    config, shared, _link = folder
    content = b"copied tag and bytes\n"
    draft = _draft(config, content, path="notes/copied.md", key="copied-tag")
    fields = draft.artifact["immutable"]
    token = secrets.token_urlsafe(12)
    staging_name = f".ciao-share-{token}.d"
    parent_fd, _chain = folder_io.open_chain(str(shared), tuple(fields["path"].split("/")))
    staging_fd = -1
    try:
        staging_fd, identity = folder_io.create_staging_dir(_staging_root(config), staging_name)
        temp_dev, temp_ino = folder_io.create_staged_file(staging_fd, "note", content, token)
        external = os.open(
            "copied.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=parent_fd,
        )
        try:
            os.write(external, content)
            folder_io.set_operation_tag(external, token)
            os.fsync(external)
        finally:
            os.close(external)
        attempt = {
            "attempt": 1, "kind": "folder", "status": "flushed", "token": token,
            "staging_name": staging_name,
            "staging_root": _staging_root(config),
            "staging_dev": identity.dev, "staging_ino": identity.ino,
            "temp_dev": temp_dev, "temp_ino": temp_ino,
            "proposal_id": fields["proposal_id"], "revision": fields["revision"],
            "destination_fingerprint": fields["destination_fingerprint"],
            "path": fields["path"],
        }
        share_publish._write_state(config, "home", fields, {"attempts": [attempt]})
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)
        os.close(parent_fd)

    reconciled = asyncio.run(share_publish.reconcile_share(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ))
    assert reconciled.outcome == "changed_upstream"
    assert (shared / "notes" / "copied.md").read_bytes() == content
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is None


def test_folder_crash_after_receipt_before_cleanup_replays_historically(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"receipt first\n", path="notes/receipt-crash.md", key="receipt-crash")
    monkeypatch.setattr(share_publish.folder_io, "discard_staging", lambda *_args, **_kwargs: None)
    published = _publish(config, draft)
    assert published.outcome == "published"
    staging_name = _attempts(config, draft)[-1]["staging_name"]
    assert os.path.isdir(os.path.join(_staging_root(config), staging_name))
    replay = _publish(config, draft)
    assert replay.outcome == "already_published"
    assert replay.receipt_id == published.receipt_id
    assert (shared / "notes" / "receipt-crash.md").read_bytes() == b"receipt first\n"


def test_folder_post_receipt_replacement_is_not_rewritten_or_deleted(folder) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"published once\n", path="notes/replaced.md", key="post-receipt")
    published = _publish(config, draft)
    assert published.outcome == "published"
    target = shared / "notes" / "replaced.md"
    target.unlink()
    target.write_bytes(b"external later\n")
    replay = _publish(config, draft)
    assert replay.outcome == "already_published"
    assert replay.receipt_id == published.receipt_id
    assert target.read_bytes() == b"external later\n"
    assert share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    ) is not None


def test_folder_path_swap_is_refused_and_writes_nothing(folder) -> None:
    config, shared, _link = folder
    nested = shared / "nested"
    nested.mkdir()
    outside = config.workspace_root.parent / "outside"
    outside.mkdir()
    draft = _draft(config, b"do not escape\n", path="nested/new.md", key="path-swap")
    nested.rename(shared / "nested-old")
    (shared / "nested").symlink_to(outside, target_is_directory=True)

    result = _publish(config, draft)
    assert result.outcome in {"unsafe_target", "brain_unavailable"}
    assert not (outside / "new.md").exists()
    assert not (shared / "nested-old" / "new.md").exists()


def test_folder_root_and_parent_swaps_are_detected_by_the_recorded_chain(folder) -> None:
    config, shared, link = folder
    nested = shared / "nested"
    nested.mkdir()
    draft = _draft(config, b"chain\n", path="nested/chain.md", key="chain-swap")
    # Replace a real parent with another real directory: no symlink involved.
    nested.rename(shared / "nested-old")
    replacement = shared / "nested"
    replacement.mkdir()
    result = _publish(config, draft)
    assert result.outcome == "unsafe_target"
    assert not (replacement / "chain.md").exists()
    assert not (shared / "nested-old" / "chain.md").exists()

    # A relink at the same path with a new lifetime cannot reuse the review.
    relinked_draft = _draft(config, b"relinked\n", path="other.md", key="relinked")
    config.links = [
        SharedBrainLink(
            name="team", path=link.path, identity=link.identity,
            lifetime_id="lifetime-two", kind="folder",
        )
    ]
    config.workspaces["home"].shared_brains = config.links
    _persist(config)
    relinked = _publish(config, relinked_draft)
    assert relinked.outcome in {"brain_unavailable", "brain_changed"}
    assert not (shared / "other.md").exists()


def test_folder_symlinks_directories_and_special_files_are_refused(folder) -> None:
    config, shared, _link = folder
    outside = config.workspace_root.parent / "outside.txt"
    outside.write_bytes(b"outside\n")
    (shared / "notes" / "symlink.md").symlink_to(outside)
    (shared / "notes" / "directory.md").mkdir()
    if hasattr(os, "mkfifo"):
        os.mkfifo(shared / "notes" / "pipe.md")
    (shared / "alias").symlink_to(config.workspace_root.parent, target_is_directory=True)

    for name in ("symlink.md", "directory.md", "pipe.md"):
        refusal = share_proposals.draft_share(
            config, workspace="home", relative_path=f"notes/{name}", operation="create",
            proposed=b"never\n", request_key="special-" + name,
        )
        assert isinstance(refusal, share_proposals.ShareRefusal)
        assert refusal.code == "unsafe_target"
    escaped = share_proposals.draft_share(
        config, workspace="home", relative_path="alias/escape.md", operation="create",
        proposed=b"never\n", request_key="parent-symlink",
    )
    assert isinstance(escaped, share_proposals.ShareRefusal)
    assert escaped.code == "unsafe_target"
    assert outside.read_bytes() == b"outside\n"


def test_folder_protected_root_is_refused_before_any_write(tmp_path: Path) -> None:
    root = tmp_path / "protected"
    root.mkdir()
    vault = root / "vault"
    inside = vault / "inside"
    inside.mkdir(parents=True)
    link = SharedBrainLink(
        name="team", path=str(inside.resolve()), identity=f"folder:{inside.resolve()}",
        lifetime_id="protected", kind="folder",
    )
    config = Config(root, vault, [link])
    _persist(config)
    refusal = share_proposals.draft_share(
        config, workspace="home", relative_path="new.md", operation="create",
        proposed=b"never\n", request_key="protected-root",
    )
    assert isinstance(refusal, share_proposals.ShareRefusal)
    assert refusal.code == "brain_unavailable"
    assert not (inside / "new.md").exists()


def test_folder_staging_failure_removes_only_its_own_partial_file(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, shared, _link = folder
    parent_fd, _chain = folder_io.open_chain(str(shared), ("notes", "x.md"))
    staging_fd = -1
    try:
        staging_fd, _identity = folder_io.create_staging_dir(_staging_root(_config), ".ciao-share-failed.d")

        def fail_fsync(_fd: int) -> None:
            raise OSError(errno.EIO, "disk error")

        monkeypatch.setattr(folder_io.os, "fsync", fail_fsync)
        with pytest.raises(OSError):
            folder_io.create_staged_file(staging_fd, "note", b"partial", "failed-token")
        assert folder_io.stat_entry(staging_fd, "note") is None
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)
        try:
            os.rmdir(os.path.join(_staging_root(_config), ".ciao-share-failed.d"))
        except OSError:
            pass
        os.close(parent_fd)


def test_folder_permission_failure_is_inaccessible_and_writes_nothing(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder

    def denied(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "permission denied")

    monkeypatch.setattr(share_proposals.folder_io, "open_chain", denied)
    refusal = share_proposals.draft_share(
        config, workspace="home", relative_path="notes/denied.md", operation="create",
        proposed=b"never\n", request_key="permission-denied",
    )
    assert isinstance(refusal, share_proposals.ShareRefusal)
    assert refusal.code == "brain_unavailable"
    assert not (shared / "notes" / "denied.md").exists()


def test_folder_unsupported_tag_refuses_and_writes_nothing(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"no tag\n", path="notes/unsupported.md", key="unsupported-tag")

    def unsupported(*_args, **_kwargs):
        raise folder_io.UnsupportedFilesystem(errno.ENOTSUP, "operation not supported")

    monkeypatch.setattr(share_publish.folder_io, "create_staged_file", unsupported)
    result = _publish(config, draft)
    assert result.outcome == "manual_export_required"
    assert result.receipt_id is None
    assert not (shared / "notes" / "unsupported.md").exists()
    attempts = _attempts(config, draft)
    assert attempts[-1]["status"] == "rejected"
    assert attempts[-1]["retryable"] is False
    assert not share_publish.can_retry_publication(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    )
    assert not any(path.name.startswith(".ciao-share-") for path in shared.rglob("*"))
    assert _publish(config, draft).outcome == "manual_export_required"


def test_folder_transient_staging_failure_does_not_claim_success_and_can_retry(
    folder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"retry me\n", path="notes/retry.md", key="transient-volume")
    original = share_publish.folder_io.create_staged_file

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EIO, "transient I/O error")

    monkeypatch.setattr(share_publish.folder_io, "create_staged_file", unsupported)
    result = _publish(config, draft)
    assert result.outcome == "draft_unavailable"
    assert not (shared / "notes" / "retry.md").exists()
    attempts = _attempts(config, draft)
    assert attempts[-1]["status"] == "rejected"
    assert attempts[-1]["retryable"] is True
    assert share_publish.can_retry_publication(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    )

    monkeypatch.setattr(share_publish.folder_io, "create_staged_file", original)
    retried = _publish(config, draft)
    assert retried.outcome == "published"
    assert (shared / "notes" / "retry.md").read_bytes() == b"retry me\n"


def test_folder_preview_blocks_after_an_external_note_appears(folder) -> None:
    config, shared, _link = folder
    draft = _draft(config, b"reviewed\n", path="notes/preview.md", key="preview-conflict")
    _rows, by_id = proposal_service._scan_proposal_rows(config)
    ctx = by_id[draft.proposal_id]
    before = proposal_service.preview_row(config, ctx)
    assert before["link_kind"] == "folder"
    assert before["blocked_reason"] is None
    (shared / "notes" / "preview.md").write_bytes(b"external\n")
    after = proposal_service.preview_row(config, ctx)
    assert after["blocked_reason"] == "folder_update_unsupported"
    assert after["before"] is None
    assert after["after"] == "reviewed\n"


def test_folder_receipt_is_not_undoable(folder) -> None:
    config, _shared, _link = folder
    draft = _draft(config, b"no undo\n", path="notes/no-undo.md", key="no-undo")
    published = _publish(config, draft)
    assert published.outcome == "published"
    receipt = share_publish.get_share_receipt(
        config, workspace="home", proposal_id=draft.proposal_id,
        expected_revision=draft.artifact["review_token"],
    )
    assert receipt is not None
    refusal = asyncio.run(share_publish.draft_share_reversal(
        config, workspace="home", receipt_id=receipt["id"],
    ))
    assert isinstance(refusal, share_proposals.ShareRefusal)
    assert refusal.code == "undo_unsupported"
