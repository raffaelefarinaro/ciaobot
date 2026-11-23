"""Shared brains: linked Git checkouts, identity, state and fast-forward-only updates.

Every test runs real git in ``tmp_path``. ``origin`` is a bare repository on disk,
so nothing touches the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import shared_brain as sb
from ciao.config import SharedBrainLink
from ciao.git_proc import GIT_TIMEOUT_DETAIL, run_git as real_run_git

IDENT = ("-c", "user.name=Test", "-c", "user.email=test@example.com")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *IDENT, "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class FakeConfig:
    """The config surface ``validate_brain_path`` reads."""

    def __init__(self, root: Path, vaults: dict[str, Path] | None = None) -> None:
        self.workspace_root = root / "install"
        self.state_path = root / "install" / "state" / "state.json"
        self._vaults = dict(vaults or {})
        self.workspaces: dict[str, object] = {}

    def workspace_names(self) -> list[str]:
        return list(self._vaults)

    def workspace_vault_root(self, name: str) -> Path:
        return self._vaults[name]


def make_checkout(path: Path, origin_url: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    if origin_url is not None:
        git(path, "remote", "add", "origin", origin_url)
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def pair(root: Path) -> SimpleNamespace:
    """A bare origin, a seed that pushes to it, and a clone (the checkout under test)."""
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = make_checkout(root / "seed", str(origin))
    (seed / "a.md").write_text("one\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "one")
    git(seed, "push", "-q", "-u", "origin", "main")
    clone = root / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return SimpleNamespace(origin=origin, seed=seed, clone=clone, root=root)


def push_from_seed(pair: SimpleNamespace, filename: str) -> str:
    (pair.seed / filename).write_text(filename + "\n", encoding="utf-8")
    git(pair.seed, "add", ".")
    git(pair.seed, "commit", "-q", "-m", filename)
    git(pair.seed, "push", "-q", "origin", "main")
    return git(pair.seed, "rev-parse", "HEAD")


ENVS: list = []


def record_git(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = sb.run_git

    async def recording(workspace, *args, timeout=None, env=None):
        calls.append(list(args))
        ENVS.append(env)
        return await real(workspace, *args, timeout=timeout, env=env)

    monkeypatch.setattr(sb, "run_git", recording)
    return calls


# --- identity and paths -------------------------------------------------------


def test_canonical_identity_unifies_aliases() -> None:
    expected = "github.com/acme/notes"
    for url in (
        "git@github.com:Acme/Notes.git",
        "https://github.com/acme/notes",
        "https://user:token@github.com/acme/notes.git/",
        "ssh://git@github.com/acme/notes",
    ):
        assert sb.canonical_identity(url) == expected, url
    assert sb.canonical_identity("git@gitlab.com:acme/notes.git") != expected
    assert sb.canonical_identity("git@github.com:other/notes.git") != expected
    with pytest.raises(ValueError, match="no remote called origin"):
        sb.canonical_identity("")


@pytest.mark.parametrize("where", ["install", "vault_inside", "vault_parent", "runtime"])
def test_validate_refuses_install_vault_and_runtime_overlap(root: Path, where: str) -> None:
    vault = root / "vaults" / "alice"
    config = FakeConfig(root, {"alice": vault})
    target = {
        "install": config.workspace_root,
        "vault_inside": vault / "notes",
        "vault_parent": root / "vaults",
        "runtime": config.state_path.parent,
    }[where]
    make_checkout(target)
    with pytest.raises(ValueError, match="outside the Ciaobot install"):
        sb.validate_brain_path(config, str(target))


def test_validate_refuses_non_checkout_and_subfolder(root: Path) -> None:
    config = FakeConfig(root)
    plain = root / "plain"
    plain.mkdir()
    with pytest.raises(ValueError):
        sb.validate_brain_path(config, str(plain))
    top = make_checkout(root / "repo")
    with pytest.raises(ValueError):
        sb.validate_brain_path(config, str(top / "sub"))


def test_validate_returns_resolved_sibling_and_empty(root: Path) -> None:
    config = FakeConfig(root)
    sibling = make_checkout(root / "sibling")
    assert sb.validate_brain_path(config, str(sibling)) == str(sibling.resolve())
    assert sb.validate_brain_path(config, "") == ""
    with pytest.raises(ValueError, match="control character"):
        sb.validate_brain_path(config, str(sibling) + "\n")


def test_resolve_links_names_identity_and_limits(root: Path, monkeypatch) -> None:
    config = FakeConfig(root)
    acme = make_checkout(root / "acme", "https://github.com/acme/notes")
    other = make_checkout(root / "other", "https://github.com/other/notes")
    dupe = make_checkout(root / "dupe", "git@github.com:Acme/Notes.git")
    bare = make_checkout(root / "bare")
    rows = [{"path": str(acme)}, {"path": str(other)}]
    links = sb.resolve_links(config, "work", rows, [])
    assert [link.name for link in links] == ["notes", "notes-2"]
    assert links[0].identity == "github.com/acme/notes"

    with pytest.raises(ValueError, match="same repository"):
        sb.resolve_links(config, "work", [{"path": str(acme)}, {"path": str(dupe)}], [])
    with pytest.raises(ValueError, match="already used"):
        sb.resolve_links(config, "work", [{"path": str(acme), "name": "a"}, {"path": str(other), "name": "a"}], [])
    with pytest.raises(ValueError, match="name must"):
        sb.resolve_links(config, "work", [{"path": str(acme), "name": "Bad Name"}], [])
    with pytest.raises(ValueError, match="origin"):
        sb.resolve_links(config, "work", [{"path": str(bare)}], [])
    extra = [make_checkout(root / f"r{i}", f"https://github.com/x/r{i}") for i in range(6)]
    with pytest.raises(ValueError, match="at most"):
        sb.resolve_links(config, "work", [{"path": str(p)} for p in extra], [])

    # An existing path keeps its link and runs no git call.
    existing = sb.resolve_links(config, "work", [{"path": str(acme)}], [])
    calls: list[tuple] = []
    real_git = sb._git

    def spy(*args: str):
        calls.append(args)
        return real_git(*args)

    monkeypatch.setattr(sb, "_git", spy)
    kept = sb.resolve_links(config, "work", [{"path": str(acme)}], existing)
    assert kept == existing and calls == []
    with pytest.raises(ValueError, match="already linked"):
        sb.resolve_links(config, "work", [{"path": str(acme), "name": "renamed"}], existing)


def test_removing_one_link_leaves_the_others_the_folder_and_the_vault(pair: SimpleNamespace, monkeypatch) -> None:
    config = FakeConfig(pair.root, {"alice": pair.root / "vaults" / "alice"})
    vault_note = pair.root / "vaults" / "alice" / "note.md"
    vault_note.parent.mkdir(parents=True)
    vault_note.write_text("private\n", encoding="utf-8")
    other = make_checkout(pair.root / "other", "https://github.com/x/other")
    clone_link = SharedBrainLink(name="clone", path=str(pair.clone.resolve()), identity="github.com/x/notes")
    links = sb.resolve_links(config, "work", [{"path": str(other)}], [clone_link])
    assert links[0].name == "other"
    head = git(pair.clone, "rev-parse", "HEAD")
    kept = sb.resolve_links(config, "work", [{"path": str(other)}], [clone_link, *links])
    assert kept == links  # the clone's row is gone, the other link is the same object
    assert kept[0] is not clone_link
    assert git(pair.clone, "rev-parse", "HEAD") == head
    assert git(pair.clone, "status", "--porcelain") == ""
    assert vault_note.read_text(encoding="utf-8") == "private\n"


def test_brain_state_reports_missing_branch_head_dirty_and_fetch_time(root: Path) -> None:
    checkout = make_checkout(root / "notes", "https://github.com/x/notes")
    (checkout / "a.md").write_text("one\n", encoding="utf-8")
    git(checkout, "add", ".")
    git(checkout, "commit", "-q", "-m", "one")
    head = git(checkout, "rev-parse", "HEAD")
    git(checkout, "update-ref", "refs/remotes/origin/main", head)
    git(checkout, "branch", "--set-upstream-to=origin/main", "main")
    link = SharedBrainLink(name="notes", path=str(checkout.resolve()), identity="github.com/x/notes")

    state = sb.brain_state(link, FakeConfig(root))
    assert state["missing"] is False and state["branch"] == "main"
    assert state["head"] == head[:7] and state["head_date"]
    assert state["upstream"] is True and state["dirty"] is False
    assert state["origin_changed"] is False
    assert state["last_update"] is None

    (checkout / "a.md").write_text("edited\n", encoding="utf-8")
    assert sb.brain_state(link, FakeConfig(root))["dirty"] is True

    git(checkout, "remote", "set-url", "origin", "https://github.com/x/elsewhere")
    assert sb.brain_state(link, FakeConfig(root))["origin_changed"] is True

    gone = SharedBrainLink(name="gone", path=str(root / "gone"), identity=link.identity)
    assert sb.brain_state(gone, FakeConfig(root))["missing"] is True


def test_fetched_at_is_a_stat_and_none_without_fetch_head(root: Path, monkeypatch) -> None:
    checkout = root / "fake"
    (checkout / ".git").mkdir(parents=True)
    assert sb.fetched_at(str(checkout)) is None

    def boom(*_a, **_k):
        raise AssertionError("fetched_at must not spawn a process")

    monkeypatch.setattr(subprocess, "run", boom)
    (checkout / ".git" / "FETCH_HEAD").write_text("", encoding="utf-8")
    stamp = sb.fetched_at(str(checkout))
    assert stamp is not None and stamp.endswith("Z")


# --- update_brain case matrix -------------------------------------------------


def _assert_result(result: dict, outcome: str, reason: str) -> None:
    assert result["outcome"] == outcome, result
    assert result["reason"] == reason, result


@pytest.mark.asyncio
async def test_update_row1_missing(root: Path, monkeypatch) -> None:
    calls = record_git(monkeypatch)
    result = await sb.update_brain(str(root / "nowhere"), FakeConfig(root))
    _assert_result(result, "failed", "missing")
    assert calls == []


@pytest.mark.asyncio
async def test_update_row2_detached(pair: SimpleNamespace) -> None:
    git(pair.clone, "checkout", "-q", "--detach")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(str(pair.clone), FakeConfig(pair.root)), "skipped", "detached")
    assert git(pair.clone, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_update_row3_no_upstream(pair: SimpleNamespace) -> None:
    git(pair.clone, "checkout", "-q", "-b", "local-only")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(str(pair.clone), FakeConfig(pair.root)), "skipped", "no_upstream")
    assert git(pair.clone, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_update_row4_fetch_failed_redacts_and_caps(pair: SimpleNamespace, monkeypatch) -> None:
    async def failing(workspace, *args, timeout=None, env=None):
        if "fetch" in args:
            return (128, "", "fatal: cannot reach https://user:SECRETVALUE123@host/x " + "y" * 900)
        return await real_run_git(workspace, *args, timeout=timeout, env=env)

    monkeypatch.setattr(sb, "run_git", failing)
    result = await sb.update_brain(str(pair.clone), FakeConfig(pair.root))
    _assert_result(result, "failed", "fetch_failed")
    assert "SECRETVALUE123" not in result["detail"]
    assert len(result["detail"]) <= sb.MAX_REASON_CHARS


@pytest.mark.asyncio
async def test_update_row5_fetch_timeout(pair: SimpleNamespace, monkeypatch) -> None:
    async def timing_out(workspace, *args, timeout=None, env=None):
        if "fetch" in args:
            return (-1, "", GIT_TIMEOUT_DETAIL)
        return await real_run_git(workspace, *args, timeout=timeout, env=env)

    monkeypatch.setattr(sb, "run_git", timing_out)
    _assert_result(await sb.update_brain(str(pair.clone), FakeConfig(pair.root)), "failed", "timeout")


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
async def test_update_row6_ahead_or_equal_is_up_to_date(pair: SimpleNamespace, dirty: bool) -> None:
    (pair.clone / "local.md").write_text("mine\n", encoding="utf-8")
    git(pair.clone, "add", ".")
    git(pair.clone, "commit", "-q", "-m", "local")
    if dirty:
        (pair.clone / "a.md").write_text("uncommitted\n", encoding="utf-8")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(str(pair.clone), FakeConfig(pair.root)), "up_to_date", "")
    assert git(pair.clone, "rev-parse", "HEAD") == head
    assert (git(pair.clone, "status", "--porcelain") != "") is dirty


@pytest.mark.asyncio
async def test_update_row7_diverged(pair: SimpleNamespace) -> None:
    (pair.clone / "local.md").write_text("mine\n", encoding="utf-8")
    git(pair.clone, "add", ".")
    git(pair.clone, "commit", "-q", "-m", "local")
    push_from_seed(pair, "remote.md")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(str(pair.clone), FakeConfig(pair.root)), "failed", "diverged")
    assert git(pair.clone, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_update_row8_behind_with_local_changes_is_skipped(pair: SimpleNamespace) -> None:
    push_from_seed(pair, "remote.md")
    (pair.clone / "a.md").write_text("edited\n", encoding="utf-8")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(str(pair.clone), FakeConfig(pair.root)), "skipped", "local_changes")
    assert git(pair.clone, "rev-parse", "HEAD") == head
    assert (pair.clone / "a.md").read_text(encoding="utf-8") == "edited\n"


@pytest.mark.asyncio
async def test_update_row9_behind_clean_fast_forwards(pair: SimpleNamespace) -> None:
    new_head = push_from_seed(pair, "remote.md")
    (pair.clone / ".obsidian").mkdir()
    (pair.clone / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (pair.clone / ".DS_Store").write_text("x", encoding="utf-8")
    _assert_result(await sb.update_brain(str(pair.clone), FakeConfig(pair.root)), "updated", "")
    assert git(pair.clone, "rev-parse", "HEAD") == new_head
    assert git(pair.clone, "status", "--porcelain", "--untracked-files=no") == ""
    assert (pair.clone / ".obsidian" / "app.json").exists()


@pytest.mark.asyncio
async def test_update_row10_merge_failed_leaves_head(pair: SimpleNamespace) -> None:
    push_from_seed(pair, "collide.md")
    (pair.clone / "collide.md").write_text("untracked local\n", encoding="utf-8")
    head = git(pair.clone, "rev-parse", "HEAD")
    result = await sb.update_brain(str(pair.clone), FakeConfig(pair.root))
    _assert_result(result, "failed", "merge_failed")
    assert git(pair.clone, "rev-parse", "HEAD") == head
    assert (pair.clone / "collide.md").read_text(encoding="utf-8") == "untracked local\n"


@pytest.mark.asyncio
async def test_update_never_runs_a_rewriting_git_command(pair: SimpleNamespace, monkeypatch) -> None:
    calls = record_git(monkeypatch)
    push_from_seed(pair, "remote.md")
    await sb.update_brain(str(pair.clone), FakeConfig(pair.root))
    (pair.clone / "a.md").write_text("edited\n", encoding="utf-8")
    push_from_seed(pair, "more.md")
    await sb.update_brain(str(pair.clone), FakeConfig(pair.root))
    forbidden = {"reset", "rebase", "stash", "checkout", "clean", "push", "--force"}
    assert calls, "the update should have run git"
    for argv in calls:
        assert not forbidden & set(argv), argv


@pytest.mark.asyncio
async def test_update_serializes_per_path(pair: SimpleNamespace, monkeypatch) -> None:
    events: list[str] = []

    async def slow(workspace, *args, timeout=None, env=None):
        events.append(f"start {args[-1]}")
        await asyncio.sleep(0.01)
        events.append(f"end {args[-1]}")
        return (0, "", "")

    monkeypatch.setattr(sb, "run_git", slow)
    await asyncio.gather(sb.update_brain(str(pair.clone), FakeConfig(pair.root)), sb.update_brain(str(pair.clone), FakeConfig(pair.root)))
    assert [e.split()[0] for e in events] == ["start", "end", "start", "end"]


@pytest.mark.asyncio
async def test_update_loop_updates_each_distinct_path_once(root: Path, monkeypatch) -> None:
    monkeypatch.setattr(sb, "FIRST_UPDATE_DELAY_S", 0)
    monkeypatch.setattr(sb, "UPDATE_INTERVAL_S", 3600)
    seen: list[str] = []

    async def record(path: str, config=None) -> dict:
        seen.append(path)
        return {"outcome": "up_to_date", "reason": "", "detail": "", "at": ""}

    monkeypatch.setattr(sb, "update_brain", record)
    config = SimpleNamespace(
        workspaces={
            "a": SimpleNamespace(shared_brains=[SharedBrainLink("x", "/p/one", "h/o/x")]),
            "b": SimpleNamespace(
                shared_brains=[
                    SharedBrainLink("x", "/p/one", "h/o/x"),
                    SharedBrainLink("y", "/p/two", "h/o/y"),
                ]
            ),
        }
    )
    task = asyncio.create_task(sb.update_loop(config))
    for _ in range(200):
        if len(seen) >= 2:
            break
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert seen == ["/p/one", "/p/two"]


@pytest.mark.asyncio
async def test_update_loop_caps_each_pass_and_rotates(monkeypatch) -> None:
    monkeypatch.setattr(sb, "FIRST_UPDATE_DELAY_S", 0)
    monkeypatch.setattr(sb, "UPDATE_INTERVAL_S", 0)
    monkeypatch.setattr(sb, "MAX_BRAINS_PER_PASS", 2)
    seen: list[str] = []

    async def record(path: str, config=None) -> dict:
        seen.append(path)
        return {"outcome": "up_to_date", "reason": "", "detail": "", "at": ""}

    monkeypatch.setattr(sb, "update_brain", record)
    links = [SharedBrainLink(f"n{i}", f"/p/{i}", f"h/o/{i}") for i in range(3)]
    config = SimpleNamespace(workspaces={"a": SimpleNamespace(shared_brains=links)})
    task = asyncio.create_task(sb.update_loop(config))
    for _ in range(500):
        if len(seen) >= 4:
            break
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert seen[:4] == ["/p/0", "/p/1", "/p/2", "/p/0"]


@pytest.mark.asyncio
async def test_one_failing_brain_does_not_block_the_others(pair: SimpleNamespace, monkeypatch) -> None:
    fine = pair.root / "fine-clone"
    subprocess.run(["git", "clone", "-q", str(pair.origin), str(fine)], check=True)
    push_from_seed(pair, "remote.md")  # both clones are now behind

    real = sb.run_git

    async def timing_out_first(workspace, *args, timeout=None, env=None):
        if "fetch" in args and str(workspace) == str(pair.clone):
            return (-1, "", GIT_TIMEOUT_DETAIL)
        return await real(workspace, *args, timeout=timeout, env=env)

    monkeypatch.setattr(sb, "run_git", timing_out_first)
    counts = await sb._run_pass(FakeConfig(pair.root), [str(pair.clone), str(fine)])
    assert counts == {"failed": 1, "updated": 1}
    assert git(fine, "rev-parse", "HEAD") == git(pair.seed, "rev-parse", "HEAD")


def test_schedule_update_keeps_a_reference_until_done(monkeypatch) -> None:
    async def quick(path: str, config=None) -> dict:
        return {"outcome": "up_to_date", "reason": "", "detail": "", "at": ""}

    monkeypatch.setattr(sb, "update_brain", quick)

    async def run() -> None:
        sb.schedule_update("/p/x", FakeConfig(Path("/p")))
        assert len(sb._pending) == 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(sb._pending) == 0

    asyncio.run(run())


# --- agent surface and wiring -----------------------------------------------


def test_agent_workspace_list_omits_the_path(root: Path) -> None:
    from ciao.config import CiaoConfig
    from ciao.control_plane import CiaoControlPlane

    config = CiaoConfig.from_env(
        {
            "CIAO_WORKSPACE": str(root),
            "CIAO_RUNTIME_ROOT": str(root / ".runtime"),
            "PWA_AUTH_TOKEN": "t",
        }
    )
    from ciao.config import WorkspaceConfig

    brain_path = str(make_checkout(root / "secret-brain-folder").resolve())
    config.workspaces["linked"] = WorkspaceConfig(
        name="linked",
        vault_root="linked",
        shared_brains=[SharedBrainLink("secretbrain", brain_path, "github.com/x/y")],
    )
    plane = CiaoControlPlane(
        config, project_chat_manager=SimpleNamespace(), schedule_manager=SimpleNamespace()
    )
    result = plane.workspaces_list(SimpleNamespace(workspace="linked"))
    serialized = repr(result)
    assert "shared_brains" not in serialized
    assert brain_path not in serialized and "secretbrain" not in serialized


def test_no_agent_surface_names_the_setting() -> None:
    ciao_dir = Path(sb.__file__).resolve().parent
    for relative in ("agent_cli.py", "control_plane.py", "mcp_server.py"):
        source = (ciao_dir / relative).read_text(encoding="utf-8")
        assert "shared_brains" not in source, relative


# --- runtime re-checks, hardening and link-time refusals ----------------------


@pytest.mark.asyncio
async def test_folder_whose_git_was_removed_under_a_parent_repo_is_not_updated(
    pair: SimpleNamespace, monkeypatch
) -> None:
    """A deleted .git must not let git read a parent repository as the brain."""
    parent = make_checkout(pair.root / "parent")
    (parent / "p.md").write_text("parent\n", encoding="utf-8")
    git(parent, "add", ".")
    git(parent, "commit", "-q", "-m", "parent")
    brain = parent / "brain"
    subprocess.run(["git", "clone", "-q", str(pair.origin), str(brain)], check=True)
    shutil.rmtree(brain / ".git")
    parent_head = git(parent, "rev-parse", "HEAD")
    calls = record_git(monkeypatch)

    result = await sb.update_brain(str(brain), FakeConfig(pair.root))

    assert result["outcome"] == "failed" and result["reason"] == "not_a_checkout"
    assert not [a for a in calls if "fetch" in a or "merge" in a]
    assert git(parent, "rev-parse", "HEAD") == parent_head


@pytest.mark.asyncio
async def test_symlink_swapped_in_after_linking_is_refused(pair: SimpleNamespace, monkeypatch) -> None:
    linked = pair.root / "linked"
    subprocess.run(["git", "clone", "-q", str(pair.origin), str(linked)], check=True)
    stored = str(linked.resolve())
    shutil.move(str(linked), str(pair.root / "moved-away"))
    os.symlink(pair.clone, linked)  # a checkout that lives where a vault could be
    calls = record_git(monkeypatch)

    result = await sb.update_brain(stored, FakeConfig(pair.root))

    assert result["reason"] == "moved"
    assert calls == []


@pytest.mark.asyncio
async def test_runtime_refuses_a_folder_that_moved_into_a_vault(pair: SimpleNamespace) -> None:
    vault = pair.root / "vaults" / "alice"
    config = FakeConfig(pair.root, {"alice": vault})
    vault.mkdir(parents=True)
    brain = vault / "notes"
    subprocess.run(["git", "clone", "-q", str(pair.origin), str(brain)], check=True)
    result = await sb.update_brain(str(brain), config)
    assert result == {**result, "outcome": "failed", "reason": "outside_install"}


def test_bad_rows_are_dropped_on_load(tmp_path: Path) -> None:
    from ciao.config import shared_brains_from_rows

    good = {"name": "notes", "path": "/away/notes", "identity": "github.com/a/notes"}
    rows = [
        good,
        {"name": "Bad Name", "path": "/away/x", "identity": "github.com/a/x"},
        {"name": "rel", "path": "relative/path", "identity": "github.com/a/rel"},
        {"name": "ctl", "path": "/away/c\n", "identity": "github.com/a/c"},
        {"name": "noid", "path": "/away/n", "identity": ""},
        {"name": "notes", "path": "/away/other", "identity": "github.com/a/other"},
        "not a dict",
    ]
    assert shared_brains_from_rows(rows) == [sb.SharedBrainLink(**good)]


def test_load_drops_a_brain_pointing_into_the_install(tmp_path: Path) -> None:
    from ciao.config import CiaoConfig

    install = tmp_path / "install"
    install.mkdir()
    registry = tmp_path / "install" / ".runtime"
    registry.mkdir()
    inside = {"name": "inside", "path": str(install / "vaultish"), "identity": "github.com/a/i"}
    outside = {"name": "outside", "path": str(tmp_path / "away"), "identity": "github.com/a/o"}
    (registry / "workspaces.json").write_text(
        json.dumps([{"name": "personal", "vault_root": "personal", "shared_brains": [inside, outside]}]),
        encoding="utf-8",
    )
    config = CiaoConfig.from_env(
        {"CIAO_WORKSPACE": str(install), "CIAO_RUNTIME_ROOT": str(registry), "PWA_AUTH_TOKEN": "t"}
    )
    assert [link.name for link in config.workspace("personal").shared_brains] == ["outside"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,value",
    [
        ("filter.lfs.smudge", "cat"),
        ("core.sshCommand", "ssh"),
        ("core.gitProxy", "/bin/true"),
        ("core.fsmonitor", "true"),
        ("credential.helper", "store"),
        ("remote.origin.uploadpack", "/bin/sh"),
        ("include.path", "/tmp/evil"),
        ("includeIf.gitdir:/x/.path", "/tmp/evil"),
    ],
)
async def test_link_refuses_a_checkout_whose_config_runs_programs(
    root: Path, key: str, value: str
) -> None:
    checkout = make_checkout(root / "hostile", "https://github.com/x/hostile")
    git(checkout, "config", key, value)
    with pytest.raises(ValueError, match="can run programs"):
        sb.resolve_links(FakeConfig(root), "work", [{"path": str(checkout)}], [])


@pytest.mark.asyncio
async def test_fetch_and_merge_run_hardened(pair: SimpleNamespace, monkeypatch) -> None:
    push_from_seed(pair, "remote.md")
    calls: list[tuple[list[str], dict]] = []
    real = sb.run_git

    async def recording(workspace, *args, timeout=None, env=None):
        calls.append((list(args), dict(env or {})))
        return await real(workspace, *args, timeout=timeout, env=env)

    monkeypatch.setattr(sb, "run_git", recording)
    assert (await sb.update_brain(str(pair.clone), FakeConfig(pair.root)))["outcome"] == "updated"
    fetch = next(c for c in calls if "fetch" in c[0])
    merge = next(c for c in calls if "merge" in c[0])
    for argv, env in (fetch, merge):
        for pair_ in ("credential.helper=", "maintenance.auto=false", "gc.auto=0"):
            assert pair_ in argv
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
        assert env["GIT_ASKPASS"] == ""
        assert "SSH_ASKPASS" not in env
        assert env["GIT_CEILING_DIRECTORIES"] == str(pair.clone.parent)


def test_canonical_identity_error_is_redacted() -> None:
    with pytest.raises(ValueError) as exc:
        sb.canonical_identity("https://user:SECRETVALUE123@")
    assert "SECRETVALUE123" not in str(exc.value)


@pytest.mark.asyncio
async def test_resolution_does_not_block_the_event_loop(monkeypatch, tmp_path: Path) -> None:
    from ciao.web import routes_api
    from ciao.config import WorkspaceConfig

    def slow_resolve(config, workspace, raw, existing):
        time.sleep(0.3)  # stands in for several git subprocesses
        return []

    from ciao.config import CiaoConfig

    monkeypatch.setattr(sb, "resolve_links", slow_resolve)
    config = CiaoConfig.from_env(
        {"CIAO_WORKSPACE": str(tmp_path), "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"), "PWA_AUTH_TOKEN": "t"}
    )
    existing = WorkspaceConfig(name="work", vault_root="work")
    config.workspaces["work"] = existing
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    await routes_api._workspace_from_request_async(
        {"name": "work", "shared_brains": [{"path": "/x"}]}, config=config, existing=existing
    )
    task.cancel()
    assert ticks >= 5


def test_schedule_update_skips_a_path_already_pending(monkeypatch, tmp_path: Path) -> None:
    started: list[str] = []

    async def slow(path, config=None):
        started.append(path)
        await asyncio.sleep(0.05)
        return {"outcome": "up_to_date", "reason": "", "detail": "", "at": ""}

    monkeypatch.setattr(sb, "update_brain", slow)

    async def run() -> None:
        sb.schedule_update(str(tmp_path / "x"), FakeConfig(tmp_path))
        sb.schedule_update(str(tmp_path / "x"), FakeConfig(tmp_path))
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert len(started) == 1


def test_forget_unlinked_prunes_lock_and_result_entries() -> None:
    sb._locks["/gone"] = asyncio.Lock()
    sb._last_update["/gone"] = {"outcome": "failed"}
    sb._locks["/kept"] = asyncio.Lock()
    sb._last_update["/kept"] = {"outcome": "updated"}
    sb._forget_unlinked({"/kept"})
    assert "/gone" not in sb._locks and "/gone" not in sb._last_update
    assert "/kept" in sb._locks and "/kept" in sb._last_update
