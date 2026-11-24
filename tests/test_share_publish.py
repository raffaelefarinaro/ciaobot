"""Isolated bare-Git acceptance for shared-note publication."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import threading
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
        await asyncio.to_thread(
            proposal_service._rewrite_queue_single,
            Path(ctx["path"]), ctx["line"], ctx["row"]["raw"],
            ctx["row"]["text"], ctx["row"]["kind"], False,
            ctx["workspace"], Path(config.workspace_vault_root(ctx["workspace"])),
        )
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
