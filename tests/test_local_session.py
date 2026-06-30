"""Tests for ciao/local_session.py: the per-device working-branch flow.

Every instance runs on its own ``dev/<device>`` branch cut from ``origin/main``
and lands work on ``main`` via ``try_merge_to_main`` (clean merge -> direct
push; conflict -> hand off to a chat). The safety rule the tests pin down:
never discard unmerged work, and never push a conflicted merge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ciao.local_session import (
    LocalSessionManager,
    current_branch,
    device_branch_name,
    ensure_device_branch,
    resync_to_main,
    try_merge_to_main,
)


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(repo),
    }
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _identify(repo: Path) -> None:
    """Pin a repo-local identity so async commits in local_session work."""
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "user.email", "t@e.com")


def _make_world(tmp_path: Path) -> tuple[Path, Path]:
    """Bare origin + a clone on main with one commit."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _identify(seed)
    _write(seed / "README.md", "seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(origin), str(local))
    _identify(local)
    return local, origin


def test_device_branch_name() -> None:
    assert device_branch_name("laptop") == "dev/laptop"


# ── ensure_device_branch ───────────────────────────────────────────────────


async def test_ensure_device_branch_creates_from_origin_main(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path)
    branch = await ensure_device_branch(local, device_name="mini")
    assert branch == "dev/mini"
    assert current_branch(local) == "dev/mini"
    assert "main" in _git(local, "branch", "--format=%(refname:short)").split()


async def test_ensure_device_branch_keeps_existing_unmerged_work(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path)
    await ensure_device_branch(local, device_name="mini")
    _write(local / "wip.md", "work in progress\n")
    _git(local, "add", "-A")
    _git(local, "commit", "-q", "-m", "wip")
    head_before = _git(local, "rev-parse", "HEAD")

    # A second ensure (e.g. restart) must NOT discard the wip commit.
    branch = await ensure_device_branch(local, device_name="mini")
    assert branch == "dev/mini"
    assert _git(local, "rev-parse", "HEAD") == head_before
    assert (local / "wip.md").exists()


# ── try_merge_to_main ────────────────────────────────────────────────────────


async def test_try_merge_clean_pushes_main_without_deploy_flag(tmp_path: Path) -> None:
    local, origin = _make_world(tmp_path)
    await ensure_device_branch(local, device_name="mini")
    # After the package/workspace split, even a path named like app code is
    # just workspace content. App deploys are handled by package upgrades.
    _write(local / "ciao" / "feature.py", "x = 1\n")

    result = await try_merge_to_main(local, branch="dev/mini")
    assert result["ok"] is True
    assert result["merged"] is True
    assert result["pushed"] is True
    assert result["deploy_needed"] is False
    # Continues on the device branch, now containing the merged work.
    assert current_branch(local) == "dev/mini"
    # origin/main advanced with the feature.
    check = tmp_path / "check"
    _git(tmp_path, "clone", "-q", str(origin), str(check))
    assert (check / "ciao" / "feature.py").exists()


async def test_try_merge_no_code_change_does_not_flag_deploy(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path)
    await ensure_device_branch(local, device_name="mini")
    _write(local / "memory-vault" / "note.md", "a note\n")  # non-code path

    result = await try_merge_to_main(local, branch="dev/mini")
    assert result["merged"] is True
    assert result["deploy_needed"] is False


async def test_preflight_never_flags_workspace_changes_for_deploy(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path)
    await ensure_device_branch(local, device_name="mini")
    _write(local / "ciao" / "feature.py", "x = 1\n")
    mgr = LocalSessionManager(
        workspace=local,
        runtime_root=tmp_path / ".runtime",
        device_name="mini",
    )

    result = await mgr.preflight()

    assert result["dirty"] is True
    assert result["deploy_needed"] is False
    assert result["changed_files"]["code"] == ["ciao/feature.py"]


async def test_try_merge_conflict_aborts_and_returns_to_branch(tmp_path: Path) -> None:
    local, origin = _make_world(tmp_path)
    await ensure_device_branch(local, device_name="mini")
    # Device edits README.
    _write(local / "README.md", "device version\n")
    _git(local, "add", "-A")
    _git(local, "commit", "-q", "-m", "device edit")
    # Meanwhile main advances with a conflicting edit to the same file.
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _identify(other)
    _write(other / "README.md", "main version\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "main edit")
    _git(other, "push", "-q")

    result = await try_merge_to_main(local, branch="dev/mini")
    assert result["ok"] is True
    assert result["merged"] is False
    assert result["conflict"] is True
    # Merge aborted, back on the device branch, no conflict markers left.
    assert current_branch(local) == "dev/mini"
    assert "<<<<<<<" not in (local / "README.md").read_text()
    assert "MERGE_HEAD" not in _git(local, "status")


# ── resync_to_main ─────────────────────────────────────────────────────────


async def test_resync_to_main_repoints_device_branch(tmp_path: Path) -> None:
    local, origin = _make_world(tmp_path)
    await ensure_device_branch(local, device_name="mini")
    # main advances elsewhere (e.g. the merge chat pushed it).
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _identify(other)
    _write(other / "merged.md", "merged by chat\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "chat merge")
    _git(other, "push", "-q")

    ok, _ = await resync_to_main(local, branch="dev/mini")
    assert ok is True
    assert current_branch(local) == "dev/mini"
    assert (local / "merged.md").exists()


def _advance_main(tmp_path: Path, origin: Path, name: str) -> None:
    """Push a new commit to origin/main from a throwaway clone."""
    other = tmp_path / name
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _identify(other)
    _write(other / f"{name}.md", f"{name}\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", name)
    _git(other, "push", "-q")


async def test_resync_with_dirty_tree_still_brings_in_main(tmp_path: Path) -> None:
    # The live PWA workspace is almost always dirty; resync must not abort on it.
    local, origin = _make_world(tmp_path)
    await ensure_device_branch(local, device_name="mini")
    _advance_main(tmp_path, origin, "chatmerge")
    _write(local / "README.md", "locally edited, uncommitted\n")  # dirty tracked file

    ok, _ = await resync_to_main(local, branch="dev/mini")
    assert ok is True
    assert current_branch(local) == "dev/mini"
    assert (local / "chatmerge.md").exists()  # main's commit pulled in


async def test_resync_preserves_unpushed_device_commit(tmp_path: Path) -> None:
    # A snapshot committed after handback must not be discarded by resync.
    local, origin = _make_world(tmp_path)
    await ensure_device_branch(local, device_name="mini")
    _write(local / "snapshot.md", "post-handback snapshot\n")
    _git(local, "add", "-A")
    _git(local, "commit", "-q", "-m", "snapshot")
    _advance_main(tmp_path, origin, "chatmerge")

    ok, _ = await resync_to_main(local, branch="dev/mini")
    assert ok is True
    assert (local / "snapshot.md").exists()  # local work kept
    assert (local / "chatmerge.md").exists()  # main brought in


async def test_try_merge_push_failure(tmp_path: Path, monkeypatch) -> None:
    local, origin = _make_world(tmp_path)
    await ensure_device_branch(local, device_name="mini")
    _write(local / "ciao" / "feature.py", "x = 1\n")

    import ciao.local_session
    orig_git = ciao.local_session._git

    async def mock_git(workspace, *args, **kwargs):
        if args[:3] == ("push", "origin", "main"):
            return 1, "", "fatal: push rejected"
        return await orig_git(workspace, *args, **kwargs)

    monkeypatch.setattr(ciao.local_session, "_git", mock_git)

    result = await try_merge_to_main(local, branch="dev/mini")
    assert result["ok"] is False
    assert result["step"] == "push main"
    assert "push rejected" in result["error"]

    # Assert we are back on the device branch
    assert current_branch(local) == "dev/mini"

    # Assert local main is reset (does not contain the device commits)
    _git(local, "checkout", "main")
    assert not (local / "ciao" / "feature.py").exists()


async def test_try_merge_pull_failure(tmp_path: Path, monkeypatch) -> None:
    local, origin = _make_world(tmp_path)
    await ensure_device_branch(local, device_name="mini")
    _write(local / "ciao" / "feature.py", "x = 1\n")

    import ciao.local_session
    orig_git = ciao.local_session._git

    async def mock_git(workspace, *args, **kwargs):
        if args[:1] == ("pull",):
            return 1, "", "fatal: pull failed"
        return await orig_git(workspace, *args, **kwargs)

    monkeypatch.setattr(ciao.local_session, "_git", mock_git)

    result = await try_merge_to_main(local, branch="dev/mini")
    assert result["ok"] is False
    assert result["step"] == "pull main"
    assert "pull failed" in result["error"]

    # Assert we are back on the device branch
    assert current_branch(local) == "dev/mini"
