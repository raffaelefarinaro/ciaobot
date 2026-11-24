"""Shared-note proposal filing uses the tracked upstream image and private state."""

from __future__ import annotations

import asyncio
import subprocess
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import share_proposals as sp
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
        self.link = link
        self.links = [link]
        self.vaults = {"home": vault}
        self.logs_root = root / "logs"

    def workspace_names(self) -> list[str]:
        return list(self.vaults)

    def agent_root_targets(self) -> list[tuple[Path, str]]:
        return [(self.workspace_root, "home")]

    def workspace_vault_root(self, name: str) -> Path:
        return self.vaults[name]

    def workspace(self, name: str | None) -> SimpleNamespace | None:
        return SimpleNamespace(shared_brains=self.links) if name in self.vaults else None


@pytest.fixture
def linked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(seed)], check=True)
    (seed / "n.md").write_bytes("before\r\nλ\n".encode())
    git(seed, "add", ".")
    git(seed, "commit", "--quiet", "-m", "seed")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "--quiet", "-u", "origin", "main")
    clone = tmp_path / "brain"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)], check=True)
    git(clone, "remote", "set-url", "origin", HOSTED)
    monkeypatch.setattr(sp.shared_brain, "_HARDEN", sp.shared_brain._HARDEN + ("-c", f"url.{origin}.insteadOf={HOSTED}", "-c", "protocol.file.allow=always"))
    link = SharedBrainLink(name="notes", path=str(clone), identity="github.com/x/notes")
    vault = tmp_path / "vault"
    vault.mkdir()
    return Config(tmp_path, vault, link), clone


def test_share_identity_preserves_exact_upstream_bytes_and_private_queue(linked: tuple[Config, Path]) -> None:
    config, clone = linked
    # The checkout may contain local edits; the proposal binds the upstream blob.
    (clone / "n.md").write_bytes(b"working copy only")
    draft = sp.draft_share(
        config, workspace="home", relative_path="n.md", operation="replace",
        proposed=b"after\r\n\n", request_key="request-one",
    )
    assert isinstance(draft, sp.ShareDraft)
    loaded = sp.load_share(config, workspace="home", proposal_id=draft.proposal_id)
    assert isinstance(loaded, dict)
    fields = loaded["immutable"]
    assert fields["original"] == "before\r\nλ\n"
    assert fields["proposed"] == "after\r\n\n"
    assert fields["upstream_ref"] == "refs/heads/main"
    assert fields["base_blob_oid"] == git(clone, "rev-parse", "refs/remotes/origin/main:n.md")
    queue = (config.vault / "Workspace" / "Memory-Proposals.md").read_text()
    assert "n.md" not in queue and "before" not in queue and "after" not in queue
    assert "github.com/x/notes" not in queue
    assert "[share " in queue
    sidecar = config.state_path.parent / "secrets" / "share-proposals"
    assert sidecar.is_dir()
    assert sidecar.stat().st_mode & 0o077 == 0


def test_share_rejects_credentials_and_bad_paths_before_filing(linked: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    config, _ = linked
    writes: list[bytes] = []
    monkeypatch.setattr(sp, "_write_private", lambda _path, value: writes.append(value))
    refusal = sp.draft_share(config, workspace="home", relative_path="../n.md", operation="create", proposed=b"x", request_key="bad-path")
    assert isinstance(refusal, sp.ShareRefusal) and refusal.code == "invalid_path"
    refusal = sp.draft_share(config, workspace="home", relative_path="new.md", operation="create", proposed=b"api_key=abcdef", request_key="secret")
    assert isinstance(refusal, sp.ShareRefusal) and refusal.code == "secret_detected"
    assert writes == []
    assert not (config.vault / "Workspace" / "Memory-Proposals.md").exists()


def test_share_request_key_is_idempotent_but_cannot_change_content(linked: tuple[Config, Path]) -> None:
    config, _ = linked
    args = dict(workspace="home", relative_path="new.md", operation="create", proposed=b"draft\n", request_key="same")
    one = sp.draft_share(config, **args)
    two = sp.draft_share(config, **args)
    assert isinstance(one, sp.ShareDraft) and isinstance(two, sp.ShareDraft)
    assert one.proposal_id == two.proposal_id
    assert (config.vault / "Workspace" / "Memory-Proposals.md").read_text().count("[share ") == 1
    refusal = sp.draft_share(config, **{**args, "proposed": b"changed"})
    assert isinstance(refusal, sp.ShareRefusal) and refusal.code == "request_conflict"


def test_share_sync_git_reader_works_inside_running_event_loop(linked: tuple[Config, Path]) -> None:
    config, _ = linked

    async def call_sync_api():
        return sp.draft_share(config, workspace="home", relative_path="new.md", operation="create", proposed=b"inside-loop", request_key="async-context")

    result = asyncio.run(call_sync_api())
    assert isinstance(result, sp.ShareDraft)


def test_share_retry_requires_same_operation_and_proposal_shape(linked: tuple[Config, Path]) -> None:
    config, _ = linked
    inferred = sp.draft_share(config, workspace="home", relative_path="new.md", operation=None, proposed=b"draft", request_key="inferred")
    assert isinstance(inferred, sp.ShareDraft)
    changed_operation = sp.draft_share(config, workspace="home", relative_path="new.md", operation="create", proposed=b"draft", request_key="inferred")
    assert isinstance(changed_operation, sp.ShareRefusal) and changed_operation.code == "request_conflict"

    deletion = sp.draft_share(config, workspace="home", relative_path="n.md", operation="delete", proposed=None, request_key="delete")
    assert isinstance(deletion, sp.ShareDraft)
    missing_operation = sp.draft_share(config, workspace="home", relative_path="n.md", operation=None, proposed=None, request_key="delete")
    assert isinstance(missing_operation, sp.ShareRefusal) and missing_operation.code == "invalid_operation"
    wrong_shape = sp.draft_share(config, workspace="home", relative_path="n.md", operation="delete", proposed=b"", request_key="delete")
    assert isinstance(wrong_shape, sp.ShareRefusal) and wrong_shape.code == "invalid_operation"


def test_share_preserves_absent_empty_and_explicit_delete_as_distinct_images(linked: tuple[Config, Path]) -> None:
    config, clone = linked
    (clone / "n.md").write_bytes(b"")
    git(clone, "add", "n.md")
    git(clone, "commit", "--quiet", "-m", "empty note")
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    deleted = sp.draft_share(config, workspace="home", relative_path="n.md", operation="delete", proposed=None, request_key="delete-empty")
    created = sp.draft_share(config, workspace="home", relative_path="new.md", operation="create", proposed=b"", request_key="create-empty")
    assert isinstance(deleted, sp.ShareDraft) and isinstance(created, sp.ShareDraft)
    deleted_fields = deleted.artifact["immutable"]
    created_fields = created.artifact["immutable"]
    assert deleted_fields["original"] == "" and deleted_fields["proposed"] is None
    assert created_fields["original"] is None and created_fields["proposed"] == ""


def test_share_rejects_swapped_and_cross_workspace_artifacts(linked: tuple[Config, Path]) -> None:
    config, _ = linked
    first = sp.draft_share(config, workspace="home", relative_path="first.md", operation="create", proposed=b"first", request_key="first")
    second = sp.draft_share(config, workspace="home", relative_path="second.md", operation="create", proposed=b"second", request_key="second")
    assert isinstance(first, sp.ShareDraft) and isinstance(second, sp.ShareDraft)
    queue = config.vault / "Workspace" / "Memory-Proposals.md"
    first_id = first.artifact["immutable"]["sidecar_id"]
    second_id = second.artifact["immutable"]["sidecar_id"]
    queue.write_text(queue.read_text().replace(f"[share {second_id}]", f"[share {first_id}]"), encoding="utf-8")
    swapped = sp.load_share(config, workspace="home", proposal_id=second.proposal_id)
    assert isinstance(swapped, sp.ShareRefusal) and swapped.code == "draft_corrupt"

    config.vaults["other"] = config.workspace_root.parent / "other-vault"
    config.vaults["other"].mkdir()
    cross_workspace = sp.load_share(config, workspace="other", proposal_id=first.proposal_id)
    assert isinstance(cross_workspace, sp.ShareRefusal) and cross_workspace.code == "draft_corrupt"


def test_share_rejects_gitlink_and_directory_leaves(linked: tuple[Config, Path]) -> None:
    config, clone = linked
    commit = git(clone, "rev-parse", "HEAD")
    git(clone, "update-index", "--add", "--cacheinfo", f"160000,{commit},submodule.md")
    (clone / "folder.md").mkdir()
    (clone / "folder.md" / "inside.md").write_text("child", encoding="utf-8")
    git(clone, "add", "folder.md")
    git(clone, "commit", "--quiet", "-m", "gitlink and directory")
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    gitlink = sp.draft_share(config, workspace="home", relative_path="submodule.md", operation="create", proposed=b"unsafe", request_key="gitlink")
    directory = sp.draft_share(config, workspace="home", relative_path="folder.md", operation="create", proposed=b"unsafe", request_key="directory")
    assert isinstance(gitlink, sp.ShareRefusal) and gitlink.code == "unsafe_target"
    assert isinstance(directory, sp.ShareRefusal) and directory.code == "unsafe_target"


def test_share_distinguishes_git_read_failure_from_an_absent_target(linked: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.git_proc import GitCapture

    config, clone = linked
    normal = sp.draft_share(config, workspace="home", relative_path="absent.md", operation="create", proposed=b"valid", request_key="absent")
    assert isinstance(normal, sp.ShareDraft)
    monkeypatch.setattr(sp, "run_git_sync_bounded", lambda *_args, **_kwargs: GitCapture(-1, b"", b"timed out", True))
    failure = sp.draft_share(config, workspace="home", relative_path="another-absent.md", operation="create", proposed=b"valid", request_key="read-failure")
    assert isinstance(failure, sp.ShareRefusal) and failure.code == "upstream_unavailable"


def test_share_same_key_concurrent_calls_create_one_row(linked: tuple[Config, Path]) -> None:
    config, _ = linked
    args = dict(config=config, workspace="home", relative_path="new.md", operation="create", proposed=b"draft", request_key="parallel")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: sp.draft_share(**args), range(2)))
    assert all(isinstance(result, sp.ShareDraft) for result in results)
    assert results[0].proposal_id == results[1].proposal_id
    assert (config.vault / "Workspace" / "Memory-Proposals.md").read_text().count("[share ") == 1


def test_share_concurrent_brain_cap_has_one_winner(linked: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    config, _ = linked
    monkeypatch.setattr(sp, "MAX_PER_BRAIN", 1)
    calls = [
        dict(config=config, workspace="home", relative_path=f"note-{index}.md", operation="create", proposed=b"draft", request_key=f"request-{index}")
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: sp.draft_share(**args), calls))
    assert sum(isinstance(result, sp.ShareDraft) for result in results) == 1
    refusal = next(result for result in results if isinstance(result, sp.ShareRefusal))
    assert refusal.code == "capacity" and refusal.limit == 1 and refusal.count == 1


def test_share_retries_sidecar_only_and_already_visible_interruption(linked: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    config, _ = linked
    original_writer = sp.memory_receipts.write_queue_atomically
    calls = 0

    def fail_before_replace(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated interruption after sidecar persistence")
        return original_writer(*args, **kwargs)

    monkeypatch.setattr(sp.memory_receipts, "write_queue_atomically", fail_before_replace)
    args = dict(config=config, workspace="home", relative_path="new.md", operation="create", proposed=b"draft", request_key="retry")
    first = sp.draft_share(**args)
    assert isinstance(first, sp.ShareRefusal) and first.code == "draft_unavailable"
    second = sp.draft_share(**args)
    assert isinstance(second, sp.ShareDraft)
    assert (config.vault / "Workspace" / "Memory-Proposals.md").read_text().count("[share ") == 1


def test_share_recovers_visible_row_before_rechecking_git(linked: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    config, _ = linked
    original_writer = sp.memory_receipts.write_queue_atomically
    first = True

    def fail_after_replace(*args, **kwargs):
        nonlocal first
        original_writer(*args, **kwargs)
        if first:
            first = False
            raise OSError("simulated lost response after queue replace")

    monkeypatch.setattr(sp.memory_receipts, "write_queue_atomically", fail_after_replace)
    args = dict(config=config, workspace="home", relative_path="new.md", operation="create", proposed=b"draft", request_key="visible-retry")
    interrupted = sp.draft_share(**args)
    assert isinstance(interrupted, sp.ShareRefusal) and interrupted.code == "draft_unavailable"
    monkeypatch.setattr(sp, "_qualify", lambda *_args, **_kwargs: pytest.fail("visible retry re-ran Git qualification"))
    recovered = sp.draft_share(**args)
    assert isinstance(recovered, sp.ShareDraft)


def test_share_retry_after_dismissal_does_not_resurrect_row(linked: tuple[Config, Path]) -> None:
    config, _ = linked
    args = dict(config=config, workspace="home", relative_path="new.md", operation="create", proposed=b"draft", request_key="dismissed")
    draft = sp.draft_share(**args)
    assert isinstance(draft, sp.ShareDraft)
    queue = config.vault / "Workspace" / "Memory-Proposals.md"
    sidecar_id = draft.artifact["immutable"]["sidecar_id"]
    from ciao.proposal_actions import record_decision
    from ciao.proposal_tracking import walk_proposal_queue

    row = next(iter(walk_proposal_queue("home", "home/Workspace/Memory-Proposals.md", queue.read_text())))
    record_decision(queue, action="dismiss", text=row.bullet.text, kind="share", via="pwa", outcome="dismissed")
    from ciao.memory_proposals import remove_proposal_by_substring

    remove_proposal_by_substring(queue, row.bullet.target)
    retried = sp.draft_share(**args)
    assert isinstance(retried, sp.ShareRefusal) and retried.code == "request_conflict"
    assert sidecar_id not in queue.read_text()


def test_share_limits_apply_during_orphan_recovery_and_across_brains(linked: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    config, clone = linked
    monkeypatch.setattr(sp, "MAX_PER_BRAIN", 1)
    original_writer = sp.memory_receipts.write_queue_atomically
    failed = False

    def interrupt_first(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("orphan sidecar")
        return original_writer(*args, **kwargs)

    monkeypatch.setattr(sp.memory_receipts, "write_queue_atomically", interrupt_first)
    orphan_args = dict(config=config, workspace="home", relative_path="orphan.md", operation="create", proposed=b"orphan", request_key="orphan")
    assert isinstance(sp.draft_share(**orphan_args), sp.ShareRefusal)
    live = sp.draft_share(config, workspace="home", brain="notes", relative_path="live.md", operation="create", proposed=b"live", request_key="live")
    assert isinstance(live, sp.ShareDraft)
    recovered = sp.draft_share(**orphan_args)
    assert isinstance(recovered, sp.ShareRefusal) and recovered.code == "capacity"
    assert recovered.limit == 1 and recovered.count == 1

    monkeypatch.setattr(sp, "MAX_PER_BRAIN", 10)
    monkeypatch.setattr(sp, "MAX_PER_WORKSPACE", 1)
    config.links.append(SharedBrainLink(name="second", path=str(clone), identity="github.com/x/notes"))
    cross_brain = sp.draft_share(config, workspace="home", brain="second", relative_path="other.md", operation="create", proposed=b"other", request_key="workspace-cap")
    assert isinstance(cross_brain, sp.ShareRefusal) and cross_brain.code == "capacity"
    assert cross_brain.limit == 1 and cross_brain.count == 1


def test_share_adversarial_utf8_private_path_and_size_refusals(linked: tuple[Config, Path]) -> None:
    config, _ = linked
    cases = [
        (b"\xff", "invalid_utf8"),
        (b"x" * (sp.MAX_BYTES + 1), "too_large"),
        ((str(Path.home()) + "/secret.md").encode(), "private_path_mentioned"),
    ]
    for index, (content, code) in enumerate(cases):
        refusal = sp.draft_share(config, workspace="home", relative_path=f"new-{index}.md", operation="create", proposed=content, request_key=f"adversarial-{index}")
        assert isinstance(refusal, sp.ShareRefusal) and refusal.code == code
    assert sp.MAX_SIDECAR_BYTES >= 2 * sp.MAX_BYTES * 6


def test_share_oversized_tracked_blobs_are_too_large_not_git_failures(linked: tuple[Config, Path]) -> None:
    config, clone = linked
    for index, size in enumerate((sp.MAX_BYTES + 1, sp.MAX_BYTES + 2, sp.MAX_BYTES + 1025, sp.MAX_BYTES + 20_000)):
        name = f"large-{index}.md"
        (clone / name).write_bytes(b"x" * size)
        git(clone, "add", name)
        git(clone, "commit", "--quiet", "-m", f"large image {index}")
        git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
        refusal = sp.draft_share(
            config,
            workspace="home",
            relative_path=name,
            operation="replace",
            proposed=b"replacement",
            request_key=f"too-large-{index}",
        )
        assert isinstance(refusal, sp.ShareRefusal)
        assert refusal.code == "too_large", f"size={size}: {refusal.code}"


def test_share_load_rejects_tampered_immutable_fields(linked: tuple[Config, Path]) -> None:
    config, _ = linked
    draft = sp.draft_share(config, workspace="home", relative_path="new.md", operation="create", proposed=b"private", request_key="tamper")
    assert isinstance(draft, sp.ShareDraft)
    sidecar_id = draft.artifact["immutable"]["sidecar_id"]
    sidecar = config.state_path.parent / "secrets" / "share-proposals" / (hashlib.sha256(("home\0sidecar\0" + sidecar_id).encode()).hexdigest() + ".json")
    artifact = json.loads(sidecar.read_text())
    artifact["immutable"]["path"] = "different.md"
    sp._write_private(sidecar, json.dumps(artifact).encode())
    refusal = sp.load_share(config, workspace="home", proposal_id=draft.proposal_id)
    assert isinstance(refusal, sp.ShareRefusal) and refusal.code == "draft_corrupt"


def test_share_sidecar_limit_covers_two_maximum_control_text_images(linked: tuple[Config, Path]) -> None:
    config, clone = linked
    content = b"\x01" * sp.MAX_BYTES
    (clone / "n.md").write_bytes(content)
    git(clone, "add", "n.md")
    git(clone, "commit", "--quiet", "-m", "control text", "--allow-empty-message")
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    draft = sp.draft_share(config, workspace="home", relative_path="n.md", operation="replace", proposed=content, request_key="maximum-images")
    assert isinstance(draft, sp.ShareDraft)
    assert len(draft.artifact["immutable"]["original"]) == sp.MAX_BYTES
    assert len(draft.artifact["immutable"]["proposed"]) == sp.MAX_BYTES
    stored = list((config.state_path.parent / "secrets" / "share-proposals").glob("*.json"))
    assert len(stored) == 2
    assert all(path.stat().st_size <= sp.MAX_SIDECAR_BYTES for path in stored)


def test_share_private_runtime_paths_stay_out_of_backup_scope(linked: tuple[Config, Path]) -> None:
    from ciao import backup_scope

    config, _ = linked
    default_path = config.state_path.parent / "secrets" / "share-proposals" / "draft.json"
    assert not backup_scope.is_eligible(default_path, config)
    config.state_path = config.workspace_root / "custom-runtime" / "state.json"
    in_install_path = config.state_path.parent / "secrets" / "share-proposals" / "draft.json"
    assert not backup_scope.is_eligible(in_install_path, config)
    config.state_path = config.workspace_root.parent / "external-runtime" / "state.json"
    out_of_install_path = config.state_path.parent / "secrets" / "share-proposals" / "draft.json"
    assert not backup_scope.is_eligible(out_of_install_path, config)


def test_share_storage_refuses_workspace_export_overlap(linked: tuple[Config, Path]) -> None:
    config, _ = linked
    config.state_path = config.workspace_root / ".archived-workspaces" / "export" / ".runtime" / "state.json"
    refusal = sp.draft_share(config, workspace="home", relative_path="new.md", operation="create", proposed=b"draft", request_key="export-overlap")
    assert isinstance(refusal, sp.ShareRefusal) and refusal.code == "draft_unavailable"


def test_share_refuses_symlink_ancestor_in_tracked_tree(linked: tuple[Config, Path]) -> None:
    config, clone = linked
    (clone / "links").symlink_to(".", target_is_directory=True)
    git(clone, "add", "links")
    git(clone, "commit", "--quiet", "-m", "symlink")
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    refusal = sp.draft_share(config, workspace="home", relative_path="links/n.md", operation="create", proposed=b"unsafe", request_key="symlink")
    assert isinstance(refusal, sp.ShareRefusal) and refusal.code == "unsafe_target"
