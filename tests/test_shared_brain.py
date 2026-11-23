"""Shared brains: linked Git checkouts, identity, state and fast-forward-only updates.

Every test runs real git in ``tmp_path``. ``origin`` is a bare repository on disk,
so nothing touches the network.
"""

from __future__ import annotations

import asyncio
import subprocess
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


def record_git(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = sb.run_git

    async def recording(workspace, *args, timeout=None):
        calls.append(list(args))
        return await real(workspace, *args, timeout=timeout)

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

    state = sb.brain_state(link)
    assert state["missing"] is False and state["branch"] == "main"
    assert state["head"] == head[:7] and state["head_date"]
    assert state["upstream"] is True and state["dirty"] is False
    assert state["origin_changed"] is False
    assert state["last_update"] is None

    (checkout / "a.md").write_text("edited\n", encoding="utf-8")
    assert sb.brain_state(link)["dirty"] is True

    git(checkout, "remote", "set-url", "origin", "https://github.com/x/elsewhere")
    assert sb.brain_state(link)["origin_changed"] is True

    gone = SharedBrainLink(name="gone", path=str(root / "gone"), identity=link.identity)
    assert sb.brain_state(gone)["missing"] is True


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
    result = await sb.update_brain(str(root / "nowhere"))
    _assert_result(result, "failed", "missing")
    assert calls == []


@pytest.mark.asyncio
async def test_update_row2_detached(pair: SimpleNamespace) -> None:
    git(pair.clone, "checkout", "-q", "--detach")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(str(pair.clone)), "skipped", "detached")
    assert git(pair.clone, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_update_row3_no_upstream(pair: SimpleNamespace) -> None:
    git(pair.clone, "checkout", "-q", "-b", "local-only")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(str(pair.clone)), "skipped", "no_upstream")
    assert git(pair.clone, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_update_row4_fetch_failed_redacts_and_caps(pair: SimpleNamespace, monkeypatch) -> None:
    async def failing(workspace, *args, timeout=None):
        if "fetch" in args:
            return (128, "", "fatal: cannot reach https://user:SECRETVALUE123@host/x " + "y" * 900)
        return await real_run_git(workspace, *args, timeout=timeout)

    monkeypatch.setattr(sb, "run_git", failing)
    result = await sb.update_brain(str(pair.clone))
    _assert_result(result, "failed", "fetch_failed")
    assert "SECRETVALUE123" not in result["detail"]
    assert len(result["detail"]) <= sb.MAX_REASON_CHARS


@pytest.mark.asyncio
async def test_update_row5_fetch_timeout(pair: SimpleNamespace, monkeypatch) -> None:
    async def timing_out(workspace, *args, timeout=None):
        if "fetch" in args:
            return (-1, "", GIT_TIMEOUT_DETAIL)
        return await real_run_git(workspace, *args, timeout=timeout)

    monkeypatch.setattr(sb, "run_git", timing_out)
    _assert_result(await sb.update_brain(str(pair.clone)), "failed", "timeout")


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
async def test_update_row6_ahead_or_equal_is_up_to_date(pair: SimpleNamespace, dirty: bool) -> None:
    (pair.clone / "local.md").write_text("mine\n", encoding="utf-8")
    git(pair.clone, "add", ".")
    git(pair.clone, "commit", "-q", "-m", "local")
    if dirty:
        (pair.clone / "a.md").write_text("uncommitted\n", encoding="utf-8")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(str(pair.clone)), "up_to_date", "")
    assert git(pair.clone, "rev-parse", "HEAD") == head
    assert (git(pair.clone, "status", "--porcelain") != "") is dirty


@pytest.mark.asyncio
async def test_update_row7_diverged(pair: SimpleNamespace) -> None:
    (pair.clone / "local.md").write_text("mine\n", encoding="utf-8")
    git(pair.clone, "add", ".")
    git(pair.clone, "commit", "-q", "-m", "local")
    push_from_seed(pair, "remote.md")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(str(pair.clone)), "failed", "diverged")
    assert git(pair.clone, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_update_row8_behind_with_local_changes_is_skipped(pair: SimpleNamespace) -> None:
    push_from_seed(pair, "remote.md")
    (pair.clone / "a.md").write_text("edited\n", encoding="utf-8")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(str(pair.clone)), "skipped", "local_changes")
    assert git(pair.clone, "rev-parse", "HEAD") == head
    assert (pair.clone / "a.md").read_text(encoding="utf-8") == "edited\n"


@pytest.mark.asyncio
async def test_update_row9_behind_clean_fast_forwards(pair: SimpleNamespace) -> None:
    new_head = push_from_seed(pair, "remote.md")
    (pair.clone / ".obsidian").mkdir()
    (pair.clone / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (pair.clone / ".DS_Store").write_text("x", encoding="utf-8")
    _assert_result(await sb.update_brain(str(pair.clone)), "updated", "")
    assert git(pair.clone, "rev-parse", "HEAD") == new_head
    assert git(pair.clone, "status", "--porcelain", "--untracked-files=no") == ""
    assert (pair.clone / ".obsidian" / "app.json").exists()


@pytest.mark.asyncio
async def test_update_row10_merge_failed_leaves_head(pair: SimpleNamespace) -> None:
    push_from_seed(pair, "collide.md")
    (pair.clone / "collide.md").write_text("untracked local\n", encoding="utf-8")
    head = git(pair.clone, "rev-parse", "HEAD")
    result = await sb.update_brain(str(pair.clone))
    _assert_result(result, "failed", "merge_failed")
    assert git(pair.clone, "rev-parse", "HEAD") == head
    assert (pair.clone / "collide.md").read_text(encoding="utf-8") == "untracked local\n"


@pytest.mark.asyncio
async def test_update_never_runs_a_rewriting_git_command(pair: SimpleNamespace, monkeypatch) -> None:
    calls = record_git(monkeypatch)
    push_from_seed(pair, "remote.md")
    await sb.update_brain(str(pair.clone))
    (pair.clone / "a.md").write_text("edited\n", encoding="utf-8")
    push_from_seed(pair, "more.md")
    await sb.update_brain(str(pair.clone))
    forbidden = {"reset", "rebase", "stash", "checkout", "clean", "push", "--force"}
    assert calls, "the update should have run git"
    for argv in calls:
        assert not forbidden & set(argv), argv


@pytest.mark.asyncio
async def test_update_serializes_per_path(pair: SimpleNamespace, monkeypatch) -> None:
    events: list[str] = []

    async def slow(workspace, *args, timeout=None):
        events.append(f"start {args[-1]}")
        await asyncio.sleep(0.01)
        events.append(f"end {args[-1]}")
        return (0, "", "")

    monkeypatch.setattr(sb, "run_git", slow)
    await asyncio.gather(sb.update_brain(str(pair.clone)), sb.update_brain(str(pair.clone)))
    assert [e.split()[0] for e in events] == ["start", "end", "start", "end"]


@pytest.mark.asyncio
async def test_update_loop_updates_each_distinct_path_once(root: Path, monkeypatch) -> None:
    monkeypatch.setattr(sb, "FIRST_UPDATE_DELAY_S", 0)
    monkeypatch.setattr(sb, "UPDATE_INTERVAL_S", 3600)
    seen: list[str] = []

    async def record(path: str) -> dict:
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

    async def record(path: str) -> dict:
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

    async def timing_out_first(workspace, *args, timeout=None):
        if "fetch" in args and str(workspace) == str(pair.clone):
            return (-1, "", GIT_TIMEOUT_DETAIL)
        return await real(workspace, *args, timeout=timeout)

    monkeypatch.setattr(sb, "run_git", timing_out_first)
    counts = await sb._run_pass([str(pair.clone), str(fine)])
    assert counts == {"failed": 1, "updated": 1}
    assert git(fine, "rev-parse", "HEAD") == git(pair.seed, "rev-parse", "HEAD")


def test_schedule_update_keeps_a_reference_until_done(monkeypatch) -> None:
    async def quick(path: str) -> dict:
        return {"outcome": "up_to_date", "reason": "", "detail": "", "at": ""}

    monkeypatch.setattr(sb, "update_brain", quick)

    async def run() -> None:
        sb.schedule_update("/p/x")
        assert len(sb._tasks) == 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(sb._tasks) == 0

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
